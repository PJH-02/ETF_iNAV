"""Frozen intraday v2 hypotheses. All existing-history results are exploratory.

Uses the v1 bracket resolver's conservative gap/SL ordering, with stricter TP
trade-through and v2 session rules. No tuning grids and no actual orders.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd
from intraday_bracket_backtest import Exit, floor_tick
from prepare_intraday_v2_data import investor_gate, aggregate_chart_ticks

from research_paths import CODE, DOCS, RESULTS, resolve_input

ROOT = CODE
SPEC_PATH = DOCS / "intraday_v2_spec.json"
OUT = RESULTS / "backtest_intraday_v2"


def digest(path):
    return hashlib.sha256(resolve_input(path).read_bytes()).hexdigest()


def bracket_exit(open_, high, low, stop, target, tick):
    if open_ <= stop:
        return Exit(open_, "stop_gap", True)
    if open_ >= target + tick:
        return Exit(target, "target_gap", True)
    hit_stop, hit_target = low <= stop, high >= target + tick
    if hit_stop:
        return Exit(stop, "stop_ambiguous" if hit_target else "stop", False, hit_target)
    if hit_target:
        return Exit(target, "target", False)
    return None


def load_etf(path=resolve_input("market_1m_data/KODEX200_069500_1m_toss.csv")):
    x = pd.read_csv(resolve_input(path))
    # Prevent 15:31 after-hours bars becoming a second synthetic 15:30 event.
    x = x[pd.to_datetime(x.timestamp).dt.strftime("%H:%M").le("15:30")].copy()
    raw = pd.to_datetime(x.timestamp)
    # Proven against Kiwoom for continuous session. Auction timestamp is an
    # event time, not the right boundary of a continuous minute.
    auction = raw.dt.strftime("%H:%M").eq("15:30")
    x["timestamp"] = raw.where(auction, raw - pd.Timedelta(minutes=1))
    x["date"] = x.timestamp.dt.strftime("%Y-%m-%d")
    all_dates = sorted(x.date.unique())
    auction_closes = x.loc[auction & x.volume.gt(0)].set_index("date").close
    daily_path = resolve_input("kiwoom_research_data/069500_1d_kiwoom.parquet")
    x["auction_verified"] = False
    if daily_path.exists():
        daily = pd.read_parquet(daily_path).set_index("date").close
        x.loc[auction, "auction_verified"] = x.loc[auction, "close"].eq(x.loc[auction, "date"].map(daily))
        auction_closes = daily  # Official daily close also covers excluded irregular sessions.
    preceding = dict(zip(all_dates[1:], all_dates[:-1]))
    x["prior_session_date"] = x.date.map(preceding)
    x["prior_auction_close"] = x.date.map(preceding).map(auction_closes)
    x["hm"] = x.timestamp.dt.strftime("%H:%M")
    x = x[x.hm.between("09:00", "15:19") | x.hm.eq("15:30")].copy()
    if x.timestamp.duplicated().any():
        raise ValueError("Duplicate ETF timestamps")
    if x[["open", "high", "low", "close", "volume"]].isna().any().any():
        raise ValueError("Missing OHLCV")
    if ((x.low > x[["open", "close"]].min(axis=1)) | (x.high < x[["open", "close"]].max(axis=1)) | (x.low > x.high) | (x.close <= 0) | (x.volume < 0)).any():
        raise ValueError("Invalid OHLCV")
    eligible, exclusions = [], []
    for day, g in x.groupby("date", sort=True):
        first, end = g[g.hm.eq("09:00")], g[g.hm.eq("15:19")]
        if first.empty or end.empty or first.volume.iloc[0] <= 0 or end.volume.iloc[0] <= 0:
            exclusions.append({"date": day, "reason": "missing_positive_volume_regular_open_or_liquidation_bar"})
        else:
            eligible.append(day)
    x = x[x.date.isin(eligible)].sort_values("timestamp").reset_index(drop=True)
    return x, pd.DataFrame(exclusions, columns=["date", "reason"])


def basic_features(x):
    x = x.copy()
    x["hm"] = x.timestamp.dt.strftime("%H:%M")
    x["date"] = x.timestamp.dt.strftime("%Y-%m-%d")
    continuous = x.hm.le("15:19")
    if "pressure" not in x:
        x["pressure"] = ((2 * x.close - x.high - x.low) / (x.high - x.low).replace(0, np.nan)).fillna(0)
    if "vwap" not in x:
        x["vwap"] = x.close  # Minute-close-weighted price, not true trade VWAP.
    x["clv"] = ((2 * x.close - x.high - x.low) / (x.high - x.low).replace(0, np.nan)).fillna(0)
    gap = x.timestamp.diff().ne(pd.Timedelta(minutes=1)) | x.date.ne(x.date.shift()) | ~continuous
    x["segment"] = gap.cumsum()
    grouped = x.groupby("segment", sort=False)
    x["prev_close"] = grouped.close.shift()
    x["pre_high"] = grouped.high.transform(lambda v: v.shift().rolling(5, min_periods=5).max())
    x["pre_low"] = grouped.low.transform(lambda v: v.shift().rolling(5, min_periods=5).min())
    x["pre_range"] = x.pre_high - x.pre_low
    x["range_bp"] = x.pre_range / x.close * 10000
    x["ret5"] = x.close / grouped.close.shift(5) - 1
    pv = x.vwap * x.volume
    pre_pv = pv.groupby(x.segment).transform(lambda v: v.shift().rolling(5, min_periods=5).sum())
    pre_v = grouped.volume.transform(lambda v: v.shift().rolling(5, min_periods=5).sum())
    x["pre_vwap"] = pre_pv / pre_v.replace(0, np.nan)
    x["pressure5"] = grouped.pressure.transform(lambda v: v.rolling(5, min_periods=5).mean())
    x["bucket"] = ((x.timestamp.dt.hour * 60 + x.timestamp.dt.minute - 540) // 5).astype(int)
    return x


def prior_normalization(x, days=20):
    x = x.copy()
    for column in ["range_median", "volume_median", "pressure_q25", "pressure_q75"]:
        x[column] = np.nan
    dates = sorted(x.date.unique())
    for i in range(days, len(dates)):
        history = x[x.date.isin(dates[i-days:i]) & x.hm.le("15:19")]
        grouped = history.groupby("bucket")
        q = grouped[["range_bp", "volume"]].median().rename(columns={"range_bp": "range_median", "volume": "volume_median"})
        flow_quantiles = grouped.pressure.quantile([.25, .75]).unstack()
        q["pressure_q25"], q["pressure_q75"] = flow_quantiles[.25], flow_quantiles[.75]
        mask = x.date.eq(dates[i])
        for col in q:
            x.loc[mask, col] = x.loc[mask, "bucket"].map(q[col])
    return x


def normal_signals(x, strategy, spec, gates=None):
    tick = spec["etf_tick_won"]
    compressed = x.range_bp.le(x.range_median)
    breakout = x.close.gt(x.pre_high) & x.clv.gt(0)
    volume = x.volume.ge(x.volume_median)
    if strategy == "M_breakout":
        raw = compressed & breakout & volume
    elif strategy == "M_investor_trend":
        raw = breakout & volume & x.date.map(gates if gates is not None else {}).fillna(False)
    elif strategy == "T3_pressure_breakout":
        raw = compressed & x.close.gt(x.pre_high) & x.pressure.gt(0) & x.pressure.ge(x.pressure_q75)
    elif strategy == "T2_absorption":
        shock = x.pressure.lt(0) & x.pressure.le(x.pressure_q25) & (x.close - x.open).abs().le(tick)
        prior_shock = shock.groupby(x.segment).shift().fillna(False).astype(bool)
        raw = prior_shock & x.pressure.gt(0) & x.close.ge(x.prev_close + tick)
    else:
        raise ValueError(strategy)
    if strategy == "T2_absorption":
        target = x.pre_vwap.groupby(x.segment).shift() - tick
        stop = x.low.groupby(x.segment).shift() - tick
    else:
        target, stop = x.pre_high + x.pre_range, x.pre_low - tick
    signals = pd.DataFrame({"raw": raw.fillna(False), "target": target.map(lambda v: floor_tick(v, tick) if pd.notna(v) else np.nan),
                            "stop": stop, "invalidation": x.pre_high if strategy != "T2_absorption" else np.nan}, index=x.index)
    signals.loc[x.hm.gt(spec["normal_entry_last"]), "raw"] = False
    signals["strategy"] = strategy
    return signals


def daily_morning(x):
    rows = []
    prior_close = np.nan
    for date, g in x.groupby("date", sort=True):
        g = g.set_index("hm")
        auction = g.loc["15:30", "close"] if "15:30" in g.index and g.loc["15:30", "volume"] > 0 and g.loc["15:30", "auction_verified"] else np.nan
        complete = all(t in g.index and g.loc[t, "volume"] > 0 for t in ["09:00", "09:29", "15:00"]) and sum((g.index >= "14:30") & (g.index <= "14:59")) == 30
        previous = g.prior_auction_close.iloc[0] if "prior_auction_close" in g else prior_close
        if complete and pd.notna(auction):
            rows.append({"date": date, "first_return": g.loc["09:29", "close"] / previous - 1 if pd.notna(previous) else np.nan,
                         "open30_return": g.loc["09:29", "close"] / g.loc["09:00", "open"] - 1,
                         "last30_return": auction / g.loc["15:00", "open"] - 1,
                         "reference": g.loc["15:00", "open"],
                         "stop_reference": g.loc[(g.index >= "14:30") & (g.index <= "14:59"), "low"].min()})
        prior_close = auction  # Missing prior auction does not silently use an older close.
    return pd.DataFrame(rows)


def morning_signals(x, spec):
    d = daily_morning(x)
    d["predicted_return"] = np.nan
    d["training_max_date"] = ""
    n = spec["daily_regression_days"]
    for i in range(n, len(d)):
        hist = d.iloc[i-n:i].dropna(subset=["first_return", "last30_return"])
        if len(hist) != n or pd.isna(d.iloc[i].first_return):
            continue
        coef = np.linalg.lstsq(np.column_stack([np.ones(n), hist.first_return]), hist.last30_return, rcond=None)[0]
        d.loc[d.index[i], "predicted_return"] = float(coef @ [1, d.iloc[i].first_return])
        d.loc[d.index[i], "training_max_date"] = hist.date.max()
    s = pd.DataFrame({"raw": False, "target": np.nan, "stop": np.nan, "invalidation": np.nan,
                      "strategy": "M_open_close"}, index=x.index)
    lookup = d.set_index("date")
    for i, row in x[x.hm.eq(spec["late_entry"])].iterrows():
        if row.date not in lookup.index:
            continue
        a = lookup.loc[row.date]
        pred = a.predicted_return
        if pd.notna(pred) and pred * 10000 >= spec["minimum_target_bp"]:
            s.loc[i, ["raw", "target", "stop"]] = [True, floor_tick(a.reference * (1 + pred), spec["etf_tick_won"]),
                                                      a.stop_reference - spec["etf_tick_won"]]
    return s, d


def demand_features(etf, universe, stocks):
    """All three historical members must be present, synchronously; no ffill."""
    if universe.duplicated(["effective_date", "rank"]).any():
        raise ValueError("Duplicate daily universe ranks")
    if (universe.asof_date >= universe.effective_date).any():
        raise ValueError("Universe look-ahead")
    if "prior_session_date" in etf:
        prior_dates = etf.groupby("date").prior_session_date.first()
        matched = universe.effective_date.map(prior_dates)
        if universe.loc[matched.notna(), "asof_date"].ne(matched[matched.notna()]).any():
            raise ValueError("Stale universe: asof date must be the previous actual session")
    p = stocks.copy()
    p["symbol"] = p.symbol.astype(str).str.zfill(6)
    p["timestamp"] = pd.to_datetime(p.timestamp)
    if p.duplicated(["symbol", "timestamp"]).any():
        raise ValueError("Duplicate stock bars")
    prepared = []
    for symbol, g in p.groupby("symbol"):
        a = basic_features(g.sort_values("timestamp").reset_index(drop=True))
        a["symbol"] = symbol
        prepared.append(a)
    p = pd.concat(prepared).merge(universe, left_on=["date", "symbol"], right_on=["effective_date", "symbol"], how="inner")
    good = universe.groupby("effective_date").agg(count=("symbol", "nunique"), weight=("weight", "sum"))
    if not (good["count"].eq(3) & np.isclose(good.weight, 1)).all():
        raise ValueError("Universe needs exactly three unique weighted securities each day")
    p["weighted_return"] = p.ret5 * p.weight
    p["weighted_pressure"] = p.pressure5 * p.weight
    p["positive"] = (p.ret5 > 0) & (p.pressure5 > 0) & (p.volume > 0)
    p["usable"] = p.ret5.notna() & p.pressure5.notna() & (p.volume > 0)
    agg = p.groupby("timestamp").agg(demand_return=("weighted_return", "sum"), demand_pressure=("weighted_pressure", "sum"),
              positive_count=("positive", "sum"), members=("symbol", "nunique"), usable_count=("usable", "sum"))
    agg.loc[agg.members.ne(3) | agg.usable_count.ne(3), ["demand_return", "demand_pressure"]] = np.nan
    out = etf.merge(agg, left_on="timestamp", right_index=True, how="left")
    out["forward5"] = out.groupby("segment").close.shift(-5) / out.close - 1
    return out


def demand_signals(x, spec):
    s = pd.DataFrame({"raw": False, "target": np.nan, "stop": np.nan, "invalidation": np.nan,
                      "strategy": "D_market_demand"}, index=x.index)
    model_audit = []
    dates = sorted(x.date.unique())
    cols = ["demand_return", "demand_pressure", "ret5"]
    n = spec["normalization_days"]
    for i in range(n, len(dates)):
        h = x[x.date.isin(dates[i-n:i])].dropna(subset=cols + ["forward5"])
        if len(h) < spec["minimum_model_rows"]:
            continue
        a = np.column_stack([np.ones(len(h)), h[cols].to_numpy()])
        coef = np.linalg.lstsq(a, h.forward5, rcond=None)[0]
        current = x[x.date.eq(dates[i])].dropna(subset=cols)
        pred = np.column_stack([np.ones(len(current)), current[cols]]) @ coef
        ok = (pred * 10000 >= spec["minimum_target_bp"]) & current.positive_count.ge(2) & current.hm.le(spec["normal_entry_last"])
        index = current.index[ok]
        target = current.loc[index, "close"] * (1 + pred[ok])
        s.loc[index, "raw"] = True
        s.loc[index, "target"] = target.map(lambda v: floor_tick(v, spec["etf_tick_won"]))
        s.loc[index, "stop"] = current.loc[index, "pre_low"] - spec["etf_tick_won"]
        model_audit.append({"date": dates[i], "training_max_date": h.date.max(), "rows": len(h), "coefficients": coef.tolist()})
    return s, model_audit


def accounting(entry, exit_price, slip_bp, fee_bp):
    buy = entry * (1 + slip_bp / 2 / 10000)
    sell = exit_price * (1 - slip_bp / 2 / 10000)
    gross = (sell / buy - 1) * 10000
    return {"price_gross_bp": (exit_price / entry - 1) * 10000, "gross_bp": gross,
            "net_bp": gross - fee_bp / 2 * (1 + sell / buy), "entry_fill": buy, "exit_fill": sell}


def simulate(x, signals, spec, slip_bp):
    """Sequential brackets. No intra-minute path information is fabricated."""
    trades, rejected = [], []
    delay = spec["entry_delay_bars"] + 1
    position, pending = None, None
    armed = True
    bars = x.to_dict("records")
    sigs = signals.to_dict("records")
    late = signals.strategy.iloc[0] == "M_open_close"
    last_date = None
    for i, bar in enumerate(bars):
        signal = sigs[i]
        if bar["date"] != last_date:
            if position is not None:
                raise AssertionError("Position survived scheduled EOD exit")
            pending, armed, last_date = None, True, bar["date"]
        exited = False
        if position is not None:
            reason, price = None, None
            if bar["hm"] == (spec["closing_auction"] if late else spec["normal_liquidation"]):
                reason, price = ("auction_close", bar["close"]) if late else ("session_close", bar["open"])
            elif bar["hm"] <= "15:19" and bar["volume"] > 0:
                # Trade-through required for TP; SL-first utility retained from v1.
                result = bracket_exit(bar["open"], bar["high"], bar["low"],
                                      position["stop"], position["target"], spec["etf_tick_won"])
                if result is not None:
                    reason, price = result.reason, result.price
            if reason:
                record = {**position, "exit_time": str(bar["timestamp"]), "exit_date": bar["date"],
                          "exit_price": price, "exit_reason": reason,
                          "holding_minutes": (bar["timestamp"] - position["entry_timestamp"]).total_seconds() / 60,
                          **accounting(position["entry_price"], price, slip_bp, spec["fees_roundtrip_bp"])}
                record.pop("entry_timestamp")
                trades.append(record)
                position, exited = None, True
        if pending is not None and pending["execute_i"] == i:
            p, pending = pending, None
            entry, target, stop = bar["open"], p["target"], p["stop"]
            allowed = late or bar["hm"] <= spec["normal_entry_last"]
            if not exited and position is None and allowed and bar["volume"] > 0 and stop < entry and (target / entry - 1) * 10000 >= spec["minimum_target_bp"]:
                position = {"date": bar["date"], "strategy": signal["strategy"], "signal_time": p["signal_time"],
                            "entry_time": str(bar["timestamp"]), "entry_timestamp": bar["timestamp"],
                            "entry_price": entry, "target": target, "stop": stop, "slippage_roundtrip_bp": slip_bp}
                armed = False
                # Entry-bar brackets are active immediately.
                result = bracket_exit(bar["open"], bar["high"], bar["low"], stop, target, spec["etf_tick_won"])
                if result is not None:
                    record = {**position, "exit_time": str(bar["timestamp"]), "exit_date": bar["date"],
                              "exit_price": result.price, "exit_reason": result.reason, "holding_minutes": 0,
                              **accounting(entry, result.price, slip_bp, spec["fees_roundtrip_bp"])}
                    record.pop("entry_timestamp")
                    trades.append(record)
                    position, exited = None, True
            else:
                rejected.append({"date": bar["date"], "signal_time": p["signal_time"], "reason": "entry_geometry_volume_or_session"})
        if not signal["raw"]:
            armed = True
        if signal["raw"] and armed and position is None and pending is None and not exited:
            armed = False
            j = i if late else i + delay
            if j >= len(bars) or bars[j]["date"] != bar["date"] or (not late and bars[j]["timestamp"] - bar["timestamp"] != pd.Timedelta(minutes=delay)):
                rejected.append({"date": bar["date"], "signal_time": str(bar["timestamp"]), "reason": "missing_delay_bars"})
                continue
            if late:
                entry = bar["open"]
                if bar["volume"] > 0 and signal["stop"] < entry and (signal["target"] / entry - 1) * 10000 >= spec["minimum_target_bp"]:
                    position = {"date": bar["date"], "strategy": signal["strategy"], "signal_time": f"{bar['date']} 09:30 KST + prior-day model",
                                "entry_time": str(bar["timestamp"]), "entry_timestamp": bar["timestamp"], "entry_price": entry,
                                "target": signal["target"], "stop": signal["stop"], "slippage_roundtrip_bp": slip_bp}
                    result = bracket_exit(bar["open"], bar["high"], bar["low"], signal["stop"], signal["target"], spec["etf_tick_won"])
                    if result:
                        record = {**position, "exit_time": str(bar["timestamp"]), "exit_date": bar["date"],
                                  "exit_price": result.price, "exit_reason": result.reason, "holding_minutes": 0,
                                  **accounting(entry, result.price, slip_bp, spec["fees_roundtrip_bp"])}
                        record.pop("entry_timestamp")
                        trades.append(record)
                        position = None
            else:
                pending = {"execute_i": j, "signal_time": str(bar["timestamp"]), "target": signal["target"], "stop": signal["stop"]}
    if position is not None:
        raise AssertionError("Missing liquidation event")
    return pd.DataFrame(trades), pd.DataFrame(rejected, columns=["date", "signal_time", "reason"])


def statistics(trades, calendar, seed=20261007):
    if trades.empty:
        return {"trades": 0, "days": len(calendar), "trades_per_day": 0, "active_day_pct": 0}
    daily = trades.groupby("date").agg(count=("net_bp", "size"), net_sum=("net_bp", "sum"))
    daily = daily.reindex(calendar).fillna(0)
    rng = np.random.default_rng(seed)
    a = daily[["count", "net_sum"]].to_numpy()
    boot = []
    for _ in range(2000):
        starts = rng.integers(0, max(1, len(a) - 4), size=int(np.ceil(len(a) / 5)))
        sample = np.concatenate([a[j:j+5] for j in starts])[:len(a)]
        if sample[:, 0].sum() > 0:
            boot.append(sample[:, 1].sum() / sample[:, 0].sum())
    top_days = daily.net_sum.nlargest(5).index
    removed = trades[~trades.date.isin(top_days)]
    return {"trades": len(trades), "days": len(calendar), "trades_per_day": len(trades) / len(calendar),
            "active_day_pct": float((daily['count'] > 0).mean() * 100),
            "price_gross_mean_bp": trades.price_gross_bp.mean(), "gross_mean_bp": trades.gross_bp.mean(),
            "net_mean_bp": trades.net_bp.mean(), "net_median_bp": trades.net_bp.median(),
            "net_win_pct": float((trades.net_bp > 0).mean() * 100),
            "holding_median_minutes": trades.holding_minutes.median(),
            "net_p10_bp": trades.net_bp.quantile(.1), "net_p90_bp": trades.net_bp.quantile(.9),
            "net_mean_ex_best5_days_bp": removed.net_bp.mean() if len(removed) else None,
            "block5_net_ci95_bp": np.quantile(boot, [.025, .975]).tolist() if boot else None}


def run():
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    OUT.mkdir(exist_ok=True)
    x, excluded = load_etf()
    from audit_intraday_v2_ticks import latest_manifest, audit_tick_batch
    tick_manifest_path = latest_manifest()
    tick_manifest = json.loads(tick_manifest_path.read_text(encoding="utf-8")) if tick_manifest_path else None
    coverage = audit_tick_batch(tick_manifest, x) if tick_manifest else {"qualified": False, "complete_dates": []}
    (OUT / "tick_coverage_audit.json").write_text(json.dumps(coverage, indent=2), encoding="utf-8")
    excluded.to_csv(OUT / "excluded_days.csv", index=False)
    x = prior_normalization(basic_features(x), spec["normalization_days"])
    calendar = sorted(x.date.unique())[spec["normalization_days"]:]
    jobs = [("M_breakout", normal_signals(x, "M_breakout", spec), x, calendar)]
    late, morning = morning_signals(x, spec)
    morning.to_csv(OUT / "morning_close_models.csv", index=False)
    late_dates = morning.loc[morning.predicted_return.notna(), "date"].tolist()
    # M_open_close gets only days with verified actual auction prints and fitted models.
    lx = x[x.date.isin(late_dates)].reset_index(drop=True)
    ls = late.loc[x.date.isin(late_dates)].reset_index(drop=True)
    if not lx.empty:
        jobs.append(("M_open_close", ls, lx, late_dates))
    statuses = {"M_breakout": "READY", "M_open_close": "READY" if len(lx) else "INSUFFICIENT_COMPLETE_DAILY_TRAINING",
                "D_market_demand": "MISSING_POINT_IN_TIME_UNIVERSE_AND_MEMBER_BARS",
                "M_investor_trend": "MISSING_KOSPI200_AGGREGATE_INVESTOR_FLOW",
                "T2_absorption": "TICK_COVERAGE_BELOW_120_COMPLETE_DAYS",
                "T3_pressure_breakout": "TICK_COVERAGE_BELOW_120_COMPLETE_DAYS"}
    sources = {"ETF": str(resolve_input("market_1m_data/KODEX200_069500_1m_toss.csv"))}
    if tick_manifest_path:
        sources["tick_manifest"] = str(tick_manifest_path)
    for key, path in [("kiwoom_daily_closing_price_validation", resolve_input("kiwoom_research_data/069500_1d_kiwoom.parquet")),
                      ("kiwoom_minute_validation", resolve_input("kiwoom_research_data/069500_1m_kiwoom.parquet"))]:
        if path.exists():
            sources[key] = str(path)
    if coverage["qualified"]:
        columns = ["timestamp", "open", "high", "low", "close", "volume", "batch_id"]
        ticks = pd.concat([pd.read_parquet(resolve_input(p), columns=columns) for p in tick_manifest["raw_pages"]], ignore_index=True)
        ticks = ticks[ticks.timestamp.dt.strftime("%Y-%m-%d").isin(coverage["complete_dates"])]
        flow = aggregate_chart_ticks(ticks)
        tx = x.drop(columns=["pressure", "vwap"]).merge(flow[["timestamp", "pressure", "vwap"]], on="timestamp", how="inner")
        tx = prior_normalization(basic_features(tx), spec["normalization_days"])
        tc = sorted(tx.date.unique())[spec["normalization_days"]:]
        jobs = [(name, normal_signals(tx, name, spec), tx, tc) for name in ["T2_absorption", "T3_pressure_breakout"]]
        statuses.update({"T2_absorption": "READY_CHART_TICK_RULE_PROXY", "T3_pressure_breakout": "READY_CHART_TICK_RULE_PROXY",
                         "M_breakout": "NOT_SELECTED_WITH_SUFFICIENT_TICKS", "M_open_close": "NOT_SELECTED_WITH_SUFFICIENT_TICKS",
                         "M_investor_trend": "NOT_SELECTED_WITH_SUFFICIENT_TICKS"})
        sources["tick_manifest"] = str(tick_manifest_path)
    universe_path = resolve_input("kiwoom_research_data/daily_top3_universe.csv")
    stocks_path = resolve_input("kiwoom_research_data/top3_member_bars.parquet")
    if universe_path.exists() and stocks_path.exists():
        u = pd.read_csv(universe_path, dtype={"symbol": str})
        dx = demand_features(x, u, pd.read_parquet(stocks_path))
        ds, audit = demand_signals(dx, spec)
        (OUT / "demand_model_audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
        dc = sorted(dx.loc[dx.demand_return.notna(), "date"].unique())[spec["normalization_days"]:]
        jobs.append(("D_market_demand", ds, dx, dc))
        statuses["D_market_demand"] = "READY"
        sources.update({"universe": str(universe_path), "stocks": str(stocks_path)})
    flows_path = resolve_input("kiwoom_research_data/kospi200_investor_daily.csv")
    if flows_path.exists() and not coverage["qualified"]:
        flows = investor_gate(pd.read_csv(flows_path), sorted(x.date.unique()), spec["investor_percentile_lookback_days"])
        flows.to_csv(OUT / "prior_day_investor_gates.csv", index=False)
        gates = flows.set_index("date").investor_gate.to_dict()
        fc = flows.loc[flows.usable, "date"].tolist()
        jobs.append(("M_investor_trend", normal_signals(x, "M_investor_trend", spec, gates), x, fc))
        statuses["M_investor_trend"] = "READY"
        sources["flows"] = str(flows_path)
    rows = []
    for name, signals, tape, days in jobs:
        # No trade on warm-up dates, but features remain available in the tape.
        signals = signals.copy()
        signals.loc[~tape.date.isin(days), "raw"] = False
        signals.assign(timestamp=tape.timestamp).to_csv(OUT / f"{name}_signals.csv", index=False)
        base_trades, rejected = simulate(tape, signals, spec, spec["primary_slippage_roundtrip_bp"])
        for slip in spec["slippage_roundtrip_bp"]:
            # Fixed friction changes cash accounting, not eligibility or price
            # events in this engine. Reuse those events for the cost scenarios.
            trades = base_trades.copy()
            if not trades.empty:
                trades["slippage_roundtrip_bp"] = slip
                trades["entry_fill"] = trades.entry_price * (1 + slip / 2 / 10000)
                trades["exit_fill"] = trades.exit_price * (1 - slip / 2 / 10000)
                trades["gross_bp"] = (trades.exit_fill / trades.entry_fill - 1) * 10000
                trades["net_bp"] = trades.gross_bp - spec["fees_roundtrip_bp"] / 2 * (1 + trades.exit_fill / trades.entry_fill)
            trades.to_csv(OUT / f"{name}_slip{slip}_trades.csv", index=False)
            rejected.to_csv(OUT / f"{name}_slip{slip}_rejections.csv", index=False)
            rows.append({"strategy": name, "slippage_roundtrip_bp": slip, **statistics(trades, days)})
            if not trades.empty:
                trades.assign(month=pd.to_datetime(trades.date).dt.strftime("%Y-%m")).groupby("month").agg(
                    trades=("net_bp", "size"), gross_mean_bp=("gross_bp", "mean"), net_mean_bp=("net_bp", "mean"),
                    net_median_bp=("net_bp", "median")).to_csv(OUT / f"{name}_slip{slip}_monthly.csv")
    summary = pd.DataFrame(rows)
    summary.to_csv(OUT / "summary.csv", index=False)
    manifest = {"spec_sha256": digest(SPEC_PATH), "spec": spec, "statuses": statuses,
                "inputs": {k: {"path": v, "sha256": digest(v)} for k, v in sources.items()},
                "history_role": "EXPLORATORY_PREVIOUSLY_SEEN_HISTORY_NO_FRESH_OOS",
                "run_date": "2026-10-07", "eligible_regular_days": x.date.nunique(), "excluded_days": len(excluded),
                "active_mode": "tick_proxy" if coverage["qualified"] else "minute_fallback",
                "dependency_code_sha256": {p: digest(ROOT / p) for p in ["prepare_intraday_v2_data.py", "intraday_bracket_backtest.py", "audit_intraday_v2_ticks.py"]},
                "raw_etf_coverage": "2022-11-23..2026-10-06", "source_code_sha256": digest(__file__)}
    (OUT / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    primary = summary[summary.slippage_roundtrip_bp.eq(spec["primary_slippage_roundtrip_bp"])]
    print(primary.to_string(index=False))
    print(json.dumps(statuses, ensure_ascii=False))


if __name__ == "__main__":
    run()
