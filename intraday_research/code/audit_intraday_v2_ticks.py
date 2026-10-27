"""Coverage gate compares tick volumes to independently checked minute bars."""
import json
from pathlib import Path
import pandas as pd

from research_paths import WORKSPACE, resolve_input

ROOT = WORKSPACE


def audit_tick_batch(manifest, etf, required_days=120):
    chunks = []
    for p in manifest.get("raw_pages", []):
        x = pd.read_parquet(resolve_input(ROOT / p), columns=["timestamp", "volume"])
        x["timestamp"] = x.timestamp.dt.floor("min")
        chunks.append(x.groupby("timestamp").volume.sum())
    if not chunks:
        return {"qualified": False, "complete_dates": [], "required_days": required_days, "days": []}
    volumes = pd.concat(chunks).groupby(level=0).sum()
    records, complete = [], []
    # T2/T3 trade the continuous session and liquidate before the auction.
    expected = etf[etf.hm.le("15:19")].copy()
    # Closing-auction volume in Toss's final candle can include other prints.
    # Compare native KRX tick volumes with Kiwoom's KRX chart, not that candle.
    minute_path = resolve_input(ROOT / "kiwoom_research_data/069500_1m_kiwoom.parquet")
    if minute_path.exists():
        minute = pd.read_parquet(minute_path, columns=["timestamp", "volume"]).set_index("timestamp").volume
        values = expected.timestamp.map(minute)
        expected.loc[values.notna(), "volume"] = values[values.notna()]
    earliest, latest = volumes.index.min().date().isoformat(), volumes.index.max().date().isoformat()
    for day, g in expected[expected.date.between(earliest, latest)].groupby("date"):
        if day >= "2026-10-07":
            continue  # Frozen run excludes current partial session.
        observed = volumes.reindex(g.timestamp).fillna(0).to_numpy()
        matches = observed == g.volume.to_numpy()
        has_session = len(g) == 380 and g.hm.eq("09:00").any() and g.hm.eq("15:19").any()
        ok = bool(matches.all() and has_session)
        records.append({"date": day, "expected_positive_volume_bars": len(g), "matched_bars": int(matches.sum()), "complete": ok})
        if ok:
            complete.append(day)
    return {"qualified": len(complete) >= required_days, "complete_dates": complete,
            "complete_common_days": len(complete), "required_days": required_days,
            "collection_status": manifest.get("status"), "session": "09:00..15:19 continuous; auction not traded by T2/T3",
            "side_source": "chart_tick_rule_proxy_only", "days": records}


def latest_manifest():
    paths = sorted(resolve_input(ROOT / "kiwoom_research_data/ticks/069500").glob("*/manifest.json"))
    return paths[-1] if paths else None


if __name__ == "__main__":
    from intraday_v2_backtest import load_etf
    x, _ = load_etf()
    p = latest_manifest()
    result = audit_tick_batch(json.loads(p.read_text(encoding="utf-8")), x) if p else {"qualified": False}
    resolve_input(ROOT / "kiwoom_research_data/tick_coverage_audit.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "days"}))
