#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
collect_preus_sparse_history.py

장기간 regime-dependency 검증용 QQQ/SPY PREUS sparse collector.

PREUS 정의
----------
PREUS_t = log(P_t,15:20 KST / C_US,previous regular session)

필요 데이터
-----------
1) QQQ/SPY 미국 일봉 close
2) 각 KRX 거래일의 15:20 KST 시점 QQQ/SPY 가격

핵심 설계
---------
- 전체 QQQ/SPY 1분봉을 연속 수집하지 않는다.
- 각 KRX 거래일마다 Toss 1m candle API를 15:20 KST 기준으로 1회 조회한다.
- cutoff 직전 마지막 가격만 저장한다.
- 미국 일봉은 별도로 1d candle pagination으로 수집한다.
- KRX 거래일은 외국인 순매수 XLSX를 우선 사용하고, 없으면 KODEX200 daily를 사용한다.
- 중단 후 재실행 가능하도록 checkpoint/resume 지원.
- Toss 1m timestamp convention 검증 전이므로
  <=15:20 과 <15:20 두 snapshot을 모두 저장한다.

출력
----
market_1m_data/
    QQQ_daily_toss.csv
    SPY_daily_toss.csv
    QQQ_preus_1520_sparse.csv
    SPY_preus_1520_sparse.csv
    QQQ_preus_1520_enriched.csv
    SPY_preus_1520_enriched.csv
    QQQ_SPY_preus_1520_merged.csv

.env
----
TOSS_CLIENT_ID=...
TOSS_CLIENT_SECRET=...
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv


# ============================================================
# Configuration
# ============================================================

KST = "Asia/Seoul"

TOSS_BASE = "https://openapi.tossinvest.com"
TOSS_COUNT = 200
TOSS_REQ_GAP = 0.22
TOSS_ADJUSTED = False

from research_paths import DATA, ENV_PATH, resolve_input

OUT_DIR = DATA / "1m_data"

load_dotenv(dotenv_path=ENV_PATH)

SYMBOLS = {
    "QQQ": "QQQ",
    "SPY": "SPY",
}

PREUS_CUTOFF_HHMM = "15:20"

# 마지막 관측가격이 cutoff에서 이 이상 멀면 usable=False.
MAX_STALENESS_MIN = 30.0

DAILY_LOOKBACK_BUFFER_DAYS = 14

CHECKPOINT_EVERY = 25
RESUME = True

# 필요 시 직접 기간 제한. None이면 calendar source 전체 기간.
FORCE_START_DATE: str | None = None
FORCE_END_DATE: str | None = None

FOREIGN_CALENDAR_CANDIDATES = [
    DATA / "market_1m_data/KRX 외국인 순매수.xlsx",
    resolve_input("KRX 외국인 순매수.xlsx"),
    DATA / "KRX 외국인 순매수(1).xlsx",
]

KODEX_CALENDAR_CANDIDATES = [
    DATA / "KODEX200_069500_daily_20100101_20260928.csv",
    DATA / "KODEX200_069500_daily.csv",
    DATA / "market_1m_data" / "KODEX200_069500_daily_20100101_20260928.csv",
]


# ============================================================
# Common helpers
# ============================================================

def save_csv(df: pd.DataFrame, path: Path) -> None:
    if df.empty:
        print(f"[SKIP] empty -> {path.name}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"[SAVE] {path.name}: {len(df):,} rows")


def safe_numeric(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for col in columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def find_first_existing(candidates: list[Path]) -> Path | None:
    for p in candidates:
        if p.exists():
            return p
    return None


def parse_date_column(df: pd.DataFrame) -> pd.Series:
    for c in ["일자", "date", "Date", "timestamp"]:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors="coerce")
            if s.notna().mean() >= 0.8:
                try:
                    s = s.dt.tz_localize(None)
                except TypeError:
                    pass
                return s.dt.normalize()

    c = df.columns[0]
    s = pd.to_datetime(df[c], errors="coerce")

    if s.notna().mean() < 0.8:
        raise ValueError(
            f"날짜 컬럼 식별 실패. columns={list(df.columns)}"
        )

    try:
        s = s.dt.tz_localize(None)
    except TypeError:
        pass

    return s.dt.normalize()


def load_krx_decision_dates() -> tuple[pd.DatetimeIndex, Path]:
    """
    실제 KRX 거래일 목록.
    외국인 순매수 XLSX를 우선 사용하고,
    없으면 KODEX200 daily CSV를 사용한다.
    """
    foreign_path = find_first_existing(
        FOREIGN_CALENDAR_CANDIDATES
    )

    if foreign_path is not None:
        df = pd.read_excel(foreign_path)
        dates = (
            parse_date_column(df)
            .dropna()
            .drop_duplicates()
            .sort_values()
        )
        idx = pd.DatetimeIndex(dates)
        source = foreign_path

    else:
        kodex_path = find_first_existing(
            KODEX_CALENDAR_CANDIDATES
        )

        if kodex_path is None:
            raise FileNotFoundError(
                "KRX calendar source를 찾지 못했습니다.\n"
                "KRX 외국인 순매수.xlsx 또는 KODEX200 daily CSV가 필요합니다."
            )

        df = pd.read_csv(kodex_path)
        dates = (
            parse_date_column(df)
            .dropna()
            .drop_duplicates()
            .sort_values()
        )
        idx = pd.DatetimeIndex(dates)
        source = kodex_path

    if FORCE_START_DATE is not None:
        idx = idx[idx >= pd.Timestamp(FORCE_START_DATE)]

    if FORCE_END_DATE is not None:
        idx = idx[idx <= pd.Timestamp(FORCE_END_DATE)]

    if len(idx) == 0:
        raise RuntimeError(
            "설정된 기간에 KRX decision date가 없습니다."
        )

    return idx, source


# ============================================================
# Toss client
# ============================================================

class TossClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.token: str | None = None
        self._shown_rate_limit = False

    def close(self) -> None:
        self.session.close()

    def auth(self) -> None:
        client_id = os.getenv("TOSS_CLIENT_ID")
        client_secret = os.getenv("TOSS_CLIENT_SECRET")

        if not client_id or not client_secret:
            raise RuntimeError(
                "TOSS_CLIENT_ID / TOSS_CLIENT_SECRET을 찾을 수 없습니다.\n"
                f"확인한 .env 경로: {ENV_PATH}"
            )

        response = self.session.post(
            f"{TOSS_BASE}/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=20,
        )
        response.raise_for_status()

        payload = response.json()
        self.token = (
            payload.get("access_token")
            or payload["accessToken"]
        )

        print(f"[TOSS AUTH] client_id={client_id[:4]}...")

    def get(
        self,
        path: str,
        params: dict,
    ) -> requests.Response:
        if not self.token:
            self.auth()

        for attempt in range(8):
            response = self.session.get(
                f"{TOSS_BASE}{path}",
                headers={
                    "Authorization": f"Bearer {self.token}"
                },
                params=params,
                timeout=30,
            )

            if response.status_code == 401 and attempt == 0:
                print("[TOSS 401] re-authenticate")
                self.auth()
                continue

            if response.status_code == 429:
                retry_after = float(
                    response.headers.get("Retry-After", 1)
                )
                print(
                    f"[TOSS 429] sleep {retry_after:.2f}s"
                )
                time.sleep(retry_after)
                continue

            response.raise_for_status()

            if not self._shown_rate_limit:
                print(
                    "[TOSS RATE]",
                    response.headers.get("X-RateLimit-Limit"),
                    "TPS",
                )
                self._shown_rate_limit = True

            time.sleep(TOSS_REQ_GAP)
            return response

        raise RuntimeError(
            f"Toss request retry exhausted: {path}"
        )

    @staticmethod
    def normalize_candles(
        rows: list[dict],
        symbol: str,
    ) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows).rename(
            columns={
                "openPrice": "open",
                "highPrice": "high",
                "lowPrice": "low",
                "closePrice": "close",
            }
        )

        df = safe_numeric(
            df,
            [
                "open",
                "high",
                "low",
                "close",
                "volume",
                "tradingValue",
            ],
        )

        if "timestamp" not in df.columns:
            raise RuntimeError(
                "Toss candle response has no timestamp. "
                f"columns={df.columns.tolist()}"
            )

        df["timestamp"] = (
            pd.to_datetime(df["timestamp"], utc=True)
            .dt.tz_convert(KST)
        )

        df["date_kst"] = (
            df["timestamp"]
            .dt.tz_localize(None)
            .dt.normalize()
        )

        df["symbol"] = symbol

        wanted = [
            "timestamp",
            "date_kst",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "tradingValue",
            "currency",
            "symbol",
        ]

        cols = [
            c for c in wanted
            if c in df.columns
        ]

        return (
            df[cols]
            .dropna(subset=["timestamp", "open", "close"])
            .drop_duplicates(subset=["timestamp"], keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

    def collect_stock_daily(
        self,
        *,
        symbol: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        """
        QQQ/SPY 1d candle history.
        """
        start_kst = start.tz_localize(KST)
        end_exclusive = (
            end + pd.Timedelta(days=1)
        ).tz_localize(KST)

        before = (
            f"{end:%Y-%m-%d}T23:59:59+09:00"
        )

        previous_before: str | None = None
        chunks: list[pd.DataFrame] = []
        page_no = 0

        while True:
            response = self.get(
                "/api/v1/candles",
                {
                    "symbol": symbol,
                    "interval": "1d",
                    "count": TOSS_COUNT,
                    "adjusted": str(
                        TOSS_ADJUSTED
                    ).lower(),
                    "before": before,
                },
            )

            payload = response.json()
            result = payload.get("result", payload)

            rows = result.get("candles", [])
            next_before = result.get("nextBefore")

            df = self.normalize_candles(
                rows,
                symbol,
            )

            if df.empty:
                print(
                    f"[DAILY {symbol}] STOP: empty"
                )
                break

            page_no += 1
            chunks.append(df)

            oldest = df["timestamp"].min()
            newest = df["timestamp"].max()

            print(
                f"[DAILY {symbol} PAGE {page_no:04d}] "
                f"{len(df):3d} | {oldest} -> {newest}"
            )

            if oldest <= start_kst:
                break

            if not next_before:
                print(
                    f"[DAILY {symbol}] STOP: no nextBefore"
                )
                break

            if (
                next_before == before
                or next_before == previous_before
            ):
                raise RuntimeError(
                    f"Daily pagination cursor stalled: {next_before}"
                )

            previous_before = before
            before = next_before

        if not chunks:
            return pd.DataFrame()

        return (
            pd.concat(chunks, ignore_index=True)
            .loc[
                lambda x:
                    (x["timestamp"] >= start_kst)
                    & (x["timestamp"] < end_exclusive)
            ]
            .drop_duplicates(subset=["timestamp"], keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

    def fetch_cutoff_snapshot(
        self,
        *,
        symbol: str,
        krx_date: pd.Timestamp,
    ) -> dict:
        """
        한 KRX 거래일의 15:20 KST 직전 QQQ/SPY 가격을 조회.

        둘 다 저장:
          - timestamp <= 15:20
          - timestamp <  15:20

        Toss 1m timestamp semantics 확인 후 하나를 선택한다.
        """
        hh, mm = map(
            int,
            PREUS_CUTOFF_HHMM.split(":"),
        )

        date_naive = (
            pd.Timestamp(krx_date)
            .tz_localize(None)
            .normalize()
        )

        cutoff = (
            date_naive
            + pd.Timedelta(hours=hh, minutes=mm)
        ).tz_localize(KST)

        response = self.get(
            "/api/v1/candles",
            {
                "symbol": symbol,
                "interval": "1m",
                "count": TOSS_COUNT,
                "adjusted": str(
                    TOSS_ADJUSTED
                ).lower(),
                "before": cutoff.isoformat(),
            },
        )

        payload = response.json()
        result = payload.get("result", payload)
        rows = result.get("candles", [])

        df = self.normalize_candles(
            rows,
            symbol,
        )

        base = {
            "krx_date": date_naive,
            "symbol": symbol,
            "cutoff_kst": cutoff,
            "status": "ok",
            "n_api_rows": len(df),
        }

        if df.empty:
            return {
                **base,
                "status": "empty",
            }

        same_date = df[
            df["date_kst"] == date_naive
        ].copy()

        if same_date.empty:
            return {
                **base,
                "status": "no_same_kst_date",
            }

        inclusive = same_date[
            same_date["timestamp"] <= cutoff
        ]

        strict = same_date[
            same_date["timestamp"] < cutoff
        ]

        def snapshot_fields(
            x: pd.DataFrame,
            prefix: str,
        ) -> dict:
            if x.empty:
                return {
                    f"{prefix}_timestamp": pd.NaT,
                    f"{prefix}_close": np.nan,
                    f"{prefix}_staleness_min": np.nan,
                    f"{prefix}_usable": False,
                }

            row = x.iloc[-1]
            ts = row["timestamp"]
            close = float(row["close"])

            stale = (
                cutoff - ts
            ).total_seconds() / 60.0

            return {
                f"{prefix}_timestamp": ts,
                f"{prefix}_close": close,
                f"{prefix}_staleness_min": stale,
                f"{prefix}_usable": bool(
                    0 <= stale <= MAX_STALENESS_MIN
                ),
            }

        out = {
            **base,
            **snapshot_fields(
                inclusive,
                "le_cutoff",
            ),
            **snapshot_fields(
                strict,
                "lt_cutoff",
            ),
        }

        if not (
            out["le_cutoff_usable"]
            or out["lt_cutoff_usable"]
        ):
            out["status"] = "stale_or_missing"

        return out


# ============================================================
# Resume / sparse collection
# ============================================================

def load_existing_snapshot(
    path: Path,
) -> pd.DataFrame:
    if not RESUME or not path.exists():
        return pd.DataFrame()

    df = pd.read_csv(path)

    if "krx_date" not in df.columns:
        return pd.DataFrame()

    df["krx_date"] = (
        pd.to_datetime(
            df["krx_date"],
            errors="coerce",
        )
        .dt.normalize()
    )

    for c in [
        "cutoff_kst",
        "le_cutoff_timestamp",
        "lt_cutoff_timestamp",
    ]:
        if c in df.columns:
            df[c] = pd.to_datetime(
                df[c],
                errors="coerce",
            )

    return (
        df.dropna(subset=["krx_date"])
        .drop_duplicates(subset=["krx_date"], keep="last")
        .sort_values("krx_date")
        .reset_index(drop=True)
    )


def collect_sparse_symbol(
    client: TossClient,
    *,
    symbol: str,
    krx_dates: pd.DatetimeIndex,
) -> pd.DataFrame:
    path = (
        OUT_DIR
        / f"{symbol}_preus_1520_sparse.csv"
    )

    existing = load_existing_snapshot(
        path
    )

    done = set(
        existing["krx_date"]
        .dt.normalize()
        .tolist()
    ) if not existing.empty else set()

    pending = [
        d for d in krx_dates
        if pd.Timestamp(d).normalize()
        not in done
    ]

    print(
        f"[SPARSE {symbol}] "
        f"existing={len(done):,} "
        f"pending={len(pending):,}"
    )

    new_rows: list[dict] = []

    for i, d in enumerate(
        pending,
        start=1,
    ):
        try:
            row = client.fetch_cutoff_snapshot(
                symbol=symbol,
                krx_date=pd.Timestamp(d),
            )

        except Exception as exc:
            row = {
                "krx_date":
                    pd.Timestamp(d).normalize(),

                "symbol":
                    symbol,

                "status":
                    f"error:{type(exc).__name__}",

                "error_message":
                    str(exc)[:500],
            }

            print(
                f"[SPARSE {symbol} ERROR] "
                f"{pd.Timestamp(d).date()} | {exc}"
            )

        new_rows.append(row)

        if i <= 5 or i % 50 == 0:
            print(
                f"[SPARSE {symbol}] "
                f"{i:5d}/{len(pending):5d} | "
                f"{pd.Timestamp(d).date()} | "
                f"status={row.get('status')}"
            )

        if (
            i % CHECKPOINT_EVERY == 0
            or i == len(pending)
        ):
            new_df = pd.DataFrame(
                new_rows
            )

            combined = (
                pd.concat(
                    [existing, new_df],
                    ignore_index=True,
                )
                if not existing.empty
                else new_df
            )

            combined["krx_date"] = (
                pd.to_datetime(
                    combined["krx_date"],
                    errors="coerce",
                )
                .dt.normalize()
            )

            combined = (
                combined
                .dropna(subset=["krx_date"])
                .drop_duplicates(
                    subset=["krx_date"],
                    keep="last",
                )
                .sort_values("krx_date")
                .reset_index(drop=True)
            )

            save_csv(
                combined,
                path,
            )

    return load_existing_snapshot(
        path
    )


# ============================================================
# PREUS calculation
# ============================================================

def prepare_daily_for_asof(
    daily: pd.DataFrame,
    symbol: str,
) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()

    x = daily.copy()

    x["us_session_date"] = (
        x["timestamp"]
        .dt.tz_convert(KST)
        .dt.tz_localize(None)
        .dt.normalize()
    )

    return (
        x[
            [
                "us_session_date",
                "close",
            ]
        ]
        .dropna()
        .drop_duplicates(
            "us_session_date",
            keep="last",
        )
        .sort_values(
            "us_session_date"
        )
        .rename(
            columns={
                "close":
                    f"{symbol}_daily_close"
            }
        )
    )


def attach_previous_us_close_and_preus(
    snapshot: pd.DataFrame,
    daily: pd.DataFrame,
    symbol: str,
) -> pd.DataFrame:
    if snapshot.empty:
        return snapshot.copy()

    s = snapshot.copy()

    s["krx_date"] = (
        pd.to_datetime(
            s["krx_date"],
            errors="coerce",
        )
        .dt.normalize()
    )

    d = prepare_daily_for_asof(
        daily,
        symbol,
    )

    if d.empty:
        s[
            f"{symbol}_prev_us_close"
        ] = np.nan
        return s

    # 15:20 KST에는 같은 날짜 미국 정규장이 아직 열리지 않았으므로
    # 반드시 krx_date보다 이전인 가장 최근 US regular close 사용.
    merged = pd.merge_asof(
        s.sort_values("krx_date"),
        d.sort_values("us_session_date"),
        left_on="krx_date",
        right_on="us_session_date",
        direction="backward",
        allow_exact_matches=False,
    )

    prev_col = (
        f"{symbol}_prev_us_close"
    )

    merged = merged.rename(
        columns={
            f"{symbol}_daily_close":
                prev_col
        }
    )

    for mode in [
        "le_cutoff",
        "lt_cutoff",
    ]:
        price_col = (
            f"{mode}_close"
        )

        usable_col = (
            f"{mode}_usable"
        )

        out_col = (
            f"{symbol}_preus_{mode}"
        )

        price = pd.to_numeric(
            merged.get(price_col),
            errors="coerce",
        )

        prev = pd.to_numeric(
            merged.get(prev_col),
            errors="coerce",
        )

        usable = merged.get(
            usable_col,
            False,
        )

        if isinstance(usable, pd.Series):
            usable = (
                usable.astype(str)
                .str.lower()
                .map({
                    "true": True,
                    "false": False,
                })
                .fillna(False)
            )
        else:
            usable = False

        ret = np.log(
            price / prev
        )

        ret = ret.where(
            usable
            & price.gt(0)
            & prev.gt(0)
        )

        merged[out_col] = ret

    return merged


def merge_symbols(
    qqq: pd.DataFrame,
    spy: pd.DataFrame,
) -> pd.DataFrame:
    def prep(
        df: pd.DataFrame,
        symbol: str,
    ) -> pd.DataFrame:
        x = df.copy()

        rename = {}

        for c in x.columns:
            if c == "krx_date":
                continue

            if c.startswith(
                f"{symbol}_"
            ):
                continue

            rename[c] = (
                f"{symbol}_{c}"
            )

        return x.rename(
            columns=rename
        )

    q = prep(
        qqq,
        "QQQ",
    )

    s = prep(
        spy,
        "SPY",
    )

    return (
        q.merge(
            s,
            on="krx_date",
            how="outer",
        )
        .sort_values("krx_date")
        .reset_index(drop=True)
    )


# ============================================================
# Main
# ============================================================

def main() -> None:
    OUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    print("=" * 96)
    print("QQQ / SPY SPARSE PREUS HISTORY COLLECTOR")
    print("daily US close + 15:20 KST sparse overnight snapshot")
    print("=" * 96)

    krx_dates, calendar_source = (
        load_krx_decision_dates()
    )

    start_date = (
        krx_dates.min()
        - pd.Timedelta(
            days=DAILY_LOOKBACK_BUFFER_DAYS
        )
    ).normalize()

    end_date = (
        krx_dates.max()
        .normalize()
    )

    print(
        f"KRX dates : {krx_dates.min().date()} "
        f"-> {krx_dates.max().date()} | "
        f"n={len(krx_dates):,}"
    )

    print(
        f"Calendar  : {calendar_source}"
    )

    print(
        f"Daily pull: {start_date.date()} "
        f"-> {end_date.date()}"
    )

    print(
        f"Cutoff    : {PREUS_CUTOFF_HHMM} KST"
    )

    print(
        f"Max stale : {MAX_STALENESS_MIN:.0f} min"
    )

    client = TossClient()

    daily_map: dict[str, pd.DataFrame] = {}
    snapshot_map: dict[str, pd.DataFrame] = {}

    try:
        # 1) QQQ/SPY daily
        for label, symbol in SYMBOLS.items():
            print()
            print("-" * 96)
            print(f"[DAILY] {label}")
            print("-" * 96)

            daily = client.collect_stock_daily(
                symbol=symbol,
                start=start_date,
                end=end_date,
            )

            daily_map[label] = daily

            save_csv(
                daily,
                OUT_DIR
                / f"{label}_daily_toss.csv",
            )

        # 2) one sparse query per KRX day
        for label, symbol in SYMBOLS.items():
            print()
            print("-" * 96)
            print(f"[SPARSE PREUS] {label}")
            print("-" * 96)

            snap = collect_sparse_symbol(
                client,
                symbol=symbol,
                krx_dates=krx_dates,
            )

            snapshot_map[label] = snap

    finally:
        client.close()

    # 3) previous US close + PREUS
    enriched: dict[str, pd.DataFrame] = {}

    for label in SYMBOLS:
        x = attach_previous_us_close_and_preus(
            snapshot_map[label],
            daily_map[label],
            label,
        )

        enriched[label] = x

        save_csv(
            x,
            OUT_DIR
            / f"{label}_preus_1520_enriched.csv",
        )

        for mode in [
            "le_cutoff",
            "lt_cutoff",
        ]:
            c = (
                f"{label}_preus_{mode}"
            )

            valid = x[c].dropna()

            if len(valid):
                first_date = (
                    x.loc[
                        valid.index,
                        "krx_date",
                    ]
                    .min()
                    .date()
                )

                last_date = (
                    x.loc[
                        valid.index,
                        "krx_date",
                    ]
                    .max()
                    .date()
                )

                print(
                    f"[COVERAGE {label} {mode}] "
                    f"n={len(valid):,} | "
                    f"{first_date} -> {last_date}"
                )
            else:
                print(
                    f"[COVERAGE {label} {mode}] n=0"
                )

    merged = merge_symbols(
        enriched["QQQ"],
        enriched["SPY"],
    )

    save_csv(
        merged,
        OUT_DIR
        / "QQQ_SPY_preus_1520_merged.csv",
    )

    print()
    print("=" * 96)
    print("[DONE]")
    print("=" * 96)

    print(
        "Timestamp convention 확인 후 하나만 사용:"
    )

    print(
        "  *_preus_le_cutoff : timestamp <= 15:20"
    )

    print(
        "  *_preus_lt_cutoff : timestamp < 15:20"
    )


if __name__ == "__main__":
    main()
