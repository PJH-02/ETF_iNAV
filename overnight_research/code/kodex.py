import os
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

# ============================================================
# Configuration
# ============================================================
BASE = "https://openapi.tossinvest.com"
SYMBOL = "229200"  # KODEX 150
START_DATE = pd.Timestamp("2010-01-01")
END_DATE = pd.Timestamp("2026-09-28")
COUNT = 200
REQ_GAP = 0.06

from research_paths import DATA, ENV_PATH

OUT_DIR = DATA / "toss_kodex150_daily"
OUT_FILE = OUT_DIR / "KODEX150_229200_daily_20100101_20260928.csv"

# Load .env from the same directory as this script.
load_dotenv(dotenv_path=ENV_PATH)


def auth(session: requests.Session) -> str:
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

    print(f"[ENV] loaded: {ENV_PATH}")
    print(f"[ENV] TOSS_CLIENT_ID: {client_id[:4]}...")

    response = session.post(
        f"{BASE}/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json()
    return payload.get("access_token") or payload["accessToken"]


def api_get(
    session: requests.Session,
    token: str,
    path: str,
    params: dict,
) -> requests.Response:
    while True:
        response = session.get(
            f"{BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )

        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", 1))
            print(f"[429] rate limited -> sleep {retry_after:.2f}s")
            time.sleep(retry_after)
            continue

        response.raise_for_status()
        time.sleep(REQ_GAP)
        return response


def fetch_page(
    session: requests.Session,
    token: str,
    before: str | None = None,
):
    params = {
        "symbol": SYMBOL,
        "interval": "1d",
        "count": COUNT,
        "adjusted": "false",
    }

    if before is not None:
        params["before"] = before

    response = api_get(
        session,
        token,
        "/api/v1/candles",
        params,
    )

    result = response.json()["result"]

    if not getattr(fetch_page, "shown_limit", False):
        print("[RATE]", response.headers.get("X-RateLimit-Limit"), "TPS")
        fetch_page.shown_limit = True

    return result.get("candles", []), result.get("nextBefore")


def normalize(rows: list[dict]) -> pd.DataFrame:
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

    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Toss timestamp -> KST
    df["timestamp"] = (
        pd.to_datetime(df["timestamp"], utc=True)
        .dt.tz_convert("Asia/Seoul")
    )
    df["date"] = df["timestamp"].dt.tz_localize(None).dt.normalize()
    df["symbol"] = SYMBOL

    columns = [
        "timestamp",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "symbol",
    ]

    return (
        df[columns]
        .dropna(subset=["timestamp", "open", "close"])
        .reset_index(drop=True)
    )


def collect_daily(
    session: requests.Session,
    token: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    # Korean market timezone.
    before = f"{end:%Y-%m-%d}T23:59:59+09:00"

    chunks: list[pd.DataFrame] = []
    page_no = 0
    previous_before = None

    while True:
        rows, next_before = fetch_page(session, token, before)
        df = normalize(rows)

        if df.empty:
            print("[STOP] empty page")
            break

        page_no += 1
        chunks.append(df)

        oldest = df["date"].min()
        newest = df["date"].max()
        print(
            f"[PAGE {page_no:03d}] "
            f"rows={len(df):3d} | "
            f"{oldest.date()} -> {newest.date()}"
        )

        # Reached requested lower bound.
        if oldest <= start:
            break

        # No more historical data.
        if not next_before:
            print("[STOP] nextBefore is empty")
            break

        # Defensive guard against a broken/repeating cursor.
        if next_before == before or next_before == previous_before:
            raise RuntimeError(
                f"Pagination cursor did not advance: {next_before}"
            )

        previous_before = before
        before = next_before

    if not chunks:
        return pd.DataFrame()

    result = pd.concat(chunks, ignore_index=True)

    result = (
        result[
            (result["date"] >= start)
            & (result["date"] <= end)
        ]
        .drop_duplicates(subset=["date"], keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )

    return result


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("KODEX 150 DAILY COLLECTOR")
    print("=" * 72)
    print(f"symbol : {SYMBOL}")
    print(f"period : {START_DATE.date()} -> {END_DATE.date()}")
    print(f"output : {OUT_FILE}")

    with requests.Session() as session:
        token = auth(session)
        df = collect_daily(
            session=session,
            token=token,
            start=START_DATE,
            end=END_DATE,
        )

    if df.empty:
        raise RuntimeError("수집된 데이터가 없습니다.")

    df.to_csv(OUT_FILE, index=False, encoding="utf-8-sig")

    print("-" * 72)
    print(f"[DONE] rows  : {len(df):,}")
    print(f"[DONE] range : {df['date'].min().date()} -> {df['date'].max().date()}")
    print(f"[DONE] saved : {OUT_FILE.resolve()}")
    print("=" * 72)


if __name__ == "__main__":
    main()
