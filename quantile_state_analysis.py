# -*- coding: utf-8 -*-
"""
63일 Rolling Quantile 기반 미국장 방향성 예측 분석
====================================================

research.md(연구 설계) + interview.md(구현 명세) + review.md(핵심 수정 2건)를
구현한 단일 스크립트다.

핵심 설계 (review.md 반영):
  1) rolling percentile은 반드시 각 KR feature 시계열(전체 KRX 거래일 히스토리)에서
     먼저 계산하고, 그 후 US target과 same-date inner join 한다.
     (join 후 rolling을 하면 63 KRX 거래일이 아니라 63 KR∩US 공통거래일이 되어
      state 정의가 바뀌는 버그가 생김 — review.md §2)
  2) Hard verdict(+1%p / n>=30 / 최근 5반기 중 3 positive)는 폐기했고,
     Up-side / Down-side diagnostic summary를 독립 출력한다 (review.md §3~4).
  3) 보고서에서 Core와 Extension을 구분해 해석한다 (review.md §7).

실행:  python3 quantile_state_analysis.py
출력:  analysis_output/ (report.md, PNG 플롯, 표 CSV)
"""

import warnings
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless 환경 강제 (plan.md §11)

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning)

# ============================================================
# CONFIG — 모든 파라미터 상수 (재현성, plan.md §2.2)
# ============================================================
DATA_DIR = Path("data")
OUT_DIR = Path("analysis_output")

# 수집일 부분 행(장중 수집으로 불완전) — 모든 파일에서 drop (interview.md §2.2)
PARTIAL_DATE = pd.Timestamp("2026-09-28")

PAIRS = {"304940": "QQQ", "219480": "SPY"}          # KR ETF -> US target
DEFAULT_WINDOW = 63
WINDOWS = [42, 63, 84]                               # robustness (63 = 기본)
TERCILE_EDGES = (1.0 / 3.0, 2.0 / 3.0)
FLAT_BANDS = [0.001, 0.002]                          # ±0.1%(기본), ±0.2%(robustness) — 0% 제외
LATE_INTERVALS = [(15, 0, 15, 30), (15, 10, 15, 30), (15, 20, 15, 30)]  # (h,m,h,m)
SUB_INTERVALS = [(15, 0, 15, 10), (15, 10, 15, 20), (15, 20, 15, 30)]
PCT_GRID = [0.0025, 0.0050, 0.0075, 0.0100]          # P(R^US > n%) 그리드
SMALL_N_WARN = 30        # review.md §3.3: 판정 조건이 아닌 소표본 warning flag
CONT_Q_BINS = 5          # 연속 quantile heatmap bin 수
RET_TOL = 1e-9           # ret_oc 재계산 / 1m 교차검증 허용 오차

# OLS/로짓 실행 조건(plan.md §7.2): Up/Down 양방향 uplift 절댓값이 모두 이 값 미만이고
# Late/Day monotonicity가 모두 0이면 "약한 신호"로 보고 실행. 상수로 노출.
WEAK_UPLIFT_PP = 1.0

# NYSE 조기폐장(반일) 날짜 — 분석에서 제외하지 않고 부록 참고용으로만 출력
EARLY_CLOSE_DATES = [
    "2021-11-26", "2021-12-24", "2022-07-04",
    "2022-11-25", "2022-12-23", "2023-07-03", "2023-11-24", "2023-12-22",
    "2024-07-03", "2024-11-29", "2024-12-24", "2025-07-03", "2025-11-28",
    "2025-12-24", "2026-07-02",
]

STATE_ORDER = ["LL", "LM", "LH", "ML", "MM", "MH", "HL", "HM", "HH"]

# ============================================================
# 콘솔 로그 (한국어, plan.md §8.3)
# ============================================================

def log(msg: str) -> None:
    print(msg, flush=True)


# ============================================================
# 데이터 로딩/정제 (plan.md §2.3)
# ============================================================

def load_daily(symbol: str) -> pd.DataFrame:
    """일봉 CSV 로드. PARTIAL_DATE drop, ret_oc 검증."""
    path = DATA_DIR / f"{symbol}_daily.csv"
    if symbol == "069500":
        path = DATA_DIR / "KODEX200_069500_daily_20100101_20260928.csv"
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["date"] != PARTIAL_DATE].sort_values("date").reset_index(drop=True)
    for c in ["open", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # ret_oc 재계산 검증 (부록용 카운트)
    recomputed = np.log(df["close"] / df["open"])
    if "ret_oc" in df.columns:
        bad = int((np.abs(df["ret_oc"] - recomputed) > RET_TOL).sum())
    else:
        df["ret_oc"] = recomputed
        bad = 0
    log(f"[로드] {symbol}: {len(df)}행, {df['date'].min().date()} ~ {df['date'].max().date()}, ret_oc 불일치={bad}")
    return df[["date", "open", "close", "ret_oc"]]


def load_late_features(symbol: str) -> pd.DataFrame:
    path = DATA_DIR / f"{symbol}_late_features.csv"
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["date"] != PARTIAL_DATE].sort_values("date").reset_index(drop=True)
    log(f"[로드] {symbol} late_features: {len(df)}행, {df['date'].min().date()} ~ {df['date'].max().date()}")
    return df[["date", "ret_1500_1530"]].rename(columns={"ret_1500_1530": "late_ret"})


def load_1m(symbol: str) -> pd.DataFrame:
    """1분봉 로드 (sub-interval / late-interval variant 재계산용)."""
    path = DATA_DIR / f"{symbol}_1500_1530_1m.csv"
    df = pd.read_csv(path, usecols=["timestamp", "date", "open", "close"])
    df["date"] = pd.to_datetime(df["date"])
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Seoul")
    df["hhmm"] = df["ts"].dt.strftime("%H:%M")
    df = df[df["date"] != PARTIAL_DATE].sort_values("ts").reset_index(drop=True)
    for c in ["open", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def interval_returns_1m(min1: pd.DataFrame, start_hm: str, end_hm: str) -> pd.DataFrame:
    """
    1분봉에서 [start_hm, end_hm] 구간 수익률 = log(end봉 close / start봉 open).
    무결성 규칙(interview.md §4.4): 시작 봉과 끝 봉이 모두 존재하는 날만 사용.

    min1: 원본 1분봉(ts/hhmm 포함) 또는 precomputed comb(date 인덱스, interval 컬럼).
    """
    col = f"ret_{start_hm.replace(':', '')}_{end_hm.replace(':', '')}"
    if "hhmm" in min1.columns:
        rows = []
        for d, g in min1.groupby("date"):
            g = g.sort_values("ts")
            s = g[g["hhmm"] == start_hm]
            e = g[g["hhmm"] == end_hm]
            if s.empty or e.empty:
                continue  # endpoint 없음 → 제외
            rows.append({"date": d, col:
                        np.log(float(e.iloc[-1]["close"]) / float(s.iloc[0]["open"]))})
        return pd.DataFrame(rows)
    # precomputed comb 경로: date 인덱스의 컬럼 값 사용
    if col in min1.columns:
        out = min1[[col]].dropna().copy()
        out["date"] = out.index
        return out[["date", col]].reset_index(drop=True)
    raise KeyError(f"1m 데이터에 {col} 컬럼이 없다")


# ============================================================
# 코어 계산 유틸 (plan.md §3)
# ============================================================

def rolling_pct_rank_midpoint(values: np.ndarray, window: int) -> np.ndarray:
    """
    Midpoint tie rolling percentile (interview.md §3.2).

      Q_t = (#{s in [t-window, t-1] : R_s < R_t}
             + 0.5 * #{s in [t-window, t-1] : R_s == R_t}) / window

    - 현재 시점 t는 과거 표본에 미포함 (look-ahead 방지).
    - reference는 '이 시계열의 직전 window개 유효 관측치'다. 즉 KR ETF의
      직전 KRX 거래일 기준이며, US 거래일과의 교집합이 아니다 (review.md 수정 B).
    - 유효 과거 관측치가 window개 미만이면 NaN (strict warmup).
    - pandas rolling rank의 tie 옵션('average' 등)과 다름에 주의.

    self-check: values=[1,1,2,3], window=3 →
      t=3: past=[1,1,2], R=3 → (#below=3, #equal=0) → 3/3 = 1.0
    self-check: values=[5,1,1,1], window=3 →
      t=3: past=[5,1,1], R=1 → (#below=0, #equal=2) → 1/3
    """
    out = np.full(len(values), np.nan)
    for t in range(window, len(values)):
        past = values[t - window:t]
        cur = values[t]
        below = float(np.count_nonzero(past < cur))
        equal = float(np.count_nonzero(past == cur))
        out[t] = (below + 0.5 * equal) / window
    return out


def _self_check() -> None:
    q = rolling_pct_rank_midpoint(np.array([1.0, 1.0, 2.0, 3.0]), 3)
    assert np.isnan(q[:3]).all(), "warmup 구간은 NaN이어야 함"
    assert abs(q[3] - 1.0) < 1e-12, f"self-check 실패: {q[3]}"  # past 전체가 cur보다 작음
    q2 = rolling_pct_rank_midpoint(np.array([5.0, 1.0, 1.0, 1.0]), 3)
    assert abs(q2[3] - (0.0 + 0.5 * 2.0) / 3.0) < 1e-12, "midpoint tie 실패"
    q3 = rolling_pct_rank_midpoint(np.array([1.0, 2.0, 3.0, 2.0]), 3)
    assert abs(q3[3] - (1.0 + 0.5 * 1.0) / 3.0) < 1e-12, "midpoint 혼합 실패"


def tercile(q: np.ndarray) -> np.ndarray:
    """Low(0) / Mid(1) / High(2): Q<1/3, 1/3<=Q<2/3, Q>=2/3. NaN은 -1."""
    lo, hi = TERCILE_EDGES
    out = np.full(len(q), -1, dtype=int)
    valid = ~np.isnan(q)
    out[valid] = np.digitize(q[valid], [lo, hi], right=False)
    return out


def tercile_label(k: int) -> str:
    return {-1: "", 0: "Low", 1: "Mid", 2: "High"}[k]


def state_code(day_k: int, late_k: int) -> str:
    letters = {0: "L", 1: "M", 2: "H"}
    return letters[day_k] + letters[late_k]


def build_kr_state(kr_daily: pd.DataFrame, late: pd.DataFrame, window: int,
                   late_ret_col: str = "late_ret") -> pd.DataFrame:
    """
    review.md 수정 1의 올바른 순서:
      1) KR feature 시계열 각각에서 rolling quantile 계산 (US join 전!)
      2) KR 내부에서 Day/Late 결합 → 9-state
      (US join은 별도 함수 pair_join에서 수행)
    """
    day = kr_daily[["date", "ret_oc"]].rename(columns={"ret_oc": "kr_ret"}).copy()
    day["q_day"] = rolling_pct_rank_midpoint(day["kr_ret"].to_numpy(), window)
    day["day_k"] = tercile(day["q_day"].to_numpy())

    lt = late[["date", late_ret_col]].rename(columns={late_ret_col: "late_ret"}).copy()
    lt["q_late"] = rolling_pct_rank_midpoint(lt["late_ret"].to_numpy(), window)
    lt["late_k"] = tercile(lt["q_late"].to_numpy())

    kr_state = day[["date", "kr_ret", "q_day", "day_k"]].merge(
        lt[["date", "late_ret", "q_late", "late_k"]], on="date", how="inner")
    kr_state = kr_state[(kr_state["day_k"] >= 0) & (kr_state["late_k"] >= 0)].copy()
    kr_state["state"] = [state_code(a, b) for a, b in zip(kr_state["day_k"], kr_state["late_k"])]
    return kr_state.sort_values("date").reset_index(drop=True)


def pair_join(kr_state: pd.DataFrame, us_daily: pd.DataFrame) -> pd.DataFrame:
    """state 완성 후 US target과 same-date inner join (이 시점이 join의 전부)."""
    us = us_daily[["date", "ret_oc"]].rename(columns={"ret_oc": "us_ret"})
    pair = kr_state.merge(us, on="date", how="inner").sort_values("date").reset_index(drop=True)
    return pair


def classify_target(pair: pd.DataFrame, band: float) -> pd.DataFrame:
    out = pair.copy()
    out["target"] = np.where(out["us_ret"] > band, "Up",
                             np.where(out["us_ret"] < -band, "Down", "Flat"))
    return out


# ============================================================
# 집계 유틸 (plan.md §3.5)
# ============================================================

def baseline(df: pd.DataFrame) -> dict:
    n = len(df)
    return {
        "n": n,
        "P(Up)": float((df["target"] == "Up").mean()),
        "P(Flat)": float((df["target"] == "Flat").mean()),
        "P(Down)": float((df["target"] == "Down").mean()),
    }


def state_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for st in STATE_ORDER:
        g = df[df["state"] == st]
        n = len(g)
        if n == 0:
            continue
        p_up = float((g["target"] == "Up").mean())
        p_flat = float((g["target"] == "Flat").mean())
        p_down = float((g["target"] == "Down").mean())
        rows.append({"Day": st[0], "Late": st[1], "state": st, "n": n,
                     "Up": int((g["target"] == "Up").sum()),
                     "Flat": int((g["target"] == "Flat").sum()),
                     "Down": int((g["target"] == "Down").sum()),
                     "P(Up)": p_up, "P(Flat)": p_flat, "P(Down)": p_down})
    tbl = pd.DataFrame(rows)
    bl = baseline(df)
    tbl["P(Up)-baseline"] = tbl["P(Up)"] - bl["P(Up)"]
    tbl["P(Down)-baseline"] = tbl["P(Down)"] - bl["P(Down)"]
    return tbl


def monotonicity(tbl: pd.DataFrame) -> dict:
    """
    review.md §6: Late/Day 양방향 모두 핵심 diagnostic (hard cutoff 없음).
    Late 기준: 각 Day 행에서 Late L→M→H일수록 P(Up) 증가 / P(Down) 감소 행 수 (0~3).
    Day 기준:  각 Late 행에서 Day L→M→H일수록 동일 카운트.
    """
    piv_up = tbl.pivot(index="Day", columns="Late", values="P(Up)")
    piv_dn = tbl.pivot(index="Day", columns="Late", values="P(Down)")
    late_up = late_dn = day_up = day_dn = 0
    if {"L", "M", "H"}.issubset(piv_up.columns):
        late_up = int(((piv_up["L"] < piv_up["M"]) & (piv_up["M"] < piv_up["H"])).sum())
        late_dn = int(((piv_dn["L"] > piv_dn["M"]) & (piv_dn["M"] > piv_dn["H"])).sum())
    if {"L", "M", "H"}.issubset(piv_up.index):
        day_up = int(((piv_up.loc["L"] < piv_up.loc["M"]) & (piv_up.loc["M"] < piv_up.loc["H"])).sum())
        day_dn = int(((piv_dn.loc["L"] > piv_dn.loc["M"]) & (piv_dn.loc["M"] > piv_dn.loc["H"])).sum())
    return {"late_monotonic_up_rows": late_up, "late_monotonic_down_rows": late_dn,
            "day_monotonic_up_rows": day_up, "day_monotonic_down_rows": day_dn}


def halfyear_stats(df: pd.DataFrame) -> pd.DataFrame:
    """반기 전체 descriptive (제한 없음, plan.md §4.5)."""
    d = df.copy()
    d["half"] = d["date"].dt.year.astype(str) + "H" + np.where(d["date"].dt.month <= 6, "1", "2")
    rows = []
    for h, g in d.groupby("half"):
        b_up = float((g["target"] == "Up").mean())
        b_dn = float((g["target"] == "Down").mean())
        for st in STATE_ORDER:
            s = g[g["state"] == st]
            if len(s) == 0:
                continue
            rows.append({"half": h, "state": st, "n": len(s),
                         "P(Up)": float((s["target"] == "Up").mean()),
                         "baseline_P(Up)": b_up,
                         "uplift": float((s["target"] == "Up").mean()) - b_up,
                         "P(Down)": float((s["target"] == "Down").mean()),
                         "baseline_P(Down)": b_dn,
                         "down_uplift": float((s["target"] == "Down").mean()) - b_dn})
    return pd.DataFrame(rows)


def positive_halves(half_df: pd.DataFrame, state: str) -> tuple[int, int]:
    s = half_df[half_df["state"] == state]
    return int((s["uplift"] > 0).sum()), int(len(s))


# ============================================================
# Diagnostic summary (plan.md §3.6, review.md §4 — hard rule 없음)
# ============================================================

def up_side_diagnostic(tbl: pd.DataFrame, mono: dict, half_df: pd.DataFrame) -> dict:
    hh = tbl[tbl["state"] == "HH"].iloc[0]
    bl = {"P(Up)": float((tbl["Up"].sum()) / tbl["n"].sum())}
    pos, tot = positive_halves(half_df, "HH")
    return {
        "P(Up|HH)": float(hh["P(Up)"]), "baseline_P(Up)": bl["P(Up)"],
        "HH_uplift_pp": (float(hh["P(Up)"]) - bl["P(Up)"]) * 100.0,
        "HH_n": int(hh["n"]),
        "late_mono_up_rows": mono["late_monotonic_up_rows"],
        "day_mono_up_rows": mono["day_monotonic_up_rows"],
        "HH_positive_halves": pos, "HH_total_halves": tot,
    }


def positive_halves_down(half_df: pd.DataFrame, state: str) -> tuple[int, int]:
    """Down-side positive halves: 반기별 (P(Down|state) - 반기 baseline P(Down)) > 0 개수."""
    s = half_df[half_df["state"] == state]
    return int((s["down_uplift"] > 0).sum()), int(len(s))


def down_side_diagnostic(tbl: pd.DataFrame, mono: dict, half_df: pd.DataFrame) -> dict:
    ll = tbl[tbl["state"] == "LL"].iloc[0]
    n_total = int(tbl["n"].sum())
    bl_down = float(tbl["Down"].sum() / n_total)
    pos, tot = positive_halves_down(half_df, "LL")
    return {
        "P(Down|LL)": float(ll["P(Down)"]), "baseline_P(Down)": bl_down,
        "LL_uplift_pp": (float(ll["P(Down)"]) - bl_down) * 100.0,
        "LL_n": int(ll["n"]),
        "late_mono_down_rows": mono["late_monotonic_down_rows"],
        "day_mono_down_rows": mono["day_monotonic_down_rows"],
        "LL_positive_halves": pos, "LL_total_halves": tot,
    }


def small_n_states(tbl: pd.DataFrame) -> list[str]:
    return [r["state"] for _, r in tbl.iterrows() if r["n"] < SMALL_N_WARN]


def weak_signal(diags: dict) -> bool:
    """plan.md §7.2: OLS/로짓 실행 조건. 하드 판정이 아니라 실행 트리거 상수 기반."""
    up = diags["up"]; dn = diags["down"]
    return (abs(up["HH_uplift_pp"]) < WEAK_UPLIFT_PP and abs(dn["LL_uplift_pp"]) < WEAK_UPLIFT_PP
            and up["late_mono_up_rows"] == 0 and dn["late_mono_down_rows"] == 0
            and up["day_mono_up_rows"] == 0 and dn["day_mono_down_rows"] == 0)


# ============================================================
# 분석 모듈: Core (plan.md §4)
# ============================================================

def analyze_core(pair: pd.DataFrame, label: str, out_dir: Path) -> dict:
    log(f"[CORE] {label}: 표본 {len(pair)}개, {pair['date'].min().date()} ~ {pair['date'].max().date()}")
    res = {}
    res["pair"] = pair
    res["baseline"] = baseline(pair)
    res["table"] = state_table(pair)
    res["mono"] = monotonicity(res["table"])
    res["half"] = halfyear_stats(pair)
    res["diag"] = {
        "up": up_side_diagnostic(res["table"], res["mono"], res["half"]),
        "down": down_side_diagnostic(res["table"], res["mono"], res["half"]),
    }
    res["small_n"] = small_n_states(res["table"])
    res["table"].to_csv(out_dir / f"state_table_{label}.csv", index=False)
    res["half"].to_csv(out_dir / f"halfyear_{label}.csv", index=False)
    return res


# ============================================================
# 분석 모듈: Robustness (plan.md §5)
# ============================================================

def robustness_variants(data: dict, min1_by_symbol: dict) -> dict:
    """
    variants: (이름, kr_state_df, 태그유형) 목록. 기본(63d, ±0.1%, 15:00–15:30) 제외 5개:
      ±0.2% band / 42d / 84d / 15:10–15:30 / 15:20–15:30
    band variant는 pair를 다시 분류하고, window/late variant는 pair 자체가 다르다.
    """
    out = {}
    for sym, us_sym in PAIRS.items():
        label = us_sym.lower()
        us_daily = data["us"][us_sym]
        base_state = data["kr_state"][sym][63]
        variants = []
        # threshold variant (±0.2%): 동일 pair, band만 변경
        variants.append(("band0.2pct", pair_join(base_state, us_daily), "band", 0.002))
        # window variants
        for w in [42, 84]:
            variants.append((f"window{w}", pair_join(data["kr_state"][sym][w], us_daily), "window", None))
        # late interval variants (1m 재계산, 같은 window=63)
        for (h1, m1, h2, m2) in LATE_INTERVALS[1:]:
            col = f"ret_{h1:02d}{m1:02d}_{h2:02d}{m2:02d}"
            late_df = (min1_by_symbol[sym][[col]].reset_index()
                       .rename(columns={"index": "date", col: "late_ret"}))
            st = build_kr_state(data["kr"][sym], late_df, 63, "late_ret")
            variants.append((f"late{h1:02d}{m1:02d}_{h2:02d}{m2:02d}", pair_join(st, us_daily), "late", None))
        out[us_sym] = variants
    return out


def evaluate_variant(pair: pd.DataFrame, band: float | None, label: str, us_sym: str, out_dir: Path) -> dict:
    band_use = band if band is not None else 0.001
    p = classify_target(pair, band_use)
    tbl = state_table(p)
    mono = monotonicity(tbl)
    row = {
        "variant": label, "target": us_sym,
        "P(Up|HH)_sign": "+" if tbl.loc[tbl["state"] == "HH", "P(Up)-baseline"].iloc[0] > 0 else "-",
        "P(Down|LL)_sign": "+" if tbl.loc[tbl["state"] == "LL", "P(Down)-baseline"].iloc[0] > 0 else "-",
        "late_mono_up_rows": mono["late_monotonic_up_rows"],
        "min_state_n": int(tbl["n"].min()),
        "valid_start": p["date"].min().date(),
        "n": len(p),
    }
    tbl.to_csv(out_dir / f"variant_{us_sym.lower()}_{label}.csv", index=False)
    return row


# ============================================================
# 분석 모듈: Extensions (plan.md §6)
# ============================================================

def confirmation_analysis(data: dict) -> pd.DataFrame:
    """Both-High 정의: 304940 HH & 219480 Day=High&Late=High (그리고 대칭)."""
    rows = []
    pairs = {}
    for sym, us_sym in PAIRS.items():
        p = classify_target(pair_join(data["kr_state"][sym][63], data["us"][us_sym]), 0.001)
        pairs[sym] = p
    (sym_a, sym_b) = list(PAIRS.keys())
    for main_sym, conf_sym, us_sym in [(sym_a, sym_b, "QQQ"), (sym_b, sym_a, "SPY")]:
        main = pairs[main_sym][["date", "state", "us_ret"]]
        conf = pairs[conf_sym][["date", "day_k", "late_k"]].rename(
            columns={"day_k": "c_day", "late_k": "c_late"})
        m = main.merge(conf, on="date", how="inner")
        both = m[(m["state"] == "HH") & (m["c_day"] == 2) & (m["c_late"] == 2)]
        other = m[(m["state"] == "HH") & ~((m["c_day"] == 2) & (m["c_late"] == 2))]
        rows.append({"pair": f"{main_sym}->{us_sym}", "cond": "HH & counter Both-High",
                     "n": len(both), "P(Up)": float((both["us_ret"] > 0.001).mean()) if len(both) else np.nan,
                     "avg_ret": float(both["us_ret"].mean()) if len(both) else np.nan})
        rows.append({"pair": f"{main_sym}->{us_sym}", "cond": "HH & counter else",
                     "n": len(other), "P(Up)": float((other["us_ret"] > 0.001).mean()) if len(other) else np.nan,
                     "avg_ret": float(other["us_ret"].mean()) if len(other) else np.nan})
    return pd.DataFrame(rows)


def pct_grid(data: dict) -> pd.DataFrame:
    rows = []
    for sym, us_sym in PAIRS.items():
        p = classify_target(pair_join(data["kr_state"][sym][63], data["us"][us_sym]), 0.001)
        for st in STATE_ORDER:
            g = p[p["state"] == st]
            if g.empty:
                continue
            row = {"pair": us_sym, "state": st, "n": len(g)}
            for n_thr in PCT_GRID:
                row[f"P(>{n_thr*100:.2f}%)"] = float((g["us_ret"] > n_thr).mean())
            rows.append(row)
        row = {"pair": us_sym, "state": "baseline", "n": len(p)}
        for n_thr in PCT_GRID:
            row[f"P(>{n_thr*100:.2f}%)"] = float((p["us_ret"] > n_thr).mean())
        rows.append(row)
    return pd.DataFrame(rows)


def subinterval_analysis(data: dict, min1_by_symbol: dict) -> pd.DataFrame:
    """§14 후보1: 3개 sub-interval 각각 독립 quantile (1m 시리즈 자체 히스토리, join 전)."""
    rows, combo_rows = [], []
    for sym, us_sym in PAIRS.items():
        if us_sym != "QQQ":   # 대표로 NASDAQ 페어만 상세 분해 (§14 후보1 취지)
            continue
        pair = classify_target(pair_join(data["kr_state"][sym][63], data["us"][us_sym]), 0.001)
        for (h1, m1, h2, m2) in SUB_INTERVALS:
            col = f"ret_{h1:02d}{m1:02d}_{h2:02d}{m2:02d}"
            late_df = (min1_by_symbol[sym][[col]].reset_index()
                       .rename(columns={"index": "date", col: "late_ret"}))
            st = build_kr_state(data["kr"][sym], late_df, 63, "late_ret")
            for cond_name, cond in [("DayHH & subHigh", (st["day_k"] == 2) & (st["late_k"] == 2)),
                                    ("DayHH & subLow", (st["day_k"] == 2) & (st["late_k"] == 0))]:
                sub = st[cond][["date"]].merge(pair[["date", "us_ret"]], on="date", how="inner")
                rows.append({"sub_interval": f"{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}", "cond": cond_name,
                             "n": len(sub), "P(QQQ Up)": float((sub["us_ret"] > 0.001).mean()) if len(sub) else np.nan})
        # continuation/reversal: 초반(15:00-15:15) vs 후반(15:15-15:30) 부호 조합
        early = interval_returns_1m(min1_by_symbol[sym], "15:00", "15:15").rename(
            columns={"ret_1500_1515": "early"})
        late_ = interval_returns_1m(min1_by_symbol[sym], "15:15", "15:30").rename(
            columns={"ret_1515_1530": "latehalf"})
        combo = pair[["date", "us_ret"]].merge(early, on="date").merge(late_, on="date")
        combo["combo"] = np.where((combo["early"] > 0) & (combo["latehalf"] > 0), "up/up",
                          np.where((combo["early"] > 0) & (combo["latehalf"] <= 0), "up/reverse",
                          np.where((combo["early"] <= 0) & (combo["latehalf"] > 0), "down/reverse", "down/down")))
        for c, g in combo.groupby("combo"):
            combo_rows.append({"combo": c, "n": len(g),
                               "P(QQQ Up)": float((g["us_ret"] > 0.001).mean()),
                               "avg_us_ret": float(g["us_ret"].mean())})
    return pd.DataFrame(rows), pd.DataFrame(combo_rows)


def gap_analysis(data: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """§15: Gap = log(069500 다음 영업일 open / 당일 close)."""
    k = data["kodex200"].copy()
    k["next_open"] = k["open"].shift(-1)
    k["next_date"] = k["date"].shift(-1)
    k["gap_days"] = (k["next_date"] - k["date"]).dt.days
    long_gaps = int((k["gap_days"] > 3).sum())
    rows, rows2, day_rows = [], [], []
    for sym, us_sym in PAIRS.items():
        pair = classify_target(pair_join(data["kr_state"][sym][63], data["us"][us_sym]), 0.001)
        g = pair.merge(k[["date", "next_open", "close", "gap_days"]], on="date", how="inner")
        g = g.dropna(subset=["next_open"])
        g["gap"] = np.log(g["next_open"] / g["close"])
        for _, r in g.iterrows():
            day_rows.append({"pair": us_sym, "US_outcome": r["target"], "gap": float(r["gap"])})
        for t in ["Up", "Flat", "Down"]:
            s = g[g["target"] == t]
            rows.append({"pair": us_sym, "US_outcome": t, "n": len(s),
                         "avg_gap": float(s["gap"].mean()) if len(s) else np.nan,
                         "P(gap>0)": float((s["gap"] > 0).mean()) if len(s) else np.nan})
        for st in STATE_ORDER:
            s = g[g["state"] == st]
            if s.empty:
                continue
            rows2.append({"pair": us_sym, "state": st, "n": len(s),
                          "P(US Up)": float((s["target"] == "Up").mean()),
                          "avg_us_ret": float(s["us_ret"].mean()),
                          "avg_next_gap": float(s["gap"].mean())})
    gap_tbl = pd.DataFrame(rows)
    log(f"[GAP] 069500 날짜쌍 간격>3일 건수: {long_gaps}")
    return gap_tbl, pd.DataFrame(rows2), pd.DataFrame(day_rows)


# ============================================================
# 조건부 진단: OLS / Logit / 연속 heatmap (plan.md §7)
# ============================================================

def run_ols_logit(pair: pd.DataFrame, label: str) -> dict:
    import statsmodels.api as sm
    out = {}
    d = pair.dropna(subset=["kr_ret", "late_ret", "us_ret"])
    X = sm.add_constant(d[["kr_ret", "late_ret"]])
    ols = sm.OLS(d["us_ret"], X).fit(cov_type="HAC", cov_kwds={"maxlags": 5})
    out["ols"] = {"n": int(ols.nobs), "beta_day": float(ols.params["kr_ret"]),
                  "p_day": float(ols.pvalues["kr_ret"]), "beta_late": float(ols.params["late_ret"]),
                  "p_late": float(ols.pvalues["late_ret"]), "r2": float(ols.rsquared)}
    up = d[d["us_ret"] > 0.001].assign(y=1.0)
    dn = d[d["us_ret"] < -0.001].assign(y=0.0)
    lg_d = pd.concat([up, dn]).dropna(subset=["q_day", "q_late"])
    Xl = sm.add_constant(lg_d[["q_day", "q_late"]])
    logit = sm.Logit(lg_d["y"], Xl).fit(disp=0)
    out["logit"] = {"n": int(logit.nobs), "beta_qday": float(logit.params["q_day"]),
                    "p_qday": float(logit.pvalues["q_day"]), "beta_qlate": float(logit.params["q_late"]),
                    "p_qlate": float(logit.pvalues["q_late"]), "pseudo_r2": float(logit.prsquared)}
    return out


def continuous_heatmap_data(pair: pd.DataFrame, bins: int) -> pd.DataFrame:
    d = pair.dropna(subset=["q_day", "q_late"]).copy()
    d["dq"] = pd.qcut(d["q_day"], bins, labels=False, duplicates="drop")
    d["lq"] = pd.qcut(d["q_late"], bins, labels=False, duplicates="drop")
    return d.groupby(["dq", "lq"]).agg(P_Up=("us_ret", lambda s: float((s > 0.001).mean())),
                                       n=("us_ret", "size")).reset_index()


# ============================================================
# 플롯 (plan.md §8.1 — 라벨 영어)
# ============================================================

def plot_all(data: dict, core: dict, var_rows: list, grid: pd.DataFrame,
             gap_tbl: pd.DataFrame, gap_days: pd.DataFrame, sub_df: pd.DataFrame,
             cont: dict) -> None:
    import matplotlib.pyplot as plt

    for sym, us_sym in PAIRS.items():
        label = us_sym.lower()
        r = core[us_sym]
        tbl, bl = r["table"], r["baseline"]
        # 1) 3x3 P(Up) heatmap
        piv = tbl.pivot(index="Day", columns="Late", values="P(Up)")
        cnt = tbl.pivot(index="Day", columns="Late", values="n")
        fig, ax = plt.subplots(figsize=(5.5, 4.5))
        im = ax.imshow(piv.values, cmap="RdYlGn", vmin=0.3, vmax=0.7)
        ax.set_xticks(range(3), ["Low", "Mid", "High"]); ax.set_yticks(range(3), ["Low", "Mid", "High"])
        ax.set_xlabel("Late state"); ax.set_ylabel("Day state")
        ax.set_title(f"P(US Up | state) - {us_sym} pair (baseline={bl['P(Up)']:.3f})")
        for i in range(3):
            for j in range(3):
                ax.text(j, i, f"{piv.values[i, j]:.3f}\nn={int(cnt.values[i, j])}",
                        ha="center", va="center", fontsize=9)
        fig.colorbar(im); fig.tight_layout()
        fig.savefig(OUT_DIR / f"state_heatmap_{label}.png", dpi=150); plt.close(fig)
        # 2) uplift bars
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.bar(tbl["state"], tbl["P(Up)-baseline"] * 100, color="steelblue")
        ax.axhline(0, color="black", lw=0.8)
        ax.set_title(f"Uplift vs baseline (pp) - {us_sym} pair"); ax.set_ylabel("P(Up|state)-P(Up), pp")
        fig.tight_layout(); fig.savefig(OUT_DIR / f"uplift_bars_{label}.png", dpi=150); plt.close(fig)
        # 3) half-year stability (HH/LL 강조)
        half = r["half"]
        fig, ax = plt.subplots(figsize=(8, 4.5))
        for st in STATE_ORDER:
            s = half[half["state"] == st]
            if s.empty:
                continue
            lw = 2.5 if st in ("HH", "LL") else 0.8
            ax.plot(s["half"], s["uplift"] * 100, marker="o", ms=3, lw=lw, label=st)
        ax.axhline(0, color="black", lw=0.8)
        ax.set_title(f"Half-year uplift by state (pp) - {us_sym} pair"); ax.legend(ncol=3, fontsize=7)
        ax.tick_params(axis="x", rotation=45)
        fig.tight_layout(); fig.savefig(OUT_DIR / f"halfyear_stability_{label}.png", dpi=150); plt.close(fig)
        # 5) threshold grid heatmap
        gg = grid[grid["pair"] == us_sym]
        cols = [c for c in gg.columns if c.startswith("P(>")]
        mat = gg[gg["state"] != "baseline"].set_index("state")[cols]
        fig, ax = plt.subplots(figsize=(7, 4.5))
        im = ax.imshow(mat.values.astype(float), cmap="viridis", aspect="auto")
        ax.set_xticks(range(len(cols)), [c.replace("P(>", "").replace(")", "%") for c in cols])
        ax.set_yticks(range(len(mat)), mat.index)
        ax.set_title(f"P(US return > n%) by state - {us_sym} pair")
        for i in range(len(mat)):
            for j in range(len(cols)):
                ax.text(j, i, f"{mat.values[i, j]*100:.1f}", ha="center", va="center", fontsize=8, color="white")
        fig.colorbar(im); fig.tight_layout()
        fig.savefig(OUT_DIR / f"threshold_grid_{label}.png", dpi=150); plt.close(fig)
        # 7) continuous quantile heatmap (항상 생성)
        c = cont[us_sym]
        piv2 = c.pivot(index="dq", columns="lq", values="P_Up")
        fig, ax = plt.subplots(figsize=(5.5, 4.5))
        im = ax.imshow(piv2.values.astype(float), cmap="RdYlGn", vmin=0.3, vmax=0.7)
        ax.set_xlabel("Late quantile bin"); ax.set_ylabel("Day quantile bin")
        ax.set_title(f"Continuous Qday x Qlate P(Up) - {us_sym} pair")
        for i in range(piv2.shape[0]):
            for j in range(piv2.shape[1]):
                v = piv2.values[i, j]
                if not np.isnan(v):
                    ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=9)
        fig.colorbar(im); fig.tight_layout()
        fig.savefig(OUT_DIR / f"continuous_quantile_heatmap_{label}.png", dpi=150); plt.close(fig)
    # 4) robustness summary (페어 오프셋 bar)
    fig, ax = plt.subplots(figsize=(9, 4))
    vr = pd.DataFrame(var_rows)
    labels = sorted(vr["variant"].unique())
    xpos = np.arange(len(labels))
    for k_i, (us_sym, g) in enumerate(vr.groupby("target")):
        g = g.set_index("variant").reindex(labels)
        vals = [1.0 if s == "+" else (-1.0 if s == "-" else 0.0) for s in g["P(Up|HH)_sign"]]
        ax.bar(xpos + (k_i - 0.5) * 0.4, vals, width=0.38, label=us_sym)
    ax.set_xticks(xpos, labels, rotation=45, fontsize=8)
    ax.axhline(0, color="black", lw=0.8)
    ax.set_title("HH uplift sign across variants (+1 / -1)")
    ax.legend()
    fig.tight_layout(); fig.savefig(OUT_DIR / "robustness_summary.png", dpi=150); plt.close(fig)
    # 6) gap 분포 box plot (일별 gap 분포 기반)
    fig, ax = plt.subplots(figsize=(6, 4))
    order = ["Up", "Flat", "Down"]
    gd = gap_days[gap_days["pair"] == "QQQ"] if not gap_days.empty else gap_days
    data_by_t = [gd[gd["US_outcome"] == t]["gap"].dropna() * 100 for t in order]
    ax.boxplot(data_by_t, tick_labels=order)
    ax.axhline(0, color="black", lw=0.8)
    ax.set_title("Next KR open gap (log %) by US outcome (QQQ pair)")
    ax.set_ylabel("gap, log %")
    fig.tight_layout(); fig.savefig(OUT_DIR / "gap_by_us_outcome.png", dpi=150); plt.close(fig)
    # 8) late sub-interval compare
    if not sub_df.empty:
        fig, ax = plt.subplots(figsize=(7, 4))
        for cond, g in sub_df.groupby("cond"):
            ax.bar([f"{s}\n{c}" for s, c in zip(g["sub_interval"], g["cond"])], g["P(QQQ Up)"] * 100, label=cond)
        ax.axhline(0, color="black", lw=0.8)
        ax.set_title("P(QQQ Up) by late sub-interval state"); ax.tick_params(axis="x", labelsize=7)
        fig.tight_layout(); fig.savefig(OUT_DIR / "late_subinterval_compare.png", dpi=150); plt.close(fig)
    log(f"[플롯] 저장 완료: {OUT_DIR}/")


# ============================================================
# Report 작성 (plan.md §8.2 — review.md §7 구조, 한국어)
# ============================================================

def md_table(df: pd.DataFrame, float_fmt: str = "{:.3f}") -> str:
    d = df.copy()
    for c in d.columns:
        if d[c].dtype.kind == "f":
            d[c] = d[c].map(lambda v: "" if pd.isna(v) else float_fmt.format(v))
    lines = ["| " + " | ".join(map(str, d.columns)) + " |",
             "|" + "---|" * len(d.columns)]
    for _, r in d.iterrows():
        lines.append("| " + " | ".join(str(v) for v in r.values) + " |")
    return "\n".join(lines)


def write_report(data: dict, core: dict, var_rows: list, conf: pd.DataFrame,
                 grid: pd.DataFrame, sub_df: pd.DataFrame, combo_df: pd.DataFrame,
                 gap_tbl: pd.DataFrame, gap_state: pd.DataFrame, alt: dict) -> str:
    L = []
    A = L.append
    A("# 63일 Rolling Quantile 기반 미국장 방향성 분석 결과\n")
    A(f"- 실행일: {date.today()}")
    A("- 데이터: data/ 기존 CSV (오프라인). 부분 수집 행(PARTIAL_DATE) 제거 후 사용")
    A(f"- 계산 순서(review.md 핵심): **각 KR feature 시계열(전체 KRX 거래일)에서 rolling quantile → 9-state → US same-date join**")
    A("- 하드 판정 규칙 없음: Up-side / Down-side diagnostic을 독립 출력 (review.md §4)\n")

    # 1. Core Result
    A("## 1. Core Result (63d, 15:00-15:30, ±0.1%)\n")
    for us_sym, r in core.items():
        pair = r["pair"]; bl = r["baseline"]
        A(f"### 1.{list(PAIRS.values()).index(us_sym)+1} {us_sym} 페어\n")
        A(f"- 유효 표본: {len(pair)}개 ({pair['date'].min().date()} ~ {pair['date'].max().date()})")
        A(f"- Baseline: P(Up)={bl['P(Up)']:.3f}, P(Flat)={bl['P(Flat)']:.3f}, P(Down)={bl['P(Down)']:.3f}\n")
        A("**9-state 마스터 표**\n")
        A(md_table(r["table"][["Day", "Late", "state", "n", "Up", "Flat", "Down",
                              "P(Up)", "P(Flat)", "P(Down)", "P(Up)-baseline", "P(Down)-baseline"]]) + "\n")
        m = r["mono"]
        A(f"- Monotonicity(Late L→M→H): P(Up) 증가 행 {m['late_monotonic_up_rows']}/3, "
          f"P(Down) 감소 행 {m['late_monotonic_down_rows']}/3")
        A(f"- Monotonicity(Day L→M→H): P(Up) 증가 행 {m['day_monotonic_up_rows']}/3, "
          f"P(Down) 감소 행 {m['day_monotonic_down_rows']}/3")
        if r["small_n"]:
            A(f"- 소표본 warning (n<{SMALL_N_WARN}): {', '.join(r['small_n'])}")
        d_up, d_dn = r["diag"]["up"], r["diag"]["down"]
        A("\n**Up-side diagnostic (독립 평가)**\n")
        A(f"- P(Up|HH)={d_up['P(Up|HH)']:.3f} vs baseline {d_up['baseline_P(Up)']:.3f} "
          f"(uplift {d_up['HH_uplift_pp']:+.1f}%p), HH n={d_up['HH_n']}")
        A(f"- Late/Day monotonic(P(Up)): {d_up['late_mono_up_rows']}/3, {d_up['day_mono_up_rows']}/3")
        A(f"- HH positive halves: {d_up['HH_positive_halves']}/{d_up['HH_total_halves']}")
        A("\n**Down-side diagnostic (독립 평가)**\n")
        A(f"- P(Down|LL)={d_dn['P(Down|LL)']:.3f} vs baseline {d_dn['baseline_P(Down)']:.3f} "
          f"(uplift {d_dn['LL_uplift_pp']:+.1f}%p), LL n={d_dn['LL_n']}")
        A(f"- Late/Day monotonic(P(Down)): {d_dn['late_mono_down_rows']}/3, {d_dn['day_mono_down_rows']}/3")
        A(f"- LL positive halves: {d_dn['LL_positive_halves']}/{d_dn['LL_total_halves']}")
        A("\n(위 라벨은 Strong/Mixed/Weak 참고 가이드라인으로만 읽고, 하드 규칙이 아니다 — review.md §4.1)\n")
        A("**반기별 stability (전체 유효 반기, descriptive)**\n")
        hs = r["half"]
        hh_pos = hs[hs["state"] == "HH"]; ll_pos = hs[hs["state"] == "LL"]
        A(f"- HH positive halves = {int((hh_pos['uplift'] > 0).sum())}/{len(hh_pos)}, "
          f"LL positive halves(Down 기준) = {int((ll_pos['down_uplift'] > 0).sum())}/{len(ll_pos)}\n")
        piv_half = (hs.pivot(index="half", columns="state", values="uplift")
                    .reindex(columns=STATE_ORDER).reset_index())
        for c in STATE_ORDER:
            piv_half[c] = piv_half[c] * 100
        A(md_table(piv_half[["half"] + STATE_ORDER], "{:+.1f}") + "  ← 반기별 uplift(%p), state별\n")

    # 2. Core Robustness
    A("## 2. Core Robustness (band ±0.2%)\n")
    vr = pd.DataFrame(var_rows)
    A(md_table(vr[vr["variant"] == "band0.2pct"][["target", "variant", "P(Up|HH)_sign",
                                                  "P(Down|LL)_sign", "late_mono_up_rows", "min_state_n", "n"]]) + "\n")

    # 3. Extended Diagnostics
    A("## 3. Extended Diagnostics (해석 경계: 확정 판단의 근거가 아닌 보조 단서)\n")
    A("### 3.1 Window / Late interval variants\n")
    A(md_table(vr[vr["variant"] != "band0.2pct"][["target", "variant", "P(Up|HH)_sign",
                                                  "P(Down|LL)_sign", "late_mono_up_rows",
                                                  "min_state_n", "valid_start", "n"]]) + "\n")
    A("### 3.2 219480/304940 Confirmation (Both-High)\n")
    A(md_table(conf, "{:.3f}") + "\n")
    A("### 3.3 Late sub-interval 분해 (QQQ 페어, 15:00-15:10/15:10-15:20/15:20-15:30)\n")
    A(md_table(sub_df, "{:.3f}") + "\n")
    A("**초반/후반 부호 조합 (continuation vs reversal)**\n")
    A(md_table(combo_df, "{:.4f}") + "\n")

    # 4. Follow-up Economic Analysis
    A("## 4. Follow-up Economic Analysis\n")
    A("### 4.1 P(R^US > n%) 그리드\n")
    A(md_table(grid, "{:.3f}") + "\n")
    A("### 4.2 익일 국장 Gap (KODEX200 069500)\n")
    A(md_table(gap_tbl, "{:.3f}") + "\n")
    A("**KR state → 미국장 → 익일 gap**\n")
    A(md_table(gap_state, "{:.3f}") + "\n")

    # 5. Alternative Models
    A("## 5. Alternative Models (연속 quantile heatmap은 항상, OLS/로짓은 약한 신호 시)\n")
    for us_sym, r in alt.items():
        A(f"### {us_sym} 페어\n")
        if r.get("ols"):
            o, lg = r["ols"], r["logit"]
            A(f"- OLS(HAC5): n={o['n']}, beta_day={o['beta_day']:.4f}(p={o['p_day']:.3f}), "
              f"beta_late={o['beta_late']:.4f}(p={o['p_late']:.3f}), R2={o['r2']:.3f}")
            A(f"- Logit(Up vs Down): n={lg['n']}, beta_qday={lg['beta_qday']:.3f}(p={lg['p_qday']:.3f}), "
              f"beta_qlate={lg['beta_qlate']:.3f}(p={lg['p_qlate']:.3f}), pseudoR2={lg['pseudo_r2']:.3f}\n")
        else:
            A("- OLS/로짓 미실행 (약한 신호 조건 미충족 — signal 존재 가능성)\n")

    # 6. 데이터 품질 노트
    A("## 6. 데이터 품질 노트 (부록)\n")
    A(f"- PARTIAL_DATE({PARTIAL_DATE.date()}) 행: 모든 파일에서 제거")
    A(f"- US 조기폐장(반일) 참고 날짜(제외하지 않음): {', '.join(EARLY_CLOSE_DATES)}")
    A("- US 가격은 미수정(adjusted=false) 수집 — O→C 방향성에는 배당 왜곡 미미")
    A("- 1분봉 재계산은 endpoint(시작/끝 봉) 존재 날만 사용; late_features와 교차검증 결과는 콘솔 로그 참조")

    # 7. §20 8개 질문 답변
    A("\n## 7. research.md §20의 8개 질문에 대한 답\n")
    A(f"1. 당일 KR 선물 ETF 강약 ↔ 미국장 방향: Core 표의 Day 축 uplift 구조 참조")
    A(f"2. 마지막 30분 추가 정보: Late 축 monotonicity {core['QQQ']['mono']['late_monotonic_up_rows']}/3 (QQQ 기준)")
    A(f"3. Day/Late 동방향 시 continuation: HH P(Up)={core['QQQ']['diag']['up']['P(Up|HH)']:.3f} vs baseline "
      f"{core['QQQ']['diag']['up']['baseline_P(Up)']:.3f}")
    A(f"4. HH/LL vs baseline 차이: HH uplift {core['QQQ']['diag']['up']['HH_uplift_pp']:+.1f}%p, "
      f"LL(Down) uplift {core['QQQ']['diag']['down']['LL_uplift_pp']:+.1f}%p (QQQ 기준)")
    A(f"5. 반기별 동방향 유지: HH positive halves {core['QQQ']['diag']['up']['HH_positive_halves']}/"
      f"{core['QQQ']['diag']['up']['HH_total_halves']}")
    A(f"6. NASDAQ vs S&P 동시 관측: QQQ HH {core['QQQ']['diag']['up']['HH_uplift_pp']:+.1f}%p vs "
      f"SPY HH {core['SPY']['diag']['up']['HH_uplift_pp']:+.1f}%p")
    A(f"7. P(R>n%) 확장: §4.1 그리드 참조")
    A(f"8. 익일 국장 gap 전달: §4.2 표 참조\n")

    # 8. 해석 및 다음 단계
    A("## 8. 해석 및 다음 단계\n")
    A("- Core+Core Robustness에서 부호/구조 일관성이 유지되는지가 1차 판단 기준이다 (review.md §7).")
    A("- HH/LL 구조가 monotonic하고 반기별로 방향이 유지되면 §14의 원인 세분화(30분 구조 분해, confirmation)로 진행한다.")
    A("- 약한 신호로 판단되면 §17 대응(연속 quantile, OLS/로짓, feature 재검토)을 따른다.")

    report = "\n".join(L)
    (OUT_DIR / "report.md").write_text(report, encoding="utf-8")
    return report


# ============================================================
# main (plan.md §10 순서)
# ============================================================

def main() -> None:
    _self_check()
    log("[PHASE 1] 데이터 로딩")
    OUT_DIR.mkdir(exist_ok=True)
    data = {"kr": {}, "late": {}, "us": {}, "kr_state": {}, "min1": {}}

    for sym, us_sym in PAIRS.items():
        data["kr"][sym] = load_daily(sym)
        data["late"][sym] = load_late_features(sym)
        data["us"][us_sym] = load_daily(us_sym)

    data["kodex200"] = load_daily("069500")

    # 1m 로딩 + sub/late interval 재계산 + late_features 교차검증
    min1_by_symbol = {}
    for sym in PAIRS:
        min1 = load_1m(sym)
        frames = {}
        for (h1, m1, h2, m2) in set(LATE_INTERVALS + SUB_INTERVALS) | {(15, 0, 15, 15), (15, 15, 15, 30)}:
            df_i = interval_returns_1m(min1, f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}")
            col = [c for c in df_i.columns if c != "date"][0]
            frames[col] = df_i.set_index("date")[col]
        comb = pd.DataFrame(frames)
        # 교차검증: 재계산 ret_1500_1530 vs late_features
        recalc = comb["ret_1500_1530"].rename("recalc").to_frame().join(
            data["late"][sym].set_index("date")["late_ret"].rename("orig"), how="inner").dropna()
        mism = int((np.abs(recalc["recalc"] - recalc["orig"]) > RET_TOL).sum())
        log(f"[1m 검증] {sym}: 비교 {len(recalc)}일 중 불일치 {mism}일 (허용오차 {RET_TOL})")
        min1_by_symbol[sym] = comb
    data["min1"] = min1_by_symbol

    log("[PHASE 2] KR 히스토리 기반 quantile/state (US join 전)")
    for sym in PAIRS:
        data["kr_state"][sym] = {}
        for w in WINDOWS:
            st = build_kr_state(data["kr"][sym], data["late"][sym], w)
            data["kr_state"][sym][w] = st
            if w == DEFAULT_WINDOW:
                log(f"  {sym} w={w}: KR state {len(st)}행, "
                    f"Day quantile 시작 {st['date'].min().date()} (Day warmup은 2021-12 말~, Late가 병목)")

    log("[PHASE 3] Core 분석")
    core = {}
    for sym, us_sym in PAIRS.items():
        pair = pair_join(data["kr_state"][sym][DEFAULT_WINDOW], data["us"][us_sym])
        core[us_sym] = analyze_core(classify_target(pair, 0.001), us_sym.lower(), OUT_DIR)
    data["_core_pairs"] = {us: core[us]["pair"] for us in core}

    log("[PHASE 4] Robustness variants")
    var_rows = []
    variants = robustness_variants(data, min1_by_symbol)
    for us_sym, vs in variants.items():
        for (label, pair, kind, band) in vs:
            var_rows.append(evaluate_variant(pair, band, label, us_sym, OUT_DIR))
    log(f"  variants 평가 {len(var_rows)}건")

    log("[PHASE 5] Extensions")
    conf = confirmation_analysis(data)
    grid = pct_grid(data)
    sub_df, combo_df = subinterval_analysis(data, min1_by_symbol)
    gap_tbl, gap_state, gap_days = gap_analysis(data)
    conf.to_csv(OUT_DIR / "confirmation.csv", index=False)
    grid.to_csv(OUT_DIR / "pct_grid.csv", index=False)
    sub_df.to_csv(OUT_DIR / "subinterval.csv", index=False)
    combo_df.to_csv(OUT_DIR / "subinterval_combo.csv", index=False)
    gap_tbl.to_csv(OUT_DIR / "gap_by_us_outcome.csv", index=False)
    gap_state.to_csv(OUT_DIR / "gap_by_state.csv", index=False)

    log("[PHASE 6] 진단 (연속 heatmap 항상, OLS/로짓 조건부)")
    alt, cont = {}, {}
    for us_sym in PAIRS.values():
        pair = core[us_sym]["pair"]
        cont[us_sym] = continuous_heatmap_data(pair, CONT_Q_BINS)
        if weak_signal(core[us_sym]["diag"]):
            alt[us_sym] = run_ols_logit(pair, us_sym.lower())
            log(f"  {us_sym}: 약한 신호 → OLS/로짓 실행")
        else:
            alt[us_sym] = {"ols": None, "logit": None}
            log(f"  {us_sym}: 신호 존재 → OLS/로짓 미실행 (연속 heatmap만)")

    log("[PHASE 7] 플롯 + Report")
    plot_all(data, core, var_rows, grid, gap_tbl, gap_days, sub_df, cont)
    write_report(data, core, var_rows, conf, grid, sub_df, combo_df, gap_tbl, gap_state, alt)

    log("[완료] analysis_output/report.md 생성")


if __name__ == "__main__":
    main()
