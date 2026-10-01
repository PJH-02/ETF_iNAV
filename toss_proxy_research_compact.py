import os, time, shutil
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError as e:
    raise ImportError(
        "python-dotenv가 필요합니다. 먼저 실행하세요:\n"
        "pip install python-dotenv"
    ) from e

# Always load .env from the same directory as this script.
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

import numpy as np
import pandas as pd
import requests
# import statsmodels.api as sm

BASE = "https://openapi.tossinvest.com"
OUT = Path("toss_proxy_research")
YEARS, COUNT, REQ_GAP = 7, 200, 0.06

SYMBOLS = {"304940": "KR", "219480": "KR", "QQQ": "US", "SPY": "US"}
PAIRS = {"304940": "QQQ", "219480": "SPY"}
KR_ETFS = tuple(PAIRS)


def auth(s):
    client_id = os.getenv("TOSS_CLIENT_ID")
    client_secret = os.getenv("TOSS_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise RuntimeError(
            f"TOSS_CLIENT_ID / TOSS_CLIENT_SECRET을 찾을 수 없습니다.\n"
            f"확인한 .env 경로: {ENV_PATH}\n"
            ".env 예시:\n"
            "TOSS_CLIENT_ID=your_client_id\n"
            "TOSS_CLIENT_SECRET=your_client_secret"
        )

    print(f"[ENV] loaded: {ENV_PATH}")
    print(f"[ENV] TOSS_CLIENT_ID: {client_id[:4]}...")

    r = s.post(
        f"{BASE}/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=20,
    )
    r.raise_for_status()
    j = r.json()
    return j.get("access_token") or j["accessToken"]


def get(s, token, path, params):
    while True:
        r = s.get(
            f"{BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 1)))
            continue
        r.raise_for_status()
        time.sleep(REQ_GAP)
        return r


def page(s, token, symbol, interval, before=None):
    p = {"symbol": symbol, "interval": interval, "count": COUNT, "adjusted": "false"}
    if before:
        p["before"] = before
    r = get(s, token, "/api/v1/candles", p)
    j = r.json()["result"]

    if not getattr(page, "shown_limit", False):
        print("[RATE]", r.headers.get("X-RateLimit-Limit"), "TPS")
        page.shown_limit = True

    return j.get("candles", []), j.get("nextBefore")


def norm(rows, symbol, market):
    if not rows:
        return pd.DataFrame()

    x = pd.DataFrame(rows).rename(columns={
        "openPrice": "open", "highPrice": "high",
        "lowPrice": "low", "closePrice": "close",
    })

    for c in ["open", "high", "low", "close", "volume"]:
        x[c] = pd.to_numeric(x[c], errors="coerce")

    tz = "Asia/Seoul" if market == "KR" else "America/New_York"
    x["timestamp"] = pd.to_datetime(x["timestamp"], utc=True).dt.tz_convert(tz)
    x["date"] = x["timestamp"].dt.tz_localize(None).dt.normalize()
    x["symbol"] = symbol

    return x[[
        "timestamp", "date", "open", "high", "low", "close", "volume", "symbol"
    ]].dropna(subset=["timestamp", "open", "close"])


def daily(s, token, symbol, market, start, end):
    before = f"{end:%Y-%m-%d}T23:59:59" + ("+09:00" if market == "KR" else "-04:00")
    out = []

    while True:
        rows, nxt = page(s, token, symbol, "1d", before)
        x = norm(rows, symbol, market)
        if x.empty:
            break
        out.append(x)
        if x["date"].min() <= start or not nxt:
            break
        before = nxt

    x = pd.concat(out, ignore_index=True)
    x = (
        x[(x["date"] >= start) & (x["date"] <= end)]
        .drop_duplicates("date", keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )
    x["ret_oc"] = np.log(x["close"] / x["open"])
    return x


def close_minutes(s, token, symbol, start, end):
    before = f"{end:%Y-%m-%d}T15:00:00+09:00"
    out, pages = [], 0

    while True:
        rows, nxt = page(s, token, symbol, "1m", before)
        x = norm(rows, symbol, "KR")
        if x.empty:
            break

        pages += 1
        hhmm = x["timestamp"].dt.strftime("%H:%M")
        y = x[
            (x["date"] >= start) &
            (x["date"] <= end) &
            (hhmm >= "14:30") &
            (hhmm <= "15:00")
        ]
        if not y.empty:
            out.append(y)

        if pages % 100 == 0:
            print(f"[1m] {symbol}: pages={pages}, oldest={x['date'].min().date()}")

        if x["date"].min() <= start or not nxt:
            break
        before = nxt

    if not out:
        return pd.DataFrame()

    x = (
        pd.concat(out, ignore_index=True)
        .drop_duplicates("timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    print(
        f"[1m DONE] {symbol}: {x['date'].nunique()} dates, "
        f"{x['date'].min().date()} -> {x['date'].max().date()}"
    )
    return x


def late_features(x):
    if x.empty:
        return pd.DataFrame()

    rows = []
    for d, g in x.groupby("date"):
        g = g.sort_values("timestamp")
        rows.append({
            "date": d,
            "ret_1500_1530": np.log(g.iloc[-1]["close"] / g.iloc[0]["open"]),
            "rv_1500_1530": np.sqrt(np.nansum(np.log(g["close"]).diff() ** 2)),
            "volume_1500_1530": g["volume"].sum(),
            "n_1m": len(g),
        })
    return pd.DataFrame(rows)


# def regress(kr, us, late, label):
#     d = (
#         kr[["date", "ret_oc"]].rename(columns={"ret_oc": "kr_ret"})
#         .merge(us[["date", "ret_oc"]].rename(columns={"ret_oc": "us_ret"}), on="date")
#     )
#     if not late.empty:
#         d = d.merge(late, on="date", how="left")

#     rows = []

#     z = d.dropna(subset=["kr_ret", "us_ret"])
#     m = sm.OLS(z["us_ret"], sm.add_constant(z[["kr_ret"]])).fit(
#         cov_type="HAC", cov_kwds={"maxlags": 5}
#     )
#     rows.append({
#         "pair": label, "model": "daily", "n": len(z),
#         "beta_daily": m.params["kr_ret"], "p_daily": m.pvalues["kr_ret"],
#         "beta_late": np.nan, "p_late": np.nan, "r2": m.rsquared,
#     })

#     if "ret_1500_1530" in d:
#         z = d.dropna(subset=["kr_ret", "ret_1500_1530", "us_ret"])
#         if len(z) >= 20:
#             X = sm.add_constant(z[["kr_ret", "ret_1500_1530"]])
#             m = sm.OLS(z["us_ret"], X).fit(cov_type="HAC", cov_kwds={"maxlags": 5})
#             rows.append({
#                 "pair": label, "model": "daily+late30m", "n": len(z),
#                 "beta_daily": m.params["kr_ret"], "p_daily": m.pvalues["kr_ret"],
#                 "beta_late": m.params["ret_1500_1530"],
#                 "p_late": m.pvalues["ret_1500_1530"], "r2": m.rsquared,
#             })

#     return d, rows


def main():
    OUT.mkdir(exist_ok=True)
    end = pd.Timestamp.now("Asia/Seoul").tz_localize(None).normalize()# - pd.Timedelta(days=1)
    start = end - pd.DateOffset(years=YEARS)

    s = requests.Session()
    token = auth(s)

    # ds = {}
    # for sym, market in SYMBOLS.items():
    #     print("[DAILY]", sym)
    #     ds[sym] = daily(s, token, sym, market, start, end)
    #     ds[sym].to_csv(OUT / f"{sym}_daily.csv", index=False)

    mins, feats = {}, {}
    for sym in KR_ETFS:
        print("[1M]", sym)
        mins[sym] = close_minutes(s, token, sym, start, end)
        mins[sym].to_csv(OUT / f"{sym}_1430_1500_1m.csv", index=False)
        # feats[sym] = late_features(mins[sym])
        # feats[sym].to_csv(OUT / f"{sym}_late_features.csv", index=False)

    # summary = []
    # for kr, us in PAIRS.items():
    #     paired, rows = regress(ds[kr], ds[us], feats[kr], f"{kr}->{us}")
    #     paired.to_csv(OUT / f"{kr}_{us}_paired.csv", index=False)
    #     summary += rows

    # pd.DataFrame(summary).to_csv(OUT / "regression_summary.csv", index=False)
    # print(pd.DataFrame(summary).to_string(index=False))
    print("DONE:", shutil.make_archive("toss_proxy_research_results", "zip", OUT))


if __name__ == "__main__":
    main()