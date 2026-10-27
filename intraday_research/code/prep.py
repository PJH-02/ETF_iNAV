from pathlib import Path

import numpy as np
import pandas as pd


# ============================================================
# Configuration
# ============================================================

from research_paths import resolve_input

ETF_FILE = resolve_input("market_1m_data/KODEX200_069500_1m_toss.csv")
FUT_FILE = resolve_input("market_1m_data/KOSPI200_FUT_FRONT_1m_kis.csv")
OUT_FILE = resolve_input("market_1m_data/KOSPI200_FUT_ETF_1m_aligned.csv")

# 연구 시간.
# 15:19 bar는 15:19:00 ~ 15:20:00 구간.
SESSION_START = "09:00"
SESSION_END = "15:19"

# 현재 파일 구조에서 관측된 timestamp convention.
ETF_TIMESTAMP_SHIFT_MIN = -1   # TOSS: bar-end -> bar-start
FUT_TIMESTAMP_SHIFT_MIN = 0    # KIS: already bar-start


# ============================================================
# Helpers
# ============================================================

def validate_ohlcv(df: pd.DataFrame, prefix: str) -> None:
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)

    if missing:
        raise ValueError(f"{prefix}: missing columns: {sorted(missing)}")

    if df[list(required)].isna().any().any():
        raise ValueError(f"{prefix}: OHLCV contains NaN")

    bad = (
        (df["high"] < df[["open", "close", "low"]].max(axis=1))
        | (df["low"] > df[["open", "close", "high"]].min(axis=1))
        | (df["volume"] < 0)
    )

    if bad.any():
        raise ValueError(
            f"{prefix}: invalid OHLCV rows = {int(bad.sum())}"
        )


def load_etf(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)

    validate_ohlcv(df, "ETF")

    df["timestamp_raw"] = pd.to_datetime(df["timestamp"])

    # TOSS timestamp is empirically bar-end in the attached file.
    df["bar_start"] = (
        df["timestamp_raw"]
        + pd.Timedelta(minutes=ETF_TIMESTAMP_SHIFT_MIN)
    )

    df["bar_end"] = df["bar_start"] + pd.Timedelta(minutes=1)

    df["trade_date"] = df["bar_start"].dt.date

    return df


def load_future(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)

    validate_ohlcv(df, "FUT")

    df["timestamp_raw"] = pd.to_datetime(df["timestamp"])

    df["bar_start"] = (
        df["timestamp_raw"]
        + pd.Timedelta(minutes=FUT_TIMESTAMP_SHIFT_MIN)
    )

    df["bar_end"] = df["bar_start"] + pd.Timedelta(minutes=1)

    df["trade_date"] = df["bar_start"].dt.date

    return df


def filter_session(df: pd.DataFrame) -> pd.DataFrame:
    hhmm = df["bar_start"].dt.strftime("%H:%M")

    mask = (
        (hhmm >= SESSION_START)
        & (hhmm <= SESSION_END)
    )

    return df.loc[mask].copy()


def check_unique(df: pd.DataFrame, name: str) -> None:
    dup = df["bar_start"].duplicated(keep=False)

    if dup.any():
        sample = df.loc[dup, ["bar_start"]].head(10)
        raise ValueError(
            f"{name}: duplicate normalized bar_start found\n{sample}"
        )


# ============================================================
# Timestamp convention audit
# ============================================================

def shift_correlation_audit(
    etf: pd.DataFrame,
    fut: pd.DataFrame,
    shifts=(-2, -1, 0, 1, 2),
) -> pd.DataFrame:

    e = etf[["timestamp_raw", "trade_date", "close"]].copy()
    f = fut[
        ["timestamp_raw", "trade_date", "close", "contract_code"]
    ].copy()

    # Returns must never cross a trading day.
    e["ret"] = (
        e.groupby("trade_date")["close"]
        .transform(lambda x: np.log(x).diff())
    )

    # Also block futures return across a contract boundary.
    f["ret"] = (
        f.groupby(["trade_date", "contract_code"])["close"]
        .transform(lambda x: np.log(x).diff())
    )

    rows = []

    for shift in shifts:
        ee = e.copy()

        ee["join_ts"] = (
            ee["timestamp_raw"]
            + pd.Timedelta(minutes=shift)
        )

        merged = ee.merge(
            f,
            left_on="join_ts",
            right_on="timestamp_raw",
            suffixes=("_etf", "_fut"),
            how="inner",
        )

        merged = merged[
            merged["ret_etf"].notna()
            & merged["ret_fut"].notna()
        ]

        # Extreme bad records should not determine timestamp convention.
        merged = merged[
            (merged["ret_etf"].abs() < 0.02)
            & (merged["ret_fut"].abs() < 0.02)
        ]

        corr = merged["ret_etf"].corr(merged["ret_fut"])

        rows.append(
            {
                "etf_shift_minutes": shift,
                "n": len(merged),
                "return_corr": corr,
            }
        )

    return pd.DataFrame(rows)


# ============================================================
# Build aligned panel
# ============================================================

etf = load_etf(ETF_FILE)
fut = load_future(FUT_FILE)

print("\nTimestamp shift audit")
print(
    shift_correlation_audit(etf, fut)
    .to_string(index=False)
)

etf = filter_session(etf)
fut = filter_session(fut)

check_unique(etf, "ETF")
check_unique(fut, "FUT")


etf = etf[
    [
        "bar_start",
        "bar_end",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]
].rename(
    columns={
        "open": "etf_open",
        "high": "etf_high",
        "low": "etf_low",
        "close": "etf_close",
        "volume": "etf_volume",
    }
)


fut_cols = [
    "bar_start",
    "bar_end",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "contract_code",
    "contract_expiry",
    "contract_changed",
]

fut = fut[fut_cols].rename(
    columns={
        "open": "fut_open",
        "high": "fut_high",
        "low": "fut_low",
        "close": "fut_close",
        "volume": "fut_volume",
    }
)


panel = fut.merge(
    etf,
    on=["bar_start", "bar_end", "trade_date"],
    how="inner",
    validate="one_to_one",
)

panel = panel.sort_values("bar_start").reset_index(drop=True)


# ============================================================
# Prevent overnight / roll contamination
# ============================================================

# New day
new_day = panel["trade_date"].ne(
    panel["trade_date"].shift()
)

# New futures contract
new_contract = panel["contract_code"].ne(
    panel["contract_code"].shift()
)

panel["segment_start"] = new_day | new_contract


# 1-minute log returns.
panel["fut_ret_1m"] = np.log(
    panel["fut_close"] / panel["fut_close"].shift()
)

panel["etf_ret_1m"] = np.log(
    panel["etf_close"] / panel["etf_close"].shift()
)

# Never use overnight or roll-gap return as signal.
panel.loc[
    panel["segment_start"],
    ["fut_ret_1m", "etf_ret_1m"],
] = np.nan


# ============================================================
# Basic alignment diagnostic
# ============================================================

valid = panel[
    panel["fut_ret_1m"].notna()
    & panel["etf_ret_1m"].notna()
].copy()

overall_corr = valid["fut_ret_1m"].corr(
    valid["etf_ret_1m"]
)

daily_corr = (
    valid.groupby("trade_date")
    .apply(
        lambda x: x["fut_ret_1m"].corr(x["etf_ret_1m"]),
        include_groups=False,
    )
    .dropna()
)

print("\nAligned panel")
print(f"rows               : {len(panel):,}")
print(f"days               : {panel['trade_date'].nunique():,}")
print(f"first bar          : {panel['bar_start'].min()}")
print(f"last bar           : {panel['bar_start'].max()}")
print(f"overall 1m corr    : {overall_corr:.6f}")
print(f"median daily corr  : {daily_corr.median():.6f}")

if overall_corr < 0.8:
    raise RuntimeError(
        "Aligned FUT/ETF return correlation is unexpectedly low. "
        "Do not continue strategy research before checking timestamps."
    )


# ============================================================
# Save
# ============================================================

panel.to_csv(
    OUT_FILE,
    index=False,
    encoding="utf-8-sig",
)

print(f"\nSaved: {OUT_FILE.resolve()}")