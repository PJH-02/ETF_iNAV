#!/usr/bin/env python3
"""Standalone read-only Korean stock/ETF candle collector (Python >=3.10).

Dependencies: pip install requests pandas python-dotenv
No research_paths, VibeTrading, accounts or order API dependency.

Any Korean stock/ETF code is selected via --symbols; no ticker whitelist,
hedging assumptions, proxy mappings or instrument-specific calendar paths.
--interval 1d collects daily OHLCV only; 1m retains the closing snapshots.
Toss: TOSS_CLIENT_ID/TOSS_CLIENT_SECRET; Kiwoom: KIWOOM_APP_KEY/
KIWOOM_APP_SECRET (real KRX only). --env-file may be repeated.

Example from any cwd:
  python collect_us_etf_close_snapshots.py --symbols 069500 --interval 1d --start 2021-01-01
  python collect_us_etf_close_snapshots.py --symbols 133690,360750,069500 --interval 1d
  python collect_us_etf_close_snapshots.py --symbols 133690,360750 --interval 1m
  python collect_us_etf_close_snapshots.py --symbols 069500 --interval 1d --provider offline
  python collect_us_etf_close_snapshots.py --symbols 069500 --probe-date 2021-12-21 --provider kiwoom
  python collect_us_etf_close_snapshots.py --self-test

Toss end labels were checked against native Kiwoom start labels on 2026-10-02:
304940 30/30 and 219480 49/49 overlapping positive-volume OHLCV bars match
after -1 minute. --toss-label can override this convention if the feed changes.
The 15:19 START candle finishes at 15:20. Closing auction execution is 15:30,
not a 15:20 fill. This file collects observations; it does not simulate fills.

KIS FHKST03010230 officially retains at most one year, so it cannot supply
2021 backfill today and is not used as a long-history backend here.
Saved local 2021 records are imports, not evidence of current API retention.
Exact candle fields and as-of last-trade fields are separate. A zero-volume
vendor candle is never treated as a new trade. As-of prices carry an explicit
same-day last-trade time and age, and are not simulated execution prices.

Official specifications:
https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
https://openapi.kiwoom.com/guide/apiGuideContents/07/ka10080
https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock/inquire_time_dailychartprice
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
import requests
from dotenv import load_dotenv

KST = timezone(timedelta(hours=9))
CODE = Path(__file__).resolve().parent
STUDY = CODE.parent if CODE.name == "code" else CODE
DATA = STUDY / "data"
DEFAULT_OUT = DATA / "market_data"
FIELDS = ["open", "high", "low", "close", "volume"]
EMPTY_COLUMNS = ["timestamp", *FIELDS, "symbol", "source", "origin", "date"]


class CollectionError(RuntimeError):
    pass


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def save_csv(path, frame):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temp, index=False, encoding="utf-8-sig")
    temp.replace(path)


def load_environment(paths, provider):
    if paths:
        candidates = list(map(Path, paths))
        if not all(p.is_file() for p in candidates):
            raise CollectionError("Explicit env file missing")
    else:
        candidates = [Path.cwd() / ".env", CODE / ".env", STUDY / ".env", STUDY.parent / ".env"]
    for path in candidates:
        if path.is_file():
            load_dotenv(path, override=False)  # Never print/store values.


class Client:
    def __init__(self, provider, gap=.25, max_calls=10000):
        self.provider, self.gap, self.max_calls = provider, gap, max_calls
        self.session, self.token, self.last, self.calls = requests.Session(), None, 0.0, 0
        self.auth_refreshes = 0

    def request(self, method, url, **kwargs):
        renewed = False
        for attempt in range(6):
            if self.calls >= self.max_calls:
                raise CollectionError("CLIENT_REQUEST_CAP; resume with a higher --max-calls")
            time.sleep(max(0, self.gap - (time.monotonic() - self.last)))
            self.last, self.calls = time.monotonic(), self.calls + 1
            try:
                response = self.session.request(method, url, timeout=30, **kwargs)
                if response.status_code == 401 and self.token and not renewed and not url.endswith("/oauth2/token"):
                    self.authenticate(force=True)
                    self.auth_refreshes += 1
                    renewed = True
                    for name in kwargs.get("headers", {}):
                        if name.lower() == "authorization":
                            kwargs["headers"][name] = "Bearer " + self.token
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    time.sleep(min(2 ** attempt, 16))
                    continue
                if response.status_code >= 400:
                    raise CollectionError(f"{self.provider}: HTTP {response.status_code}")
                data = response.json()
                if str(data.get("return_code", 0)) in {"1700", "8005"}:
                    time.sleep(min(2 ** attempt, 16))
                    continue
                if str(data.get("return_code", 0)) != "0":
                    raise CollectionError(f"{self.provider}: API code {data.get('return_code')}")
                return data, response.headers
            except (requests.RequestException, ValueError):
                if attempt == 5:
                    raise CollectionError(f"{self.provider}: network/JSON retries exhausted") from None
                time.sleep(min(2 ** attempt, 16))
        raise CollectionError(f"{self.provider}: rate-limit retries exhausted")

    def authenticate(self, force=False):
        if self.token and not force:
            return
        self.token = None
        if self.provider == "toss":
            key, secret = os.getenv("TOSS_CLIENT_ID"), os.getenv("TOSS_CLIENT_SECRET")
            if not key or not secret:
                raise CollectionError("Missing TOSS_CLIENT_ID / TOSS_CLIENT_SECRET")
            data, _ = self.request("POST", "https://openapi.tossinvest.com/oauth2/token", data={
                "grant_type": "client_credentials", "client_id": key, "client_secret": secret})
            self.token = data.get("access_token") or data.get("accessToken")
        else:
            key, secret = os.getenv("KIWOOM_APP_KEY"), os.getenv("KIWOOM_APP_SECRET")
            if not key or not secret:
                raise CollectionError("Missing KIWOOM_APP_KEY / KIWOOM_APP_SECRET")
            if os.getenv("KIWOOM_USE_MOCK", "false").lower() in {"true", "1", "yes"}:
                raise CollectionError("Real-market KRX history required; mock mode rejected")
            data, _ = self.request("POST", "https://api.kiwoom.com/oauth2/token", json={
                "grant_type": "client_credentials", "appkey": key, "secretkey": secret})
            self.token = data.get("token")
        if not self.token:
            raise CollectionError(f"{self.provider}: token missing")

    def page(self, symbol, interval, day, cursor=""):
        self.authenticate()
        if self.provider == "toss":
            before = cursor or day + ("T15:21:00+09:00" if interval == "1m" else "T23:59:59+09:00")
            data, _ = self.request("GET", "https://openapi.tossinvest.com/api/v1/candles",
                headers={"Authorization": "Bearer " + self.token},
                params={"symbol": symbol, "interval": interval, "count": 200,
                        "before": before, "adjusted": "false"})
            result = data.get("result", {})
            return result.get("candles", []), result.get("nextBefore") or ""
        api_id = "ka10080" if interval == "1m" else "ka10081"
        body = {"stk_cd": symbol, "upd_stkpc_tp": "0", "base_dt": day.replace("-", "")}
        if interval == "1m":
            body["tic_scope"] = "1"
        data, headers = self.request("POST", "https://api.kiwoom.com/api/dostk/chart", json=body,
            headers={"authorization": "Bearer " + self.token, "api-id": api_id,
                     "Content-Type": "application/json;charset=UTF-8",
                     "cont-yn": "Y" if cursor else "N", "next-key": cursor})
        field = "stk_min_pole_chart_qry" if interval == "1m" else "stk_dt_pole_chart_qry"
        return data.get(field, []), headers.get("next-key", "") if headers.get("cont-yn") == "Y" else ""


def symbol_text(value):
    """Recover leading zeros from numeric CSV codes, without naming an ETF."""
    value = str(value).strip().upper()
    if re.fullmatch(r"[0-9]+\.0", value):
        value = value[:-2]
    if value.isdigit():
        value = value.zfill(6)
    return value


def parse_symbols(value):
    symbols = [s.strip().upper() for s in value.split(",")]
    if any(not re.fullmatch(r"[0-9A-Z]{6}", s) for s in symbols) or len(set(symbols)) != len(symbols):
        raise CollectionError("--symbols requires distinct 6-character Korean codes, retaining leading zeros")
    return symbols


def korean_timestamps(values, fmt=None):
    values = values.astype(str).str.strip()
    if fmt:
        return pd.to_datetime(values, format=fmt, errors="raise").dt.tz_localize("Asia/Seoul")
    aware = values.str.contains(r"(?:Z|[+-][0-9]{2}:?[0-9]{2})$", regex=True)
    result = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns, Asia/Seoul]")
    if aware.any():
        result.loc[aware] = pd.to_datetime(values.loc[aware], utc=True, format="mixed", errors="raise").dt.tz_convert("Asia/Seoul")
    if (~aware).any():
        result.loc[~aware] = pd.to_datetime(values.loc[~aware], format="mixed", errors="raise").dt.tz_localize("Asia/Seoul")
    return result


def normalize(rows, provider, symbol, interval="1m", origin="api"):
    """Accept broker payloads and normalized CSVs using the same OHLCV schema."""
    x = rows.copy() if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    if x.empty:
        return pd.DataFrame(columns=EMPTY_COLUMNS)
    symbol = symbol_text(symbol)
    canonical = set(FIELDS).issubset(x.columns) and ("timestamp" in x or "date" in x)
    native_kiwoom = not canonical and provider == "kiwoom"
    if native_kiwoom:
        key = "cntr_tm" if interval == "1m" else "dt"
        if key not in x:
            raise CollectionError(f"{symbol} {interval}: Kiwoom response missing {key}")
        x = x[x[key].fillna("").astype(str).str.len().eq(14 if interval == "1m" else 8)].copy()
        if x.empty:
            return pd.DataFrame(columns=EMPTY_COLUMNS)
        x["timestamp"] = korean_timestamps(x[key], "%Y%m%d%H%M%S" if interval == "1m" else "%Y%m%d")
        x = x.rename(columns={"cur_prc": "close", "open_pric": "open", "high_pric": "high", "low_pric": "low", "trde_qty": "volume"})
    else:
        if not canonical:
            x = x.rename(columns={"openPrice": "open", "highPrice": "high", "lowPrice": "low", "closePrice": "close"})
        if "timestamp" not in x:
            if interval != "1d" or "date" not in x:
                raise CollectionError(f"{symbol} {interval}: timestamp missing")
            x["timestamp"] = x["date"]
        x["timestamp"] = korean_timestamps(x.timestamp)
    missing = set(FIELDS).difference(x.columns)
    if missing:
        raise CollectionError(f"{symbol} {interval}: missing OHLCV fields {sorted(missing)}")
    if "symbol" in x and not x.symbol.dropna().map(symbol_text).eq(symbol).all():
        raise CollectionError(f"{symbol} {interval}: input contains another ticker; prices were not relabeled")
    for c in FIELDS:
        try:
            x[c] = pd.to_numeric(x[c].astype(str).str.replace(",", "", regex=False), errors="raise")
        except (ValueError, TypeError):
            raise CollectionError(f"{symbol} {provider} {interval}: nonnumeric {c}") from None
        if native_kiwoom:
            x[c] = x[c].abs()  # Chart signs encode price direction, not trade side.
    invalid = x[FIELDS].isna().any(axis=1) | x[FIELDS[:-1]].le(0).any(axis=1) | x.volume.lt(0)
    invalid |= x.high.lt(x[["open", "close", "low"]].max(axis=1)) | x.low.gt(x[["open", "close", "high"]].min(axis=1))
    if invalid.any():
        row = x.loc[invalid].iloc[0]
        raise CollectionError(f"{symbol} {provider} {interval}: invalid OHLCV at {row.timestamp.isoformat()}")
    if "source" not in x:
        x["source"] = provider
    else:
        x["source"] = x.source.fillna(provider)
    x["symbol"], x["origin"] = symbol, origin
    x["date"] = x.timestamp.dt.strftime("%Y-%m-%d")
    return x[EMPTY_COLUMNS].sort_values("timestamp").reset_index(drop=True)


def unique_bars(x):
    if x.empty:
        return x
    if x.groupby(["symbol", "timestamp"])[FIELDS].nunique().gt(1).any().any():
        raise CollectionError("Conflicting same-symbol/time bars; originals retained")
    return x.drop_duplicates(["symbol", "timestamp"]).sort_values("timestamp").reset_index(drop=True)


def collect_daily(client, symbol, start, end, out, refresh=False):
    path = out / f"{symbol}_daily_{client.provider}.csv"
    meta_path = path.with_suffix(".cache.json")
    lower = (pd.Timestamp(start) - pd.Timedelta(days=31)).strftime("%Y-%m-%d")
    if path.exists() and not refresh:
        x = normalize(pd.read_csv(path, dtype={"symbol":str}), client.provider, symbol, "1d", origin=str(path))
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        if (meta.get("symbol") == symbol and meta.get("start", "9999") <= start and meta.get("end", "") >= end) or (not x.empty and x.date.min() <= lower and x.date.max() >= end):
            return x
    parts, cursor, seen = [], "", set()
    for page in range(100):
        rows, nxt = client.page(symbol, "1d", end, cursor)
        save_json(out / "raw" / client.provider / symbol / f"daily_{start}_{end}" / f"page_{page + 1:03d}.json",
                  {"provider":client.provider, "symbol":symbol, "interval":"1d", "adjusted":False,
                   "candles":rows, "retrieved_at_utc":datetime.now(timezone.utc).isoformat()})
        x = normalize(rows, client.provider, symbol, "1d")
        if x.empty:
            break
        parts.append(x)
        if x.date.min() <= lower or not nxt:
            break
        if nxt == cursor or nxt in seen:
            raise CollectionError("Daily continuation cursor stalled")
        seen.add(nxt)
        cursor = nxt
    else:
        raise CollectionError("Daily CLIENT_PAGE_CAP")
    if not parts:
        raise CollectionError(f"Daily trading calendar unavailable for {symbol}")
    result = unique_bars(pd.concat(parts, ignore_index=True))
    result = result[result.date.between(lower, end)].copy()
    save_csv(path, result)
    save_json(meta_path, {"symbol":symbol, "provider":client.provider, "start":start, "end":end,
                          "actual_first":result.date.min(), "actual_last":result.date.max()})
    return result


def collect_window(client, symbol, day, out, refresh=False):
    path = out / "raw" / client.provider / symbol / (day + ".json")
    if path.exists() and not refresh:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("requested_day") != day or saved.get("symbol") != symbol or saved.get("provider") != client.provider:
            raise CollectionError("Raw cache identity mismatch")
        x = unique_bars(normalize(saved["candles"], client.provider, symbol, origin=str(path)))
        return target_window(x, day)
    rows_all, cursor, seen = [], "", set()
    for page in range(30):
        rows, nxt = client.page(symbol, "1m", day, cursor)
        x = normalize(rows, client.provider, symbol)
        rows_all.extend(rows)
        if x.empty or x.timestamp.min() <= pd.Timestamp(day + " 14:19", tz="Asia/Seoul") or not nxt:
            break
        if nxt == cursor or nxt in seen:
            raise CollectionError("Minute continuation cursor stalled")
        seen.add(nxt)
        cursor = nxt
    else:
        raise CollectionError("Minute CLIENT_PAGE_CAP")
    save_json(path, {"provider": client.provider, "symbol": symbol, "requested_day": day,
                     "interval": "1m", "adjusted": False, "pages": page + 1, "candles": rows_all,
                     "retrieved_at_utc": datetime.now(timezone.utc).isoformat()})
    x = unique_bars(normalize(rows_all, client.provider, symbol, origin=str(path)))
    # A holiday/retention response can return an older date. Never assign it to today.
    return target_window(x, day)


def target_window(x, day):
    if x.empty:
        return x
    return x[x.date.eq(day) & x.timestamp.dt.strftime("%H:%M").between("14:19", "15:21")].copy()


def reference_calendar(local_dir, explicit_path):
    """Use actual market dates, never assume weekdays were trading days."""
    if not explicit_path:
        return [], None
    path = Path(explicit_path).resolve()
    if not path.is_file():
        if explicit_path:
            raise CollectionError("Explicit calendar CSV missing")
        return [], None
    frame = pd.read_csv(path, usecols=["date"])
    dates = pd.to_datetime(frame.date, format="%Y-%m-%d", errors="raise").dt.strftime("%Y-%m-%d")
    return sorted(dates.dropna().unique()), {"path": str(path.resolve()), "sha256": sha(path), "role": "KRX_trading_dates_only_no_price_substitution"}


def parse_local_files(values, symbols):
    result = {}
    for value in values or []:
        symbol, separator, filename = value.partition("=")
        symbol = symbol.strip().upper()
        if not separator or symbol not in symbols or symbol in result:
            raise CollectionError("--local-file must be SYMBOL=PATH for a requested ticker, without duplicates")
        path = Path(filename).expanduser().resolve()
        if not path.is_file():
            raise CollectionError(f"{symbol}: explicit local CSV missing")
        result[symbol] = path
    return result


def local_history(symbol, directory, interval, explicit_path=None, provider="toss"):
    if explicit_path:
        paths = [explicit_path]
    elif interval == "1d":
        exact = directory / f"{symbol}_daily.csv"
        paths = [exact] if exact.is_file() else sorted(set(directory.glob(f"{symbol}_daily*.csv")) | set(directory.glob(f"*_{symbol}_daily*.csv")))
    else:
        paths = sorted(set(directory.glob(f"{symbol}*_1m.csv")) | set(directory.glob(f"*_{symbol}_1m*.csv")))
    parts, provenance = [], []
    for path in paths:
        frame = pd.read_csv(path, dtype={"symbol":str, "cntr_tm":str, "dt":str})
        source = "kiwoom" if "cntr_tm" in frame or "dt" in frame else provider
        parts.append(normalize(frame, source, symbol, interval, origin=str(path.resolve())))
        provenance.append({"path": str(path.resolve()), "sha256": sha(path), "role":f"imported_{interval}_history"})
    if not parts:
        return pd.DataFrame(columns=EMPTY_COLUMNS), []
    return unique_bars(pd.concat(parts, ignore_index=True)), provenance


def local_bars(symbol, directory, explicit_path=None, provider="toss"):
    x, provenance = local_history(symbol, directory, "1m", explicit_path, provider)
    if not x.empty:
        x = x[x.timestamp.dt.strftime("%H:%M").between("14:19", "15:21")].copy()
    return x, provenance


def write_manifest(args, out, symbols, inputs, collection, coverage, client, outputs):
    value = {"schema_version":2, "start_requested":args.start, "end_requested":args.end,
        "provider":args.provider, "interval":args.interval, "symbols":symbols, "local_inputs":inputs,
        "collection":collection, "coverage":coverage, "api_calls":client.calls if client else 0,
        "auth_refreshes":client.auth_refreshes if client else 0,
        "price_adjustment":"unadjusted", "timezone":"Asia/Seoul", "execution":"No fills simulated",
        "source_code_sha256":sha(__file__), "output_sha256":{p.name:sha(p) for p in outputs}}
    if args.interval == "1m":
        value.update({"toss_bar_label":args.toss_label, "kiwoom_bar_label":"start",
            "return_baseline":"Instrument open / previous KRX close",
            "missing_policy":"Exact prices never forward filled; asof fields identify earlier same-day trades and their age",
            "asof_policy":"Last positive-volume same-day bar available at cutoff, within 60 minutes; not a fill price"})
    else:
        value["missing_policy"] = "Actual daily OHLCV only; no artificial weekday rows or forward-filled prices"
    save_json(out / "manifest.json", value)
    print(json.dumps({"status":"COMPLETED_WITH_EXPLICIT_COVERAGE", "interval":args.interval,
                      "out":str(out), "coverage":coverage}, ensure_ascii=True))


def run_daily(args, out, local_dir, symbols, client, explicit):
    calendar, calendar_provenance = reference_calendar(local_dir, args.calendar)
    inputs = [calendar_provenance] if calendar_provenance else []
    parts, collection, outputs, coverage, panel = [], [], [], [], None
    for symbol in symbols:
        if client:
            daily = collect_daily(client, symbol, args.start, args.end, out, args.refresh)
        else:
            daily, provenance = local_history(symbol, local_dir, "1d", explicit.get(symbol), args.local_provider)
            if daily.empty:
                raise CollectionError(f"{symbol}: offline daily CSV missing or empty")
            inputs.extend(provenance)
        selected = daily[daily.date.between(args.start, args.end)].sort_values("date").copy()
        if selected.date.duplicated().any():
            raise CollectionError(f"{symbol}: conflicting daily timestamps on the same date")
        path = out / f"{symbol}_daily.csv"
        save_csv(path, selected)
        outputs.append(path)
        parts.append(selected)
        index = selected.set_index("date").drop(columns=["symbol"])
        index.columns = [symbol + "_" + c for c in index.columns]
        panel = index if panel is None else panel.join(index, how="outer")
        expected = [d for d in calendar if args.start <= d <= args.end]
        coverage.append({"symbol":symbol, "daily_rows":len(selected),
            "first_date":selected.date.min() if len(selected) else None,
            "last_date":selected.date.max() if len(selected) else None,
            "expected_calendar_days":len(expected) if calendar else None,
            "missing_calendar_days":len(set(expected).difference(selected.date)) if calendar else None})
        collection.append({"symbol":symbol, "interval":"1d", "calendar_reference_used":bool(calendar),
                           "daily_prices_first":daily.date.min(), "daily_prices_last":daily.date.max()})
    if not parts or all(x.empty for x in parts):
        raise CollectionError("No daily bars in the requested period")
    combined = pd.concat(parts, ignore_index=True).sort_values(["date","symbol"])
    panel["all_daily_available"] = panel[[s + "_close" for s in symbols]].notna().all(axis=1)
    for name, frame in [("daily.csv",combined), ("daily_panel.csv",panel.reset_index()), ("coverage.csv",pd.DataFrame(coverage))]:
        path = out / name
        save_csv(path, frame)
        outputs.append(path)
    write_manifest(args, out, symbols, inputs, collection, coverage, client, outputs)


def snapshot(x, day, cutoff, toss_label):
    deadline = pd.Timestamp(day + " " + cutoff, tz="Asia/Seoul")
    z = x[x.date.eq(day)].copy()
    result = {"status": "MISSING_EXACT_MINUTE", "usable": False, "price": None, "volume": None,
              "asof_status": "NO_TRADE_IN_COLLECTED_LOOKBACK", "asof_usable": False,
              "asof_price": None, "asof_last_trade_available_at": None,
              "asof_staleness_minutes": None, "asof_source": None, "asof_origin": None,
              "zero_volume_price_matches_last_trade": None}
    if z.empty:
        return result
    shift = z.source.map(lambda s: 1 if s == "kiwoom" or (s == "toss" and toss_label == "start") else 0)
    z["available_at"] = z.timestamp + pd.to_timedelta(shift, unit="min")
    # An explicit as-of observation is distinct from an exact-minute candle.
    # Only real same-day trades in a conventional 60-minute lookback qualify.
    recent = z[z.volume.gt(0) & z.available_at.le(deadline) & z.available_at.ge(deadline - pd.Timedelta(minutes=60))]
    if not recent.empty:
        last = recent.sort_values("available_at").iloc[-1]
        age = float((deadline - last.available_at).total_seconds() / 60)
        result.update({"asof_status": "TRADE_AT_CUTOFF" if age == 0 else "EARLIER_SAME_DAY_TRADE",
                       "asof_usable": True, "asof_price": float(last.close),
                       "asof_last_trade_available_at": last.available_at.isoformat(),
                       "asof_staleness_minutes": age, "asof_source": last.source, "asof_origin": last.origin})
    # Preserve exact target-minute observations, including explicit no-trade flags.
    exact = z[z.available_at.eq(deadline)]
    if exact.empty:
        return result
    row = exact.iloc[0]
    if len(exact) > 1 and exact[FIELDS].nunique().gt(1).any():
        raise CollectionError("Conflicting exact-cutoff observations")
    ok = bool(row.volume > 0)
    result.update({"status": "OK" if ok else "NO_TRADE_IN_TARGET_MINUTE", "usable": ok,
            "price": float(row.close), "volume": float(row.volume), "source": row.source,
            "origin": row.origin, "bar_label": row.timestamp.isoformat(),
            "bar_start": (deadline - pd.Timedelta(minutes=1)).isoformat(),
            "available_at": deadline.isoformat()})
    if not ok and result["asof_usable"]:
        result["zero_volume_price_matches_last_trade"] = bool(float(row.close) == result["asof_price"])
    return result


def run(args):
    symbols = parse_symbols(args.symbols)
    out = Path(args.out).resolve() if args.out else DEFAULT_OUT / args.provider / args.interval / "_".join(symbols)
    out = out.resolve()
    old_manifest = out / "manifest.json"
    if old_manifest.exists():
        previous = json.loads(old_manifest.read_text(encoding="utf-8"))
        if set(previous.get("symbols", [])) != set(symbols) or previous.get("provider") != args.provider or previous.get("interval", "1m") != args.interval:
            raise CollectionError("Output directory belongs to another ticker set/provider/interval; choose --out or use the automatic default")
    out.mkdir(parents=True, exist_ok=True)
    local_dir = Path(args.local_dir).resolve()
    explicit = parse_local_files(args.local_file, symbols)
    client = None
    if args.provider != "offline":
        load_environment(args.env_file, args.provider)
        client = Client(args.provider, args.request_gap, args.max_calls)
    if args.probe_date:
        for symbol in symbols:
            for day in args.probe_date:
                x = collect_window(client, symbol, day, out, args.refresh)
                print(json.dumps({"provider": args.provider, "symbol": symbol, "requested_day": day,
                                  "target_window_rows": len(x), "cutoff_1520": snapshot(x, day, "15:20", args.toss_label)}, ensure_ascii=True))
        return
    if args.interval == "1d":
        run_daily(args, out, local_dir, symbols, client, explicit)
        return
    calendar, calendar_provenance = reference_calendar(local_dir, args.calendar)
    records, inputs, collection = [], [], []
    if calendar_provenance:
        inputs.append(calendar_provenance)
    for symbol in symbols:
        local, provenance = local_bars(symbol, local_dir, explicit.get(symbol), args.local_provider)
        inputs.extend(provenance)
        if args.provider == "offline":
            daily, daily_provenance = local_history(symbol, local_dir, "1d", provider=args.local_provider)
            if daily.empty:
                raise CollectionError(f"Offline daily CSV missing for {symbol}")
            inputs.extend(daily_provenance)
        else:
            daily = collect_daily(client, symbol, args.start, args.end, out, args.refresh)
        daily = daily.sort_values("date").drop_duplicates("date")
        daily["prev_close"] = daily.close.shift(1)
        daily["prev_date"] = daily.date.shift(1)
        dates = sorted(set(calendar).union(daily.date))
        selected = pd.DataFrame({"date": dates})
        selected["expected_prev_date"] = selected.date.shift(1)
        selected = selected[selected.date.between(args.start, args.end)].merge(daily, on="date", how="left", validate="one_to_one")
        # A daily-data gap must not become a multi-day "previous close" return.
        selected.loc[selected.prev_date.ne(selected.expected_prev_date), "prev_close"] = float("nan")
        for number, row in enumerate(selected.itertuples(index=False), 1):
            day = row.date
            bars = local[local.date.eq(day)].copy()
            expected_labels = {"15:19", "15:20"} if args.toss_label == "end" else {"15:18", "15:19"}
            has_cutoff = not bars.empty and expected_labels.issubset(set(bars.timestamp.dt.strftime("%H:%M")))
            if client and (not has_cutoff or args.refresh):
                new = collect_window(client, symbol, day, out, args.refresh)
                if not bars.empty and args.provider == "toss":
                    bars = unique_bars(pd.concat([bars, new], ignore_index=True))
                else:
                    bars = new  # Keep providers separate; never silently mix quote conventions.
            result = {"date": day, "symbol": symbol,
                      "daily_open": row.open, "daily_close": row.close, "previous_krx_close": row.prev_close,
                      "daily_price_available": bool(pd.notna(row.open)), "daily_close_is_post_signal": True}
            for cutoff, suffix in [("15:19", "1519"), ("15:20", "1520")]:
                value = snapshot(bars, day, cutoff, args.toss_label)
                result.update({k + "_" + suffix: v for k,v in value.items()})
                price = value.get("price")
                result["return_from_krx_open_" + suffix] = price / row.open - 1 if price and pd.notna(row.open) else None
                result["return_from_previous_krx_close_" + suffix] = price / row.prev_close - 1 if price and pd.notna(row.prev_close) else None
                asof_price = value.get("asof_price")
                result["asof_return_from_krx_open_" + suffix] = asof_price / row.open - 1 if asof_price and pd.notna(row.open) else None
                result["asof_return_from_previous_krx_close_" + suffix] = asof_price / row.prev_close - 1 if asof_price and pd.notna(row.prev_close) else None
            records.append(result)
            # Checkpoint CSV is resumable; raw per-date cache avoids repeated API calls.
            if number % 50 == 0:
                save_csv(out / "snapshots.csv", pd.DataFrame(records))
                print(json.dumps({"symbol": symbol, "days_processed": number, "date": day, "api_calls": client.calls if client else 0}))
        collection.append({"symbol": symbol, "expected_calendar_days": len(selected), "provider": args.provider,
                           "first_calendar_day": selected.date.min() if len(selected) else None,
                           "last_calendar_day": selected.date.max() if len(selected) else None,
                           "daily_prices_first": daily.date.min(), "daily_prices_last": daily.date.max(),
                           "calendar_reference_used": bool(calendar_provenance),
                           "calendar_has_dates_before_start": any(d < args.start for d in dates)})
    x = pd.DataFrame(records)
    if x.empty:
        raise CollectionError("No requested trading-calendar days")
    save_csv(out / "snapshots.csv", x)
    panel = None
    coverage, outputs = [], [out / "snapshots.csv"]
    for symbol in symbols:
        part = x[x.symbol.eq(symbol)].copy()
        save_csv(out / f"{symbol}_snapshots.csv", part)
        outputs.append(out / f"{symbol}_snapshots.csv")
        index = part.set_index("date").drop(columns=["symbol"])
        index.columns = [symbol + "_" + c for c in index.columns]
        panel = index if panel is None else panel.join(index, how="outer")
        usable = part[part.usable_1520.eq(True)]
        observed = part.loc[part.price_1520.notna(), "date"]
        asof_usable = part[part.asof_usable_1520.eq(True)]
        coverage.append({"symbol": symbol, "calendar_days": len(part), "usable_1520_days": len(usable),
                         "first_observed_1520": observed.min() if len(observed) else None,
                         "first_usable_1520": usable.date.min() if len(usable) else None,
                         "last_usable_1520": usable.date.max() if len(usable) else None,
                         "asof_usable_1520_days": len(asof_usable),
                         "first_asof_usable_1520": asof_usable.date.min() if len(asof_usable) else None,
                         "missing_1520_days": int(part.status_1520.eq("MISSING_EXACT_MINUTE").sum()),
                         "no_trade_1520_days": int(part.status_1520.eq("NO_TRADE_IN_TARGET_MINUTE").sum())})
    panel["all_usable_1520"] = panel[[s + "_usable_1520" for s in symbols]].eq(True).all(axis=1)
    panel["all_asof_usable_1520"] = panel[[s + "_asof_usable_1520" for s in symbols]].eq(True).all(axis=1)
    save_csv(out / "proxy_panel.csv", panel.reset_index())
    save_csv(out / "coverage.csv", pd.DataFrame(coverage))
    outputs.extend([out / "proxy_panel.csv", out / "coverage.csv"])
    write_manifest(args, out, symbols, inputs, collection, coverage, client, outputs)


def self_test():
    import unittest
    class Checks(unittest.TestCase):
        def fixture(self, day="2021-12-20", volume=10, label="15:20"):
            return normalize([{"timestamp":day + "T" + label + ":00+09:00", "openPrice":"100", "highPrice":"102", "lowPrice":"99", "closePrice":"101", "volume":volume}], "toss", "304940")
        def test_end_label_is_available_at_cutoff(self):
            r = snapshot(self.fixture(), "2021-12-20", "15:20", "end")
            self.assertTrue(r["usable"])
            self.assertIn("15:19:00", r["bar_start"])
        def test_start_label_is_not_available_early(self):
            self.assertFalse(snapshot(self.fixture(), "2021-12-20", "15:20", "start")["usable"])
        def test_never_use_prior_day(self):
            self.assertEqual(snapshot(self.fixture(), "2021-12-21", "15:20", "end")["status"], "MISSING_EXACT_MINUTE")
        def test_zero_volume_is_explicit(self):
            self.assertEqual(snapshot(self.fixture(volume=0), "2021-12-20", "15:20", "end")["status"], "NO_TRADE_IN_TARGET_MINUTE")
        def test_no_auction_close_as_signal(self):
            self.assertFalse(snapshot(self.fixture(label="15:30"), "2021-12-20", "15:20", "end")["usable"])
        def test_conflicting_duplicates_rejected(self):
            x = self.fixture()
            y = x.copy()
            y["close"] = 102
            with self.assertRaises(CollectionError):
                unique_bars(pd.concat([x,y]))
        def test_empty_placeholder(self):
            self.assertTrue(normalize([{"cntr_tm":""}], "kiwoom", "304940").empty)
        def test_native_start_label(self):
            x = self.fixture(label="15:19")
            x["source"] = "kiwoom"
            self.assertTrue(snapshot(x, "2021-12-20", "15:20", "end")["usable"])
        def test_asof_last_trade_excludes_future_auction(self):
            x = pd.concat([self.fixture(label="15:18"), self.fixture(label="15:30")])
            result = snapshot(x, "2021-12-20", "15:20", "end")
            self.assertEqual(result["status"], "MISSING_EXACT_MINUTE")
            self.assertTrue(result["asof_usable"])
            self.assertEqual(result["asof_staleness_minutes"], 2)
        def test_asof_no_cross_day_substitution(self):
            self.assertFalse(snapshot(self.fixture(), "2021-12-21", "15:20", "end")["asof_usable"])
        def test_asof_reports_stale_zero_volume_bar(self):
            x = pd.concat([self.fixture(label="15:18"), self.fixture(volume=0)])
            result = snapshot(x, "2021-12-20", "15:20", "end")
            self.assertFalse(result["usable"])
            self.assertTrue(result["asof_usable"])
            self.assertEqual(result["asof_staleness_minutes"], 2)
            self.assertTrue(result["zero_volume_price_matches_last_trade"])
        def test_cached_holiday_has_no_prior_day_rows(self):
            import tempfile
            class FakeClient:
                provider = "toss"
                def page(self, *a, **k):
                    raise AssertionError("Cache must avoid a network call")
            with tempfile.TemporaryDirectory() as directory:
                out = Path(directory)
                rows = [{"timestamp":"2021-12-20T15:20:00+09:00", "openPrice":"100", "highPrice":"102", "lowPrice":"99", "closePrice":"101", "volume":10}]
                save_json(out / "raw/toss/304940/2021-12-21.json", {"provider":"toss", "symbol":"304940", "requested_day":"2021-12-21", "candles":rows})
                self.assertTrue(collect_window(FakeClient(), "304940", "2021-12-21", out).empty)
        def test_invalidated_token_renews_once(self):
            from unittest.mock import patch
            class Response:
                def __init__(self, code, value):
                    self.status_code, self.value, self.headers = code, value, {}
                def json(self):
                    return self.value
            class Session:
                def __init__(self):
                    self.calls = 0
                def request(self, method, url, **kwargs):
                    self.calls += 1
                    if self.calls == 1:
                        return Response(401, {})
                    if url.endswith("/oauth2/token"):
                        return Response(200, {"access_token":"new-token"})
                    if kwargs["headers"]["Authorization"] != "Bearer new-token":
                        raise AssertionError("Old token was reused")
                    return Response(200, {"result":{}})
            with patch.dict(os.environ, {"TOSS_CLIENT_ID":"test", "TOSS_CLIENT_SECRET":"test"}):
                client = Client("toss", gap=0)
                client.token, client.session = "old-token", Session()
                client.request("GET", "https://example.invalid/candles", headers={"Authorization":"Bearer old-token"})
                self.assertEqual(client.auth_refreshes, 1)
                self.assertEqual(client.session.calls, 3)
        def test_repeated_401_does_not_loop_authentication(self):
            from unittest.mock import patch
            class Response:
                headers = {}
                def __init__(self, auth):
                    self.status_code, self.auth = (200 if auth else 401), auth
                def json(self):
                    return {"access_token":"new-token"} if self.auth else {}
            class Session:
                def request(self, method, url, **kwargs):
                    return Response(url.endswith("/oauth2/token"))
            with patch.dict(os.environ, {"TOSS_CLIENT_ID":"test", "TOSS_CLIENT_SECRET":"test"}):
                client = Client("toss", gap=0)
                client.token, client.session = "old-token", Session()
                with self.assertRaises(CollectionError):
                    client.request("GET", "https://example.invalid/candles", headers={"Authorization":"Bearer old-token"})
                self.assertEqual(client.auth_refreshes, 1)
        def test_daily_any_ticker_and_leading_zero(self):
            for symbol in ["069500","133690","360750","0005A0"]:
                rows = [{"timestamp":"2021-01-04T00:00:00+09:00", "openPrice":"100", "highPrice":"102", "lowPrice":"99", "closePrice":"101", "volume":"10"}]
                result = normalize(rows, "toss", symbol, "1d")
                self.assertEqual(result.iloc[0].symbol, symbol)
                self.assertEqual(result.iloc[0].date, "2021-01-04")
        def test_canonical_daily_csv_without_timezone(self):
            rows = [{"date":"2021-01-04", "symbol":69500, "open":100,"high":102,"low":99,"close":101,"volume":10}]
            result = normalize(rows, "toss", "069500", "1d")
            self.assertEqual(result.iloc[0].timestamp.hour, 0)
            self.assertEqual(result.iloc[0].symbol, "069500")
        def test_native_and_cached_kiwoom_daily(self):
            rows = [{"dt":"20210104", "cur_prc":"-101", "open_pric":"-100", "high_pric":"-102", "low_pric":"-99", "trde_qty":"10"}]
            original = normalize(rows, "kiwoom", "069500", "1d")
            cached = normalize(original.to_dict("records"), "kiwoom", "069500", "1d")
            self.assertEqual(cached.iloc[0].close, 101)
            self.assertEqual(cached.iloc[0].source, "kiwoom")
        def test_ticker_mismatch_never_relabels_prices(self):
            x = self.fixture()
            with self.assertRaises(CollectionError):
                normalize(x, "toss", "069500", "1d")
        def test_cli_symbols_accept_spaced_codes(self):
            self.assertEqual(parse_symbols("069500, 133690, 360750"), ["069500","133690","360750"])
            self.assertEqual(parse_symbols("0005a0"), ["0005A0"])
            with self.assertRaises(CollectionError):
                parse_symbols("69500")
        def test_daily_only_skips_minute_requests_and_trims_buffer(self):
            import tempfile
            from types import SimpleNamespace
            from unittest.mock import patch
            class DailyClient:
                provider, auth_refreshes, calls = "toss", 0, 0
                def page(self, symbol, interval, day, cursor):
                    if interval != "1d":
                        raise AssertionError("Daily mode requested minute bars")
                    self.calls += 1
                    rows = [{"timestamp":d+"T00:00:00+09:00", "openPrice":"100", "highPrice":"102", "lowPrice":"99", "closePrice":"101", "volume":"10"} for d in ["2020-12-01","2021-01-04","2021-01-08"]]
                    return rows, ""
            with tempfile.TemporaryDirectory() as directory:
                out = Path(directory)
                args = SimpleNamespace(provider="toss",interval="1d",start="2021-01-01",end="2021-01-08",calendar=None,refresh=False)
                client = DailyClient()
                with patch("builtins.print"):
                    run_daily(args, out, out, ["069500","133690","360750"], client, {})
                data = pd.read_csv(out / "069500_daily.csv", dtype={"symbol":str})
                self.assertEqual(data.date.tolist(), ["2021-01-04","2021-01-08"])
                self.assertTrue(data.symbol.eq("069500").all())
                self.assertEqual(client.calls, 3)
                self.assertFalse((out / "snapshots.csv").exists())
                again = collect_daily(client, "069500", args.start, args.end, out)
                self.assertEqual(client.calls, 3)
                self.assertFalse(again.empty)
        def test_local_filename_is_not_ticker_specific(self):
            import tempfile
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "any_name.csv"
                save_csv(path, pd.DataFrame([{"date":"2021-01-04", "symbol":"069500", "open":100,"high":102,"low":99,"close":101,"volume":10}]))
                data, sources = local_history("069500", path.parent, "1d", path)
                self.assertEqual(len(data), 1)
                self.assertEqual(len(sources), 1)
        def test_new_minute_ticker_without_existing_local_csv(self):
            import tempfile
            from types import SimpleNamespace
            from unittest.mock import patch
            class FreshClient:
                provider, auth_refreshes, calls = "toss", 0, 0
                def page(self, symbol, interval, day, cursor=""):
                    self.calls += 1
                    times = ["2020-12-01T00:00:00+09:00","2021-01-04T00:00:00+09:00"] if interval == "1d" else ["2021-01-04T15:19:00+09:00","2021-01-04T15:20:00+09:00"]
                    return [{"timestamp":t,"openPrice":"100","highPrice":"102","lowPrice":"99","closePrice":"101","volume":"10"} for t in times], ""
            with tempfile.TemporaryDirectory() as directory:
                out = Path(directory)
                args = SimpleNamespace(symbols="069500",interval="1m",provider="toss",out=str(out),local_dir=str(out),
                    local_file=None,local_provider="toss",env_file=None,request_gap=0,max_calls=100,
                    probe_date=None,calendar=None,start="2021-01-04",end="2021-01-04",refresh=False,toss_label="end")
                client = FreshClient()
                with patch("builtins.print"), patch(__name__+".load_environment"), patch(__name__+".Client",return_value=client):
                    run(args)
                data = pd.read_csv(out / "069500_snapshots.csv",dtype={"symbol":str})
                self.assertEqual(client.calls, 2)
                self.assertTrue(data.usable_1520.all())
    result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(Checks))
    if not result.wasSuccessful():
        raise SystemExit(1)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", choices=["toss", "kiwoom", "offline"], default="toss")
    p.add_argument("--symbols", help="Comma-separated Korean stock/ETF codes, e.g. 069500,133690,360750")
    p.add_argument("--interval", choices=["1d", "1m"], default="1m", help="1d: daily OHLCV only; 1m: intraday snapshots")
    p.add_argument("--start", default="2021-01-01")
    p.add_argument("--end", default=(datetime.now(KST).date() - timedelta(days=1)).isoformat())
    p.add_argument("--out", help="Default: data/market_data/{provider}/{interval}/{tickers}")
    p.add_argument("--local-dir", default=str(DATA))
    p.add_argument("--calendar", help="Optional CSV with date column; dates only, no price substitution")
    p.add_argument("--local-file", action="append", help="Optional SYMBOL=CSV override for the selected interval; repeatable")
    p.add_argument("--local-provider", choices=["toss","kiwoom"], default="toss", help="Source for local CSVs without a source column")
    p.add_argument("--env-file", action="append")
    p.add_argument("--probe-date", action="append", help="Read-only dated minute probe; repeatable")
    p.add_argument("--request-gap", type=float, default=.25)
    p.add_argument("--max-calls", type=int, default=10000)
    p.add_argument("--toss-label", choices=["start", "end"], default="end")
    p.add_argument("--refresh", action="store_true", help="Requery cached API responses, preserve local originals")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    if args.self_test:
        self_test()
        return
    if not args.symbols:
        p.error("--symbols is required unless --self-test is used")
    try:
        for day in [args.start, args.end, *(args.probe_date or [])]:
            if datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") != day:
                raise ValueError("Date format")
    except ValueError:
        p.error("Dates must use YYYY-MM-DD")
    if args.start > args.end or args.request_gap < 0 or args.max_calls < 1:
        p.error("Invalid dates/request limits")
    if args.provider == "offline" and args.probe_date:
        p.error("--probe-date requires an API provider")
    if args.interval == "1d" and args.probe_date:
        p.error("--probe-date is a minute probe; use --interval 1m")
    try:
        run(args)
    except CollectionError as exc:
        raise SystemExit(str(exc)) from None
    except Exception as exc:
        # Unexpected response/parse failures: retain raw inputs, expose no payload.
        raise SystemExit(f"Collection failed: {type(exc).__name__}; inspect code/data locally") from None


if __name__ == "__main__":
    main()
