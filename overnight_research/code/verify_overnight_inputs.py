"""Check relocated existing input loaders without generating strategy results."""
import importlib
import json
from pathlib import Path
import bt
import quantile_state_analysis as quantile
from research_paths import RESULTS


def verify():
    checks = []
    daily, path = bt.load_kodex200()
    checks.append({"loader": "bt.load_kodex200", "rows": len(daily), "path": str(path)})
    for symbol in bt.MINUTE_PARTS:
        frame, paths = bt.load_minute(symbol)
        checks.append({"loader": f"bt.load_minute({symbol})", "rows": len(frame), "paths": list(map(str, paths))})
    for symbol in ["069500", "304940", "219480", "QQQ", "SPY"]:
        frame = quantile.load_daily(symbol)
        assert not frame.empty
        checks.append({"loader": f"quantile.load_daily({symbol})", "rows": len(frame)})
    for symbol in ["304940", "219480"]:
        for fn in [quantile.load_late_features, quantile.load_1m]:
            frame = fn(symbol)
            assert not frame.empty
            checks.append({"loader": f"quantile.{fn.__name__}({symbol})", "rows": len(frame)})
    modules = [(importlib.import_module("test"), "load_research_dataset", "FOREIGN_XLSX_CANDIDATES"),
               (importlib.import_module("f1_f2_f3_minute_eda_aligned_causal"), "load_research", "FOREIGN_CANDIDATES")]
    for module, loader, candidates in modules:
        frame, path = getattr(module, loader)()
        assert not frame.empty
        assert any(Path(p).is_file() for p in getattr(module, candidates)), "Foreign flow input missing"
        checks.append({"loader": f"{module.__name__}.{loader}", "rows": len(frame), "path": str(path),
                       "foreign_input_exists": True})
    result = {"status": "PASS", "input_load_checks": checks, "backtests_rerun": False}
    (RESULTS / "input_verification_after_reorganization.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "PASS", "input_load_checks": len(checks), "backtests_rerun": False}))


if __name__ == "__main__":
    verify()
