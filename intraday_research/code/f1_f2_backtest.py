from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


# ============================================================
# 0. Configuration
# ============================================================

from research_paths import RESULTS, resolve_input

DEFAULT_INPUT = resolve_input("market_1m_data/KOSPI200_FUT_ETF_1m_aligned.csv")
DEFAULT_OUT = RESULTS / "backtest_f1_f2_minute_is"

# Input timestamp semantics:
#   timestamp/bar_start=09:00 means the 09:00~09:01 bar.
# Full OHLCV of that row is available only at 09:01.
BAR_SIZE_MIN = 1
SESSION_START = "09:00"
SESSION_END = "15:19"
TIME_BUCKET_MIN = 5

# Keep the newest sample completely locked. No OOS feature, signal, trade,
# or performance metric is computed by this script.
OOS_HOLDOUT_FRAC = 0.20
MIN_OOS_DAYS = 40
MIN_IS_DAYS = 120

# Rolling causal normalization. For each test date, only the PRIOR N trading
# days in IS are used to estimate same-time-of-day-bucket statistics.
ROLLING_HISTORY_DAYS = 60
MIN_HISTORY_OBS_PER_BUCKET = 80

# F1: positive relative dislocation = ETF underperformed futures.
# The primary research lookback from the EDA is 3m; the others are predefined
# sensitivity checks, not post-hoc additions.
F1_LOOKBACKS = [1, 3, 5, 10]
F1_GAP_Q = 0.90
F1_HOLD_MINUTES = [1, 3, 5, 10]
F1_STATE_FILTERS = [
    "all_positive_gap",
    "future_up_lead",
    "etf_downside_overshoot",
    "sign_divergence",
]

# F2: selected strategy from EDA.
# Previous completed bar: volume > same-bucket historical median AND
# abs(close-open) <= same-bucket historical median.
# Current completed bar: close > previous close = upward resolution.
# Then buy ETF at the NEXT bar open.
F2_HOLD_MINUTES = [5, 10]

# Execution/cost assumptions. These are intentionally configurable because
# minute OHLCV has no historical BBO/queue information.
DEFAULT_STARTING_CASH = 10_000_000.0
DEFAULT_ORDER_NOTIONAL = 1_000_000.0
DEFAULT_FEE_BPS_PER_SIDE = 1.0
DEFAULT_SLIPPAGE_BPS_PER_SIDE = 1.0

# Signal bar close -> next bar open boundary execution.
DEFAULT_ENTRY_DELAY_BARS = 1

RNG_SEED = 20261007
BOOTSTRAP_N = 1000


# ============================================================
# 1. CLI / filesystem
# ============================================================


def parse_args():
    p = argparse.ArgumentParser(
        description=(
            "IS-only causal minute backtest for F1 relative dislocation and "
            "F2 median-compression -> upward-resolution. OOS stays locked."
        )
    )
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--history-days", type=int, default=ROLLING_HISTORY_DAYS)
    p.add_argument("--starting-cash", type=float, default=DEFAULT_STARTING_CASH)
    p.add_argument("--order-notional", type=float, default=DEFAULT_ORDER_NOTIONAL)
    p.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS_PER_SIDE)
    p.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS_PER_SIDE)
    p.add_argument("--entry-delay-bars", type=int, default=DEFAULT_ENTRY_DELAY_BARS, choices=[1, 2, 3])
    return p.parse_args()


def mkdirs(root: Path):
    d = {
        "root": root,
        "f1": root / "f1",
        "f2": root / "f2",
        "diag": root / "diagnostics",
    }
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    return d


# ============================================================
# 2. Input validation / causality
# ============================================================


def load_aligned(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)

    x = pd.read_csv(path, encoding="utf-8-sig")

    if "bar_start" in x.columns:
        ts_col = "bar_start"
    elif "timestamp" in x.columns:
        ts_col = "timestamp"
    else:
        raise ValueError("Input must contain 'bar_start' or 'timestamp' with bar-start semantics")

    required = {
        "fut_open", "fut_high", "fut_low", "fut_close", "fut_volume",
        "etf_open", "etf_high", "etf_low", "etf_close", "etf_volume",
    }
    missing = sorted(required - set(x.columns))
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    x["bar_start"] = pd.to_datetime(x[ts_col], errors="coerce")
    if x["bar_start"].isna().any():
        raise ValueError("Invalid timestamps in aligned input")

    # A 09:00 row is the 09:00~09:01 interval. Its full OHLCV is usable at 09:01.
    x["bar_end"] = x["bar_start"] + pd.Timedelta(minutes=BAR_SIZE_MIN)
    x["available_at"] = x["bar_end"]

    x = (
        x.sort_values("bar_start")
        .drop_duplicates("bar_start", keep="last")
        .reset_index(drop=True)
    )
    x["trade_date"] = x["bar_start"].dt.date

    for c in required:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    if x[list(required)].isna().any().any():
        raise ValueError("Required OHLCV contains NaN/non-numeric values")

    for prefix in ["fut", "etf"]:
        px = [f"{prefix}_open", f"{prefix}_high", f"{prefix}_low", f"{prefix}_close"]
        bad = (
            (x[f"{prefix}_high"] < x[px].max(axis=1))
            | (x[f"{prefix}_low"] > x[px].min(axis=1))
            | (x[f"{prefix}_volume"] < 0)
            | (x[px] <= 0).any(axis=1)
        )
        if bad.any():
            raise ValueError(f"{prefix}: invalid OHLCV rows={int(bad.sum())}")

    hhmm = x["bar_start"].dt.strftime("%H:%M")
    x = x[(hhmm >= SESSION_START) & (hhmm <= SESSION_END)].copy()

    # Segment boundaries prevent returns/trades from crossing missing minutes,
    # trading days, or futures rolls.
    dt = x["bar_start"].diff()
    start = x["trade_date"].ne(x["trade_date"].shift()) | dt.ne(pd.Timedelta(minutes=1))

    if "contract_code" in x.columns:
        start |= x["contract_code"].ne(x["contract_code"].shift())

    if "contract_changed" in x.columns:
        cc = x["contract_changed"]
        if cc.dtype == object:
            cc = cc.astype(str).str.lower().isin(["1", "true", "t", "yes", "y"])
        else:
            cc = cc.fillna(False).astype(bool)
        start |= cc

    x["segment_start"] = start
    x["segment_id"] = start.cumsum().astype(int)

    minute = x["bar_start"].dt.hour * 60 + x["bar_start"].dt.minute
    x["tod_bucket"] = ((minute - 9 * 60) // TIME_BUCKET_MIN).astype(int)

    return x.reset_index(drop=True)


def split_is(panel: pd.DataFrame):
    dates = np.array(sorted(panel["trade_date"].unique()))
    n_oos = max(MIN_OOS_DAYS, math.ceil(len(dates) * OOS_HOLDOUT_FRAC))

    if len(dates) - n_oos < MIN_IS_DAYS:
        raise ValueError("Not enough common dates for requested locked OOS")

    is_dates = dates[:-n_oos]
    oos_dates = dates[-n_oos:]

    # IMPORTANT: only IS rows are returned. OOS rows never enter features/trades.
    is_panel = panel[panel["trade_date"].isin(is_dates)].copy()

    manifest = {
        "total_common_days": int(len(dates)),
        "is_days": int(len(is_dates)),
        "oos_locked_days": int(len(oos_dates)),
        "is_first": str(is_dates[0]),
        "is_last": str(is_dates[-1]),
        "oos_first_locked": str(oos_dates[0]),
        "oos_last_locked": str(oos_dates[-1]),
        "oos_metrics_computed": False,
    }
    return is_panel, manifest


# ============================================================
# 3. Causal features
# ============================================================


def back_ret(df: pd.DataFrame, col: str, n: int) -> pd.Series:
    prev = df.groupby("segment_id")[col].shift(n)
    return np.log(df[col] / prev) * 1e4


def add_features(x: pd.DataFrame) -> pd.DataFrame:
    x = x.copy()

    # Full-bar feature availability is the bar end, not bar start.
    x["feature_available_at"] = x["available_at"]

    x["fut_body_signed_bp"] = np.log(x["fut_close"] / x["fut_open"]) * 1e4
    x["fut_body_abs_bp"] = x["fut_body_signed_bp"].abs()

    for n in F1_LOOKBACKS:
        x[f"fut_ret_{n}m_bp"] = back_ret(x, "fut_close", n)
        x[f"etf_ret_{n}m_bp"] = back_ret(x, "etf_close", n)
        x[f"f1_gap_{n}m_bp"] = x[f"fut_ret_{n}m_bp"] - x[f"etf_ret_{n}m_bp"]

    return x


# ============================================================
# 4. Rolling prior-day same-bucket normalization
# ============================================================


def add_rolling_thresholds(x: pd.DataFrame, history_days: int):
    """Attach causal same-5-minute-bucket historical thresholds.

    For each current trading date d, thresholds use only the previous
    `history_days` IS trading dates. The current date never contributes to its
    own normalization.
    """

    x = x.copy()
    dates = list(sorted(x["trade_date"].unique()))

    # Columns used by F1/F2.
    specs: dict[str, list[float]] = {
        "fut_volume": [0.50],
        "fut_body_abs_bp": [0.50],
    }
    for n in F1_LOOKBACKS:
        specs[f"f1_gap_{n}m_bp"] = [F1_GAP_Q]

    for col, qs in specs.items():
        for q in qs:
            x[f"{col}_q{int(round(q * 100)):02d}"] = np.nan
        x[f"{col}_hist_n"] = np.nan

    audit = []

    for i, d in enumerate(dates):
        if i < history_days:
            continue

        hist_dates = set(dates[i - history_days:i])
        hist = x[x["trade_date"].isin(hist_dates)]
        cur_idx = x.index[x["trade_date"].eq(d)]

        for col, qs in specs.items():
            hz = hist[["tod_bucket", col]].dropna()
            if hz.empty:
                continue

            counts = hz.groupby("tod_bucket")[col].size()
            qtab = hz.groupby("tod_bucket")[col].quantile(qs).unstack(level=-1)

            bucket_map = x.loc[cur_idx, "tod_bucket"]
            hist_n = bucket_map.map(counts)
            x.loc[cur_idx, f"{col}_hist_n"] = hist_n.to_numpy()

            for q in qs:
                out_col = f"{col}_q{int(round(q * 100)):02d}"
                vals = bucket_map.map(qtab[q])
                vals = vals.where(hist_n >= MIN_HISTORY_OBS_PER_BUCKET)
                x.loc[cur_idx, out_col] = vals.to_numpy()

            # One audit row per date/feature, not per minute, to keep file compact.
            valid_counts = counts[counts >= MIN_HISTORY_OBS_PER_BUCKET]
            audit.append({
                "trade_date": str(d),
                "feature": col,
                "history_days": history_days,
                "history_first": str(dates[i - history_days]),
                "history_last": str(dates[i - 1]),
                "eligible_buckets": int(len(valid_counts)),
                "median_hist_n_per_eligible_bucket": (
                    float(valid_counts.median()) if len(valid_counts) else np.nan
                ),
            })

    return x, pd.DataFrame(audit)


# ============================================================
# 5. Signal construction
# ============================================================


def make_f1_signals(x: pd.DataFrame) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}

    for w in F1_LOOKBACKS:
        gap = x[f"f1_gap_{w}m_bp"]
        q90 = x[f"f1_gap_{w}m_bp_q90"]
        f = x[f"fut_ret_{w}m_bp"]
        e = x[f"etf_ret_{w}m_bp"]

        extreme = gap.gt(0) & gap.ge(q90) & q90.notna()

        # Positive relative gap means ETF underperformed futures over the lookback.
        states = {
            "all_positive_gap": extreme,
            "future_up_lead": extreme & f.gt(0) & e.ge(0),
            "etf_downside_overshoot": extreme & f.le(0) & e.lt(0),
            "sign_divergence": extreme & f.gt(0) & e.lt(0),
        }

        for state, mask in states.items():
            out[f"F1_w{w}__{state}"] = mask.fillna(False)

    return out


def make_f2_signal(x: pd.DataFrame) -> pd.Series:
    # Compression is known when the PREVIOUS bar completes.
    compression = (
        (x["fut_volume"] > x["fut_volume_q50"])
        & (x["fut_body_abs_bp"] <= x["fut_body_abs_bp_q50"])
        & x["fut_volume_q50"].notna()
        & x["fut_body_abs_bp_q50"].notna()
    )

    # Explicit groupwise shift of boolean compression without adding a temporary
    # user-visible dependency on DataFrame column naming.
    tmp = x[["segment_id"]].copy()
    tmp["compression"] = compression.astype(bool)
    prev_comp = tmp.groupby("segment_id")["compression"].shift(1).eq(True)

    prev_close = x.groupby("segment_id")["fut_close"].shift(1)

    # Selected EDA rule: Median compression -> upward resolution versus previous close.
    # This signal becomes known only when the CURRENT (resolution) bar closes.
    signal = prev_comp & x["fut_close"].gt(prev_close)
    return signal.fillna(False)


# ============================================================
# 6. Execution helpers
# ============================================================


def attach_execution_prices(x: pd.DataFrame, hold_minutes: int, entry_delay_bars: int):
    """Return next-open entry and fixed-horizon open exit.

    Signal row t uses full-bar information known at bar_end_t.
    entry_delay_bars=1 -> buy at ETF open of row t+1.
    Holding H minutes -> sell at ETF open H minutes after entry.

    Groupwise shift ensures no execution crosses a gap/day/roll segment.
    """

    g = x.groupby("segment_id", group_keys=False)

    entry_price = g["etf_open"].shift(-entry_delay_bars)
    entry_time = g["bar_start"].shift(-entry_delay_bars)

    exit_shift = entry_delay_bars + hold_minutes
    exit_price = g["etf_open"].shift(-exit_shift)
    exit_time = g["bar_start"].shift(-exit_shift)

    return entry_price, entry_time, exit_price, exit_time


@dataclass
class CostConfig:
    starting_cash: float
    order_notional: float
    fee_bps: float
    slippage_bps: float


# ============================================================
# 7. One-scenario backtest
# ============================================================


def backtest_signal(
    x: pd.DataFrame,
    signal: pd.Series,
    strategy: str,
    scenario: str,
    hold_minutes: int,
    costs: CostConfig,
    entry_delay_bars: int,
    context: dict[str, pd.Series] | None = None,
    is_primary: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Long/flat, fixed notional, no overlapping positions within a scenario."""

    entry_raw, entry_time, exit_raw, exit_time = attach_execution_prices(x, hold_minutes, entry_delay_bars)

    candidates = pd.DataFrame({
        "signal_idx": x.index,
        "trade_date": x["trade_date"],
        "signal_bar_start": x["bar_start"],
        "signal_available_at": x["available_at"],
        "segment_id": x["segment_id"],
        "signal": signal.astype(bool),
        "entry_time": entry_time,
        "entry_raw_open": entry_raw,
        "exit_time": exit_time,
        "exit_raw_open": exit_raw,
    })

    if context:
        for name, values in context.items():
            candidates[name] = values.reindex(x.index)

    c = candidates[
        candidates["signal"]
        & candidates["entry_time"].notna()
        & candidates["exit_time"].notna()
        & candidates["entry_raw_open"].notna()
        & candidates["exit_raw_open"].notna()
    ].copy()

    c = c.sort_values(["entry_time", "signal_idx"]).reset_index(drop=True)

    fee = costs.fee_bps / 1e4
    slip = costs.slippage_bps / 1e4

    trades = []
    next_free_time = None
    skipped_overlap = 0

    for row in c.itertuples(index=False):
        if next_free_time is not None and row.entry_time < next_free_time:
            skipped_overlap += 1
            continue

        entry_raw_px = float(row.entry_raw_open)
        exit_raw_px = float(row.exit_raw_open)

        # Adverse execution assumption around observed 1-minute open.
        entry_exec = entry_raw_px * (1.0 + slip)
        exit_exec = exit_raw_px * (1.0 - slip)

        max_qty_cash = costs.starting_cash / max(entry_exec * (1.0 + fee), 1e-12)
        target_qty = costs.order_notional / max(entry_exec * (1.0 + fee), 1e-12)
        qty = int(math.floor(min(max_qty_cash, target_qty)))
        if qty <= 0:
            continue

        entry_notional = qty * entry_exec
        exit_notional = qty * exit_exec
        entry_fee = entry_notional * fee
        exit_fee = exit_notional * fee

        gross_pnl = qty * (exit_raw_px - entry_raw_px)
        net_pnl = (exit_notional - exit_fee) - (entry_notional + entry_fee)

        gross_return_bp = (exit_raw_px / entry_raw_px - 1.0) * 1e4
        net_return_bp = net_pnl / (entry_notional + entry_fee) * 1e4

        trade_row = {
            "strategy": strategy,
            "scenario": scenario,
            "is_primary": bool(is_primary),
            "hold_minutes": hold_minutes,
            "entry_delay_bars": entry_delay_bars,
            "signal_bar_start": row.signal_bar_start,
            "signal_available_at": row.signal_available_at,
            "entry_time": row.entry_time,
            "exit_time": row.exit_time,
            "entry_raw_open": entry_raw_px,
            "exit_raw_open": exit_raw_px,
            "entry_exec_price": entry_exec,
            "exit_exec_price": exit_exec,
            "qty": qty,
            "entry_fee": entry_fee,
            "exit_fee": exit_fee,
            "gross_return_bp": gross_return_bp,
            "net_return_bp": net_return_bp,
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
        }
        if context:
            for name in context:
                trade_row[name] = getattr(row, name)
        trades.append(trade_row)

        # A new position may start at the same timestamp at which the prior one exits.
        next_free_time = row.exit_time

    t = pd.DataFrame(trades)

    if t.empty:
        daily = pd.DataFrame(columns=["trade_date", "trades", "net_pnl", "gross_pnl", "mean_net_bp"])
        summary = summarize_scenario(
            t, strategy, scenario, hold_minutes, int(signal.sum()), len(c),
            skipped_overlap, costs, entry_delay_bars, is_primary
        )
        return t, daily, summary

    t["trade_date"] = pd.to_datetime(t["entry_time"]).dt.date
    t["cum_net_pnl"] = t["net_pnl"].cumsum()
    t["equity"] = costs.starting_cash + t["cum_net_pnl"]
    t["equity_peak"] = t["equity"].cummax()
    t["drawdown"] = t["equity"] - t["equity_peak"]
    t["drawdown_pct"] = t["drawdown"] / t["equity_peak"].replace(0, np.nan)

    daily = (
        t.groupby("trade_date")
        .agg(
            trades=("net_pnl", "size"),
            gross_pnl=("gross_pnl", "sum"),
            net_pnl=("net_pnl", "sum"),
            mean_gross_bp=("gross_return_bp", "mean"),
            mean_net_bp=("net_return_bp", "mean"),
        )
        .reset_index()
    )

    summary = summarize_scenario(
        t, strategy, scenario, hold_minutes, int(signal.sum()), len(c),
        skipped_overlap, costs, entry_delay_bars, is_primary
    )
    return t, daily, summary


def bootstrap_day_mean_ci(t: pd.DataFrame, col: str):
    if t.empty:
        return np.nan, np.nan, np.nan
    d = t.groupby("trade_date")[col].mean().dropna().to_numpy(float)
    if len(d) == 0:
        return np.nan, np.nan, np.nan
    m = float(d.mean())
    if len(d) < 2:
        return m, np.nan, np.nan
    rng = np.random.default_rng(RNG_SEED)
    ix = rng.integers(0, len(d), size=(BOOTSTRAP_N, len(d)))
    sims = d[ix].mean(axis=1)
    lo, hi = np.quantile(sims, [0.025, 0.975])
    return m, float(lo), float(hi)


def summarize_scenario(
    t: pd.DataFrame,
    strategy: str,
    scenario: str,
    hold_minutes: int,
    raw_signals: int,
    executable_candidates: int,
    skipped_overlap: int,
    costs: CostConfig,
    entry_delay_bars: int = 1,
    is_primary: bool = False,
):
    base = {
        "strategy": strategy,
        "scenario": scenario,
        "is_primary": bool(is_primary),
        "hold_minutes": hold_minutes,
        "entry_delay_bars": int(entry_delay_bars),
        "raw_signals": raw_signals,
        "executable_candidates": executable_candidates,
        "executed_trades": int(len(t)),
        "skipped_overlap": int(skipped_overlap),
        "fee_bps_per_side": costs.fee_bps,
        "slippage_bps_per_side": costs.slippage_bps,
        "order_notional": costs.order_notional,
    }

    if t.empty:
        return {
            **base,
            "days_traded": 0,
            "gross_pnl": 0.0,
            "net_pnl": 0.0,
            "mean_gross_bp": np.nan,
            "median_gross_bp": np.nan,
            "mean_net_bp": np.nan,
            "median_net_bp": np.nan,
            "win_rate_net": np.nan,
            "profit_factor": np.nan,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "day_mean_net_bp": np.nan,
            "day_mean_net_bp_ci_low": np.nan,
            "day_mean_net_bp_ci_high": np.nan,
            "profitable_day_fraction": np.nan,
        }

    wins = t.loc[t["net_pnl"] > 0, "net_pnl"].sum()
    losses = -t.loc[t["net_pnl"] < 0, "net_pnl"].sum()
    profit_factor = wins / losses if losses > 0 else np.inf

    day_mean, day_lo, day_hi = bootstrap_day_mean_ci(t, "net_return_bp")
    daily_pnl = t.groupby("trade_date")["net_pnl"].sum()

    return {
        **base,
        "days_traded": int(t["trade_date"].nunique()),
        "gross_pnl": float(t["gross_pnl"].sum()),
        "net_pnl": float(t["net_pnl"].sum()),
        "mean_gross_bp": float(t["gross_return_bp"].mean()),
        "median_gross_bp": float(t["gross_return_bp"].median()),
        "mean_net_bp": float(t["net_return_bp"].mean()),
        "median_net_bp": float(t["net_return_bp"].median()),
        "win_rate_net": float((t["net_pnl"] > 0).mean()),
        "profit_factor": float(profit_factor),
        "max_drawdown": float(t["drawdown"].min()),
        "max_drawdown_pct": float(t["drawdown_pct"].min()),
        "day_mean_net_bp": day_mean,
        "day_mean_net_bp_ci_low": day_lo,
        "day_mean_net_bp_ci_high": day_hi,
        "profitable_day_fraction": float((daily_pnl > 0).mean()),
    }


# ============================================================
# 8. Strategy runners
# ============================================================


def run_f1(x: pd.DataFrame, dirs, costs: CostConfig, entry_delay_bars: int):
    signals = make_f1_signals(x)
    summaries = []
    all_trades = []
    all_daily = []

    for name, sig in signals.items():
        for hold in F1_HOLD_MINUTES:
            scenario = f"{name}__hold{hold}m"
            parts = name.split("__", 1)
            w = int(parts[0].replace("F1_w", ""))
            state_name = parts[1]
            context = {
                "f1_gap_bp": x[f"f1_gap_{w}m_bp"],
                "f1_gap_q90_bp": x[f"f1_gap_{w}m_bp_q90"],
                "fut_lookback_ret_bp": x[f"fut_ret_{w}m_bp"],
                "etf_lookback_ret_bp": x[f"etf_ret_{w}m_bp"],
            }
            is_primary = (w == 3 and state_name == "all_positive_gap")
            trades, daily, summary = backtest_signal(
                x=x,
                signal=sig,
                strategy="F1_relative_dislocation",
                scenario=scenario,
                hold_minutes=hold,
                costs=costs,
                entry_delay_bars=entry_delay_bars,
                context=context,
                is_primary=is_primary,
            )
            summaries.append(summary)
            if not trades.empty:
                all_trades.append(trades)
            if not daily.empty:
                daily = daily.copy()
                daily.insert(0, "scenario", scenario)
                daily.insert(0, "strategy", "F1_relative_dislocation")
                all_daily.append(daily)

    pd.DataFrame(summaries).to_csv(dirs["f1"] / "f1_summary.csv", index=False)
    (pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()).to_csv(
        dirs["f1"] / "f1_trades.csv", index=False
    )
    (pd.concat(all_daily, ignore_index=True) if all_daily else pd.DataFrame()).to_csv(
        dirs["f1"] / "f1_daily.csv", index=False
    )
    return pd.DataFrame(summaries)


def run_f2(x: pd.DataFrame, dirs, costs: CostConfig, entry_delay_bars: int):
    sig = make_f2_signal(x)
    summaries = []
    all_trades = []
    all_daily = []

    for hold in F2_HOLD_MINUTES:
        scenario = f"median_compression__up_vs_prev_close__hold{hold}m"
        prev = x.groupby("segment_id")
        context = {
            "compression_fut_volume": prev["fut_volume"].shift(1),
            "compression_volume_median": prev["fut_volume_q50"].shift(1),
            "compression_abs_body_bp": prev["fut_body_abs_bp"].shift(1),
            "compression_abs_body_median_bp": prev["fut_body_abs_bp_q50"].shift(1),
            "compression_close": prev["fut_close"].shift(1),
            "resolution_close": x["fut_close"],
            "resolution_return_bp": np.log(x["fut_close"] / prev["fut_close"].shift(1)) * 1e4,
        }
        trades, daily, summary = backtest_signal(
            x=x,
            signal=sig,
            strategy="F2_median_compression_up_resolution",
            scenario=scenario,
            hold_minutes=hold,
            costs=costs,
            entry_delay_bars=entry_delay_bars,
            context=context,
            is_primary=True,
        )
        summaries.append(summary)
        if not trades.empty:
            all_trades.append(trades)
        if not daily.empty:
            daily = daily.copy()
            daily.insert(0, "scenario", scenario)
            daily.insert(0, "strategy", "F2_median_compression_up_resolution")
            all_daily.append(daily)

    pd.DataFrame(summaries).to_csv(dirs["f2"] / "f2_summary.csv", index=False)
    (pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()).to_csv(
        dirs["f2"] / "f2_trades.csv", index=False
    )
    (pd.concat(all_daily, ignore_index=True) if all_daily else pd.DataFrame()).to_csv(
        dirs["f2"] / "f2_daily.csv", index=False
    )
    return pd.DataFrame(summaries)


# ============================================================
# 9. Audit
# ============================================================


def write_causality_audit(x: pd.DataFrame, dirs, entry_delay_bars: int):
    checks = {
        "rows": int(len(x)),
        "all_full_bar_features_available_at_bar_end": bool(
            (x["available_at"] == x["bar_start"] + pd.Timedelta(minutes=1)).all()
        ),
        "entry_rule": f"signal known at completed bar end; ETF entry {entry_delay_bars} bar(s) later at open",
        "entry_delay_bars": int(entry_delay_bars),
        "exit_rule": "fixed holding horizon; ETF exit at future bar open",
        "no_intrabar_high_low_fill_assumption": True,
        "no_bbo_queue_model": True,
        "oos_rows_used": False,
    }
    (dirs["diag"] / "causality_audit.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    cols = [
        "bar_start", "bar_end", "available_at", "trade_date", "segment_id", "tod_bucket",
        "fut_open", "fut_high", "fut_low", "fut_close", "fut_volume",
        "etf_open", "etf_high", "etf_low", "etf_close", "etf_volume",
        "fut_volume_q50", "fut_body_abs_bp", "fut_body_abs_bp_q50",
    ]
    for w in F1_LOOKBACKS:
        cols += [f"fut_ret_{w}m_bp", f"etf_ret_{w}m_bp", f"f1_gap_{w}m_bp", f"f1_gap_{w}m_bp_q90"]
    cols = [c for c in cols if c in x.columns]
    x[cols].head(300).to_csv(dirs["diag"] / "feature_timeline_sample.csv", index=False)


# ============================================================
# 10. Main
# ============================================================


def main():
    args = parse_args()
    args.input = resolve_input(args.input)
    args.out = resolve_input(args.out)
    dirs = mkdirs(args.out)

    costs = CostConfig(
        starting_cash=float(args.starting_cash),
        order_notional=float(args.order_notional),
        fee_bps=float(args.fee_bps),
        slippage_bps=float(args.slippage_bps),
    )

    panel = load_aligned(args.input)
    is_panel, manifest = split_is(panel)
    x = add_features(is_panel)
    x, threshold_audit = add_rolling_thresholds(x, history_days=int(args.history_days))

    threshold_audit.to_csv(dirs["diag"] / "rolling_threshold_audit.csv", index=False)
    write_causality_audit(x, dirs, int(args.entry_delay_bars))

    f1_summary = run_f1(x, dirs, costs, int(args.entry_delay_bars))
    f2_summary = run_f2(x, dirs, costs, int(args.entry_delay_bars))

    combined = pd.concat([f1_summary, f2_summary], ignore_index=True)
    combined.to_csv(dirs["root"] / "backtest_summary.csv", index=False)

    manifest.update({
        "input": str(args.input),
        "input_timestamp_semantics": "bar_start",
        "full_bar_feature_available_at": "bar_start + 1 minute",
        "oos_metrics_computed": False,
        "normalization": f"rolling prior {args.history_days} IS trading days, same {TIME_BUCKET_MIN}m time bucket",
        "min_history_obs_per_bucket": MIN_HISTORY_OBS_PER_BUCKET,
        "f1_gap_quantile": F1_GAP_Q,
        "f1_lookbacks_min": F1_LOOKBACKS,
        "f1_holds_min": F1_HOLD_MINUTES,
        "f1_state_filters": F1_STATE_FILTERS,
        "entry_delay_bars": int(args.entry_delay_bars),
        "f1_entry": f"signal bar completes -> ETF open {args.entry_delay_bars} bar(s) later",
        "f1_exit": "fixed open-to-open holding horizon",
        "f2_entry_condition": (
            "previous FUT bar volume > same-bucket rolling median AND abs(C-O) <= same-bucket rolling median; "
            f"current FUT close > previous FUT close; then ETF open {args.entry_delay_bars} bar(s) later"
        ),
        "f2_holds_min": F2_HOLD_MINUTES,
        "f2_exit": "fixed open-to-open holding horizon",
        "starting_cash": costs.starting_cash,
        "order_notional": costs.order_notional,
        "fee_bps_per_side": costs.fee_bps,
        "slippage_bps_per_side": costs.slippage_bps,
        "positioning": "long/flat, fixed notional, one position at a time per scenario, no pyramiding",
        "execution_limitations": [
            "minute OHLCV only; no historical bid/ask spread",
            "no queue/partial-fill model",
            "ETF open is used as boundary execution reference plus adverse slippage",
        ],
        "f3_used_as_gate": False,
    })

    (dirs["root"] / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("=" * 92)
    print("F1/F2 MINUTE BACKTEST COMPLETE — IS ONLY; OOS LOCKED")
    print("=" * 92)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"\nSummary: {(dirs['root'] / 'backtest_summary.csv').resolve()}")
    print("OOS data were not scored, traded, or summarized.")


if __name__ == "__main__":
    main()
