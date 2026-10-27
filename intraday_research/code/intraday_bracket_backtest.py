"""Frozen v1 KOSPI200/ETF bracket study. No parameter search or OOS tuning.

Joint bars create signals; an independent ETF tape manages held positions.
See intraday_bracket_spec_v1.md for economic hypotheses and execution limits.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


from research_paths import CODE, DOCS, RESULTS, resolve_input, recorded_code_provenance

ROOT = CODE
SPEC_PATH = DOCS / "intraday_bracket_spec_v1.json"
SPEC_DOC = DOCS / "intraday_bracket_spec_v1.md"
OUT_ROOT = RESULTS / "backtest_intraday_bracket_v1"
PRICE_FIELDS = ("open", "high", "low", "close")
SIGNAL_COLUMNS = [
    "strategy", "setup_id", "setup_start", "level_frozen_at",
    "context_start", "signal_bar_start", "signal_available_at", "trade_date",
    "signal_segment", "target", "stop", "entry_ceiling", "signal_etf_close",
    "normalization_max_date", "impulse_etf_krw", "shock_fut_bp",
]
TRADE_COLUMNS = [
    "strategy", "setup_id", "trade_date", "signal_bar_start",
    "signal_available_at", "level_frozen_at", "normalization_max_date",
    "entry_time", "exit_time", "exit_bar_start", "exit_reason",
    "entry_raw", "exit_raw", "entry_exec", "exit_exec", "qty",
    "target", "stop", "entry_ceiling", "gross_pnl", "net_pnl",
    "entry_fee", "exit_fee", "gross_bp", "net_bp", "holding_minutes",
    "planned_reward_bp", "planned_risk_bp", "planned_break_even_p",
    "same_bar_ambiguous", "held_zero_volume_bars", "execution_gap_unknown",
    "entry_bar_volume", "exit_bar_volume", "entry_volume_fraction",
    "exit_volume_fraction",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_spec() -> dict:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    if spec["version"] != "1.0.0":
        raise ValueError("This executable implements only the v1.0.0 specification")
    if (spec["context_minutes"], spec["setup_minutes"],
        spec["confirmation_minutes"], spec["confirmation_validity_minutes"]) != (15, 5, 1, 5):
        raise ValueError("v1 time parameters are frozen at 15/5/1/5")
    if spec["cost_gate_assumed_success_probability"] != 0.5:
        raise ValueError("v1 entry ceiling requires the frozen 50% reference assumption")
    return spec


def read_date_slice(path: Path, timestamp: str, first: str, last: str) -> pd.DataFrame:
    """Filter raw rows by date before parsing prices; IS does not build OOS features."""
    parts = []
    for chunk in pd.read_csv(path, encoding="utf-8-sig", chunksize=50000):
        raw_date = chunk[timestamp].astype(str).str[:10]
        keep = raw_date.between(first, last)
        if keep.any():
            parts.append(chunk.loc[keep].copy())
    if not parts:
        raise ValueError(f"No rows in requested dates: {path}")
    return pd.concat(parts, ignore_index=True)


def normalize_time(values: pd.Series, zone: str) -> pd.Series:
    # Sources in this study have explicit +09:00 offsets. Reject ambiguous input.
    strings = values.astype(str)
    if not strings.str.contains(r"(?:[+-]\d{2}:\d{2}|Z)$", regex=True).all():
        raise ValueError("Timestamp must carry an explicit timezone offset")
    return pd.to_datetime(values, utc=True, errors="raise").dt.tz_convert(zone)


def validate_ohlcv(x: pd.DataFrame, prefix: str = "") -> None:
    cols = [prefix + p for p in (*PRICE_FIELDS, "volume")]
    for c in cols:
        x[c] = pd.to_numeric(x[c], errors="raise")
    if not np.isfinite(x[cols].to_numpy(float)).all():
        raise ValueError(f"Nonfinite {prefix} OHLCV")
    px = x[[prefix + p for p in PRICE_FIELDS]]
    bad = ((px <= 0).any(axis=1)
           | (x[prefix + "volume"] < 0)
           | (x[prefix + "high"] < px.max(axis=1))
           | (x[prefix + "low"] > px.min(axis=1)))
    if bad.any():
        raise ValueError(f"Invalid {prefix} OHLCV: {int(bad.sum())} rows")


def load_inputs(spec: dict, partition: str):
    last = spec["is_last"] if partition == "is" else spec["oos_last"]
    joint = read_date_slice(resolve_input(spec["input_aligned"]), "bar_start", spec["is_first"], last)
    joint["bar_start"] = normalize_time(joint["bar_start"], spec["timezone"])
    tape = read_date_slice(resolve_input(spec["input_execution"]), "timestamp", spec["is_first"], last)
    tape["bar_start"] = (normalize_time(tape["timestamp"], spec["timezone"])
                         + pd.Timedelta(minutes=spec["execution_timestamp_shift_minutes"]))
    for x in (joint, tape):
        clock = x["bar_start"].dt.strftime("%H:%M")
        x.drop(x.index[~clock.between(spec["session_start"], spec["data_session_end"])], inplace=True)
        x.sort_values("bar_start", inplace=True)
        x.reset_index(drop=True, inplace=True)
        if x["bar_start"].duplicated().any():
            raise ValueError("Duplicate normalized bar_start; cannot silently keep a revision")
        x["trade_date"] = x["bar_start"].dt.strftime("%Y-%m-%d")
        x["bar_end"] = x["bar_start"] + pd.Timedelta(minutes=1)
    validate_ohlcv(joint, "fut_")
    validate_ohlcv(joint, "etf_")
    validate_ohlcv(tape)
    if "contract_code" not in joint:
        raise ValueError("Futures contract_code required to prohibit cross-roll signals")
    shared = joint.merge(tape[["bar_start", *PRICE_FIELDS, "volume"]], on="bar_start", how="left", validate="one_to_one")
    for c in (*PRICE_FIELDS, "volume"):
        if shared[c].isna().any() or not np.allclose(shared["etf_" + c], shared[c], rtol=0, atol=1e-8):
            raise ValueError(f"Independent execution tape disagrees with aligned ETF {c}")
    dates = sorted(joint["trade_date"].unique())
    is_dates = [d for d in dates if d <= spec["is_last"]]
    if len(is_dates) <= spec["calibration_days"]:
        raise ValueError("Not enough IS history")
    eval_dates = (is_dates[spec["calibration_days"]:] if partition == "is"
                  else [d for d in dates if spec["oos_first"] <= d <= spec["oos_last"]])
    if not eval_dates:
        raise ValueError("No evaluation dates")
    day_audit = []
    for d in dates:
        j, e = joint[joint.trade_date.eq(d)], tape[tape.trade_date.eq(d)]
        day_audit.append({
            "trade_date": d, "joint_bars": len(j), "etf_bars": len(e),
            "etf_bars_without_joint": len(e) - len(j),
            "etf_zero_volume_bars": int(e.volume.eq(0).sum()),
            "joint_zero_volume_bars": int((j.fut_volume.eq(0) | j.etf_volume.eq(0)).sum()),
            "first_etf_bar": str(e.bar_start.min()),
            "last_etf_bar": str(e.bar_start.max()),
        })
    metadata = {
        "loaded_through": last, "evaluation_dates": eval_dates,
        "calibration_dates": is_dates[:spec["calibration_days"]],
        "joint_rows": len(joint), "etf_execution_rows": len(tape),
        "shared_etf_ohlcv_match": True,
        "retrospective_day_quality_exclusion": False,
        "timestamp_convention": "aligned start; raw ETF end shifted -1m as in existing prep.py",
    }
    return joint, tape, eval_dates, metadata, pd.DataFrame(day_audit)


def rolling_in_segment(series: pd.Series, groups: pd.Series, n: int, op: str) -> pd.Series:
    r = series.groupby(groups, sort=False).rolling(n, min_periods=n)
    return getattr(r, op)().reset_index(level=0, drop=True).reindex(series.index)


def add_features(joint: pd.DataFrame, spec: dict) -> pd.DataFrame:
    x = joint.copy()
    eligible = x.fut_volume.gt(0) & x.etf_volume.gt(0)
    boundary = (x.trade_date.ne(x.trade_date.shift())
                | x.bar_start.diff().ne(pd.Timedelta(minutes=1))
                | x.contract_code.ne(x.contract_code.shift())
                | ~eligible | ~eligible.shift(fill_value=False))
    if "contract_changed" in x:
        changed = x.contract_changed.astype(str).str.lower().isin(["true", "1", "t", "yes"])
        boundary |= changed
    x["signal_segment"] = boundary.cumsum().astype(int)
    x["feature_eligible"] = eligible
    minute = x.bar_start.dt.hour * 60 + x.bar_start.dt.minute
    x["tod_bucket"] = ((minute - 9 * 60) // 5).astype(int)
    x["five_minute_boundary"] = minute.mod(5).eq(4)
    groups = x.signal_segment
    for tag, prefix in (("f", "fut_"), ("e", "etf_")):
        v = {c: x[prefix + c].where(eligible) for c in (*PRICE_FIELDS, "volume")}
        for n in (5, 15):
            start = v["open"].groupby(groups).shift(n - 1)
            x[f"{tag}_open{n}"] = start
            x[f"{tag}_high{n}"] = rolling_in_segment(v["high"], groups, n, "max")
            x[f"{tag}_low{n}"] = rolling_in_segment(v["low"], groups, n, "min")
            x[f"{tag}_volume{n}"] = rolling_in_segment(v["volume"], groups, n, "sum")
            x[f"{tag}_ret{n}_bp"] = np.log(v["close"] / start) * 1e4
            x[f"{tag}_range{n}_bp"] = np.log(x[f"{tag}_high{n}"] / x[f"{tag}_low{n}"]) * 1e4
            x[f"{tag}_abs_ret{n}_bp"] = x[f"{tag}_ret{n}_bp"].abs()
        x[f"{tag}_median15"] = rolling_in_segment(v["close"], groups, 15, "median")
    x["feature_available_at"] = x["bar_end"]
    return x


NORM_SPECS = {
    "fut_volume": ("n_fvol1_med", 0.5, False),
    "f_volume5": ("n_fvol5_med", 0.5, True),
    "f_range5_bp": ("n_frange5_med", 0.5, True),
    "f_ret5_bp": ("n_fret5_q20", 0.2, True),
    "f_volume15": ("n_fvol15_med", 0.5, True),
    "f_abs_ret15_bp": ("n_fabs15_med", 0.5, True),
    "f_range15_bp": ("n_frange15_med", 0.5, True),
}


def add_prior_day_normalization(x: pd.DataFrame, spec: dict):
    x = x.copy()
    dates = sorted(x.trade_date.unique())
    day_rows = {d: x.index[x.trade_date.eq(d)].to_numpy() for d in dates}
    for out, _, _ in NORM_SPECS.values():
        x[out] = np.nan
    x["normalization_max_date"] = ""
    audit = []
    h = spec["normalization_history_days"]
    for k in range(h, len(dates)):
        d = dates[k]
        history = dates[k - h:k]
        idx = day_rows[d]
        hist = x.loc[np.concatenate([day_rows[t] for t in history])]
        bucket = x.loc[idx, "tod_bucket"]
        x.loc[idx, "normalization_max_date"] = history[-1]
        for col, (out, q, edge_only) in NORM_SPECS.items():
            mask = hist.feature_eligible
            if edge_only:
                mask = mask & hist.five_minute_boundary
            z = hist.loc[mask, ["tod_bucket", col]].dropna()
            grouped = z.groupby("tod_bucket")[col]
            counts = grouped.count()
            threshold = grouped.quantile(q)
            threshold = threshold.where(counts >= spec["minimum_history_observations"])
            x.loc[idx, out] = bucket.map(threshold).to_numpy()
            audit.append({"trade_date": d, "feature": col,
                          "history_first": history[0], "history_last": history[-1],
                          "history_days": len(history), "eligible_buckets": int(threshold.notna().sum()),
                          "minimum_observations": spec["minimum_history_observations"]})
    if not (x.loc[x.normalization_max_date.ne(""), "normalization_max_date"]
            < x.loc[x.normalization_max_date.ne(""), "trade_date"]).all():
        raise AssertionError("Current/future date entered normalization")
    return x, pd.DataFrame(audit)


def floor_tick(value: float, tick: float) -> float:
    return math.floor(value / tick + 1e-9) * tick


def entry_ceiling(target: float, stop: float, friction_bps: float) -> float:
    # Frozen p=1/2 and net reference requirement=0 => G-L >= 2c.
    return (target + stop) / (2.0 + 2.0 * friction_bps / 1e4)


def build_signals(x: pd.DataFrame, eval_dates: list[str], spec: dict) -> pd.DataFrame:
    allowed = set(eval_dates)
    records = []
    tick, buffer = spec["etf_tick_krw"], spec["stop_buffer_ticks"] * spec["etf_tick_krw"]
    for j in np.flatnonzero(x.five_minute_boundary.to_numpy()):
        s = x.iloc[j]
        if s.trade_date not in allowed or j < 19:
            continue
        p = x.iloc[j - 5]  # Entire prior 15m context completed before setup began.
        if p.signal_segment != s.signal_segment:
            continue
        required = [s.f_volume5, s.f_range5_bp, s.f_ret5_bp,
                    s.n_fvol5_med, s.n_frange5_med, s.n_fret5_q20,
                    p.f_volume15, p.f_abs_ret15_bp, p.f_range15_bp,
                    p.n_fvol15_med, p.n_fabs15_med, p.n_frange15_med,
                    p.e_open15, p.e_median15]
        if not np.isfinite(required).all():
            continue
        impulse = p.etf_close - p.e_open15
        is_a = (p.f_ret15_bp > 0 and p.e_ret15_bp > 0
                and p.f_volume15 >= p.n_fvol15_med
                and s.f_volume5 >= s.n_fvol5_med
                and s.f_range5_bp <= s.n_frange5_med
                and s.f_low5 >= p.f_open15 and s.e_low5 >= p.e_open15)
        is_b = (p.f_abs_ret15_bp <= p.n_fabs15_med
                and p.f_range15_bp <= p.n_frange15_med
                and s.f_ret5_bp <= s.n_fret5_q20 and s.f_ret5_bp < 0
                and s.e_ret5_bp < 0 and s.f_volume5 >= s.n_fvol5_med)
        found_a = found_b = False
        for i in range(j + 1, min(j + 6, len(x))):
            r, prev = x.iloc[i], x.iloc[i - 1]
            if r.signal_segment != s.signal_segment:
                break
            a_confirm = is_a and not found_a and r.fut_close > s.f_high5 and r.etf_close > s.e_high5
            b_confirm = (is_b and not found_b and r.fut_low >= s.f_low5 and r.etf_low >= s.e_low5
                         and r.fut_close > prev.fut_high and r.etf_close > prev.etf_high
                         and np.isfinite(r.n_fvol1_med) and r.fut_volume <= r.n_fvol1_med)
            for strategy, confirm, target, stop in (
                ("A_continuation", a_confirm, s.e_high5 + spec["continuation_measured_move_multiplier"] * impulse, s.e_low5 - buffer),
                ("B_shock_recovery", b_confirm, p.e_median15, s.e_low5 - buffer),
            ):
                if not confirm:
                    continue
                target, stop = floor_tick(target, tick), floor_tick(stop, tick)
                records.append({
                    "strategy": strategy, "setup_id": f"{strategy}:{s.bar_end.isoformat()}",
                    "setup_start": s.bar_end - pd.Timedelta(minutes=5),
                    "level_frozen_at": s.bar_end,
                    "context_start": s.bar_end - pd.Timedelta(minutes=20),
                    "signal_bar_start": r.bar_start, "signal_available_at": r.bar_end,
                    "trade_date": r.trade_date, "signal_segment": int(r.signal_segment),
                    "target": target, "stop": stop,
                    "entry_ceiling": entry_ceiling(target, stop, spec["reference_round_trip_friction_bps"]),
                    "signal_etf_close": float(r.etf_close),
                    "normalization_max_date": r.normalization_max_date,
                    "impulse_etf_krw": float(impulse), "shock_fut_bp": float(s.f_ret5_bp),
                })
            found_a |= bool(a_confirm)
            found_b |= bool(b_confirm)
            if (found_a or not is_a) and (found_b or not is_b):
                break
    out = pd.DataFrame(records, columns=SIGNAL_COLUMNS)
    if not out.empty:
        if not ((out.level_frozen_at <= out.signal_available_at)
                & (out.normalization_max_date < out.trade_date)).all():
            raise AssertionError("Noncausal signal/level construction")
        out.sort_values(["signal_available_at", "strategy"], inplace=True)
        out.reset_index(drop=True, inplace=True)
    return out


@dataclass(frozen=True)
class Exit:
    price: float
    reason: str
    at_open: bool
    ambiguous: bool = False


def resolve_exit(open_: float, high: float, low: float, stop: float, target: float, order: str) -> Exit | None:
    if open_ <= stop:
        return Exit(open_, "stop_gap", True)
    if open_ >= target:
        return Exit(target, "target_gap", True)
    hit_stop, hit_target = low <= stop, high >= target
    if hit_stop and hit_target:
        if order == "stop_first":
            return Exit(stop, "stop_ambiguous", False, True)
        return Exit(target, "target_ambiguous", False, True)
    if hit_stop:
        return Exit(stop, "stop", False)
    if hit_target:
        return Exit(target, "target", False)
    return None


def trade_accounting(entry: float, exit_: float, qty: int, side_slip_bp: float, side_fee_bp: float) -> dict:
    slip, fee = side_slip_bp / 1e4, side_fee_bp / 1e4
    pe, px = entry * (1 + slip), exit_ * (1 - slip)
    ef, xf = qty * pe * fee, qty * px * fee
    paid = qty * pe + ef
    gross_pnl = qty * (exit_ - entry)
    net_pnl = qty * (px - pe) - ef - xf
    return {"entry_exec": pe, "exit_exec": px, "entry_fee": ef, "exit_fee": xf,
            "gross_pnl": gross_pnl, "net_pnl": net_pnl,
            "gross_bp": (exit_ / entry - 1) * 1e4, "net_bp": net_pnl / paid * 1e4}


def prepare_orders(signals: pd.DataFrame, x: pd.DataFrame, tape: pd.DataFrame, case: dict,
                   selected: list[str], spec: dict):
    scheduled = defaultdict(list)
    rejected = Counter()
    exec_index = {t: i for i, t in enumerate(tape.bar_start)}
    joint_map = {t: (int(seg), bool(ok)) for t, seg, ok in
                 zip(x.bar_start, x.signal_segment, x.feature_eligible)}
    delay = case["entry_delay_bars"]
    for s in signals[signals.strategy.isin(selected)].to_dict("records"):
        t = s["signal_bar_start"] + pd.Timedelta(minutes=delay)
        if t.strftime("%H:%M") > spec["last_entry_time"] or t.strftime("%Y-%m-%d") != s["trade_date"]:
            rejected["after_entry_cutoff"] += 1
            continue
        idx = exec_index.get(t)
        if idx is None:
            rejected["missing_entry_bar"] += 1
            continue
        if not (s["stop"] < s["signal_etf_close"] < s["target"]):
            rejected["invalid_or_exhausted_at_signal"] += 1
            continue
        # Only bars COMPLETED before arrival may cancel the delayed order.
        bad = None
        for wait in range(1, delay):
            wt = s["signal_bar_start"] + pd.Timedelta(minutes=wait)
            jm = joint_map.get(wt)
            if jm is None or jm != (s["signal_segment"], True):
                bad = "joint_gap_during_latency"
                break
            wi = exec_index.get(wt)
            if wi is None:
                bad = "execution_gap_during_latency"
                break
            w = tape.iloc[wi]
            if w.volume > 0 and (w.low <= s["stop"] or w.high >= s["target"]):
                bad = "bracket_touched_before_arrival"
                break
        if bad:
            rejected[bad] += 1
            continue
        s["entry_time"] = t
        scheduled[idx].append(s)
    priority = {n: i for i, n in enumerate(spec["portfolio_priority"])}
    for orders in scheduled.values():
        orders.sort(key=lambda s: (priority[s["strategy"]], s["setup_id"]))
    return scheduled, rejected


def simulate(signals: pd.DataFrame, x: pd.DataFrame, tape: pd.DataFrame, eval_dates: list[str],
             selected: list[str], case: dict, spec: dict):
    allowed = set(eval_dates)
    scheduled, rejected = prepare_orders(signals, x, tape, case, selected, spec)
    trades, marks = [], []
    cash, pos = spec["starting_cash_krw"], None
    side_fee = spec["fee_bps_per_side"]
    side_slip = case["slippage_bps_per_side"]
    previous_time = None

    def close_position(r, exit_: Exit, unknown_gap=False):
        nonlocal cash, pos
        et = r.bar_start if exit_.at_open else r.bar_end
        a = trade_accounting(pos["entry_raw"], exit_.price, pos["qty"], side_slip, side_fee)
        cash += pos["paid"] + a["net_pnl"]
        p, t, s = pos["entry_raw"], pos["target"], pos["stop"]
        reward, risk = (t / p - 1) * 1e4, (1 - s / p) * 1e4
        item = {k: pos[k] for k in ["strategy", "setup_id", "trade_date", "signal_bar_start",
                                    "signal_available_at", "level_frozen_at", "normalization_max_date",
                                    "entry_time", "target", "stop", "entry_ceiling"]}
        item.update({"exit_time": et, "exit_bar_start": r.bar_start,
                     "exit_reason": exit_.reason, "entry_raw": p, "exit_raw": exit_.price,
                     "qty": pos["qty"], **a,
                     "holding_minutes": (et - pos["entry_time"]).total_seconds() / 60,
                     "planned_reward_bp": reward, "planned_risk_bp": risk,
                     "planned_break_even_p": (risk + spec["reference_round_trip_friction_bps"]) / (reward + risk),
                     "same_bar_ambiguous": exit_.ambiguous,
                     "held_zero_volume_bars": pos["zero_volume_bars"],
                     "execution_gap_unknown": bool(unknown_gap or pos["execution_gap_unknown"]),
                     "entry_bar_volume": pos["entry_volume"], "exit_bar_volume": float(r.volume),
                     "entry_volume_fraction": pos["qty"] / pos["entry_volume"],
                     "exit_volume_fraction": pos["qty"] / r.volume})
        trades.append(item)
        pos = None

    for i, r in enumerate(tape.itertuples(index=False)):
        if r.trade_date not in allowed:
            continue
        clock = r.bar_start.strftime("%H:%M")
        if previous_time is not None and r.bar_start.date() != previous_time.date() and pos is not None:
            raise RuntimeError("Unverified intraday flat: position survived prior session")
        gap = (previous_time is not None and r.bar_start.date() == previous_time.date()
               and r.bar_start - previous_time > pd.Timedelta(minutes=1))
        if pos is not None and gap:
            pos["execution_gap_unknown"] = True
        previous_time = r.bar_start
        if pos is not None and r.volume > 0:
            if pos["execution_gap_unknown"]:
                close_position(r, Exit(float(r.open), "unobserved_execution_gap", True), True)
            elif clock >= spec["daily_flat_time"]:
                close_position(r, Exit(float(r.open), "daily_flat", True))
            else:
                opening = resolve_exit(float(r.open), float(r.open), float(r.open),
                                       pos["stop"], pos["target"], case["same_bar_order"])
                if opening is not None:
                    close_position(r, opening)
        orders = scheduled.get(i, [])
        for order in orders:
            if pos is not None:
                rejected["position_busy_at_open"] += 1
                continue
            if clock >= spec["daily_flat_time"]:
                rejected["daily_flat_window"] += 1
                continue
            if r.volume <= 0:
                rejected["no_etf_trade_at_entry"] += 1
                continue
            entry = float(r.open)
            if not order["stop"] < entry < order["target"]:
                rejected["invalid_bracket_at_open"] += 1
                continue
            if case["cost_gate"] and entry > order["entry_ceiling"]:
                rejected["precommitted_cost_ceiling"] += 1
                continue
            unit_cost = entry * (1 + side_slip / 1e4) * (1 + side_fee / 1e4)
            qty = math.floor(spec["order_notional_krw"] / unit_cost)
            if qty < 1 or qty * unit_cost > cash:
                rejected["insufficient_cash_or_lot"] += 1
                continue
            pos = {**order, "entry_raw": entry, "qty": qty,
                   "paid": qty * unit_cost, "entry_volume": float(r.volume),
                   "zero_volume_bars": 0, "execution_gap_unknown": False,
                   "last_mark": entry}
            cash -= pos["paid"]
        if pos is not None:
            if r.volume <= 0:
                pos["zero_volume_bars"] += 1
            else:
                exit_ = resolve_exit(float(r.open), float(r.high), float(r.low),
                                     pos["stop"], pos["target"], case["same_bar_order"])
                if exit_ is not None:
                    close_position(r, exit_)
                else:
                    pos["last_mark"] = float(r.close)
        value = 0.0 if pos is None else (pos["qty"] * pos["last_mark"]
                                        * (1 - side_slip / 1e4) * (1 - side_fee / 1e4))
        marks.append({"bar_end": r.bar_end, "trade_date": r.trade_date,
                      "equity": cash + value, "position_open": pos is not None})
    if pos is not None:
        raise RuntimeError("Final session has no observable intraday-flat execution")
    result = pd.DataFrame(trades, columns=TRADE_COLUMNS)
    if not result.empty:
        causal = ((result.entry_time >= result.signal_available_at)
                  & (result.level_frozen_at <= result.signal_available_at)
                  & (result.normalization_max_date < result.trade_date)
                  & (result.exit_time >= result.entry_time))
        if not causal.all():
            raise AssertionError("Trade timing/history audit failed")
        if not (result.entry_time.dt.date == result.exit_time.dt.date).all():
            raise AssertionError("Overnight position")
        if len(result) > 1 and not (result.entry_time.iloc[1:].reset_index(drop=True)
                                    >= result.exit_time.iloc[:-1].reset_index(drop=True)).all():
            raise AssertionError("Overlapping positions")
        if not np.isclose(cash, spec["starting_cash_krw"] + result.net_pnl.sum(), atol=1e-5):
            raise AssertionError("Cash/PnL reconciliation failed")
    return result, pd.DataFrame(marks), dict(rejected)


def daily_table(trades: pd.DataFrame, eval_dates: list[str], marks: pd.DataFrame, spec: dict):
    d = pd.DataFrame(index=pd.Index(eval_dates, name="trade_date"))
    if trades.empty:
        for c in ("trades", "gross_bp_sum", "net_bp_sum", "gross_pnl", "net_pnl"):
            d[c] = 0.0
    else:
        g = trades.groupby("trade_date").agg(
            trades=("net_bp", "size"), gross_bp_sum=("gross_bp", "sum"),
            net_bp_sum=("net_bp", "sum"), gross_pnl=("gross_pnl", "sum"), net_pnl=("net_pnl", "sum"))
        d = d.join(g).fillna(0)
    d["trades"] = d.trades.astype(int)
    d["mean_gross_bp"] = d.gross_bp_sum / d.trades.replace(0, np.nan)
    d["mean_net_bp"] = d.net_bp_sum / d.trades.replace(0, np.nan)
    d["closed_equity"] = spec["starting_cash_krw"] + d.net_pnl.cumsum()
    if not marks.empty:
        d = d.join(marks.groupby("trade_date").equity.agg(day_min_equity="min", day_end_equity="last"))
    return d.reset_index()


def block_intervals(daily: pd.DataFrame, spec: dict) -> dict:
    n = len(daily)
    count = daily.trades.to_numpy(float)
    if count.sum() == 0:
        return {"gross_ci_low": None, "gross_ci_high": None,
                "net_ci_low": None, "net_ci_high": None, "bootstrap_empty_fraction": 1.0}
    rng = np.random.default_rng(spec["random_seed"])
    b = min(spec["bootstrap_block_trading_days"], n)
    k = math.ceil(n / b)
    starts = rng.integers(0, n - b + 1, size=(spec["bootstrap_replicates"], k))
    ix = (starts[:, :, None] + np.arange(b)).reshape(len(starts), -1)[:, :n]
    denom = count[ix].sum(axis=1)
    good = denom > 0
    out = {"bootstrap_empty_fraction": float((~good).mean())}
    for col, prefix in (("gross_bp_sum", "gross"), ("net_bp_sum", "net")):
        values = daily[col].to_numpy(float)[ix].sum(axis=1)[good] / denom[good]
        lo, hi = np.quantile(values, [0.025, 0.975])
        out[prefix + "_ci_low"], out[prefix + "_ci_high"] = float(lo), float(hi)
    return out


def summarize(trades: pd.DataFrame, marks: pd.DataFrame, daily: pd.DataFrame,
              raw_signals: int, rejections: dict, spec: dict) -> dict:
    n, active = len(trades), int(daily.trades.gt(0).sum())
    out = {"raw_signals": raw_signals, "trades": n, "evaluation_days": len(daily),
           "active_days": active, "mean_trades_per_day": n / len(daily),
           "median_trades_per_day": float(daily.trades.median()),
           "active_day_fraction": active / len(daily),
           "net_pnl": float(trades.net_pnl.sum()) if n else 0.0,
           "gross_pnl": float(trades.gross_pnl.sum()) if n else 0.0,
           "mean_gross_bp": float(trades.gross_bp.mean()) if n else None,
           "mean_net_bp": float(trades.net_bp.mean()) if n else None,
           "median_gross_bp": float(trades.gross_bp.median()) if n else None,
           "median_net_bp": float(trades.net_bp.median()) if n else None,
           "p05_net_bp": float(trades.net_bp.quantile(.05)) if n else None,
           "p25_net_bp": float(trades.net_bp.quantile(.25)) if n else None,
           "p75_net_bp": float(trades.net_bp.quantile(.75)) if n else None,
           "p95_net_bp": float(trades.net_bp.quantile(.95)) if n else None,
           "gross_win_rate": float(trades.gross_bp.gt(0).mean()) if n else None,
           "net_win_rate": float(trades.net_bp.gt(0).mean()) if n else None,
           "profitable_day_fraction": float(daily.net_pnl.gt(0).mean()),
           "median_holding_minutes": float(trades.holding_minutes.median()) if n else None,
           "mean_holding_minutes": float(trades.holding_minutes.mean()) if n else None,
           "ambiguous_trade_count": int(trades.same_bar_ambiguous.sum()) if n else 0,
           "zero_volume_exposed_trades": int(trades.held_zero_volume_bars.gt(0).sum()) if n else 0,
           "unknown_execution_gap_trades": int(trades.execution_gap_unknown.sum()) if n else 0,
           "mean_planned_reward_bp": float(trades.planned_reward_bp.mean()) if n else None,
           "mean_planned_risk_bp": float(trades.planned_risk_bp.mean()) if n else None,
           "mean_planned_break_even_p": float(trades.planned_break_even_p.mean()) if n else None,
           "max_entry_volume_fraction": float(trades.entry_volume_fraction.max()) if n else None,
           "max_exit_volume_fraction": float(trades.exit_volume_fraction.max()) if n else None,
           "skip_cost_ceiling": rejections.get("precommitted_cost_ceiling", 0),
           "skip_position_busy": rejections.get("position_busy_at_open", 0)}
    if not marks.empty:
        equity = np.r_[spec["starting_cash_krw"], marks.equity.to_numpy(float)]
        peak = np.maximum.accumulate(equity)
        out["max_drawdown_krw"] = float((equity - peak).min())
        out["max_drawdown_pct"] = float(((equity - peak) / peak).min() * 100)
    else:
        out["max_drawdown_krw"] = out["max_drawdown_pct"] = 0.0
    gain = float(trades.loc[trades.net_pnl > 0, "net_pnl"].sum()) if n else 0.0
    loss = -float(trades.loc[trades.net_pnl < 0, "net_pnl"].sum()) if n else 0.0
    out["profit_factor"] = gain / loss if loss else None
    out.update(block_intervals(daily, spec))
    sufficient = n >= spec["minimum_trades_for_inference"] and active >= spec["minimum_active_days_for_inference"]
    known = out["unknown_execution_gap_trades"] == 0
    out["inference_sample_sufficient"] = bool(sufficient)
    out["point_gross_target_met"] = bool(n and out["mean_gross_bp"] >= spec["gross_target_bps"])
    out["gross_target_supported"] = bool(sufficient and known and out["gross_ci_low"] >= spec["gross_target_bps"])
    out["net_positive_supported"] = bool(sufficient and known and out["net_ci_low"] > 0)
    out["net_4bp_supported"] = bool(sufficient and known and out["net_ci_low"] >= spec["net_stretch_target_bps"])
    out["frequency_profile_met"] = bool(out["mean_trades_per_day"] >= spec["frequency_target_mean_trades_per_day"]
                                         and out["active_day_fraction"] >= spec["frequency_target_active_day_fraction"])
    if n:
        top = daily.nlargest(min(5, len(daily)), "net_pnl").trade_date
        remaining = trades[~trades.trade_date.isin(top)]
        out["mean_net_bp_excluding_top5_days"] = float(remaining.net_bp.mean()) if len(remaining) else None
    else:
        out["mean_net_bp_excluding_top5_days"] = None
    for reason in ("target", "target_gap", "target_ambiguous", "stop", "stop_gap", "stop_ambiguous", "daily_flat", "unobserved_execution_gap"):
        out["exit_" + reason] = int(trades.exit_reason.eq(reason).sum()) if n else 0
    return out


def grouped_tables(trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    columns = ["trades", "mean_gross_bp", "mean_net_bp", "net_pnl"]
    if trades.empty:
        return pd.DataFrame(columns=["month", *columns]), pd.DataFrame(columns=["entry_hour", *columns])
    t = trades.copy()
    t["month"] = t.entry_time.dt.strftime("%Y-%m")
    t["entry_hour"] = t.entry_time.dt.hour
    def table(col):
        return t.groupby(col).agg(trades=("net_bp", "size"), mean_gross_bp=("gross_bp", "mean"),
                                  mean_net_bp=("net_bp", "mean"), net_pnl=("net_pnl", "sum")).reset_index()
    return table("month"), table("entry_hour")


def json_write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def run_partition(partition: str, spec: dict):
    out = OUT_ROOT / partition
    if (out / "run_manifest.json").exists():
        raise FileExistsError(f"Refusing to overwrite a recorded {partition} run: {out}")
    out.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    print(f"[{partition}] Loading joint signal bars and independent ETF execution tape", flush=True)
    joint, tape, eval_dates, meta, data_audit = load_inputs(spec, partition)
    data_audit.to_csv(out / "data_quality_by_day.csv", index=False, encoding="utf-8-sig")
    x = add_features(joint, spec)
    x, normalization_audit = add_prior_day_normalization(x, spec)
    normalization_audit.to_csv(out / "normalization_audit.csv", index=False, encoding="utf-8-sig")
    signals = build_signals(x, eval_dates, spec)
    signals.to_csv(out / "signals.csv", index=False, encoding="utf-8-sig")
    print(f"[{partition}] {len(eval_dates)} evaluation days; {len(signals)} frozen structural signals", flush=True)
    summaries = []
    portfolios = {"A_continuation": ["A_continuation"], "B_shock_recovery": ["B_shock_recovery"],
                  "AB_one_position": spec["portfolio_priority"]}
    for case_name, case in spec["cases"].items():
        for name, selected in portfolios.items():
            folder = out / case_name / name
            folder.mkdir(parents=True, exist_ok=True)
            trades, marks, rejected = simulate(signals, x, tape, eval_dates, selected, case, spec)
            daily = daily_table(trades, eval_dates, marks, spec)
            trades.to_csv(folder / "trades.csv", index=False, encoding="utf-8-sig")
            daily.to_csv(folder / "daily.csv", index=False, encoding="utf-8-sig")
            # Per-minute marks support marked equity drawdown audit without bloating all variants.
            if case_name == "primary":
                marks.to_csv(folder / "minute_equity.csv", index=False, encoding="utf-8-sig")
            months, hours = grouped_tables(trades)
            months.to_csv(folder / "monthly.csv", index=False, encoding="utf-8-sig")
            hours.to_csv(folder / "entry_hour.csv", index=False, encoding="utf-8-sig")
            json_write(folder / "rejections.json", rejected)
            s = {"partition": partition, "case": case_name, "portfolio": name,
                 **summarize(trades, marks, daily, int(signals.strategy.isin(selected).sum()), rejected, spec)}
            summaries.append(s)
            print(f"[{partition}] {case_name}/{name}: trades={s['trades']}, "
                  f"gross={s['mean_gross_bp']}, net={s['mean_net_bp']}", flush=True)
    table = pd.DataFrame(summaries)
    table.to_csv(out / "summary.csv", index=False, encoding="utf-8-sig")
    manifest = {
        "status": "complete", "partition": partition, "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "spec_version": spec["version"], "spec_sha256": sha256(SPEC_PATH),
        "spec_document_sha256": sha256(SPEC_DOC), "code_sha256": sha256(Path(__file__)),
        "aligned_input_sha256": sha256(resolve_input(spec["input_aligned"])),
        "execution_input_sha256": sha256(resolve_input(spec["input_execution"])),
        "oos_metrics_computed": partition == "oos", "no_parameter_search": True,
        "all_cases_registered_before_new_performance": True,
        "same_bar_primary": "stop first, open gaps precede intrabar extrema",
        "ci": "unadjusted marginal 95%; 5 trading-day moving block; trade-count weighted mean",
        "signals": len(signals), **meta,
        "limitations": ["OHLC cannot certify BBO, depth, arrival before first print or partial fills",
                        "slippage assumptions are scenarios, not estimates from quotes",
                        "zero-volume bars may be halts or data problems; no fills there",
                        "source timestamp convention inherited from prep.py, overlapping OHLCV matched",
                        "parameter/strategy selection history predates this v1 and IS is exploratory"],
    }
    json_write(out / "run_manifest.json", manifest)
    return table


def format_number(value, digits=2):
    return "—" if pd.isna(value) else f"{value:,.{digits}f}"


def make_report(spec: dict):
    tables = []
    for partition in ("is", "oos"):
        path = OUT_ROOT / partition / "summary.csv"
        if path.exists():
            tables.append(pd.read_csv(path))
    if not tables:
        return
    all_results = pd.concat(tables, ignore_index=True)
    all_results.to_csv(OUT_ROOT / "all_summary.csv", index=False, encoding="utf-8-sig")
    primary = all_results[all_results.case.eq("primary")]
    lines = ["# 고정 TP·SL 전략 v1 백테스트 결과", "",
             "전략과 진단을 먼저 고정하고 가격 기반 TP·SL로 평가했다. 20분 보유 time cut은 사용하지 않았다. "
             "15:15 장 종료 관리 청산은 남아 있다. 수수료 왕복 4bp와 기본 체결비용 왕복 약 2bp를 적용했다.", "",
             "IS: 첫 60일 보정 이후의 평가 기간. OOS: 2026-07-22~2026-10-06. "
             "기존 잠금 OOS는 이번 v1에서 평가되었으며 앞으로 v1 결과를 보고 변경한 규칙의 미열람 검증 구간으로 재사용할 수 없다.", "",
             "| 구간 | 전략 | 거래 | 거래/일 | 평균 gross bp | 평균 net bp | gross 95% 구간 | net 95% 구간 | median net bp | net PnL 원 |",
             "|---|---|---:|---:|---:|---:|---|---|---:|---:|"]
    for r in primary.itertuples(index=False):
        lines.append(f"| {r.partition.upper()} | {r.portfolio} | {r.trades} | {r.mean_trades_per_day:.2f} | "
                     f"{format_number(r.mean_gross_bp)} | {format_number(r.mean_net_bp)} | "
                     f"[{format_number(r.gross_ci_low)}, {format_number(r.gross_ci_high)}] | "
                     f"[{format_number(r.net_ci_low)}, {format_number(r.net_ci_high)}] | "
                     f"{format_number(r.median_net_bp)} | {r.net_pnl:,.0f} |")
    lines += ["", "표본과 빈도 판정은 별도다. point gross 4bp 통과는 기대수익 4bp가 통계적으로 확인됐다는 뜻이 아니다. "
              "100거래·20활성일 미만은 추론 표본 부족으로 표시했다. CI는 family 다중검정 보정 없는 개별 구간이며 "
              "같은 가격 경로를 사용한 단독/통합 결과를 독립 증거로 합산하지 않는다.", "",
              "| 구간 | 전략 | 충분한 표본 | gross 4bp point | gross 4bp CI 지지 | net 양수 CI 지지 | 빈도 기준 | 활성일 비율 |",
              "|---|---|---|---|---|---|---|---:|"]
    for r in primary.itertuples(index=False):
        lines.append(f"| {r.partition.upper()} | {r.portfolio} | {r.inference_sample_sufficient} | {r.point_gross_target_met} | "
                     f"{r.gross_target_supported} | {r.net_positive_supported} | {r.frequency_profile_met} | {r.active_day_fraction:.1%} |")
    lines += ["", "**사전 고정한 진단 결과**", "",
              "| 구간 | 진단 | 전략 | 거래 | gross bp | net bp | TP/SL 동시접촉 거래 |",
              "|---|---|---|---:|---:|---:|---:|"]
    for r in all_results[~all_results.case.eq("primary")].itertuples(index=False):
        lines.append(f"| {r.partition.upper()} | {r.case} | {r.portfolio} | {r.trades} | "
                     f"{format_number(r.mean_gross_bp)} | {format_number(r.mean_net_bp)} | {r.ambiguous_trade_count} |")
    lines += ["", "경계 진입은 확정과 동일 시각의 첫 체결을 참조하는 진단이다. 주 결과로 채택하지 않는다. "
              "체결비용 stress는 사전 주문 상한을 바꾸지 않는다. TP 우선은 OHLC 순서 가정의 민감도이며 "
              "재진입 경로가 달라지므로 전체 수익의 엄밀한 상한이라고 단정하지 않는다. "
              "비용 gate 제거는 제한된 손익 구조와 빈도를 분리하는 진단이며 전략 개선으로 승격하지 않는다.", "",
              "**청산·위험과 손익 구조**", "",
              "| 구간 | 전략 | TP 계열 | SL 계열 | 장 종료 | median 보유 분 | 평균 목표폭 bp | 평균 손절폭 bp | MTM MDD 원 | 정지/0거래량 노출 거래 |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in primary.itertuples(index=False):
        tp = r.exit_target + r.exit_target_gap + r.exit_target_ambiguous
        sl = r.exit_stop + r.exit_stop_gap + r.exit_stop_ambiguous
        lines.append(f"| {r.partition.upper()} | {r.portfolio} | {tp} | {sl} | {r.exit_daily_flat} | "
                     f"{format_number(r.median_holding_minutes)} | {format_number(r.mean_planned_reward_bp)} | "
                     f"{format_number(r.mean_planned_risk_bp)} | {r.max_drawdown_krw:,.0f} | {r.zero_volume_exposed_trades} |")
    lines += ["", "신호에는 연속 공동 자료만 사용했지만 보유 중 ETF 청산은 별도 전체 ETF tape에서 추적했다. "
              "선물 공백 날짜를 삭제하지 않았다. 0거래량 봉에서 체결을 만들지 않았으며 "
              "execution tape 자체의 미관측 장중 공백 노출은 별도 표시했다. "
              "실제 호가·깊이·초 단위 도착·시장정지 여부·부분체결은 1분봉으로 인증할 수 없다.", "",
              "시장 폭 후보 C는 추가 구성종목/업종 데이터가 없어 미실행이다. "
              "임의 대체 지표로 구현했다고 주장하지 않았다. 결과를 보고 v1 시간이나 TP·SL을 수정하지 않았다.", "",
              "구체적 신호·가격·비용 공식은 `intraday_bracket_spec_v1.md`와 JSON, "
              "구현은 `intraday_bracket_backtest.py`에 있다. "
              "각 구간/진단/전략 디렉터리에 trades, daily, monthly, entry_hour, rejections를 저장했다. "
              "primary의 minute_equity는 장중 평가손익 drawdown을 확인하는 자료다."]
    (OUT_ROOT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    plot_results(primary)


def plot_results(primary: pd.DataFrame):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    partitions = list(primary.partition.unique())
    fig, axes = plt.subplots(len(partitions), 2, figsize=(12, 4 * len(partitions)), squeeze=False)
    for row, partition in enumerate(partitions):
        subset = primary[primary.partition.eq(partition)]
        for r in subset.itertuples(index=False):
            folder = OUT_ROOT / partition / "primary" / r.portfolio
            d = pd.read_csv(folder / "daily.csv")
            t = pd.read_csv(folder / "trades.csv")
            axes[row, 0].plot(pd.to_datetime(d.trade_date), d.net_pnl.cumsum(), label=r.portfolio)
            if len(t):
                axes[row, 1].hist(t.net_bp, bins=30, alpha=.4, label=r.portfolio)
        axes[row, 0].set(title=f"{partition.upper()} cumulative net PnL", ylabel="KRW")
        axes[row, 1].set(title=f"{partition.upper()} net return / trade", xlabel="bp")
        for ax in axes[row]:
            ax.axhline(0, color="black", lw=.6)
            ax.grid(alpha=.2)
            ax.legend(fontsize=8)
        axes[row, 0].tick_params(axis="x", rotation=25)
    fig.tight_layout()
    fig.savefig(OUT_ROOT / "primary_results.png", dpi=160)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--partition", choices=["is", "oos"], default="is")
    args = parser.parse_args()
    spec = load_spec()
    if args.partition == "oos":
        manifest_path = OUT_ROOT / "is" / "run_manifest.json"
        if not manifest_path.exists():
            raise RuntimeError("Complete implementation validation and IS run before opening OOS")
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous["spec_sha256"] != sha256(SPEC_PATH) or previous["spec_document_sha256"] != sha256(SPEC_DOC):
            raise RuntimeError("Spec changed after IS; OOS cannot be run as the same frozen v1")
        recorded_code_provenance(Path(__file__), previous["code_sha256"])
    run_partition(args.partition, spec)
    make_report(spec)
    print(f"Saved report: {OUT_ROOT / 'report.md'}", flush=True)


if __name__ == "__main__":
    main()
