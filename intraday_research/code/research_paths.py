"""Paths independent of cwd, plus explicit provenance for relocated research.

Legacy manifests remain immutable. Their old paths resolve through the move
inventory; code hashes refer to archived original source, guarded separately
by the hash of the current path-adjusted source. No market-data copies needed.
"""
from functools import lru_cache
import hashlib
import json
from pathlib import Path

CODE = Path(__file__).resolve().parent
STUDY = CODE.parent
WORKSPACE = STUDY.parent
DATA = STUDY / "data"
RESULTS = STUDY / "results"
DOCS = STUDY / "docs"
ENV_PATH = WORKSPACE / ".env"
INTRADAY = WORKSPACE / "intraday_research"
OVERNIGHT = WORKSPACE / "overnight_research"
LAYOUT = WORKSPACE / "_layout_operations/layout_manifest.json"


@lru_cache(maxsize=1)
def layout():
    return json.loads(LAYOUT.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def legacy_paths():
    return {r["from"]: r["to"] for r in layout()["files"]}


def resolve_input(path):
    """Resolve recorded legacy paths and new workspace paths without cwd rules.

    Explicit existing caller files and absolute paths outside this workspace
    (including temporary test fixtures) retain their usual meaning.
    """
    p = Path(path)
    if p.is_absolute():
        try:
            relative = p.relative_to(WORKSPACE).as_posix()
        except ValueError:
            return p
        if p.exists():
            return p
    else:
        relative = str(path).replace("\\", "/")
    if relative in legacy_paths():
        return WORKSPACE / legacy_paths()[relative]
    for move in layout()["moves"]:
        old = move["from"]
        if relative.startswith(old + "/"):
            return WORKSPACE / move["to"] / relative[len(old) + 1:]
    if relative.startswith(("intraday_research/", "overnight_research/")):
        return WORKSPACE / relative
    if relative.startswith("market_1m_data/"):
        name = relative.split("/", 1)[1]
        study = INTRADAY if name.startswith("KOSPI200_") else OVERNIGHT
        return study / "data/market_1m_data" / name
    if relative == "market_1m_data":
        return DATA / "market_1m_data"
    if relative.startswith("data/"):
        return OVERNIGHT / relative
    if relative.startswith("kiwoom_research_data/") or relative == "kiwoom_research_data":
        return INTRADAY / "data" / relative
    if relative in {"KRX 외국인 순매수.xlsx", "KRX 외국인 순매수(1).xlsx"}:
        return OVERNIGHT / "data/market_1m_data" / relative
    return p.resolve()


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def recorded_code_provenance(path, expected):
    """Verify original code AND separately guard today's relocated code bytes."""
    active = resolve_input(path)
    active_hash = file_digest(active)
    if active_hash == expected:
        return {"mode": "active_source", "active_sha256": active_hash}
    relative = active.relative_to(WORKSPACE).as_posix()
    record = next(r for r in layout()["files"] if r["to"] == relative)
    assert active_hash == record.get("current_sha256"), f"Relocated source changed: {active}"
    assert record["sha256"] == expected, f"Recorded source mismatch: {active}"
    archive = WORKSPACE / record["snapshot"]
    assert file_digest(archive) == expected, f"Original source snapshot changed: {archive}"
    return {"mode": "archived_original_and_guarded_relocated_source",
            "original_sha256": expected, "original_source": str(archive),
            "active_sha256": active_hash, "active_source": str(active)}
