"""Read-only verification of recorded frozen v1 results; does not rerun OOS."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

import intraday_bracket_backtest as bt
from research_paths import resolve_input, recorded_code_provenance


def main():
    root = bt.OUT_ROOT
    spec = bt.load_spec()
    manifests = [json.loads((root / p / "run_manifest.json").read_text(encoding="utf-8"))
                 for p in ("is", "oos")]
    fields = ["spec_sha256", "spec_document_sha256", "code_sha256",
              "aligned_input_sha256", "execution_input_sha256"]
    for field in fields:
        assert manifests[0][field] == manifests[1][field], field
    current = {"spec_sha256": bt.sha256(bt.SPEC_PATH),
               "spec_document_sha256": bt.sha256(bt.SPEC_DOC),
               "aligned_input_sha256": bt.sha256(resolve_input(spec["input_aligned"])),
               "execution_input_sha256": bt.sha256(resolve_input(spec["input_execution"]))}
    assert all(current[k] == manifests[0][k] for k in current)
    provenance = recorded_code_provenance(Path(bt.__file__), manifests[0]["code_sha256"])
    audit = {"status": "passed", "unit_test_count_passed_before_is": 23,
             "unit_test_command": ".venv/Scripts/python.exe -m unittest discover -s intraday_research/code -p test_intraday_bracket_backtest.py -v",
             "original_is_oos_spec_code_and_input_hashes_identical": True,
             "source_provenance": provenance,
             "oos_rerun_during_verification": False, "hashes": current, "runs": []}
    for partition in ("is", "oos"):
        summary = pd.read_csv(root / partition / "summary.csv")
        signals = pd.read_csv(root / partition / "signals.csv")
        signals = signals.set_index("setup_id")
        for row in summary.itertuples(index=False):
            folder = root / partition / row.case / row.portfolio
            t = pd.read_csv(folder / "trades.csv")
            d = pd.read_csv(folder / "daily.csv")
            assert len(t) == row.trades
            assert d.trades.sum() == len(t)
            assert np.isclose(d.net_pnl.sum(), row.net_pnl)
            if len(t):
                assert np.isclose(t.net_pnl.sum(), row.net_pnl)
                for name in ("entry_time", "exit_time", "signal_available_at", "signal_bar_start", "level_frozen_at"):
                    t[name] = pd.to_datetime(t[name], utc=True)
                assert (t.entry_time >= t.signal_available_at).all()
                assert (t.level_frozen_at <= t.signal_available_at).all()
                assert (t.normalization_max_date < t.trade_date).all()
                assert (t.entry_time.dt.date == t.exit_time.dt.date).all()
                if len(t) > 1:
                    assert (t.entry_time.iloc[1:].reset_index(drop=True)
                            >= t.exit_time.iloc[:-1].reset_index(drop=True)).all()
                assert (t.entry_bar_volume > 0).all() and (t.exit_bar_volume > 0).all()
                assert (t.qty > 0).all()
                assert np.allclose(t.target, signals.loc[t.setup_id, "target"].to_numpy())
                assert np.allclose(t.stop, signals.loc[t.setup_id, "stop"].to_numpy())
                delay = spec["cases"][row.case]["entry_delay_bars"]
                assert (t.entry_time - t.signal_bar_start == pd.Timedelta(minutes=delay)).all()
                if spec["cases"][row.case]["cost_gate"]:
                    assert (t.entry_raw <= t.entry_ceiling + 1e-9).all()
                    assert (t.planned_break_even_p <= .5 + 1e-9).all()
            audit["runs"].append({"partition": partition, "case": row.case,
                                  "portfolio": row.portfolio, "trades": len(t),
                                  "timing_bracket_cash_volume_checks_passed": True})
    bt.json_write(root / "verification_after_reorganization.json", audit)
    print(f"Verified {len(audit['runs'])} recorded runs; original source archived, spec/inputs unchanged.")
    print("No OOS simulation was rerun.")


if __name__ == "__main__":
    main()
