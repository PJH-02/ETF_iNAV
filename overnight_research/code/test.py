#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
foreign_preus_gap_backtest.py

목적
----
KODEX200 종가 -> 다음 KRX 거래일 시가 gap 전략에서

  1) 전일 KRX 외국인 순매수 > 0
  2) 당일 15:20 KST까지의 QQQ/SPY partial overnight return > 0

을 결합한 종가진입 전략을 백테스트한다.

추가로 "강한 순매수 + 강한 PREUS 상승"을 검증한다.

  - Primary strength filter : 직전 63개 유효 관측치 대비 upper tercile (q >= 2/3)
  - Robustness             : 직전 63개 유효 관측치 대비 top quintile (q >= 0.8)

중요한 용어
-----------
15:20 KST는 미국 정규장 개장 전이므로 아래 변수는 US O->C가 아니다.

  PREUS_t = log(
      QQQ/SPY의 15:20 KST 직전 마지막 관측가격
      / 전일 미국 정규장 close
  )

기존 us_oc_overnight_research.py가 만든 research_dataset.csv의
QQQ_preus_ret_1520 / SPY_preus_ret_1520를 사용한다.

외국인 변수
-----------
의사결정일 t에 사용하는 flow는 반드시 이전 KRX 거래일의 값:

  FOREIGN_PREV_t = FOREIGN_NETBUY_{t-1}

달력 날짜 t-1이 아니라 "이전 실제 KRX session"이다.
research_dataset의 KODEX200/KRX index에 외국인 데이터를 먼저 join한 뒤 shift(1)한다.

데이터 스누핑 방지
------------------
- 임계값을 백테스트 결과에 맞춰 탐색하지 않는다.
- sign, upper-tercile, top-quintile만 사전 정의.
- rolling percentile reference에 현재 관측치는 포함하지 않는다.
- 각 전략은 필요한 feature가 최초로 사용 가능한 시점부터 평가한다.
- CAGR/Calmar benchmark도 각 전략과 동일 기간으로 다시 계산한다.
- sporadic missing feature는 "no trade"로 처리하고 availability를 별도 출력한다.

핵심 성과지표
------------
Calmar = CAGR / abs(MDD)

입력
----
1) 기존 연구 데이터:
   analysis_output_us_oc/research_dataset.csv
   또는 research_dataset.csv

   필수 컬럼:
   - net_gap
   - kr_gap
   - KODEX_close
   - QQQ_preus_ret_1520
   - SPY_preus_ret_1520

2) KRX 외국인 순매수:
   data/KRX 외국인 순매수.xlsx
   또는 KRX 외국인 순매수.xlsx

   실제 첨부파일 스키마:
   - 일자
   - 외국인 합계
   (+ 기관 합계, 기타법인, 개인, 전체)

출력
----
analysis_output_foreign_preus/
  aligned_dataset.csv
  strategy_summary.csv
  sign_state_table.csv
  quantile_state_table.csv
  chronological_split.csv
  halfyear_stability.csv
  time_shift_placebo.csv
  common_benchmarks.csv
  report.md
  top_strategies_by_calmar.png
"""

from __future__ import annotations

import math
import warnings
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ============================================================
# CONFIG
# ============================================================

from research_paths import DATA, RESULTS, resolve_input

DATA_DIR = DATA / "market_1m_data"
OUT_DIR = RESULTS / "analysis_output_foreign_preus"
OUT_DIR.mkdir(parents=True, exist_ok=True)

RESEARCH_DATA_CANDIDATES = [
    RESULTS / "analysis_output_us_oc" / "research_dataset.csv",
    RESULTS / "analysis_output_us_oc/research_dataset.csv",
    DATA_DIR / "research_dataset.csv",
]

FOREIGN_XLSX_CANDIDATES = [
    DATA_DIR / "KRX 외국인 순매수.xlsx",
    resolve_input("KRX 외국인 순매수.xlsx"),
    DATA_DIR / "KRX 외국인 순매수(1).xlsx",
]

FOREIGN_DATE_COL = "일자"
FOREIGN_VALUE_COL = "외국인 합계"

ROLL_WINDOW = 63

# 사전 정의. 결과를 보고 최적 threshold를 고르기 위한 grid가 아니다.
Q_PRIMARY = 2.0 / 3.0
Q_ROBUST = 0.80

TRADING_FEE = 0.0003

PLACEBO_LAGS = [1, 2, 3]
MIN_TRADES_FOR_TOP = 30
TOP_N_PLOT = 10


# ============================================================
# I/O
# ============================================================

def find_first_existing(candidates: list[Path]) -> Path:
    for p in candidates:
        if p.exists():
            return p

    raise FileNotFoundError(
        "다음 후보 중 존재하는 파일이 없습니다:\n"
        + "\n".join(str(p) for p in candidates)
    )


def load_research_dataset() -> tuple[pd.DataFrame, Path]:
    path = find_first_existing(RESEARCH_DATA_CANDIDATES)
    df = pd.read_csv(path)

    # index가 CSV 첫 열로 저장된 경우 포함
    if "date" in df.columns:
        date_col = "date"
    else:
        unnamed = [
            c for c in df.columns
            if str(c).lower().startswith("unnamed")
        ]
        if unnamed:
            date_col = unnamed[0]
        else:
            date_col = df.columns[0]

    parsed = pd.to_datetime(df[date_col], errors="coerce")
    if parsed.notna().mean() < 0.8:
        raise ValueError(
            f"{path}: 날짜 컬럼을 식별할 수 없습니다. 후보={date_col}"
        )

    df[date_col] = parsed.dt.tz_localize(None)
    df = (
        df.dropna(subset=[date_col])
        .set_index(date_col)
        .sort_index()
    )
    df.index.name = "date"
    df = df[~df.index.duplicated(keep="last")]

    required = {
        "net_gap",
        "kr_gap",
        "KODEX_close",
        "QQQ_preus_ret_1520",
        "SPY_preus_ret_1520",
    }

    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"{path}: 필수 컬럼 누락 {sorted(missing)}"
        )

    for c in required:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    return df, path


def load_foreign_flow() -> tuple[pd.DataFrame, Path]:
    path = find_first_existing(FOREIGN_XLSX_CANDIDATES)
    df = pd.read_excel(path)

    required = {
        FOREIGN_DATE_COL,
        FOREIGN_VALUE_COL,
    }
    missing = required - set(df.columns)

    if missing:
        raise ValueError(
            f"{path}: 필수 컬럼 누락 {sorted(missing)}\n"
            f"실제 컬럼={list(df.columns)}"
        )

    date = pd.to_datetime(
        df[FOREIGN_DATE_COL],
        errors="coerce",
    ).dt.tz_localize(None)

    value = pd.to_numeric(
        df[FOREIGN_VALUE_COL]
        .astype(str)
        .str.replace(",", "", regex=False),
        errors="coerce",
    )

    out = pd.DataFrame({
        "date": date,
        "foreign_netbuy": value,
    })

    out = (
        out.dropna(subset=["date", "foreign_netbuy"])
        .drop_duplicates("date", keep="last")
        .set_index("date")
        .sort_index()
    )

    return out, path


# ============================================================
# FEATURES
# ============================================================

def rolling_pct_rank_valid_obs(
    s: pd.Series,
    window: int,
) -> pd.Series:
    """
    현재 관측치를 직전 window개의 '유효 관측치'와 비교한다.

    NaN은 reference observation으로 세지 않는다.
    현재값 자체는 절대 reference에 포함하지 않는다.

    Q_t =
      (#past < current + 0.5 * #past == current) / window
    """
    valid = s.dropna().astype(float)
    arr = valid.to_numpy()
    q = np.full(len(valid), np.nan)

    for i in range(len(valid)):
        if i < window:
            continue

        current = arr[i]
        past = arr[i-window:i]

        less = np.sum(past < current)
        equal = np.sum(past == current)

        q[i] = (
            less
            + 0.5 * equal
        ) / window

    out = pd.Series(
        np.nan,
        index=s.index,
        dtype=float,
    )

    out.loc[valid.index] = q
    return out


def build_master(
    research: pd.DataFrame,
    foreign: pd.DataFrame,
) -> pd.DataFrame:
    """
    KODEX/KRX calendar를 base로 사용한다.

    foreign_prev는 단순 calendar day -1이 아니라
    이전 KRX row의 foreign_current를 shift(1)한다.
    """
    x = research.copy()

    x = x.join(
        foreign.rename(
            columns={
                "foreign_netbuy": "foreign_current"
            }
        ),
        how="left",
    )

    # 실제 KRX session 기준 전일
    x["foreign_prev"] = (
        x["foreign_current"].shift(1)
    )

    # current flow의 rank를 먼저 만든 뒤 1 KRX session shift
    # -> t 시점에는 t-1 flow의 q63만 사용
    x["foreign_current_q63"] = (
        rolling_pct_rank_valid_obs(
            x["foreign_current"],
            ROLL_WINDOW,
        )
    )

    x["foreign_prev_q63"] = (
        x["foreign_current_q63"].shift(1)
    )

    # PREUS strength
    for symbol in ["QQQ", "SPY"]:
        raw = f"{symbol}_preus_ret_1520"
        qcol = f"{symbol}_preus_q63_recalc"

        x[qcol] = (
            rolling_pct_rank_valid_obs(
                x[raw],
                ROLL_WINDOW,
            )
        )

    # convenient sign variables
    x["foreign_prev_pos"] = (
        x["foreign_prev"] > 0
    )

    x["QQQ_preus_pos"] = (
        x["QQQ_preus_ret_1520"] > 0
    )

    x["SPY_preus_pos"] = (
        x["SPY_preus_ret_1520"] > 0
    )

    x["both_preus_pos"] = (
        x["QQQ_preus_pos"]
        & x["SPY_preus_pos"]
    )

    return x


# ============================================================
# SIGNAL SPEC
# ============================================================

class SignalSpec:
    def __init__(
        self,
        name: str,
        family: str,
        required_cols: list[str],
        rule: Callable[[pd.DataFrame], pd.Series],
        description: str,
    ):
        self.name = name
        self.family = family
        self.required_cols = required_cols
        self.rule = rule
        self.description = description


def build_signal_specs() -> list[SignalSpec]:
    specs: list[SignalSpec] = []

    # --------------------------------------------------------
    # Component signals: incremental-value diagnostics
    # --------------------------------------------------------
    specs += [
        SignalSpec(
            "FOREIGN_POS",
            "component",
            ["foreign_prev"],
            lambda x: x["foreign_prev"] > 0,
            "전일 KRX 외국인 순매수 > 0",
        ),

        SignalSpec(
            "QQQ_PREUS_POS",
            "component",
            ["QQQ_preus_ret_1520"],
            lambda x: x["QQQ_preus_ret_1520"] > 0,
            "QQQ 전일 US close -> 15:20 KST partial overnight return > 0",
        ),

        SignalSpec(
            "SPY_PREUS_POS",
            "component",
            ["SPY_preus_ret_1520"],
            lambda x: x["SPY_preus_ret_1520"] > 0,
            "SPY 전일 US close -> 15:20 KST partial overnight return > 0",
        ),

        SignalSpec(
            "BOTH_PREUS_POS",
            "component",
            [
                "QQQ_preus_ret_1520",
                "SPY_preus_ret_1520",
            ],
            lambda x: (
                (x["QQQ_preus_ret_1520"] > 0)
                & (x["SPY_preus_ret_1520"] > 0)
            ),
            "QQQ와 SPY PREUS가 모두 상승",
        ),
    ]

    # --------------------------------------------------------
    # Primary sign conjunctions
    # --------------------------------------------------------
    specs += [
        SignalSpec(
            "FOREIGN_POS__QQQ_POS",
            "sign_core",
            [
                "foreign_prev",
                "QQQ_preus_ret_1520",
            ],
            lambda x: (
                (x["foreign_prev"] > 0)
                & (x["QQQ_preus_ret_1520"] > 0)
            ),
            "전일 외국인 순매수 + QQQ PREUS 상승",
        ),

        SignalSpec(
            "FOREIGN_POS__SPY_POS",
            "sign_core",
            [
                "foreign_prev",
                "SPY_preus_ret_1520",
            ],
            lambda x: (
                (x["foreign_prev"] > 0)
                & (x["SPY_preus_ret_1520"] > 0)
            ),
            "전일 외국인 순매수 + SPY PREUS 상승",
        ),

        SignalSpec(
            "FOREIGN_POS__BOTH_POS",
            "sign_core",
            [
                "foreign_prev",
                "QQQ_preus_ret_1520",
                "SPY_preus_ret_1520",
            ],
            lambda x: (
                (x["foreign_prev"] > 0)
                & (x["QQQ_preus_ret_1520"] > 0)
                & (x["SPY_preus_ret_1520"] > 0)
            ),
            "전일 외국인 순매수 + QQQ/SPY PREUS 모두 상승",
        ),
    ]

    # --------------------------------------------------------
    # Primary strength: upper tercile
    # raw > 0도 같이 요구 -> "상위 quantile"이 음수인 regime 방지
    # --------------------------------------------------------
    q = Q_PRIMARY
    qtag = "Q67"

    specs += [
        SignalSpec(
            f"FOREIGN_{qtag}",
            "quantile_component",
            [
                "foreign_prev",
                "foreign_prev_q63",
            ],
            lambda x, q=q: (
                (x["foreign_prev"] > 0)
                & (x["foreign_prev_q63"] >= q)
            ),
            "전일 외국인 순매수 > 0 이면서 과거 63개 대비 상위 1/3",
        ),

        SignalSpec(
            f"QQQ_PREUS_{qtag}",
            "quantile_component",
            [
                "QQQ_preus_ret_1520",
                "QQQ_preus_q63_recalc",
            ],
            lambda x, q=q: (
                (x["QQQ_preus_ret_1520"] > 0)
                & (x["QQQ_preus_q63_recalc"] >= q)
            ),
            "QQQ PREUS > 0 이면서 과거 63개 대비 상위 1/3",
        ),

        SignalSpec(
            f"SPY_PREUS_{qtag}",
            "quantile_component",
            [
                "SPY_preus_ret_1520",
                "SPY_preus_q63_recalc",
            ],
            lambda x, q=q: (
                (x["SPY_preus_ret_1520"] > 0)
                & (x["SPY_preus_q63_recalc"] >= q)
            ),
            "SPY PREUS > 0 이면서 과거 63개 대비 상위 1/3",
        ),

        SignalSpec(
            f"FOREIGN_{qtag}__QQQ_{qtag}",
            "quantile_primary",
            [
                "foreign_prev",
                "foreign_prev_q63",
                "QQQ_preus_ret_1520",
                "QQQ_preus_q63_recalc",
            ],
            lambda x, q=q: (
                (x["foreign_prev"] > 0)
                & (x["foreign_prev_q63"] >= q)
                & (x["QQQ_preus_ret_1520"] > 0)
                & (x["QQQ_preus_q63_recalc"] >= q)
            ),
            "전일 외국인 순매수와 QQQ PREUS가 모두 상위 1/3",
        ),

        SignalSpec(
            f"FOREIGN_{qtag}__SPY_{qtag}",
            "quantile_primary",
            [
                "foreign_prev",
                "foreign_prev_q63",
                "SPY_preus_ret_1520",
                "SPY_preus_q63_recalc",
            ],
            lambda x, q=q: (
                (x["foreign_prev"] > 0)
                & (x["foreign_prev_q63"] >= q)
                & (x["SPY_preus_ret_1520"] > 0)
                & (x["SPY_preus_q63_recalc"] >= q)
            ),
            "전일 외국인 순매수와 SPY PREUS가 모두 상위 1/3",
        ),

        SignalSpec(
            f"FOREIGN_{qtag}__BOTH_{qtag}",
            "quantile_primary",
            [
                "foreign_prev",
                "foreign_prev_q63",
                "QQQ_preus_ret_1520",
                "QQQ_preus_q63_recalc",
                "SPY_preus_ret_1520",
                "SPY_preus_q63_recalc",
            ],
            lambda x, q=q: (
                (x["foreign_prev"] > 0)
                & (x["foreign_prev_q63"] >= q)
                & (x["QQQ_preus_ret_1520"] > 0)
                & (x["QQQ_preus_q63_recalc"] >= q)
                & (x["SPY_preus_ret_1520"] > 0)
                & (x["SPY_preus_q63_recalc"] >= q)
            ),
            "전일 외국인 + QQQ + SPY가 모두 positive & 상위 1/3",
        ),
    ]

    # --------------------------------------------------------
    # Attribution diagnostics:
    # foreign strength와 PREUS strength 중 어느 쪽이 기여하는지
    # --------------------------------------------------------
    specs += [
        SignalSpec(
            "FOREIGN_Q67__QQQ_POS",
            "quantile_diagnostic",
            [
                "foreign_prev",
                "foreign_prev_q63",
                "QQQ_preus_ret_1520",
            ],
            lambda x: (
                (x["foreign_prev"] > 0)
                & (x["foreign_prev_q63"] >= Q_PRIMARY)
                & (x["QQQ_preus_ret_1520"] > 0)
            ),
            "외국인만 상위 1/3 + QQQ는 단순 상승",
        ),

        SignalSpec(
            "FOREIGN_POS__QQQ_Q67",
            "quantile_diagnostic",
            [
                "foreign_prev",
                "QQQ_preus_ret_1520",
                "QQQ_preus_q63_recalc",
            ],
            lambda x: (
                (x["foreign_prev"] > 0)
                & (x["QQQ_preus_ret_1520"] > 0)
                & (x["QQQ_preus_q63_recalc"] >= Q_PRIMARY)
            ),
            "외국인은 단순 순매수 + QQQ만 상위 1/3",
        ),

        SignalSpec(
            "FOREIGN_Q67__SPY_POS",
            "quantile_diagnostic",
            [
                "foreign_prev",
                "foreign_prev_q63",
                "SPY_preus_ret_1520",
            ],
            lambda x: (
                (x["foreign_prev"] > 0)
                & (x["foreign_prev_q63"] >= Q_PRIMARY)
                & (x["SPY_preus_ret_1520"] > 0)
            ),
            "외국인만 상위 1/3 + SPY는 단순 상승",
        ),

        SignalSpec(
            "FOREIGN_POS__SPY_Q67",
            "quantile_diagnostic",
            [
                "foreign_prev",
                "SPY_preus_ret_1520",
                "SPY_preus_q63_recalc",
            ],
            lambda x: (
                (x["foreign_prev"] > 0)
                & (x["SPY_preus_ret_1520"] > 0)
                & (x["SPY_preus_q63_recalc"] >= Q_PRIMARY)
            ),
            "외국인은 단순 순매수 + SPY만 상위 1/3",
        ),
    ]

    # --------------------------------------------------------
    # Robustness only: top quintile
    # --------------------------------------------------------
    q = Q_ROBUST
    qtag = "Q80"

    specs += [
        SignalSpec(
            f"FOREIGN_{qtag}__QQQ_{qtag}",
            "quantile_robustness",
            [
                "foreign_prev",
                "foreign_prev_q63",
                "QQQ_preus_ret_1520",
                "QQQ_preus_q63_recalc",
            ],
            lambda x, q=q: (
                (x["foreign_prev"] > 0)
                & (x["foreign_prev_q63"] >= q)
                & (x["QQQ_preus_ret_1520"] > 0)
                & (x["QQQ_preus_q63_recalc"] >= q)
            ),
            "전일 외국인 순매수와 QQQ PREUS가 모두 top 20%",
        ),

        SignalSpec(
            f"FOREIGN_{qtag}__SPY_{qtag}",
            "quantile_robustness",
            [
                "foreign_prev",
                "foreign_prev_q63",
                "SPY_preus_ret_1520",
                "SPY_preus_q63_recalc",
            ],
            lambda x, q=q: (
                (x["foreign_prev"] > 0)
                & (x["foreign_prev_q63"] >= q)
                & (x["SPY_preus_ret_1520"] > 0)
                & (x["SPY_preus_q63_recalc"] >= q)
            ),
            "전일 외국인 순매수와 SPY PREUS가 모두 top 20%",
        ),

        SignalSpec(
            f"FOREIGN_{qtag}__BOTH_{qtag}",
            "quantile_robustness",
            [
                "foreign_prev",
                "foreign_prev_q63",
                "QQQ_preus_ret_1520",
                "QQQ_preus_q63_recalc",
                "SPY_preus_ret_1520",
                "SPY_preus_q63_recalc",
            ],
            lambda x, q=q: (
                (x["foreign_prev"] > 0)
                & (x["foreign_prev_q63"] >= q)
                & (x["QQQ_preus_ret_1520"] > 0)
                & (x["QQQ_preus_q63_recalc"] >= q)
                & (x["SPY_preus_ret_1520"] > 0)
                & (x["SPY_preus_q63_recalc"] >= q)
            ),
            "전일 외국인 + QQQ + SPY가 모두 positive & top 20%",
        ),
    ]

    return specs


# ============================================================
# PERFORMANCE
# ============================================================

def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return np.nan

    dd = (
        equity
        / equity.cummax()
        - 1.0
    )

    return float(dd.min())


def cagr_from_returns(r: pd.Series) -> float:
    r = r.dropna().astype(float)

    if len(r) == 0:
        return np.nan

    gross = float(
        (1.0 + r).prod()
    )

    if gross <= 0:
        return -1.0

    return (
        gross ** (252.0 / len(r))
        - 1.0
    )


def calmar_ratio(
    cagr: float,
    mdd: float,
) -> float:
    if (
        not np.isfinite(cagr)
        or not np.isfinite(mdd)
    ):
        return np.nan

    denom = abs(mdd)

    if denom == 0:
        if cagr > 0:
            return np.inf
        if cagr < 0:
            return -np.inf
        return np.nan

    return float(
        cagr / denom
    )


def sharpe_ratio(r: pd.Series) -> float:
    r = r.dropna().astype(float)

    if len(r) < 2:
        return np.nan

    sd = r.std(ddof=1)

    if (
        not np.isfinite(sd)
        or sd == 0
    ):
        return np.nan

    return float(
        r.mean()
        / sd
        * np.sqrt(252.0)
    )


def calc_metrics(
    daily_return: pd.Series,
) -> dict:
    r = daily_return.dropna().astype(float)

    if len(r) == 0:
        return {
            "CAGR": np.nan,
            "MDD": np.nan,
            "Calmar": np.nan,
            "Sharpe": np.nan,
            "total_return": np.nan,
        }

    eq = (
        1.0 + r
    ).cumprod()

    cagr = cagr_from_returns(r)
    mdd = max_drawdown(eq)

    return {
        "CAGR": cagr,
        "MDD": mdd,
        "Calmar": calmar_ratio(
            cagr,
            mdd,
        ),
        "Sharpe": sharpe_ratio(r),
        "total_return": float(
            eq.iloc[-1] - 1.0
        ),
    }


def resolve_eval_window(
    master: pd.DataFrame,
    required_cols: list[str],
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """
    required feature가 모두 관측되고 target(net_gap)도 있는 최초/최종 날짜를 찾는다.

    그 사이 calendar 전체를 평가기간으로 사용한다.
    중간의 sporadic missing feature는 no-trade.
    """
    mask = (
        master[required_cols]
        .notna()
        .all(axis=1)
        & master["net_gap"].notna()
    )

    dates = master.index[mask]

    if len(dates) == 0:
        raise ValueError(
            f"required feature 공통기간이 없습니다: {required_cols}"
        )

    return dates.min(), dates.max()


def evaluate_spec(
    master: pd.DataFrame,
    spec: SignalSpec,
) -> tuple[dict, pd.Series, pd.Series]:
    start, end = resolve_eval_window(
        master,
        spec.required_cols,
    )

    d = master.loc[
        (master.index >= start)
        & (master.index <= end)
        & master["net_gap"].notna()
    ].copy()

    availability = (
        d[spec.required_cols]
        .notna()
        .all(axis=1)
    )

    raw_signal = spec.rule(d)

    signal = (
        raw_signal
        .fillna(False)
        .astype(bool)
        & availability
    )

    strategy_ret = pd.Series(
        np.where(
            signal,
            d["net_gap"],
            0.0,
        ),
        index=d.index,
        name=spec.name,
    )

    # matched benchmark: 정확히 같은 KRX calendar에서 매일 close->next open
    benchmark_ret = (
        d["net_gap"]
        .astype(float)
        .rename("ALWAYS_MATCHED")
    )

    sm = calc_metrics(
        strategy_ret
    )

    bm = calc_metrics(
        benchmark_ret
    )

    trades = d.loc[
        signal,
        "net_gap",
    ]

    row = {
        "strategy": spec.name,
        "family": spec.family,
        "description": spec.description,

        "start":
            start.date().isoformat(),

        "end":
            end.date().isoformat(),

        "eval_days":
            len(d),

        "available_days":
            int(
                availability.sum()
            ),

        "availability_rate":
            float(
                availability.mean()
            ),

        "trades":
            int(
                signal.sum()
            ),

        "exposure":
            float(
                signal.mean()
            ),

        "signal_rate_available":
            (
                float(
                    signal.sum()
                    / availability.sum()
                )
                if availability.sum()
                else np.nan
            ),

        "win_rate":
            float(
                (trades > 0).mean()
            )
            if len(trades)
            else np.nan,

        "avg_trade_return":
            float(
                trades.mean()
            )
            if len(trades)
            else np.nan,

        "median_trade_return":
            float(
                trades.median()
            )
            if len(trades)
            else np.nan,

        **sm,

        "benchmark_CAGR":
            bm["CAGR"],

        "benchmark_MDD":
            bm["MDD"],

        "benchmark_Calmar":
            bm["Calmar"],

        "benchmark_Sharpe":
            bm["Sharpe"],

        "delta_CAGR":
            sm["CAGR"]
            - bm["CAGR"],

        "delta_Calmar":
            sm["Calmar"]
            - bm["Calmar"],
    }

    equity = (
        1.0 + strategy_ret
    ).cumprod()

    benchmark_equity = (
        1.0 + benchmark_ret
    ).cumprod()

    return row, equity, benchmark_equity


# ============================================================
# STATE TABLES
# ============================================================

def sign_state_table(
    master: pd.DataFrame,
) -> pd.DataFrame:
    """
    전일 foreign sign x PREUS sign 2x2.
    gross KR gap으로 경제적 conditional effect 확인.
    """
    rows = []

    for symbol in ["QQQ", "SPY"]:
        pcol = f"{symbol}_preus_ret_1520"

        d = master[
            [
                "foreign_prev",
                pcol,
                "kr_gap",
                "net_gap",
            ]
        ].dropna()

        states = {
            "F+_P+":
                (
                    (d["foreign_prev"] > 0)
                    & (d[pcol] > 0)
                ),

            "F+_P-":
                (
                    (d["foreign_prev"] > 0)
                    & (d[pcol] < 0)
                ),

            "F-_P+":
                (
                    (d["foreign_prev"] < 0)
                    & (d[pcol] > 0)
                ),

            "F-_P-":
                (
                    (d["foreign_prev"] < 0)
                    & (d[pcol] < 0)
                ),
        }

        for state, mask in states.items():
            x = d.loc[mask]

            rows.append({
                "proxy": symbol,
                "state": state,
                "n": len(x),
                "avg_kr_gap":
                    float(
                        x["kr_gap"].mean()
                    )
                    if len(x)
                    else np.nan,

                "median_kr_gap":
                    float(
                        x["kr_gap"].median()
                    )
                    if len(x)
                    else np.nan,

                "p_gap_pos":
                    float(
                        (x["kr_gap"] > 0).mean()
                    )
                    if len(x)
                    else np.nan,

                "avg_net_gap":
                    float(
                        x["net_gap"].mean()
                    )
                    if len(x)
                    else np.nan,
            })

    return pd.DataFrame(
        rows
    )


def tercile_label(q: pd.Series) -> pd.Series:
    out = pd.Series(
        np.nan,
        index=q.index,
        dtype=object,
    )

    out.loc[
        q < 1.0 / 3.0
    ] = "L"

    out.loc[
        (q >= 1.0 / 3.0)
        & (q < 2.0 / 3.0)
    ] = "M"

    out.loc[
        q >= 2.0 / 3.0
    ] = "H"

    return out


def quantile_state_table(
    master: pd.DataFrame,
) -> pd.DataFrame:
    """
    foreign q63 x PREUS q63 3x3.
    monotonicity/상위-상위 cell이 구조적으로 나은지 확인.
    """
    rows = []

    fstate = tercile_label(
        master["foreign_prev_q63"]
    )

    for symbol in ["QQQ", "SPY"]:
        pstate = tercile_label(
            master[
                f"{symbol}_preus_q63_recalc"
            ]
        )

        tmp = pd.DataFrame({
            "F": fstate,
            "P": pstate,
            "kr_gap": master["kr_gap"],
            "net_gap": master["net_gap"],
        }).dropna()

        for fs in ["L", "M", "H"]:
            for ps in ["L", "M", "H"]:
                x = tmp[
                    (tmp["F"] == fs)
                    & (tmp["P"] == ps)
                ]

                rows.append({
                    "proxy": symbol,
                    "foreign_state": fs,
                    "preus_state": ps,
                    "state": fs + ps,
                    "n": len(x),

                    "avg_kr_gap":
                        float(
                            x["kr_gap"].mean()
                        )
                        if len(x)
                        else np.nan,

                    "p_gap_pos":
                        float(
                            (x["kr_gap"] > 0).mean()
                        )
                        if len(x)
                        else np.nan,

                    "avg_net_gap":
                        float(
                            x["net_gap"].mean()
                        )
                        if len(x)
                        else np.nan,
                })

    return pd.DataFrame(
        rows
    )


# ============================================================
# ROBUSTNESS
# ============================================================

def chronological_split(
    master: pd.DataFrame,
    spec: SignalSpec,
) -> pd.DataFrame:
    start, end = resolve_eval_window(
        master,
        spec.required_cols,
    )

    dates = master.index[
        (master.index >= start)
        & (master.index <= end)
        & master["net_gap"].notna()
    ]

    if len(dates) < 60:
        return pd.DataFrame()

    n = len(dates)
    c1 = int(n * 0.50)
    c2 = int(n * 0.75)

    chunks = {
        "development_50":
            dates[:c1],

        "validation_25":
            dates[c1:c2],

        "test_25":
            dates[c2:],
    }

    rows = []

    full_signal = (
        spec.rule(master)
        .fillna(False)
        .astype(bool)
    )

    full_avail = (
        master[
            spec.required_cols
        ]
        .notna()
        .all(axis=1)
    )

    for split_name, idx in chunks.items():
        d = master.loc[idx].copy()

        sig = (
            full_signal.reindex(idx)
            .fillna(False)
            .astype(bool)
            & full_avail.reindex(idx)
            .fillna(False)
            .astype(bool)
        )

        strat = pd.Series(
            np.where(
                sig,
                d["net_gap"],
                0.0,
            ),
            index=idx,
        )

        bench = d[
            "net_gap"
        ].astype(float)

        sm = calc_metrics(strat)
        bm = calc_metrics(bench)

        tr = d.loc[
            sig,
            "net_gap",
        ]

        rows.append({
            "strategy":
                spec.name,

            "split":
                split_name,

            "start":
                idx.min()
                .date()
                .isoformat(),

            "end":
                idx.max()
                .date()
                .isoformat(),

            "days":
                len(idx),

            "trades":
                int(
                    sig.sum()
                ),

            "win_rate":
                float(
                    (tr > 0).mean()
                )
                if len(tr)
                else np.nan,

            "avg_trade_return":
                float(
                    tr.mean()
                )
                if len(tr)
                else np.nan,

            "CAGR":
                sm["CAGR"],

            "MDD":
                sm["MDD"],

            "Calmar":
                sm["Calmar"],

            "benchmark_CAGR":
                bm["CAGR"],

            "benchmark_MDD":
                bm["MDD"],

            "benchmark_Calmar":
                bm["Calmar"],
        })

    return pd.DataFrame(
        rows
    )


def halfyear_stability(
    master: pd.DataFrame,
    spec: SignalSpec,
) -> pd.DataFrame:
    start, end = resolve_eval_window(
        master,
        spec.required_cols,
    )

    d = master.loc[
        (master.index >= start)
        & (master.index <= end)
        & master["net_gap"].notna()
    ].copy()

    availability = (
        d[
            spec.required_cols
        ]
        .notna()
        .all(axis=1)
    )

    signal = (
        spec.rule(d)
        .fillna(False)
        .astype(bool)
        & availability
    )

    d["signal"] = signal

    d["half"] = [
        f"{dt.year}H{1 if dt.month <= 6 else 2}"
        for dt in d.index
    ]

    rows = []

    for half, g in d.groupby(
        "half",
        sort=True,
    ):
        sig = g[
            "signal"
        ].astype(bool)

        strat = pd.Series(
            np.where(
                sig,
                g["net_gap"],
                0.0,
            ),
            index=g.index,
        )

        bench = g[
            "net_gap"
        ].astype(float)

        tr = g.loc[
            sig,
            "net_gap",
        ]

        strat_eq = (
            1.0 + strat
        ).cumprod()

        bench_eq = (
            1.0 + bench
        ).cumprod()

        rows.append({
            "strategy":
                spec.name,

            "half":
                half,

            "days":
                len(g),

            "trades":
                int(
                    sig.sum()
                ),

            "win_rate":
                float(
                    (tr > 0).mean()
                )
                if len(tr)
                else np.nan,

            "avg_trade_return":
                float(
                    tr.mean()
                )
                if len(tr)
                else np.nan,

            "period_return":
                float(
                    strat_eq.iloc[-1]
                    - 1.0
                ),

            "MDD":
                max_drawdown(
                    strat_eq
                ),

            "benchmark_return":
                float(
                    bench_eq.iloc[-1]
                    - 1.0
                ),

            "benchmark_MDD":
                max_drawdown(
                    bench_eq
                ),
        })

    return pd.DataFrame(
        rows
    )


def time_shift_placebo(
    master: pd.DataFrame,
    spec: SignalSpec,
) -> pd.DataFrame:
    """
    final signal 자체를 KRX session 단위로 1/2/3일 뒤로 밀어서
    exact same-day alignment가 중요한지 확인.
    """
    start, end = resolve_eval_window(
        master,
        spec.required_cols,
    )

    d = master.loc[
        (master.index >= start)
        & (master.index <= end)
        & master["net_gap"].notna()
    ].copy()

    availability = (
        d[
            spec.required_cols
        ]
        .notna()
        .all(axis=1)
    )

    base_signal = (
        spec.rule(d)
        .fillna(False)
        .astype(bool)
        & availability
    )

    rows = []

    for lag in [0] + PLACEBO_LAGS:
        if lag == 0:
            signal = base_signal.copy()
        else:
            signal = (
                base_signal
                .shift(
                    lag,
                    fill_value=False,
                )
                .astype(bool)
            )

        strat = pd.Series(
            np.where(
                signal,
                d["net_gap"],
                0.0,
            ),
            index=d.index,
        )

        sm = calc_metrics(
            strat
        )

        tr = d.loc[
            signal,
            "net_gap",
        ]

        rows.append({
            "strategy":
                spec.name,

            "lag_days":
                lag,

            "trades":
                int(
                    signal.sum()
                ),

            "win_rate":
                float(
                    (tr > 0).mean()
                )
                if len(tr)
                else np.nan,

            "avg_trade_return":
                float(
                    tr.mean()
                )
                if len(tr)
                else np.nan,

            **sm,
        })

    return pd.DataFrame(
        rows
    )


# ============================================================
# COMMON BENCHMARKS
# ============================================================

def common_benchmarks(
    master: pd.DataFrame,
) -> pd.DataFrame:
    """
    가장 엄격한 sign-core 정보셋:
      foreign_prev + QQQ PREUS + SPY PREUS

    가 모두 최초 관측 가능한 시점부터 동일 common period benchmark.
    """
    req = [
        "foreign_prev",
        "QQQ_preus_ret_1520",
        "SPY_preus_ret_1520",
    ]

    start, end = resolve_eval_window(
        master,
        req,
    )

    d = master.loc[
        (master.index >= start)
        & (master.index <= end)
        & master["net_gap"].notna()
    ].copy()

    rows = []

    # Always overnight
    am = calc_metrics(
        d["net_gap"]
    )

    rows.append({
        "benchmark":
            "ALWAYS_OVERNIGHT_COMMON",

        "start":
            start.date().isoformat(),

        "end":
            end.date().isoformat(),

        "days":
            len(d),

        **am,
    })

    # Buy & Hold from KODEX close
    close = (
        d["KODEX_close"]
        .dropna()
        .astype(float)
    )

    if len(close) >= 2:
        eq = (
            close
            / close.iloc[0]
        )

        daily = (
            close
            .pct_change()
            .fillna(0.0)
        )

        cagr = (
            float(
                eq.iloc[-1]
            )
            ** (
                252.0
                / (len(close) - 1)
            )
            - 1.0
        )

        mdd = max_drawdown(
            eq
        )

        rows.append({
            "benchmark":
                "BUY_HOLD_COMMON",

            "start":
                close.index.min()
                .date()
                .isoformat(),

            "end":
                close.index.max()
                .date()
                .isoformat(),

            "days":
                len(close),

            "CAGR":
                cagr,

            "MDD":
                mdd,

            "Calmar":
                calmar_ratio(
                    cagr,
                    mdd,
                ),

            "Sharpe":
                sharpe_ratio(
                    daily
                ),

            "total_return":
                float(
                    eq.iloc[-1]
                    - 1.0
                ),
        })

    return pd.DataFrame(
        rows
    )


# ============================================================
# REPORT / PLOT
# ============================================================

def df_to_markdown(
    df: pd.DataFrame,
) -> str:
    if df.empty:
        return "_No data_"

    cols = list(df.columns)

    lines = [
        "| "
        + " | ".join(
            map(str, cols)
        )
        + " |",

        "| "
        + " | ".join(
            ["---"] * len(cols)
        )
        + " |",
    ]

    for _, row in df.iterrows():
        vals = []

        for c in cols:
            v = row[c]

            if pd.isna(v):
                vals.append("NA")

            elif isinstance(
                v,
                (float, np.floating),
            ):
                vals.append(
                    f"{v:.6f}"
                )

            else:
                vals.append(
                    str(v)
                )

        lines.append(
            "| "
            + " | ".join(vals)
            + " |"
        )

    return "\n".join(
        lines
    )


def write_report(
    summary: pd.DataFrame,
    sign_states: pd.DataFrame,
    q_states: pd.DataFrame,
    common_bench: pd.DataFrame,
    research_path: Path,
    foreign_path: Path,
) -> None:
    core = summary[
        summary["family"].isin([
            "sign_core",
            "quantile_primary",
            "quantile_robustness",
        ])
    ].sort_values(
        ["Calmar", "CAGR"],
        ascending=[False, False],
        na_position="last",
    )

    show = [
        "strategy",
        "family",
        "start",
        "end",
        "trades",
        "availability_rate",
        "win_rate",
        "avg_trade_return",
        "CAGR",
        "MDD",
        "Calmar",
        "benchmark_CAGR",
        "benchmark_MDD",
        "benchmark_Calmar",
        "delta_Calmar",
    ]

    lines = [
        "# Foreign Flow × QQQ/SPY PREUS Gap Backtest",
        "",
        "## 연구 정의",
        "",
        "- `foreign_prev`: 의사결정일의 직전 실제 KRX session 외국인 합계 순매수.",
        "- `QQQ/SPY PREUS`: 전일 미국 정규장 close → 당일 15:20 KST 직전 가격.",
        "- PREUS는 미국 정규장 O→C가 아니라 개장 전 partial overnight return.",
        "- 거래: KODEX200 당일 close 진입 → 다음 실제 KRX session open 청산.",
        f"- 거래비용: {TRADING_FEE*100:.3f}%.",
        "- 핵심 성과지표: Calmar = CAGR / |MDD|.",
        "",
        "## Threshold policy",
        "",
        "- Sign core: 외국인 순매수 > 0 + PREUS > 0.",
        "- Primary strength: rolling 63-valid-observation upper tercile.",
        "- Robustness only: rolling top quintile.",
        "- 각 rolling rank는 현재 관측치를 reference에 포함하지 않는다.",
        "",
        "## Common benchmarks",
        "",
        df_to_markdown(common_bench),
        "",
        "## Core strategies — Calmar descending",
        "",
        df_to_markdown(core[show]),
        "",
        "## Sign 2x2 state",
        "",
        df_to_markdown(sign_states),
        "",
        "## Quantile 3x3 state",
        "",
        df_to_markdown(q_states),
        "",
        "## 해석 원칙",
        "",
        "- 각 전략의 CAGR/Calmar는 feature가 최초로 실제 사용 가능한 시점부터 계산.",
        "- `benchmark_*`는 그 전략과 정확히 같은 calendar window의 ALWAYS_OVERNIGHT.",
        "- quantile 조건의 최고 Calmar 하나만 고르지 말고 Q67/Q80 방향 일관성과 표본수를 함께 확인.",
        "- sign core가 약한데 Q80만 강하면 과적합 가능성을 우선 의심.",
        "- half-year, chronological split, lag placebo가 함께 살아야 robust signal 후보로 본다.",
        "",
        "## Input",
        "",
        f"- research dataset: `{research_path}`",
        f"- foreign flow: `{foreign_path}`",
    ]

    (
        OUT_DIR
        / "report.md"
    ).write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


def plot_top(
    equities: dict[str, pd.Series],
    summary: pd.DataFrame,
) -> None:
    eligible = (
        summary[
            summary["trades"]
            >= MIN_TRADES_FOR_TOP
        ]
        .sort_values(
            ["Calmar", "CAGR"],
            ascending=[False, False],
        )
        .head(TOP_N_PLOT)
    )

    plt.figure(
        figsize=(14, 8)
    )

    for name in eligible[
        "strategy"
    ]:
        if name not in equities:
            continue

        eq = equities[name]

        plt.plot(
            eq.index,
            eq.values,
            label=name,
            linewidth=1.3,
        )

    plt.title(
        "Foreign Flow + PREUS Strategies — Top Calmar"
    )

    plt.xlabel(
        "Date"
    )

    plt.ylabel(
        "Equity"
    )

    plt.grid(
        True,
        alpha=0.3,
    )

    plt.legend(
        fontsize=8,
        ncol=2,
    )

    plt.tight_layout()

    plt.savefig(
        OUT_DIR
        / "top_strategies_by_calmar.png",
        dpi=160,
    )

    plt.close()


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    print(
        "=" * 110
    )

    print(
        "FOREIGN FLOW × QQQ/SPY PREUS KODEX200 GAP BACKTEST"
    )

    print(
        "PREUS = previous US regular close -> 15:20 KST, NOT US regular-session O->C"
    )

    print(
        "=" * 110
    )

    print(
        "[1/8] Load research dataset / KRX foreign flow"
    )

    research, research_path = (
        load_research_dataset()
    )

    foreign, foreign_path = (
        load_foreign_flow()
    )

    print(
        f"  research: "
        f"{research.index.min().date()} ~ {research.index.max().date()}, "
        f"n={len(research)}"
    )

    print(
        f"  foreign : "
        f"{foreign.index.min().date()} ~ {foreign.index.max().date()}, "
        f"n={len(foreign)}"
    )

    print(
        "[2/8] Align on KRX calendar / build lagged foreign and rolling ranks"
    )

    master = build_master(
        research,
        foreign,
    )

    # Data diagnostics
    foreign_coverage = (
        master["foreign_current"]
        .notna()
    )

    print(
        f"  foreign rows matched to KRX calendar: "
        f"{int(foreign_coverage.sum())}"
    )

    print(
        f"  QQQ PREUS non-null: "
        f"{int(master['QQQ_preus_ret_1520'].notna().sum())}"
    )

    print(
        f"  SPY PREUS non-null: "
        f"{int(master['SPY_preus_ret_1520'].notna().sum())}"
    )

    print(
        "[3/8] Build pre-specified signals"
    )

    specs = build_signal_specs()

    print(
        f"  strategies={len(specs)}"
    )

    print(
        "[4/8] Backtest each strategy with matched-period benchmark"
    )

    rows = []
    equities = {}

    for spec in specs:
        row, eq, _ = evaluate_spec(
            master,
            spec,
        )

        rows.append(
            row
        )

        equities[
            spec.name
        ] = eq

    summary = pd.DataFrame(
        rows
    ).sort_values(
        ["Calmar", "CAGR"],
        ascending=[False, False],
        na_position="last",
    )

    print(
        "[5/8] Conditional state diagnostics"
    )

    sign_states = sign_state_table(
        master
    )

    q_states = quantile_state_table(
        master
    )

    print(
        "[6/8] Robustness: chronological / half-year / lag placebo"
    )

    split_parts = []
    half_parts = []
    placebo_parts = []

    for spec in specs:
        split = chronological_split(
            master,
            spec,
        )

        if not split.empty:
            split_parts.append(
                split
            )

        half = halfyear_stability(
            master,
            spec,
        )

        if not half.empty:
            half_parts.append(
                half
            )

        placebo = time_shift_placebo(
            master,
            spec,
        )

        if not placebo.empty:
            placebo_parts.append(
                placebo
            )

    chronological = (
        pd.concat(
            split_parts,
            ignore_index=True,
        )
        if split_parts
        else pd.DataFrame()
    )

    halfyear = (
        pd.concat(
            half_parts,
            ignore_index=True,
        )
        if half_parts
        else pd.DataFrame()
    )

    placebo = (
        pd.concat(
            placebo_parts,
            ignore_index=True,
        )
        if placebo_parts
        else pd.DataFrame()
    )

    print(
        "[7/8] Common-period benchmarks / save"
    )

    bench = common_benchmarks(
        master
    )

    master.to_csv(
        OUT_DIR
        / "aligned_dataset.csv",
        encoding="utf-8-sig",
    )

    summary.to_csv(
        OUT_DIR
        / "strategy_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    sign_states.to_csv(
        OUT_DIR
        / "sign_state_table.csv",
        index=False,
        encoding="utf-8-sig",
    )

    q_states.to_csv(
        OUT_DIR
        / "quantile_state_table.csv",
        index=False,
        encoding="utf-8-sig",
    )

    chronological.to_csv(
        OUT_DIR
        / "chronological_split.csv",
        index=False,
        encoding="utf-8-sig",
    )

    halfyear.to_csv(
        OUT_DIR
        / "halfyear_stability.csv",
        index=False,
        encoding="utf-8-sig",
    )

    placebo.to_csv(
        OUT_DIR
        / "time_shift_placebo.csv",
        index=False,
        encoding="utf-8-sig",
    )

    bench.to_csv(
        OUT_DIR
        / "common_benchmarks.csv",
        index=False,
        encoding="utf-8-sig",
    )

    write_report(
        summary,
        sign_states,
        q_states,
        bench,
        research_path,
        foreign_path,
    )

    plot_top(
        equities,
        summary,
    )

    print(
        "[8/8] Result"
    )

    print()
    print(
        "=== CORE STRATEGIES BY CALMAR ==="
    )

    core = summary[
        summary["family"].isin([
            "sign_core",
            "quantile_primary",
            "quantile_robustness",
        ])
    ].copy()

    cols = [
        "strategy",
        "family",
        "start",
        "end",
        "trades",
        "availability_rate",
        "win_rate",
        "avg_trade_return",
        "CAGR",
        "MDD",
        "Calmar",
        "benchmark_CAGR",
        "benchmark_MDD",
        "benchmark_Calmar",
        "delta_Calmar",
    ]

    print(
        core[
            cols
        ].to_string(
            index=False,
            formatters={
                "availability_rate":
                    lambda x:
                        f"{x*100:.1f}%",

                "win_rate":
                    lambda x:
                        "NA"
                        if pd.isna(x)
                        else f"{x*100:.1f}%",

                "avg_trade_return":
                    lambda x:
                        "NA"
                        if pd.isna(x)
                        else f"{x*100:.3f}%",

                "CAGR":
                    lambda x:
                        "NA"
                        if pd.isna(x)
                        else f"{x*100:.2f}%",

                "MDD":
                    lambda x:
                        "NA"
                        if pd.isna(x)
                        else f"{x*100:.2f}%",

                "Calmar":
                    lambda x:
                        "NA"
                        if pd.isna(x)
                        else f"{x:.3f}",

                "benchmark_CAGR":
                    lambda x:
                        "NA"
                        if pd.isna(x)
                        else f"{x*100:.2f}%",

                "benchmark_MDD":
                    lambda x:
                        "NA"
                        if pd.isna(x)
                        else f"{x*100:.2f}%",

                "benchmark_Calmar":
                    lambda x:
                        "NA"
                        if pd.isna(x)
                        else f"{x:.3f}",

                "delta_Calmar":
                    lambda x:
                        "NA"
                        if pd.isna(x)
                        else f"{x:+.3f}",
            },
        )
    )

    print()
    print(
        "=== COMMON BENCHMARKS ==="
    )

    print(
        bench.to_string(
            index=False,
            formatters={
                "CAGR":
                    lambda x:
                        f"{x*100:.2f}%",

                "MDD":
                    lambda x:
                        f"{x*100:.2f}%",

                "Calmar":
                    lambda x:
                        f"{x:.3f}",

                "Sharpe":
                    lambda x:
                        f"{x:.3f}",
            },
        )
    )

    print()
    print(
        "Interpretation order:"
    )

    print(
        "  1) FOREIGN_POS__QQQ/SPY_POS가 component-only보다 개선되는가?"
    )

    print(
        "  2) Q67에서 sign-core보다 Calmar와 avg trade return이 개선되는가?"
    )

    print(
        "  3) Q80에서도 방향이 유지되는가? 단, 표본수가 급감하면 채택하지 않는다."
    )

    print(
        "  4) quantile_state_table에서 foreign/preus 강도가 높아질수록 gap이 대체로 개선되는가?"
    )

    print(
        "  5) chronological 3구간 / half-year에서 같은 방향이 유지되는가?"
    )

    print(
        "  6) lag0이 lag1/2/3 placebo보다 명확하게 좋은가?"
    )

    print()
    print(
        f"Output: {OUT_DIR.resolve()}"
    )


if __name__ == "__main__":
    main()
