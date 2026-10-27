"""Independent reconciliation of generated ledgers; does not rerun selection."""
import json
from pathlib import Path
import numpy as np
import pandas as pd
from intraday_v2_backtest import accounting, digest, load_etf, ROOT, OUT, SPEC_PATH
from research_paths import recorded_code_provenance


def verify():
    manifest = json.loads((OUT / "run_manifest.json").read_text(encoding="utf-8"))
    spec = manifest["spec"]
    assert manifest["spec_sha256"] == digest(SPEC_PATH)
    provenance = {"intraday_v2_backtest.py": recorded_code_provenance(
        ROOT / "intraday_v2_backtest.py", manifest["source_code_sha256"])}
    for name, expected in manifest["dependency_code_sha256"].items():
        provenance[name] = recorded_code_provenance(ROOT / name, expected)
    for source in manifest["inputs"].values():
        assert digest(source["path"]) == source["sha256"]
    tape, _ = load_etf()
    tape = tape.set_index("timestamp")
    summary = pd.read_csv(OUT / "summary.csv")
    checks = []
    for row in summary.to_dict("records"):
        name, slip = row["strategy"], row["slippage_roundtrip_bp"]
        tr = pd.read_csv(OUT / f"{name}_slip{slip}_trades.csv")
        assert len(tr) == row["trades"]
        if tr.empty:
            continue
        entry, exit_ = pd.to_datetime(tr.entry_time), pd.to_datetime(tr.exit_time)
        assert (exit_ >= entry).all()
        assert (entry.dt.date == exit_.dt.date).all()
        assert (entry.iloc[1:].reset_index(drop=True) > exit_.iloc[:-1].reset_index(drop=True)).all()
        assert (tr.stop < tr.entry_price).all()
        assert (((tr.target / tr.entry_price - 1) * 10000) >= spec["minimum_target_bp"] - 1e-8).all()
        if name != "M_open_close":
            assert (entry - pd.to_datetime(tr.signal_time)).eq(pd.Timedelta(minutes=2)).all()
        for i, trade in tr.iterrows():
            e, z = tape.loc[entry.iloc[i]], tape.loc[exit_.iloc[i]]
            assert e.open == trade.entry_price and e.volume > 0
            expected = accounting(trade.entry_price, trade.exit_price, slip, spec["fees_roundtrip_bp"])
            for key in ["gross_bp", "net_bp", "entry_fill", "exit_fill", "price_gross_bp"]:
                assert np.isclose(trade[key], expected[key], rtol=0, atol=1e-7), (name, i, key)
            reason = trade.exit_reason
            if reason == "session_close":
                assert z.hm == "15:19" and trade.exit_price == z.open
            elif reason == "auction_close":
                assert z.hm == "15:30" and z.auction_verified and trade.exit_price == z.close
            elif reason == "stop_gap":
                assert z.open <= trade.stop and trade.exit_price == z.open
            elif reason.startswith("stop"):
                assert z.low <= trade.stop and trade.exit_price == trade.stop
            elif reason == "target_gap":
                assert z.open >= trade.target + spec["etf_tick_won"] and trade.exit_price == trade.target
            elif reason == "target":
                assert z.high >= trade.target + spec["etf_tick_won"] and trade.exit_price == trade.target
            else:
                raise AssertionError(reason)
        assert np.isclose(tr.net_bp.mean(), row["net_mean_bp"], atol=1e-9)
        assert np.isclose(tr.gross_bp.mean(), row["gross_mean_bp"], atol=1e-9)
        checks.append({"strategy": name, "slippage_roundtrip_bp": slip, "trades": len(tr), "status": "PASS"})
    models = pd.read_csv(OUT / "morning_close_models.csv")
    fitted = models[models.predicted_return.notna()]
    assert (fitted.training_max_date < fitted.date).all()
    value = {"status": "PASS", "runs": checks, "known_history": manifest["history_role"],
             "source_provenance": provenance, "backtest_rerun": False,
             "spec_sha256": manifest["spec_sha256"],
             "outputs_sha256": {p.name: digest(p) for p in OUT.glob("*.csv")}}
    (OUT / "verification_after_reorganization.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
    print(json.dumps({"status": "PASS", "runs": len(checks), "ledger_trades_checked": sum(x["trades"] for x in checks)}))


if __name__ == "__main__":
    verify()
