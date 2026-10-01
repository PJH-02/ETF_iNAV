#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
bt_calmar_rolling.py

변경사항
--------
1) 절대 수익률 threshold(+0.5/+1/+1.5/+2%, max10m > 0.5/1.0% 등) 전부 제거.
2) 모든 price-strength 신호를 rolling relative-strength 방식으로 통일.
3) lookback = 20거래일, 63거래일 모두 테스트.
4) timeframe:
   - 10분: 14:30-14:40, 14:40-14:50, 14:50-15:00,
            15:00-15:10, 15:10-15:20, 15:20-15:30
   - 30분: 14:30-15:00, 15:00-15:30
   - 60분: 14:30-15:30
5) 각 timeframe return의 rolling percentile이 상위 1/3(Q >= 2/3)이면 bullish signal.
6) 기존 sign/path 가설(E/G 계열)은 절대 threshold가 아니므로 별도 path signal로 유지.
7) KODEX200 Buy & Hold는 minute 데이터 공통기간만 계산.
8) 핵심 성과지표:
      Calmar Ratio = CAGR / abs(MDD)
   최종 출력 및 strategy_summary.csv는 Calmar 내림차순.
9) signal-only와 기존 BASE_score AND signal 결합전략 모두 백테스트.
10) 모든 전략의 일별 수익률 및 실제 진입일 거래 로그 저장.

주의
----
- 14:30~15:00 / 15:00~15:30 CSV는 alternatives가 아니라 이어붙일 partitions.
- 15:30까지의 정보를 사용해 같은 날 공식 종가 체결을 가정하므로 연구용 upper bound.
"""

from __future__ import annotations

import math
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ============================================================
# CONFIG
# ============================================================

DATA_DIR = Path("data")
OUT_DIR = Path("analysis_output_closebet")
OUT_DIR.mkdir(parents=True, exist_ok=True)

SCORE_THRESHOLD = 2
TRADING_FEE = 0.0003

KODEX200_CANDIDATES = [
    DATA_DIR / "KODEX200_069500_daily_20100101_20260928.csv",
    DATA_DIR / "069500_daily.csv",
]

MINUTE_PARTS = {
    "304940": [
        DATA_DIR / "304940_1430_1500_1m.csv",
        DATA_DIR / "304940_1500_1530_1m.csv",
    ],
    "219480": [
        DATA_DIR / "219480_1430_1500_1m.csv",
        DATA_DIR / "219480_1500_1530_1m.csv",
    ],
}

PAIR_NAME = {
    "304940": "NQ",
    "219480": "SP",
}

# 핵심 변경: 20일 + 63일
ROLL_WINDOWS = [20, 63]

# rolling percentile 상위 1/3
HIGH_CUTOFF = 2 / 3

MIN_TRADES_WARN = 20
TOP_N_PRINT = 30
TOP_N_PLOT = 10

# rolling으로 비교할 모든 timeframe
ROLLING_INTERVALS = {
    # 10m
    "10m_1430_1440": ("14:30", "14:40"),
    "10m_1440_1450": ("14:40", "14:50"),
    "10m_1450_1500": ("14:50", "15:00"),
    "10m_1500_1510": ("15:00", "15:10"),
    "10m_1510_1520": ("15:10", "15:20"),
    "10m_1520_1530": ("15:20", "15:30"),

    # 30m
    "30m_1430_1500": ("14:30", "15:00"),
    "30m_1500_1530": ("15:00", "15:30"),

    # 60m
    "60m_1430_1530": ("14:30", "15:30"),
}


# ============================================================
# I/O
# ============================================================

def find_first_existing(candidates: list[Path]) -> Path:
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "다음 후보 중 존재하는 파일이 없습니다:\n"
        + "\n".join(map(str, candidates))
    )


def normalize_daily_index(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    unnamed = [
        c for c in out.columns
        if str(c).lower().startswith("unnamed")
    ]

    if "date" in out.columns:
        out["date"] = pd.to_datetime(
            out["date"], errors="coerce"
        ).dt.tz_localize(None)
        out = out.set_index("date")

    elif unnamed:
        c = unnamed[0]
        out[c] = pd.to_datetime(
            out[c], errors="coerce"
        ).dt.tz_localize(None)
        out = out.set_index(c)
        out.index.name = "date"

    else:
        first = out.columns[0]
        parsed = pd.to_datetime(out[first], errors="coerce")

        if parsed.notna().mean() > 0.9:
            out[first] = parsed.dt.tz_localize(None)
            out = out.set_index(first)
            out.index.name = "date"
        else:
            raise ValueError("daily CSV에서 날짜 컬럼을 식별할 수 없습니다.")

    out = out[~out.index.isna()].sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


def load_kodex200() -> tuple[pd.DataFrame, Path]:
    """
    9/28 행을 미리 제거하지 않는다.
    예: 9/23 close -> 9/28 open overnight return 계산에 9/28 open 필요.
    """
    path = find_first_existing(KODEX200_CANDIDATES)
    df = normalize_daily_index(pd.read_csv(path))

    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path}: 필수 컬럼 누락 {sorted(missing)}")

    for c in ["open", "high", "low", "close", "volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    return df, path


def load_minute(symbol: str) -> tuple[pd.DataFrame, list[Path]]:
    expected = MINUTE_PARTS[symbol]
    paths = [p for p in expected if p.exists()]

    if len(paths) != len(expected):
        missing = [p for p in expected if not p.exists()]
        raise FileNotFoundError(
            f"{symbol}: 필요한 minute partition이 누락됐습니다:\n"
            + "\n".join(map(str, missing))
        )

    frames = []

    for path in paths:
        df = pd.read_csv(path)

        required = {"timestamp", "open", "close"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"{path}: 필수 컬럼 누락 {sorted(missing)}")

        ts = pd.to_datetime(df["timestamp"], errors="coerce")
        try:
            ts = ts.dt.tz_localize(None)
        except (TypeError, AttributeError):
            pass

        df["timestamp"] = ts
        df = df.dropna(subset=["timestamp"]).copy()

        for c in ["open", "high", "low", "close", "volume"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")

        frames.append(df)

    merged = pd.concat(frames, ignore_index=True)

    merged = (
        merged
        .sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"], keep="last")
        .reset_index(drop=True)
    )

    merged["date"] = merged["timestamp"].dt.normalize()

    counts = merged.groupby("date").size()
    print(
        f"  {symbol} minute merge: "
        f"days={len(counts)}, "
        f"min={int(counts.min()) if len(counts) else 0}, "
        f"median={counts.median() if len(counts) else np.nan:.1f}, "
        f"max={int(counts.max()) if len(counts) else 0}, "
        f"61-bar days={int((counts == 61).sum())}"
    )

    return merged, paths


# ============================================================
# BASE SCORE
# ============================================================

def prepare_base_score(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    out["mid"] = (out["high"] + out["low"]) / 2
    out["Prev_Close"] = out["close"].shift(1)
    out["Prev_Mid"] = out["mid"].shift(1)
    out["MA2"] = out["close"].rolling(2).mean()
    out["MA3"] = out["close"].rolling(3).mean()

    score_1 = (out["close"] > out["open"]).astype(int)
    score_3 = (out["close"] > out["Prev_Close"]).astype(int)
    score_4 = (out["close"] > out["Prev_Mid"]).astype(int)
    score_6 = (out["close"] > out["MA3"]).astype(int)

    out["Total_Score"] = score_1 + score_3 + score_4 + score_6
    out["Base_Signal"] = out["Total_Score"] >= SCORE_THRESHOLD

    out["Next_Open"] = out["open"].shift(-1)
    out["Overnight_Return"] = out["Next_Open"] / out["close"] - 1
    out["Net_Return"] = out["Overnight_Return"] - TRADING_FEE

    return out


# ============================================================
# INTRADAY FEATURES
# ============================================================

def _price_at_bar(day_df: pd.DataFrame, hhmm: str, field: str) -> float:
    h, m = map(int, hhmm.split(":"))

    rows = day_df[
        (day_df["timestamp"].dt.hour == h)
        & (day_df["timestamp"].dt.minute == m)
    ]

    if rows.empty:
        return np.nan

    return float(rows.iloc[0][field])


def calc_interval_return(
    day_df: pd.DataFrame,
    start: str,
    end: str,
) -> float:
    """
    기존 코드와 동일한 정의 유지:
    log(end bar close / start bar open)
    """
    p0 = _price_at_bar(day_df, start, "open")
    p1 = _price_at_bar(day_df, end, "close")

    if (
        not np.isfinite(p0)
        or not np.isfinite(p1)
        or p0 <= 0
        or p1 <= 0
    ):
        return np.nan

    return math.log(p1 / p0)


def build_intraday_features(minute: pd.DataFrame) -> pd.DataFrame:
    rows = []

    for date, g in minute.groupby("date", sort=True):
        rec = {"date": pd.Timestamp(date)}

        for label, (start, end) in ROLLING_INTERVALS.items():
            rec[f"ret_{label}"] = calc_interval_return(g, start, end)

        rows.append(rec)

    return (
        pd.DataFrame(rows)
        .set_index("date")
        .sort_index()
    )


# ============================================================
# ROLLING SIGNALS
# ============================================================

def rolling_pct_rank_midpoint(
    s: pd.Series,
    window: int,
) -> pd.Series:
    """
    현재값은 reference에서 제외.
    직전 window개 유효 관측치와 비교.
    midpoint tie:
      Q_t = (#past < x + 0.5 * #past == x) / window
    """
    arr = s.to_numpy(dtype=float)
    result = np.full(len(arr), np.nan)

    for i in range(len(arr)):
        if i < window or not np.isfinite(arr[i]):
            continue

        past = arr[i - window:i]

        if np.sum(np.isfinite(past)) != window:
            continue

        less = np.sum(past < arr[i])
        equal = np.sum(past == arr[i])

        result[i] = (less + 0.5 * equal) / window

    return pd.Series(result, index=s.index)


def build_rolling_signals(
    features: pd.DataFrame,
    symbol: str,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, dict]]:
    """
    10m / 30m / 60m × 20d / 63d
    각각 rolling percentile >= 2/3이면 bullish High signal.
    """
    sig = pd.DataFrame(index=features.index)
    diag = pd.DataFrame(index=features.index)
    meta: dict[str, dict] = {}

    prefix = PAIR_NAME[symbol]

    for lookback in ROLL_WINDOWS:
        for label in ROLLING_INTERVALS:
            ret_col = f"ret_{label}"

            q = rolling_pct_rank_midpoint(
                features[ret_col],
                lookback,
            )

            name = f"{prefix}_ROLL{lookback}_{label}_High"

            sig[name] = q >= HIGH_CUTOFF
            diag[f"{name}_q"] = q

            timeframe = label.split("_", 1)[0]
            interval = label.split("_", 1)[1]

            meta[name] = {
                "signal_type": "rolling_high",
                "lookback": lookback,
                "timeframe": timeframe,
                "interval": interval,
            }

    # ========================================================
    # Path signals: fixed return threshold가 아니므로 유지
    # ========================================================
    front30 = features["ret_30m_1430_1500"]
    back30 = features["ret_30m_1500_1530"]
    r60 = features["ret_60m_1430_1530"]

    ten_cols = [
        "ret_10m_1430_1440",
        "ret_10m_1440_1450",
        "ret_10m_1450_1500",
        "ret_10m_1500_1510",
        "ret_10m_1510_1520",
        "ret_10m_1520_1530",
    ]

    ten = features[ten_cols]
    pos_count = (ten > 0).sum(axis=1)

    path_defs = {
        f"{prefix}_PATH_up_up":
            (front30 > 0) & (back30 > 0),

        f"{prefix}_PATH_down_up":
            (front30 < 0) & (back30 > 0),

        f"{prefix}_PATH_persistent_rise":
            pos_count >= 5,

        f"{prefix}_PATH_late_acceleration":
            (back30 > front30) & (r60 > 0),

        f"{prefix}_PATH_late_reversal_up":
            (front30 < 0) & (back30 > 0),
    }

    for name, s in path_defs.items():
        sig[name] = s
        meta[name] = {
            "signal_type": "path",
            "lookback": np.nan,
            "timeframe": "path",
            "interval": name.replace(f"{prefix}_PATH_", ""),
        }

    for c in sig.columns:
        sig[c] = sig[c].fillna(False).astype(bool)

    return sig, diag, meta


# ============================================================
# PERFORMANCE METRICS
# ============================================================

def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return np.nan

    dd = equity / equity.cummax() - 1
    return float(dd.min())


def cagr_from_daily(returns: pd.Series) -> float:
    if returns.empty:
        return np.nan

    gross = float((1 + returns).prod())

    if gross <= 0:
        return -1.0

    return gross ** (252 / len(returns)) - 1


def calmar_ratio(cagr: float, mdd: float) -> float:
    if pd.isna(cagr) or pd.isna(mdd):
        return np.nan

    denom = abs(mdd)

    if denom == 0:
        if cagr > 0:
            return np.inf
        if cagr < 0:
            return -np.inf
        return np.nan

    return cagr / denom


def sharpe(returns: pd.Series) -> float:
    if len(returns) < 2:
        return np.nan

    sd = returns.std(ddof=1)

    if not np.isfinite(sd) or sd == 0:
        return np.nan

    return float(
        returns.mean() / sd * np.sqrt(252)
    )


def evaluate_strategy(
    name: str,
    net_ret: pd.Series,
    signal: pd.Series,
    family: str,
    proxy: str,
    base_signal: pd.Series | None = None,
    metadata: dict | None = None,
):
    idx = net_ret.index.intersection(signal.index)

    r = net_ret.loc[idx].astype(float)
    s = signal.loc[idx].fillna(False).astype(bool)

    valid = r.notna()
    r = r[valid]
    s = s[valid]

    strat = pd.Series(
        np.where(s, r, 0.0),
        index=r.index,
        name=name,
    )

    eq = (1 + strat).cumprod()
    trades = r[s]

    cagr = cagr_from_daily(strat)
    mdd = max_drawdown(eq)
    calmar = calmar_ratio(cagr, mdd)

    row = {
        "strategy": name,
        "family": family,
        "proxy": proxy,

        "signal_type": np.nan,
        "lookback": np.nan,
        "timeframe": np.nan,
        "interval": np.nan,

        "start": r.index.min().date().isoformat() if len(r) else None,
        "end": r.index.max().date().isoformat() if len(r) else None,

        "eval_days": len(r),
        "trades": int(s.sum()),
        "exposure": float(s.mean()) if len(s) else np.nan,

        "win_rate":
            float((trades > 0).mean())
            if len(trades)
            else np.nan,

        "avg_trade_return":
            float(trades.mean())
            if len(trades)
            else np.nan,

        "median_trade_return":
            float(trades.median())
            if len(trades)
            else np.nan,

        "total_return":
            float(eq.iloc[-1] - 1)
            if len(eq)
            else np.nan,

        "CAGR": cagr,
        "MDD": mdd,
        "Calmar": calmar,
        "Sharpe": sharpe(strat),

        "small_n_warning":
            int(s.sum()) < MIN_TRADES_WARN,
    }

    if metadata:
        for k in ["signal_type", "lookback", "timeframe", "interval"]:
            if k in metadata:
                row[k] = metadata[k]

    if base_signal is not None:
        bs = (
            base_signal
            .reindex(r.index)
            .fillna(False)
            .astype(bool)
        )

        row["fraction_of_base_trades"] = (
            float((s & bs).sum() / bs.sum())
            if bs.sum()
            else np.nan
        )

    return row, eq, strat


def evaluate_buy_hold(
    name: str,
    close: pd.Series,
):
    """
    minute 데이터 공통 평가기간만 Buy & Hold.
    raw close 기반 price return.
    """
    close = (
        close
        .dropna()
        .astype(float)
        .sort_index()
    )

    if len(close) < 2:
        raise ValueError(
            "Buy & Hold 계산에 충분한 종가 데이터가 없습니다."
        )

    equity = close / close.iloc[0]
    daily_ret = close.pct_change().fillna(0.0)

    n_periods = len(close) - 1

    cagr = (
        float(equity.iloc[-1])
        ** (252 / n_periods)
        - 1
    )

    mdd = max_drawdown(equity)

    row = {
        "strategy": name,
        "family": "buy_hold",
        "proxy": "KR",

        "signal_type": "benchmark",
        "lookback": np.nan,
        "timeframe": np.nan,
        "interval": np.nan,

        "start": close.index.min().date().isoformat(),
        "end": close.index.max().date().isoformat(),

        "eval_days": len(close),
        "trades": 1,
        "exposure": 1.0,

        "win_rate": np.nan,
        "avg_trade_return": np.nan,
        "median_trade_return": np.nan,

        "total_return": float(equity.iloc[-1] - 1),

        "CAGR": cagr,
        "MDD": mdd,
        "Calmar": calmar_ratio(cagr, mdd),
        "Sharpe": sharpe(daily_ret),

        "small_n_warning": False,
        "fraction_of_base_trades": np.nan,
    }

    equity.name = name
    daily_ret.name = name
    return row, equity, daily_ret


def yearly_stats(name, net_ret, signal):
    idx = net_ret.index.intersection(signal.index)

    x = pd.DataFrame({
        "ret": net_ret.loc[idx],
        "signal": signal.loc[idx].fillna(False).astype(bool),
    }).dropna(subset=["ret"])

    x["strategy_ret"] = np.where(
        x["signal"],
        x["ret"],
        0.0,
    )

    x["year"] = x.index.year

    rows = []

    for year, g in x.groupby("year"):
        t = g.loc[g["signal"], "ret"]
        eq = (1 + g["strategy_ret"]).cumprod()

        rows.append({
            "strategy": name,
            "year": int(year),
            "trades": int(g["signal"].sum()),
            "win_rate":
                float((t > 0).mean())
                if len(t)
                else np.nan,
            "return": float(eq.iloc[-1] - 1),
            "MDD": max_drawdown(eq),
            "avg_trade_return":
                float(t.mean())
                if len(t)
                else np.nan,
        })

    return pd.DataFrame(rows)


def halfyear_stats(name, net_ret, signal):
    idx = net_ret.index.intersection(signal.index)

    x = pd.DataFrame({
        "ret": net_ret.loc[idx],
        "signal": signal.loc[idx].fillna(False).astype(bool),
    }).dropna(subset=["ret"])

    x["strategy_ret"] = np.where(
        x["signal"],
        x["ret"],
        0.0,
    )

    x["half"] = [
        f"{d.year}H{1 if d.month <= 6 else 2}"
        for d in x.index
    ]

    rows = []

    for half, g in x.groupby("half", sort=True):
        t = g.loc[g["signal"], "ret"]
        eq = (1 + g["strategy_ret"]).cumprod()

        rows.append({
            "strategy": name,
            "half": half,
            "trades": int(g["signal"].sum()),
            "win_rate":
                float((t > 0).mean())
                if len(t)
                else np.nan,
            "return": float(eq.iloc[-1] - 1),
            "MDD": max_drawdown(eq),
            "avg_trade_return":
                float(t.mean())
                if len(t)
                else np.nan,
        })

    return pd.DataFrame(rows)


# ============================================================
# REPORT / PLOT
# ============================================================

def df_to_markdown(df: pd.DataFrame) -> str:
    cols = list(df.columns)

    lines = [
        "| " + " | ".join(map(str, cols)) + " |",
        "| " + " | ".join(["---"] * len(cols)) + " |",
    ]

    for _, row in df.iterrows():
        vals = []

        for c in cols:
            v = row[c]

            if isinstance(v, (float, np.floating)):
                vals.append(
                    "NA" if pd.isna(v)
                    else f"{v:.6f}"
                )
            else:
                vals.append(str(v))

        lines.append(
            "| " + " | ".join(vals) + " |"
        )

    return "\n".join(lines)


def write_report(
    summary,
    base_summary,
    buy_hold_summary,
    paths,
):
    eligible = summary[
        (summary["trades"] >= MIN_TRADES_WARN)
        & summary["family"].isin(["signal_only", "combined"])
    ].copy()

    eligible = eligible.sort_values(
        ["Calmar", "CAGR"],
        ascending=[False, False],
        na_position="last",
    )

    rolling_only = eligible[
        eligible["signal_type"] == "rolling_high"
    ].copy()

    base = base_summary.iloc[0]
    bh = buy_hold_summary.iloc[0]

    show = [
        "strategy",
        "family",
        "proxy",
        "lookback",
        "timeframe",
        "interval",
        "trades",
        "win_rate",
        "CAGR",
        "MDD",
        "Calmar",
        "Sharpe",
    ]

    lines = [
        "# Rolling Relative-Strength 종가베팅 백테스트",
        "",

        "## 설정",
        "",
        f"- KODEX200: `{paths['kodex200']}`",
        f"- 304940 partitions: `{paths['304940']}`",
        f"- 219480 partitions: `{paths['219480']}`",
        f"- lookback: {ROLL_WINDOWS}",
        f"- High cutoff: rolling percentile >= {HIGH_CUTOFF:.4f}",
        "- timeframe: 10m / 30m / 60m",
        "- 고정 절대수익률 threshold는 사용하지 않음",
        "- 핵심 성과지표: Calmar = CAGR / |MDD|",
        "",

        "## Benchmark",
        "",
        f"- BUY_HOLD: CAGR {bh['CAGR']*100:.2f}%, "
        f"MDD {bh['MDD']*100:.2f}%, Calmar {bh['Calmar']:.3f}",
        f"- BASE_score: CAGR {base['CAGR']*100:.2f}%, "
        f"MDD {base['MDD']*100:.2f}%, Calmar {base['Calmar']:.3f}",
        "",

        "## Rolling 전략 — Calmar 내림차순",
        "",
        df_to_markdown(rolling_only[show]),
        "",

        "## 전체 전략 — Calmar 내림차순",
        "",
        df_to_markdown(eligible[show]),
        "",

        "## 해석 주의",
        "",
        "- 20일과 63일 결과가 같은 방향인지 우선 확인.",
        "- 10m/30m/60m 중 특정 한 구간만 우연히 좋은지 확인.",
        "- Calmar가 높아도 거래 수가 적거나 특정 반기에 성과가 몰리면 채택하지 않음.",
        "- 15:30 signal은 실제 종가동시호가 주문시점보다 늦을 수 있으므로 후속 execution-clean 검증 필요.",
    ]

    (OUT_DIR / "report.md").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


def plot_equities(equities, summary):
    eligible = summary[
        (summary["trades"] >= MIN_TRADES_WARN)
        & summary["family"].isin(["signal_only", "combined"])
    ].copy()

    eligible = eligible.sort_values(
        ["Calmar", "CAGR"],
        ascending=[False, False],
        na_position="last",
    )

    top = eligible.head(TOP_N_PLOT)["strategy"].tolist()

    plt.figure(figsize=(14, 8))

    for name in ["BUY_HOLD", "BASE_score", "ALWAYS_overnight"] + top:
        if name in equities:
            lw = 2.3 if name in {
                "BUY_HOLD",
                "BASE_score",
                "ALWAYS_overnight",
            } else 1.2

            plt.plot(
                equities[name].index,
                equities[name].values,
                label=name,
                linewidth=lw,
            )

    plt.title("Top Strategies by Calmar Ratio")
    plt.xlabel("Date")
    plt.ylabel("Equity")
    plt.grid(True, alpha=0.3)
    plt.legend(fontsize=7, ncol=2)
    plt.tight_layout()

    plt.savefig(
        OUT_DIR / "top_strategies_by_calmar.png",
        dpi=160,
    )

    plt.close()


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 100)
    print("Rolling Relative-Strength 종가베팅 백테스트")
    print("10m / 30m / 60m × 20d / 63d")
    print("Calmar = CAGR / |MDD|")
    print("=" * 100)

    # --------------------------------------------------------
    # 1. KODEX200
    # --------------------------------------------------------
    print("[1/7] KODEX200 로드 / Base score 계산")

    kodex_raw, kodex_path = load_kodex200()
    kodex_close = kodex_raw["close"].copy()

    kodex = prepare_base_score(kodex_raw)

    # Overnight 전략은 Next_Open이 존재하는 날짜까지만
    kodex = kodex[
        kodex["Net_Return"].notna()
    ].copy()

    # --------------------------------------------------------
    # 2. Minute merge + features + rolling signals
    # --------------------------------------------------------
    print("[2/7] minute merge / 10m·30m·60m feature / rolling signal 생성")

    features = {}
    signals = {}
    diagnostics = {}
    metadata = {}
    minute_paths = {}

    for symbol in ["304940", "219480"]:
        minute, paths = load_minute(symbol)
        minute_paths[symbol] = paths

        f = build_intraday_features(minute)
        s, d, m = build_rolling_signals(f, symbol)

        features[symbol] = f
        signals[symbol] = s
        diagnostics[symbol] = d
        metadata.update(m)

        valid60 = int(
            f["ret_60m_1430_1530"].notna().sum()
        )

        print(
            f"  {symbol}: "
            f"{f.index.min().date()} ~ {f.index.max().date()}, "
            f"days={len(f)}, 60m-valid={valid60}"
        )

        if valid60 == 0:
            warnings.warn(
                f"{symbol}: 14:30~15:30 60m return 계산 불가. "
                "minute partition 결합 상태를 확인하세요."
            )

    # --------------------------------------------------------
    # 3. Common window
    # --------------------------------------------------------
    print("[3/7] 공통 평가기간 정렬")

    common_start = max(
        features["304940"].index.min(),
        features["219480"].index.min(),
        kodex.index.min(),
    )

    common_end = min(
        features["304940"].index.max(),
        features["219480"].index.max(),
        kodex.index.max(),
    )

    test = kodex.loc[
        (kodex.index >= common_start)
        & (kodex.index <= common_end)
    ].copy()

    common_close = kodex_close.loc[
        (kodex_close.index >= common_start)
        & (kodex_close.index <= common_end)
    ].copy()

    for symbol in ["304940", "219480"]:
        test = test.join(
            features[symbol].add_prefix(
                f"{PAIR_NAME[symbol]}_"
            ),
            how="left",
        )

        test = test.join(
            signals[symbol],
            how="left",
        )

    # --------------------------------------------------------
    # 4. Evaluate
    # --------------------------------------------------------
    print("[4/7] Benchmark / signal-only / combined 평가")

    summaries = []
    equities = {}
    daily_returns = {}
    yearly_list = []
    half_list = []

    # Buy & Hold
    bh_row, bh_eq, bh_daily = evaluate_buy_hold(
        "BUY_HOLD",
        common_close,
    )
    summaries.append(bh_row)
    equities["BUY_HOLD"] = bh_eq
    daily_returns["BUY_HOLD"] = bh_daily

    # Always overnight
    always = pd.Series(True, index=test.index)

    row, eq, dr = evaluate_strategy(
        "ALWAYS_overnight",
        test["Net_Return"],
        always,
        "baseline",
        "KR",
        metadata={
            "signal_type": "benchmark",
            "lookback": np.nan,
            "timeframe": np.nan,
            "interval": np.nan,
        },
    )
    summaries.append(row)
    equities["ALWAYS_overnight"] = eq
    daily_returns["ALWAYS_overnight"] = dr

    # Base score
    row, eq, dr = evaluate_strategy(
        "BASE_score",
        test["Net_Return"],
        test["Base_Signal"],
        "baseline",
        "KR",
        test["Base_Signal"],
        metadata={
            "signal_type": "benchmark",
            "lookback": np.nan,
            "timeframe": np.nan,
            "interval": np.nan,
        },
    )
    summaries.append(row)
    equities["BASE_score"] = eq
    daily_returns["BASE_score"] = dr

    yearly_list.append(
        yearly_stats(
            "BASE_score",
            test["Net_Return"],
            test["Base_Signal"],
        )
    )
    half_list.append(
        halfyear_stats(
            "BASE_score",
            test["Net_Return"],
            test["Base_Signal"],
        )
    )

    # Experimental signals
    for symbol in ["304940", "219480"]:
        proxy = PAIR_NAME[symbol]

        sdf = (
            signals[symbol]
            .reindex(test.index)
            .fillna(False)
        )

        for col in sdf.columns:
            sig = sdf[col].astype(bool)
            meta = metadata.get(col, {})

            # signal-only
            name = f"SIG_{col}"

            row, eq, dr = evaluate_strategy(
                name,
                test["Net_Return"],
                sig,
                "signal_only",
                proxy,
                test["Base_Signal"],
                metadata=meta,
            )

            summaries.append(row)
            equities[name] = eq
            daily_returns[name] = dr

            yearly_list.append(
                yearly_stats(
                    name,
                    test["Net_Return"],
                    sig,
                )
            )
            half_list.append(
                halfyear_stats(
                    name,
                    test["Net_Return"],
                    sig,
                )
            )

            # Base AND signal
            comb = (
                test["Base_Signal"]
                .fillna(False)
                .astype(bool)
                & sig
            )

            name2 = f"COMB_BASE__{col}"

            row, eq, dr = evaluate_strategy(
                name2,
                test["Net_Return"],
                comb,
                "combined",
                proxy,
                test["Base_Signal"],
                metadata=meta,
            )

            summaries.append(row)
            equities[name2] = eq
            daily_returns[name2] = dr

            yearly_list.append(
                yearly_stats(
                    name2,
                    test["Net_Return"],
                    comb,
                )
            )
            half_list.append(
                halfyear_stats(
                    name2,
                    test["Net_Return"],
                    comb,
                )
            )

    summary = pd.DataFrame(summaries)

    yearly = (
        pd.concat(yearly_list, ignore_index=True)
        if yearly_list
        else pd.DataFrame()
    )

    half = (
        pd.concat(half_list, ignore_index=True)
        if half_list
        else pd.DataFrame()
    )

    # --------------------------------------------------------
    # 5. Diagnostics
    # --------------------------------------------------------
    print("[5/7] Signal frequency / rolling diagnostics 저장")

    freq_rows = []

    for symbol in ["304940", "219480"]:
        for c in signals[symbol].columns:
            s = signals[symbol][c]
            meta = metadata.get(c, {})

            freq_rows.append({
                "proxy": PAIR_NAME[symbol],
                "signal": c,
                "signal_type": meta.get("signal_type"),
                "lookback": meta.get("lookback"),
                "timeframe": meta.get("timeframe"),
                "interval": meta.get("interval"),
                "signal_days": int(s.sum()),
                "available_days": len(s),
                "signal_rate": float(s.mean()),
            })

    signal_freq = pd.DataFrame(freq_rows)

    rolling_diag = []
    for symbol in ["304940", "219480"]:
        d = diagnostics[symbol].copy()
        d["proxy"] = PAIR_NAME[symbol]
        d["date"] = d.index
        rolling_diag.append(d.reset_index(drop=True))

    rolling_diag_df = pd.concat(
        rolling_diag,
        ignore_index=True,
        sort=False,
    )

    # --------------------------------------------------------
    # 6. Save
    # --------------------------------------------------------
    print("[6/7] 결과 저장")

    summary_sorted = summary.sort_values(
        ["Calmar", "CAGR"],
        ascending=[False, False],
        na_position="last",
    )

    summary_sorted.to_csv(
        OUT_DIR / "strategy_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    yearly.to_csv(
        OUT_DIR / "yearly_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    half.to_csv(
        OUT_DIR / "halfyear_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    signal_freq.to_csv(
        OUT_DIR / "signal_frequency.csv",
        index=False,
        encoding="utf-8-sig",
    )

    rolling_diag_df.to_csv(
        OUT_DIR / "rolling_quantile_diagnostics.csv",
        index=False,
        encoding="utf-8-sig",
    )

    test.to_csv(
        OUT_DIR / "aligned_dataset.csv",
        encoding="utf-8-sig",
    )

    # --------------------------------------------------------
    # 일별 전략 수익률 저장
    # --------------------------------------------------------
    # 전략 수익률의 날짜 의미:
    # - BASE / signal-only / combined / ALWAYS:
    #   date=t 행은 KODEX200 close(t) -> next KRX open 수익률.
    #   미진입일은 0.
    # - BUY_HOLD:
    #   date=t 행은 close(t-1) -> close(t) price return.
    daily_returns_df = pd.DataFrame(index=test.index)

    # Overnight 계열 전략
    for name, series in daily_returns.items():
        if name == "BUY_HOLD":
            continue
        daily_returns_df[name] = series.reindex(test.index).fillna(0.0)

    # Buy & Hold는 close-to-close 수익률이므로 별도 컬럼명으로 명확히 표시
    daily_returns_df["BUY_HOLD_close_to_close"] = (
        daily_returns["BUY_HOLD"]
        .reindex(test.index)
        .fillna(0.0)
    )

    # 원자료/해석용 컬럼도 앞쪽에 함께 저장
    daily_returns_df.insert(
        0,
        "KODEX200_overnight_gross",
        test["Overnight_Return"].reindex(daily_returns_df.index),
    )
    daily_returns_df.insert(
        1,
        "KODEX200_overnight_net_if_traded",
        test["Net_Return"].reindex(daily_returns_df.index),
    )
    daily_returns_df.insert(
        2,
        "BASE_signal",
        test["Base_Signal"].reindex(daily_returns_df.index).fillna(False).astype(int),
    )
    daily_returns_df.insert(
        3,
        "BASE_score_value",
        test["Total_Score"].reindex(daily_returns_df.index),
    )

    daily_returns_df.to_csv(
        OUT_DIR / "daily_strategy_returns.csv",
        encoding="utf-8-sig",
        index_label="date",
    )

    # --------------------------------------------------------
    # 거래일 단위 Long-format 로그도 저장
    # signal=True인 실제 진입일만 기록.
    # 이후 PnL concentration / 약세장 분석 / 이벤트 매칭에 사용.
    # --------------------------------------------------------
    trade_log_rows = []

    # benchmark/실험전략 중 overnight 전략만
    overnight_names = [
        n for n in daily_returns.keys()
        if n != "BUY_HOLD"
    ]

    for name in overnight_names:
        sret = daily_returns[name].reindex(test.index).fillna(0.0)

        if name == "ALWAYS_overnight":
            signal_mask = pd.Series(True, index=test.index)
        elif name == "BASE_score":
            signal_mask = test["Base_Signal"].fillna(False).astype(bool)
        elif name.startswith("SIG_"):
            raw_signal_name = name[len("SIG_"):]
            signal_mask = test[raw_signal_name].fillna(False).astype(bool)
        elif name.startswith("COMB_BASE__"):
            raw_signal_name = name[len("COMB_BASE__"):]
            signal_mask = (
                test["Base_Signal"].fillna(False).astype(bool)
                & test[raw_signal_name].fillna(False).astype(bool)
            )
        else:
            signal_mask = sret.ne(0.0)

        active_dates = signal_mask[signal_mask].index

        if len(active_dates) == 0:
            continue

        tmp = pd.DataFrame({
            "date": active_dates,
            "strategy": name,
            "gross_overnight_return": test.loc[active_dates, "Overnight_Return"].values,
            "net_strategy_return": sret.loc[active_dates].values,
            "base_score": test.loc[active_dates, "Total_Score"].values,
            "base_signal": test.loc[active_dates, "Base_Signal"].astype(int).values,
        })

        trade_log_rows.append(tmp)

    if trade_log_rows:
        daily_trade_log = pd.concat(trade_log_rows, ignore_index=True)
        daily_trade_log = daily_trade_log.sort_values(["date", "strategy"])
    else:
        daily_trade_log = pd.DataFrame(columns=[
            "date",
            "strategy",
            "gross_overnight_return",
            "net_strategy_return",
            "base_score",
            "base_signal",
        ])

    daily_trade_log.to_csv(
        OUT_DIR / "daily_trade_log.csv",
        index=False,
        encoding="utf-8-sig",
    )

    plot_equities(
        equities,
        summary,
    )

    base_summary = summary[
        summary["strategy"] == "BASE_score"
    ]

    buy_hold_summary = summary[
        summary["strategy"] == "BUY_HOLD"
    ]

    write_report(
        summary,
        base_summary,
        buy_hold_summary,
        {
            "kodex200": kodex_path,
            "304940": minute_paths["304940"],
            "219480": minute_paths["219480"],
        },
    )

    # --------------------------------------------------------
    # 7. Print
    # --------------------------------------------------------
    print("[7/7] 완료")
    print()

    print(
        f"공통 평가기간: "
        f"{common_start.date()} ~ {common_end.date()}"
    )
    print()

    benchmarks = summary[
        summary["family"].isin(["buy_hold", "baseline"])
    ].copy()

    benchmarks = benchmarks.sort_values(
        ["Calmar", "CAGR"],
        ascending=[False, False],
        na_position="last",
    )

    print("=== BENCHMARKS — CALMAR DESC ===")
    print(
        benchmarks[
            [
                "strategy",
                "trades",
                "CAGR",
                "MDD",
                "Calmar",
                "Sharpe",
            ]
        ].to_string(
            index=False,
            formatters={
                "CAGR": lambda x: f"{x*100:.2f}%",
                "MDD": lambda x: f"{x*100:.2f}%",
                "Calmar": lambda x: f"{x:.3f}",
                "Sharpe": lambda x: f"{x:.3f}",
            },
        )
    )

    print()

    eligible = summary[
        (summary["trades"] >= MIN_TRADES_WARN)
        & summary["family"].isin(["signal_only", "combined"])
    ].copy()

    eligible = eligible.sort_values(
        ["Calmar", "CAGR"],
        ascending=[False, False],
        na_position="last",
    )

    print(
        f"=== TOP {TOP_N_PRINT} STRATEGIES BY CALMAR "
        f"(trades >= {MIN_TRADES_WARN}) ==="
    )

    cols = [
        "strategy",
        "family",
        "proxy",
        "lookback",
        "timeframe",
        "interval",
        "trades",
        "win_rate",
        "CAGR",
        "MDD",
        "Calmar",
        "Sharpe",
    ]

    print(
        eligible[cols]
        .head(TOP_N_PRINT)
        .to_string(
            index=False,
            formatters={
                "win_rate": lambda x: f"{x*100:.1f}%",
                "CAGR": lambda x: f"{x*100:.2f}%",
                "MDD": lambda x: f"{x*100:.2f}%",
                "Calmar": lambda x: f"{x:.3f}",
                "Sharpe": lambda x: f"{x:.3f}",
            },
        )
    )

    print()
    print(
        f"산출물: {OUT_DIR.resolve()}"
    )
    print(
        "일별 분석 파일: daily_strategy_returns.csv, daily_trade_log.csv"
    )
    print(
        "고정 절대수익률 threshold 전략은 제거했습니다. "
        "10m/30m/60m 모두 20d·63d rolling High 기준으로 비교하세요."
    )


if __name__ == "__main__":
    main()
