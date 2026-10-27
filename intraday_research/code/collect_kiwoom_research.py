"""Read-only historical data collection; reuse VibeTrading token/cache code.

No account or order endpoints. Raw pages retain identical same-second trades.
Research files are isolated from VibeTrading's live data pipeline.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import aiohttp
import pandas as pd

from research_paths import WORKSPACE, resolve_input

ROOT = WORKSPACE
OUT = resolve_input("kiwoom_research_data")
VIBE = ROOT.parent / "VibeTrading"
API = {
    "ticks": ("ka10079", "/api/dostk/chart", "stk_tic_chart_qry"),
    "minutes": ("ka10080", "/api/dostk/chart", "stk_min_pole_chart_qry"),
    "daily": ("ka10081", "/api/dostk/chart", "stk_dt_pole_chart_qry"),
    "investor": ("ka10051", "/api/dostk/sect", "inds_netprps"),
}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(6):
        try:
            tmp.replace(path)
            break
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.1 * (attempt + 1))


def auth_client():
    # Reuses the existing loader/auth/cache without printing settings or secrets.
    sys.path.insert(0, str(VIBE))
    from vibetrading.providers.kiwoom import KiwoomAuthClient
    client = KiwoomAuthClient()
    if client.use_mock:
        raise RuntimeError("Research requires real-market data, not mock data")
    return client


class ResearchClient:
    def __init__(self, auth, gap=0.25):
        self.auth, self.gap, self.last = auth, gap, 0.0

    async def page(self, session, kind, symbol, next_key="", base_date=None):
        api_id, path, _ = API[kind]
        if kind == "investor":
            body = {"mrkt_tp": "0", "amt_qty_tp": "0", "stex_tp": "1"}
        else:
            body = {"stk_cd": symbol, "tic_scope": "1", "upd_stkpc_tp": "0"}
        if kind == "daily":
            body.pop("tic_scope")
        if base_date and kind != "ticks":
            body["base_dt"] = base_date.replace("-", "")
        token = await self.auth.access_token()
        headers = {"Content-Type": "application/json;charset=UTF-8",
                   "authorization": f"Bearer {token}", "api-id": api_id,
                   "cont-yn": "Y" if next_key else "N", "next-key": next_key}
        for attempt in range(6):
            await asyncio.sleep(max(0, self.gap - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            try:
                async with session.post(self.auth.rest_url + path, json=body, headers=headers) as response:
                    data = await response.json(content_type=None)
                    if response.status == 429 or str(data.get("return_code")) in {"1700", "8005"}:
                        await asyncio.sleep(min(2 ** attempt, 16))
                        continue
                    if response.status >= 400 or str(data.get("return_code", 0)) != "0":
                        # Avoid echoing server messages, request headers, or credentials.
                        raise RuntimeError(f"{api_id}: HTTP {response.status}, code {data.get('return_code')}")
                    return data, response.headers.get("cont-yn", "N"), response.headers.get("next-key", "")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == 5:
                    raise RuntimeError(f"{api_id}: network retries exhausted") from None
                await asyncio.sleep(min(2 ** attempt, 16))
        raise RuntimeError(f"{api_id}: rate-limit retries exhausted")


def chart_rows(rows, symbol, kind, page_no, batch_id):
    x = pd.DataFrame(rows)
    if x.empty:
        return x
    if kind == "daily":
        x["timestamp"] = pd.to_datetime(x.dt, format="%Y%m%d", errors="raise").dt.tz_localize("Asia/Seoul")
    else:
        x["timestamp"] = pd.to_datetime(x.cntr_tm, format="%Y%m%d%H%M%S", errors="raise").dt.tz_localize("Asia/Seoul")
    if x.timestamp.isna().any():
        raise ValueError("Empty/invalid chart timestamp")
    for src, dst in {"cur_prc": "close", "open_pric": "open", "high_pric": "high",
                     "low_pric": "low", "trde_qty": "volume"}.items():
        x[dst] = pd.to_numeric(x[src].astype(str).str.replace(",", "", regex=False), errors="raise").abs()
    if x[["open", "high", "low", "close", "volume"]].isna().any().any() or (x.close <= 0).any():
        raise ValueError("Empty/invalid chart prices")
    x["symbol"], x["source"], x["date"] = symbol, f"kiwoom_{API[kind][0]}_KRX", x.timestamp.dt.strftime("%Y-%m-%d")
    x["page"], x["row_in_page"], x["batch_id"] = page_no, range(len(x)), batch_id
    x["side_source"] = "unknown"  # Chart price/volume signs are NOT aggressor signs.
    return x


async def collect_chart(client, session, kind, symbol, start, end, max_pages, resume_manifest=None):
    batch = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    dest = OUT / kind / symbol / batch
    key, fingerprints, times, start_page = "", set(), [], 0
    manifest = {"kind": kind, "symbol": symbol, "exchange": "KRX", "start_requested": start,
                "end_requested": end, "pages": 0, "rows": 0, "status": "RUNNING",
                "side_available": False, "timestamp_semantics": "unverified_bar_start" if kind == "minutes" else "second_precision",
                "raw_pages": [], "duplicate_policy": "preserve trades; reject repeated whole response pages"}
    if resume_manifest:
        dest = resolve_input(resume_manifest).resolve().parent
        if not dest.is_relative_to(OUT.resolve()):
            raise ValueError("Resume path must be inside research data directory")
        manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
        checkpoint = json.loads((dest / "checkpoint.json").read_text(encoding="utf-8"))
        if (checkpoint["kind"], checkpoint["symbol"], checkpoint["start"], checkpoint["end"]) != (kind, symbol, start, end):
            raise ValueError("Resume query must match original query exactly")
        key, start_page, batch = checkpoint["next_key"], checkpoint["next_page"], dest.name
        if start_page != manifest["pages"] or len(manifest["raw_pages"]) != start_page:
            raise ValueError("Incomplete checkpoint: restart as a new batch to preserve trade multiplicity")
        times = [manifest["earliest"], manifest["latest"]]
        manifest["status"] = "RUNNING"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        for page_no in range(start_page, start_page + max_pages):
            data, cont, new_key = await client.page(session, kind, symbol, key, end)
            rows = data.get(API[kind][2], [])
            date_key = "dt" if kind == "daily" else "cntr_tm"
            if rows and not any(row.get(date_key) for row in rows):
                manifest["status"] = "SERVER_EMPTY_PLACEHOLDER"
                break
            fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
            if fingerprint in fingerprints and rows:
                manifest["status"] = "REPEATED_PAGE_STOP"
                break
            fingerprints.add(fingerprint)
            x = chart_rows(rows, symbol, kind, page_no, batch)
            if x.empty:
                manifest["status"] = "SERVER_EMPTY"
                break
            path = dest / f"page_{page_no:05d}.parquet"
            x.to_parquet(path, index=False)
            manifest["raw_pages"].append(str(path.relative_to(ROOT)))
            manifest["pages"] += 1
            manifest["rows"] += len(x)
            times.extend([str(x.timestamp.min()), str(x.timestamp.max())])
            manifest["earliest"], manifest["latest"] = min(times), max(times)
            write_json(dest / "manifest.json", manifest)
            if x.date.min() < start:
                manifest["status"] = "TARGET_REACHED"
                break
            if cont != "Y" or not new_key:
                manifest["status"] = "SERVER_END"
                break
            if new_key == key:
                manifest["status"] = "CURSOR_STALLED"
                break
            key = new_key
            # Market-data continuation cursor, not an authentication token.
            write_json(dest / "checkpoint.json", {"next_key": key, "next_page": page_no + 1,
                       "kind": kind, "symbol": symbol, "start": start, "end": end})
        else:
            manifest["status"] = "CLIENT_PAGE_CAP"  # Not evidence of a server retention limit.
    except Exception as exc:
        manifest["status"] = "ERROR"
        manifest["error_type"] = type(exc).__name__
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        manifest["error_location"] = f"{Path(frame.filename).name}:{frame.lineno}"
        # Only our sanitized API errors are recorded.
        if isinstance(exc, RuntimeError) and str(exc).startswith(API[kind][0] + ":"):
            manifest["error"] = str(exc)
    write_json(dest / "manifest.json", manifest)
    if kind in {"minutes", "daily"} and manifest["raw_pages"] and manifest["status"] not in {"ERROR", "REPEATED_PAGE_STOP", "CURSOR_STALLED"}:
        x = pd.concat([pd.read_parquet(resolve_input(p)) for p in manifest["raw_pages"]], ignore_index=True)
        # Unique bar keys are valid; identical trade keys are not.
        counts = x.groupby(["symbol", "timestamp"])[["open", "high", "low", "close", "volume"]].nunique()
        if counts.gt(1).any().any():
            manifest["status"] = "CONFLICTING_BAR_VALUES"
            write_json(dest / "manifest.json", manifest)
        else:
            x = x.drop_duplicates(["symbol", "timestamp"]).sort_values("timestamp")
            x = x[x.date.between(start, end)]
            interval = "1m" if kind == "minutes" else "1d"
            canonical = OUT / f"{symbol}_{interval}_kiwoom.parquet"
            if canonical.exists():
                prior = pd.read_parquet(canonical)
                joined = prior.merge(x, on=["symbol", "timestamp"], suffixes=("_old", "_new"))
                fields = ["open", "high", "low", "close", "volume"]
                if any((joined[c + "_old"] != joined[c + "_new"]).any() for c in fields):
                    raise ValueError("Conflicting previously collected historical bars")
                x = pd.concat([prior, x]).drop_duplicates(["symbol", "timestamp"]).sort_values("timestamp")
            x.to_parquet(canonical, index=False)
    print(json.dumps({k: manifest.get(k) for k in ["kind", "symbol", "status", "pages", "rows", "earliest", "latest"]}, ensure_ascii=False), flush=True)
    return manifest


async def run(args):
    OUT.mkdir(exist_ok=True)
    auth = auth_client()
    client = ResearchClient(auth)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        manifests = []
        symbols = args.symbols.split(",")
        if args.universe:
            symbols = sorted(pd.read_csv(resolve_input(args.universe), dtype={"symbol": str}).symbol.unique())
        if args.resume_manifest and len(symbols) != 1:
            raise ValueError("Resume one symbol batch at a time")
        for symbol in symbols:
            manifests.append(await collect_chart(client, session, args.kind, symbol, args.start, args.end, args.max_pages, args.resume_manifest))
        write_json(OUT / f"latest_{args.kind}_collection.json", manifests)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["ticks", "minutes", "daily"], required=True)
    parser.add_argument("--symbols", default="069500")
    parser.add_argument("--start", default="2025-09-22")
    parser.add_argument("--end", default="2026-10-06")
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--resume-manifest")
    parser.add_argument("--universe", help="Collect union of historical members, not today's top3")
    asyncio.run(run(parser.parse_args()))
