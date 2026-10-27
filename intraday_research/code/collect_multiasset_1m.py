from __future__ import annotations

import os
import time
from calendar import monthcalendar, THURSDAY
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv


# ============================================================
# Configuration
# ============================================================

KST = "Asia/Seoul"

# ---------- Toss ----------
TOSS_BASE = "https://openapi.tossinvest.com"
TOSS_COUNT = 200
TOSS_REQ_GAP = 0.22
TOSS_ADJUSTED = False

# Toss will stop automatically when the API has no older data.
TOSS_HARD_START = pd.Timestamp("1990-01-01")

TOSS_STOCKS = {
    "QQQ": "QQQ",
    "SPY": "SPY",
    "KODEX200": "069500",
}

# Toss market-indicator API symbol.
TOSS_KOSPI_INDICATOR = "KOSPI"

# ---------- KIS ----------
KIS_BASE = "https://openapi.koreainvestment.com:9443"
KIS_REQ_GAP = 0.10

# Absolute safety bound only. The collector stops much earlier when
# the KIS API no longer returns historical 1-minute futures data.
KIS_FUTURES_HARD_START = pd.Timestamp("1996-01-01")
KIS_EMPTY_WEEKDAY_STOP = 20

# KOSPI200 futures regular-session close.
# Querying backward from this clock time lets us collect each date page-by-page.
KIS_FUTURES_END_TIME = "154500"
# KOSPI200 futures regular-session open. Keep cursors on valid minute boundaries.
KIS_FUTURES_START_TIME = "084500"

# ---------- Output ----------
END_DATE = pd.Timestamp.now(tz=KST).tz_localize(None).normalize()
from research_paths import DATA, ENV_PATH, resolve_input

OUT_DIR = DATA / "market_1m_data"

load_dotenv(dotenv_path=ENV_PATH)


# ============================================================
# Common helpers
# ============================================================

def as_kst_bounds(
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)

    start_kst = start_ts.tz_localize(KST)
    end_exclusive_kst = (end_ts + pd.Timedelta(days=1)).tz_localize(KST)
    return start_kst, end_exclusive_kst


def save_csv(df: pd.DataFrame, path: Path) -> None:
    # Route ETF/US shared inputs to their canonical overnight location.
    path = Path(path)
    if path.parent.resolve() == OUT_DIR.resolve():
        path = resolve_input(f"market_1m_data/{path.name}")
    if df.empty:
        print(f"[SKIP] empty -> {path.name}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")

    if "timestamp" in df.columns:
        print(
            f"[SAVE] {path.name}: {len(df):,} rows | "
            f"{df['timestamp'].min()} -> {df['timestamp'].max()}"
        )
    else:
        print(f"[SAVE] {path.name}: {len(df):,} rows")


def safe_numeric(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for col in columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


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
                f"확인한 .env 경로: {ENV_PATH}\n\n"
                ".env 예시:\n"
                "TOSS_CLIENT_ID=your_client_id\n"
                "TOSS_CLIENT_SECRET=your_client_secret"
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
        self.token = payload.get("access_token") or payload["accessToken"]

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
                headers={"Authorization": f"Bearer {self.token}"},
                params=params,
                timeout=30,
            )

            if response.status_code == 401 and attempt == 0:
                print("[TOSS 401] re-authenticate")
                self.auth()
                continue

            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 1))
                print(f"[TOSS 429] sleep {retry_after:.2f}s")
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

        raise RuntimeError(f"Toss request retry exhausted: {path}")

    @staticmethod
    def _normalize_candles(
        rows: list[dict],
        symbol: str,
        source_symbol: str,
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
            ["open", "high", "low", "close", "volume", "tradingValue"],
        )

        if "timestamp" not in df.columns:
            raise RuntimeError(
                f"Toss candle response has no timestamp. columns={df.columns.tolist()}"
            )

        df["timestamp"] = (
            pd.to_datetime(df["timestamp"], utc=True)
            .dt.tz_convert(KST)
        )
        df["date"] = df["timestamp"].dt.tz_localize(None).dt.normalize()

        df["symbol"] = symbol
        df["source_symbol"] = source_symbol
        df["source"] = "TOSS"

        wanted = [
            "timestamp",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "tradingValue",
            "currency",
            "symbol",
            "source_symbol",
            "source",
        ]
        columns = [c for c in wanted if c in df.columns]

        return (
            df[columns]
            .dropna(subset=["timestamp", "open", "close"])
            .drop_duplicates(subset=["timestamp"], keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

    def _collect_paginated_candles(
        self,
        *,
        path: str,
        base_params: dict,
        symbol: str,
        source_symbol: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        start_kst, end_exclusive_kst = as_kst_bounds(start, end)

        before = f"{end:%Y-%m-%d}T23:59:59+09:00"
        previous_before: str | None = None
        chunks: list[pd.DataFrame] = []
        page_no = 0

        while True:
            params = dict(base_params)
            params["before"] = before

            response = self.get(path, params)
            payload = response.json()

            result = payload.get("result", payload)
            rows = result.get("candles", [])
            next_before = result.get("nextBefore")

            df = self._normalize_candles(
                rows,
                symbol=symbol,
                source_symbol=source_symbol,
            )

            if df.empty:
                print(f"[TOSS {symbol}] STOP: empty page")
                break

            page_no += 1
            chunks.append(df)

            oldest = df["timestamp"].min()
            newest = df["timestamp"].max()

            print(
                f"[TOSS {symbol} PAGE {page_no:04d}] "
                f"{len(df):3d} rows | {oldest} -> {newest}"
            )

            if oldest <= start_kst:
                break

            if not next_before:
                print(f"[TOSS {symbol}] STOP: nextBefore is empty")
                break

            if next_before == before or next_before == previous_before:
                raise RuntimeError(
                    f"Toss pagination cursor did not advance: {next_before}"
                )

            previous_before = before
            before = next_before

        if not chunks:
            return pd.DataFrame()

        result_df = pd.concat(chunks, ignore_index=True)

        result_df = (
            result_df[
                (result_df["timestamp"] >= start_kst)
                & (result_df["timestamp"] < end_exclusive_kst)
            ]
            .drop_duplicates(subset=["timestamp"], keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

        return result_df

    def collect_stock_1m(
        self,
        *,
        symbol: str,
        label: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        return self._collect_paginated_candles(
            path="/api/v1/candles",
            base_params={
                "symbol": symbol,
                "interval": "1m",
                "count": TOSS_COUNT,
                "adjusted": str(TOSS_ADJUSTED).lower(),
            },
            symbol=label,
            source_symbol=symbol,
            start=start,
            end=end,
        )

    def collect_market_indicator_1m(
        self,
        *,
        indicator: str,
        label: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        return self._collect_paginated_candles(
            path=f"/api/v1/market-indicators/{indicator}/candles",
            base_params={
                "interval": "1m",
                "count": TOSS_COUNT,
            },
            symbol=label,
            source_symbol=indicator,
            start=start,
            end=end,
        )


# ============================================================
# KOSPI200 futures contract calendar
# ============================================================

OLD_YEAR_CODE = {
    1996: "6",
    1997: "7",
    1998: "8",
    1999: "9",
    2000: "0",
    2001: "1",
    2002: "2",
    2003: "3",
    2004: "4",
    2005: "5",
    2006: "A",
    2007: "B",
    2008: "C",
    2009: "D",
    2010: "E",
    2011: "F",
    2012: "G",
    2013: "H",
    2014: "J",
    2015: "K",
    2016: "L",
    2017: "M",
    2018: "N",
    2019: "P",
    2020: "Q",
    2021: "R",
    2022: "S",
    2023: "T",
    2024: "V",
    2025: "W",
}


def second_thursday(year: int, month: int) -> date:
    cal = monthcalendar(year, month)
    thursdays = [week[THURSDAY] for week in cal if week[THURSDAY] != 0]
    return date(year, month, thursdays[1])


def next_quarterly_month(year: int, month: int) -> tuple[int, int]:
    for q_month in (3, 6, 9, 12):
        if month <= q_month:
            return year, q_month
    return year + 1, 3


def kospi200_front_contract(
    trading_day: date,
) -> tuple[str, date]:
    """
    Front-month convention:
      - Use the expiring quarterly contract through its expiry day.
      - Roll to the next quarterly contract on the following day.
    """
    year, q_month = next_quarterly_month(
        trading_day.year,
        trading_day.month,
    )
    expiry = second_thursday(year, q_month)

    if trading_day > expiry:
        if q_month == 12:
            year += 1
            q_month = 3
        else:
            q_month += 3
        expiry = second_thursday(year, q_month)

    if year <= 2025:
        year_code = OLD_YEAR_CODE.get(year)
        if year_code is None:
            raise ValueError(
                f"Unsupported old KIS KOSPI200 futures year: {year}"
            )
        contract_code = f"101{year_code}{q_month:02d}"
    else:
        # KIS new code format from 2026:
        # A01 + one-digit year + two-digit month
        contract_code = f"A01{year % 10}{q_month:02d}"

    return contract_code, expiry


# ============================================================
# KIS client
# ============================================================

class KISClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.token: str | None = None

    def close(self) -> None:
        self.session.close()

    def auth(self) -> None:
        app_key = os.getenv("KIS_APP_KEY")
        app_secret = os.getenv("KIS_APP_SECRET")

        if not app_key or not app_secret:
            raise RuntimeError(
                "KIS_APP_KEY / KIS_APP_SECRET을 찾을 수 없습니다.\n"
                f"확인한 .env 경로: {ENV_PATH}\n\n"
                ".env 예시:\n"
                "KIS_APP_KEY=your_app_key\n"
                "KIS_APP_SECRET=your_app_secret"
            )

        response = self.session.post(
            f"{KIS_BASE}/oauth2/tokenP",
            headers={"content-type": "application/json"},
            json={
                "grant_type": "client_credentials",
                "appkey": app_key,
                "appsecret": app_secret,
            },
            timeout=20,
        )
        response.raise_for_status()

        payload = response.json()
        self.token = payload["access_token"]
        print(f"[KIS AUTH] app_key={app_key[:4]}...")

    def get(
        self,
        *,
        path: str,
        tr_id: str,
        params: dict,
        tr_cont: str = "",
    ) -> tuple[dict, requests.structures.CaseInsensitiveDict]:
        if not self.token:
            self.auth()

        app_key = os.getenv("KIS_APP_KEY")
        app_secret = os.getenv("KIS_APP_SECRET")
        assert app_key and app_secret

        for attempt in range(10):
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self.token}",
                "appkey": app_key,
                "appsecret": app_secret,
                "tr_id": tr_id,
                "custtype": "P",
            }
            if tr_cont:
                headers["tr_cont"] = tr_cont

            response = self.session.get(
                f"{KIS_BASE}{path}",
                headers=headers,
                params=params,
                timeout=30,
            )

            if response.status_code == 401 and attempt == 0:
                print("[KIS 401] re-authenticate")
                self.auth()
                continue

            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 1))
                print(f"[KIS 429] sleep {retry_after:.2f}s")
                time.sleep(retry_after)
                continue

            # KIS occasionally returns HTTP 5xx without a normal JSON body.
            # Retry transient failures, but preserve the request context so an
            # invalid cursor/parameter is immediately visible in the log.
            if 500 <= response.status_code < 600:
                body = response.text[:800].replace("\n", " ")
                print(
                    f"[KIS HTTP {response.status_code}] "
                    f"tr_id={tr_id} attempt={attempt + 1}/10 | "
                    f"params={params} | body={body!r}"
                )
                if attempt < 2:
                    time.sleep(0.75 * (attempt + 1))
                    continue
                response.raise_for_status()

            if response.status_code >= 400:
                body = response.text[:1200].replace("\n", " ")
                raise RuntimeError(
                    f"KIS HTTP error | status={response.status_code} | "
                    f"tr_id={tr_id} | params={params} | body={body}"
                )

            payload = response.json()

            msg_cd = str(payload.get("msg_cd", ""))
            msg1 = str(payload.get("msg1", ""))

            if msg_cd == "EGW00201" or "초당 거래건수" in msg1:
                sleep_sec = 1.0 + 0.25 * attempt
                print(
                    f"[KIS RATE LIMIT] {msg_cd} {msg1} "
                    f"-> sleep {sleep_sec:.2f}s"
                )
                time.sleep(sleep_sec)
                continue

            if str(payload.get("rt_cd", "0")) != "0":
                raise RuntimeError(
                    f"KIS API error | tr_id={tr_id} | "
                    f"msg_cd={msg_cd} | msg1={msg1}"
                )

            time.sleep(KIS_REQ_GAP)
            return payload, response.headers

        raise RuntimeError(f"KIS request retry exhausted: {path}")

    @staticmethod
    def _normalize_futures(
        rows: list[dict],
        *,
        contract_code: str,
        contract_expiry: date,
    ) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)

        required = {"stck_bsop_date", "stck_cntg_hour"}
        if not required.issubset(df.columns):
            return pd.DataFrame()

        day = df["stck_bsop_date"].astype(str)
        clock = df["stck_cntg_hour"].astype(str).str.zfill(6)

        timestamp = pd.to_datetime(
            day + clock,
            format="%Y%m%d%H%M%S",
            errors="coerce",
        )

        df["timestamp"] = timestamp.dt.tz_localize(KST)
        df["date"] = df["timestamp"].dt.tz_localize(None).dt.normalize()

        rename_map = {
            "futs_oprc": "open",
            "futs_hgpr": "high",
            "futs_lwpr": "low",
            "futs_prpr": "close",
            "cntg_vol": "volume",
            "acml_tr_pbmn": "trading_value",
        }
        df = df.rename(columns=rename_map)

        df = safe_numeric(
            df,
            [
                "open",
                "high",
                "low",
                "close",
                "volume",
                "trading_value",
            ],
        )

        df["symbol"] = "KOSPI200_FUT_FRONT"
        df["contract_code"] = contract_code
        df["contract_expiry"] = pd.Timestamp(contract_expiry)
        df["source"] = "KIS"

        wanted = [
            "timestamp",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trading_value",
            "symbol",
            "contract_code",
            "contract_expiry",
            "source",
        ]
        columns = [c for c in wanted if c in df.columns]

        return (
            df[columns]
            .dropna(subset=["timestamp", "open", "close"])
            .drop_duplicates(
                subset=["timestamp", "contract_code"],
                keep="last",
            )
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

    def fetch_futures_day(
        self,
        *,
        trading_day: date,
        contract_code: str,
        contract_expiry: date,
    ) -> pd.DataFrame:
        """
        Pull one trading day's 1-minute bars backward in 102-row pages.
        """
        target_date = trading_day.strftime("%Y%m%d")
        cursor = KIS_FUTURES_END_TIME
        chunks: list[pd.DataFrame] = []
        seen_cursors: set[str] = set()

        for page_no in range(1, 30):
            if cursor in seen_cursors:
                break
            seen_cursors.add(cursor)

            payload, _ = self.get(
                path=(
                    "/uapi/domestic-futureoption/v1/quotations/"
                    "inquire-time-fuopchartprice"
                ),
                tr_id="FHKIF03020200",
                params={
                    "FID_COND_MRKT_DIV_CODE": "F",
                    "FID_INPUT_ISCD": contract_code,
                    "FID_HOUR_CLS_CODE": "60",
                    "FID_PW_DATA_INCU_YN": "Y",
                    "FID_FAKE_TICK_INCU_YN": "N",
                    "FID_INPUT_DATE_1": target_date,
                    "FID_INPUT_HOUR_1": cursor,
                },
            )

            rows = payload.get("output2") or []
            if not rows:
                break

            raw = pd.DataFrame(rows)
            if "stck_bsop_date" not in raw.columns:
                break

            raw["stck_bsop_date"] = raw["stck_bsop_date"].astype(str)
            raw["stck_cntg_hour"] = (
                raw["stck_cntg_hour"].astype(str).str.zfill(6)
            )

            raw = raw[raw["stck_bsop_date"] == target_date].copy()
            if raw.empty:
                break

            day_df = self._normalize_futures(
                raw.to_dict("records"),
                contract_code=contract_code,
                contract_expiry=contract_expiry,
            )
            if day_df.empty:
                break

            chunks.append(day_df)

            oldest_clock = raw["stck_cntg_hour"].min()
            print(
                f"[KIS FUT {target_date} {contract_code} "
                f"PAGE {page_no:02d}] "
                f"{len(day_df):3d} rows | oldest={oldest_clock}"
            )

            # The endpoint is a minute-bar API.  KIS accepts clock cursors such
            # as 100000, but a second-level cursor such as 102959 can trigger
            # HTTP 500.  Always move backward by ONE FULL MINUTE and keep SS=00.
            if oldest_clock <= KIS_FUTURES_START_TIME:
                print(
                    f"[KIS FUT {target_date} {contract_code}] "
                    f"STOP: reached regular-session open "
                    f"({KIS_FUTURES_START_TIME})"
                )
                break

            oldest_dt = datetime.strptime(oldest_clock, "%H%M%S")
            oldest_dt = oldest_dt.replace(second=0, microsecond=0)
            next_cursor = (oldest_dt - timedelta(minutes=1)).strftime("%H%M%S")

            if next_cursor < KIS_FUTURES_START_TIME:
                next_cursor = KIS_FUTURES_START_TIME

            if next_cursor >= cursor:
                break

            cursor = next_cursor

        if not chunks:
            return pd.DataFrame()

        return (
            pd.concat(chunks, ignore_index=True)
            .drop_duplicates(
                subset=["timestamp", "contract_code"],
                keep="last",
            )
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

    def collect_front_month_futures(
        self,
        *,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        weekdays = pd.bdate_range(start=start, end=end)[::-1]

        chunks: list[pd.DataFrame] = []
        found_any = False
        empty_streak = 0

        for ts in weekdays:
            trading_day = ts.date()
            contract_code, expiry = kospi200_front_contract(trading_day)

            print(
                f"[KIS FUT DAY] {trading_day} "
                f"contract={contract_code} expiry={expiry}"
            )

            day_df = self.fetch_futures_day(
                trading_day=trading_day,
                contract_code=contract_code,
                contract_expiry=expiry,
            )

            if day_df.empty:
                if found_any:
                    empty_streak += 1
                    print(
                        f"[KIS FUT EMPTY] {trading_day} | "
                        f"streak={empty_streak}/{KIS_EMPTY_WEEKDAY_STOP}"
                    )
                    if empty_streak >= KIS_EMPTY_WEEKDAY_STOP:
                        print(
                            "[KIS FUT STOP] no older minute data "
                            "returned by API"
                        )
                        break
                continue

            found_any = True
            empty_streak = 0
            chunks.append(day_df)

        if not chunks:
            return pd.DataFrame()

        result = (
            pd.concat(chunks, ignore_index=True)
            .drop_duplicates(
                subset=["timestamp", "contract_code"],
                keep="last",
            )
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

        result["contract_changed"] = (
            result["contract_code"].ne(
                result["contract_code"].shift(1)
            )
        )

        return result

    @staticmethod
    def _normalize_kospi200_index(
        rows: list[dict],
    ) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)

        required = {"stck_bsop_date", "stck_cntg_hour"}
        if not required.issubset(df.columns):
            return pd.DataFrame()

        day = df["stck_bsop_date"].astype(str)
        clock = df["stck_cntg_hour"].astype(str).str.zfill(6)

        timestamp = pd.to_datetime(
            day + clock,
            format="%Y%m%d%H%M%S",
            errors="coerce",
        )

        df["timestamp"] = timestamp.dt.tz_localize(KST)
        df["date"] = df["timestamp"].dt.tz_localize(None).dt.normalize()

        df = df.rename(
            columns={
                "bstp_nmix_oprc": "open",
                "bstp_nmix_hgpr": "high",
                "bstp_nmix_lwpr": "low",
                "bstp_nmix_prpr": "close",
                "cntg_vol": "volume",
                "acml_tr_pbmn": "trading_value",
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
                "trading_value",
            ],
        )

        df["symbol"] = "KOSPI200"
        df["source_symbol"] = "2001"
        df["source"] = "KIS"

        wanted = [
            "timestamp",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trading_value",
            "symbol",
            "source_symbol",
            "source",
        ]
        columns = [c for c in wanted if c in df.columns]

        return (
            df[columns]
            .dropna(subset=["timestamp", "open", "close"])
            .drop_duplicates(subset=["timestamp"], keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

    def collect_kospi200_index_1m(
        self,
        *,
        end: pd.Timestamp,
        max_pages: int = 10000,
    ) -> pd.DataFrame:
        """
        KIS industry-index minute chart:
          market division U
          KOSPI200 industry code 2001
          FID_INPUT_HOUR_1=60 -> 1-minute bars

        The endpoint has no explicit date parameter, so we follow tr_cont
        until KIS stops returning older data.
        """
        chunks: list[pd.DataFrame] = []
        tr_cont = ""
        previous_marker: tuple | None = None

        for page_no in range(1, max_pages + 1):
            payload, headers = self.get(
                path=(
                    "/uapi/domestic-stock/v1/quotations/"
                    "inquire-time-indexchartprice"
                ),
                tr_id="FHKUP03500200",
                tr_cont=tr_cont,
                params={
                    "FID_COND_MRKT_DIV_CODE": "U",
                    "FID_ETC_CLS_CODE": "0",
                    "FID_INPUT_ISCD": "2001",
                    "FID_INPUT_HOUR_1": "60",
                    "FID_PW_DATA_INCU_YN": "Y",
                },
            )

            rows = payload.get("output2") or []
            if not rows:
                print("[KIS KOSPI200] STOP: empty page")
                break

            raw = pd.DataFrame(rows)
            if {
                "stck_bsop_date",
                "stck_cntg_hour",
            }.issubset(raw.columns):
                marker = (
                    str(raw.iloc[0]["stck_bsop_date"]),
                    str(raw.iloc[0]["stck_cntg_hour"]),
                    str(raw.iloc[-1]["stck_bsop_date"]),
                    str(raw.iloc[-1]["stck_cntg_hour"]),
                    len(raw),
                )
            else:
                marker = (len(raw),)

            if marker == previous_marker:
                print("[KIS KOSPI200] STOP: repeated page")
                break
            previous_marker = marker

            df = self._normalize_kospi200_index(rows)
            if df.empty:
                print("[KIS KOSPI200] STOP: normalization returned empty")
                break

            chunks.append(df)

            print(
                f"[KIS KOSPI200 PAGE {page_no:04d}] "
                f"{len(df):3d} rows | "
                f"{df['timestamp'].min()} -> {df['timestamp'].max()}"
            )

            server_tr_cont = str(
                headers.get("tr_cont", "")
            ).upper()

            if server_tr_cont not in {"M", "F"}:
                print(
                    "[KIS KOSPI200] STOP: "
                    f"tr_cont={server_tr_cont!r}. "
                    "KIS did not advertise a continuation page; "
                    "the index file therefore contains only the rows "
                    "returned by this request."
                )
                break

            tr_cont = "N"

        if not chunks:
            return pd.DataFrame()

        _, end_exclusive = as_kst_bounds(
            pd.Timestamp("1990-01-01"),
            end,
        )

        return (
            pd.concat(chunks, ignore_index=True)
            .loc[lambda x: x["timestamp"] < end_exclusive]
            .drop_duplicates(subset=["timestamp"], keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )


# ============================================================
# Main
# ============================================================

def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 88)
    print("KOSPI200 FRONT-MONTH FUTURES 1-MINUTE COLLECTOR")
    print("=" * 88)
    print(f"END_DATE : {END_DATE.date()}")
    print(f"OUT_DIR  : {OUT_DIR.resolve()}")
    print()

    kis = KISClient()

    try:
        print("\n" + "-" * 88)
        print("[KIS] KOSPI200 front-month futures 1m")
        print("-" * 88)

        futures = kis.collect_front_month_futures(
            start=KIS_FUTURES_HARD_START,
            end=END_DATE,
        )

        save_csv(
            futures,
            OUT_DIR / "KOSPI200_FUT_FRONT_1m_kis.csv",
        )

        if not futures.empty:
            rolls = (
                futures.groupby(
                    ["contract_code", "contract_expiry"],
                    as_index=False,
                )
                .agg(
                    first_timestamp=("timestamp", "min"),
                    last_timestamp=("timestamp", "max"),
                    rows=("timestamp", "size"),
                )
                .sort_values("first_timestamp")
                .reset_index(drop=True)
            )

            save_csv(
                rolls,
                OUT_DIR / "KOSPI200_FUT_FRONT_rolls.csv",
            )

    finally:
        kis.close()

    print("\n" + "=" * 88)
    print("[DONE]")
    print("=" * 88)


if __name__ == "__main__":
    main()
