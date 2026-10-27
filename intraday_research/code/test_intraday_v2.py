import json
import unittest
import tempfile
from unittest.mock import patch
from pathlib import Path
import numpy as np
import pandas as pd
from collect_kiwoom_research import chart_rows
from prepare_intraday_v2_data import make_universe, investor_gate, aggregate_chart_ticks
from intraday_v2_backtest import (basic_features, prior_normalization, bracket_exit,
                                 simulate, accounting, demand_signals, normal_signals)
from intraday_v2_backtest import morning_signals

from research_paths import DOCS

SPEC = json.loads((DOCS / "intraday_v2_spec.json").read_text(encoding="utf-8"))


def tape(day="2026-10-01", count=15):
    t = pd.date_range(day + " 09:00", periods=count, freq="min", tz="Asia/Seoul")
    x = pd.DataFrame({"timestamp": t, "open": 10000., "high": 10005., "low": 9995., "close": 10000., "volume": 100})
    return basic_features(x)


def signals(x):
    return pd.DataFrame({"raw": False, "target": 10030., "stop": 9980., "invalidation": np.nan,
                         "strategy": "M_breakout"}, index=x.index)


class ExecutionTests(unittest.TestCase):
    def test_zero_volume_entry_is_rejected(self):
        x = tape()
        x.loc[2, "volume"] = 0
        s = signals(x)
        s.loc[0, "raw"] = True
        t, r = simulate(x, s, SPEC, 2)
        self.assertTrue(t.empty)
        self.assertEqual(len(r), 1)

    def test_late_position_exits_at_actual_auction_print(self):
        x = tape(count=3)
        x["timestamp"] = pd.to_datetime(["2026-10-01 15:00+09:00", "2026-10-01 15:19+09:00", "2026-10-01 15:30+09:00"])
        x["hm"] = ["15:00", "15:19", "15:30"]
        x.loc[2, "close"] = 9900
        s = signals(x)
        s["strategy"] = "M_open_close"
        s.loc[0, "raw"] = True
        t, _ = simulate(x, s, SPEC, 0)
        self.assertEqual(t.iloc[0].exit_price, 9900)
        self.assertEqual(t.iloc[0].exit_reason, "auction_close")
        self.assertEqual(t.iloc[0].holding_minutes, 30)
    def test_same_bar_is_stop_first(self):
        e = bracket_exit(10000, 10050, 9950, 9980, 10030, 5)
        self.assertEqual((e.price, e.reason), (9980, "stop_ambiguous"))

    def test_tp_touch_is_not_fill(self):
        self.assertIsNone(bracket_exit(10030, 10030, 10000, 9980, 10030, 5))
        self.assertEqual(bracket_exit(10000, 10035, 9990, 9980, 10030, 5).price, 10030)

    def test_adverse_stop_gap(self):
        self.assertEqual(bracket_exit(9950, 9960, 9940, 9980, 10030, 5).price, 9950)

    def test_fee_is_four_bp_and_friction_separate(self):
        self.assertAlmostEqual(accounting(10000, 10000, 0, 4)["net_bp"], -4)
        a = accounting(10000, 10000, 2, 4)
        self.assertAlmostEqual(a["gross_bp"], -1.9998, places=3)
        self.assertAlmostEqual(a["net_bp"], -5.9994, places=3)

    def test_entry_is_after_one_full_minute_delay(self):
        x = tape()
        s = signals(x)
        s.loc[0, "raw"] = True
        x.loc[3, "high"] = 10040
        tr, _ = simulate(x, s, SPEC, 2)
        self.assertEqual(pd.Timestamp(tr.iloc[0].entry_time), x.loc[2, "timestamp"])

    def test_missing_minute_cancels_entry(self):
        x = tape().drop(index=1).reset_index(drop=True)
        s = signals(x)
        s.loc[0, "raw"] = True
        tr, rejections = simulate(x, s, SPEC, 2)
        self.assertTrue(tr.empty)
        self.assertEqual(rejections.iloc[0].reason, "missing_delay_bars")

    def test_ongoing_setup_is_not_multiple_trades(self):
        x = tape()
        x["high"] = 10040
        s = signals(x)
        s["raw"] = True
        tr, _ = simulate(x, s, SPEC, 2)
        self.assertEqual(len(tr), 1)

    def test_rearmed_new_episode_can_trade(self):
        x = tape()
        x["high"] = 10040
        s = signals(x)
        s.loc[[0, 5], "raw"] = True
        tr, _ = simulate(x, s, SPEC, 2)
        self.assertEqual(len(tr), 2)

    def test_entry_bar_stop_is_live(self):
        x = tape()
        x.loc[2, "low"] = 9970
        s = signals(x)
        s.loc[0, "raw"] = True
        tr, _ = simulate(x, s, SPEC, 2)
        self.assertEqual(tr.iloc[0].exit_reason, "stop")
        self.assertEqual(tr.iloc[0].holding_minutes, 0)

    def test_target_gap_before_entry_rejected(self):
        x = tape()
        x.loc[2, "open"] = 10030
        s = signals(x)
        s.loc[0, "raw"] = True
        tr, r = simulate(x, s, SPEC, 2)
        self.assertTrue(tr.empty)
        self.assertEqual(len(r), 1)


class CausalityTests(unittest.TestCase):
    def test_morning_model_ignores_today_closing_label(self):
        records = []
        dates = pd.bdate_range("2024-01-01", periods=256).strftime("%Y-%m-%d")
        for i, day in enumerate(dates):
            price = 10000 + i
            hms = ["09:00", "09:29"] + pd.date_range(day + " 14:30", periods=30, freq="min").strftime("%H:%M").tolist() + ["15:00", "15:30"]
            for hm in hms:
                close = price * (1.005 if hm == "09:29" else 1.0008 if hm == "15:30" else 1)
                records.append({"timestamp": pd.Timestamp(day + " " + hm, tz="Asia/Seoul"), "date": day, "hm": hm,
                                "open": price, "low": price - 10, "high": max(price, close), "close": close,
                                "volume": 100, "auction_verified": True})
        x = pd.DataFrame(records)
        a, d = morning_signals(x, SPEC)
        x.loc[x.date.eq(dates[253]) & x.hm.eq("15:30"), "close"] *= .5
        b, e = morning_signals(x, SPEC)
        pd.testing.assert_frame_equal(a.loc[x.date.eq(dates[253])], b.loc[x.date.eq(dates[253])])
        row = d[d.date.eq(dates[253])].iloc[0]
        self.assertLess(row.training_max_date, row.date)
    def test_prior_box_does_not_include_signal_bar(self):
        x = tape()
        x.loc[5, "high"] = 11000
        f = basic_features(x)
        self.assertEqual(f.loc[5, "pre_high"], 10005)

    def test_rolling_does_not_cross_missing_minutes(self):
        f = basic_features(tape().drop(index=5).reset_index(drop=True))
        self.assertTrue(pd.isna(f.loc[6, "pre_high"]))

    def test_normalization_ignores_current_and_future_days(self):
        x = pd.concat([tape("2026-09-" + str(i).zfill(2)) for i in range(1, 24)], ignore_index=True)
        a = prior_normalization(x, 20)
        y = x.copy()
        y.loc[y.date.ge("2026-09-21"), ["volume", "range_bp", "pressure"]] *= 100
        b = prior_normalization(y, 20)
        pd.testing.assert_frame_equal(a.loc[a.date.eq("2026-09-21"), ["range_median", "volume_median", "pressure_q75"]],
                                      b.loc[b.date.eq("2026-09-21"), ["range_median", "volume_median", "pressure_q75"]])

    def test_market_model_cannot_train_on_today_labels(self):
        x = pd.concat([tape("2026-09-" + str(i).zfill(2), 20) for i in range(1, 24)], ignore_index=True)
        x["demand_return"], x["demand_pressure"], x["positive_count"] = .001, .1, 3
        x["forward5"] = .002
        a, audit = demand_signals(x, SPEC)
        x.loc[x.date.ge("2026-09-21"), "forward5"] = -1
        b, _ = demand_signals(x, SPEC)
        pd.testing.assert_frame_equal(a.loc[x.date.eq("2026-09-21")], b.loc[x.date.eq("2026-09-21")])
        self.assertLess(audit[0]["training_max_date"], audit[0]["date"])


class DataTests(unittest.TestCase):
    def test_tick_coverage_rejects_volume_mismatch(self):
        from audit_intraday_v2_ticks import audit_tick_batch
        x = tape(count=380)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            p = x[["timestamp", "volume"]].copy()
            p.to_parquet(root / "page.parquet")
            with patch("audit_intraday_v2_ticks.ROOT", root):
                a = audit_tick_batch({"raw_pages": ["page.parquet"]}, x, 1)
                self.assertTrue(a["qualified"])
                p.loc[50, "volume"] += 1
                p.to_parquet(root / "page.parquet")
                b = audit_tick_batch({"raw_pages": ["page.parquet"]}, x, 1)
                self.assertFalse(b["qualified"])
    def test_tick_batches_cannot_be_concatenated(self):
        x = pd.DataFrame({"batch_id": ["a", "b"]})
        with self.assertRaises(ValueError):
            aggregate_chart_ticks(x)

    def test_mult_price_second_is_unknown_flow(self):
        x = pd.DataFrame({"timestamp": pd.to_datetime(["2026-10-01 09:00:00+09:00", "2026-10-01 09:00:01+09:00", "2026-10-01 09:00:01+09:00"]),
                          "close": [10000., 10005., 10010.], "volume": [10, 10, 10], "batch_id": "a"})
        m = aggregate_chart_ticks(x)
        self.assertEqual(m.iloc[0].volume, 30)
        self.assertEqual(m.iloc[0].unknown_volume, 30)
        self.assertEqual(m.iloc[0].pressure, 0)
    def test_same_second_identical_trades_preserved_and_price_sign_absolute(self):
        r = {"cntr_tm": "20261001090001", "cur_prc": "-10000", "open_pric": "-10000", "high_pric": "-10000", "low_pric": "-10000", "trde_qty": "10"}
        x = chart_rows([r, r], "069500", "ticks", 0, "batch")
        self.assertEqual(len(x), 2)
        self.assertEqual(x.volume.sum(), 20)
        self.assertTrue(x.side_source.eq("unknown").all())

    def test_empty_api_row_rejected(self):
        r = {"cntr_tm": "", "cur_prc": "", "open_pric": "", "high_pric": "", "low_pric": "", "trde_qty": ""}
        with self.assertRaises(ValueError):
            chart_rows([r], "069500", "minutes", 0, "batch")

    def test_daily_universe_uses_previous_close_and_replaces_member(self):
        c = pd.DataFrame([{"date": d, "symbol": s, "market_cap": cap, "market": "KOSPI", "security_type": typ, "full_universe": True}
            for d, rows in [("2026-09-01", [("000001", 100, "ordinary"), ("000002", 90, "ordinary"), ("000003", 80, "ordinary"), ("000004", 70, "ordinary"), ("000005", 1000, "preferred")]),
                            ("2026-09-02", [("000001", 100, "ordinary"), ("000002", 90, "ordinary"), ("000003", 60, "ordinary"), ("000004", 110, "ordinary")])]
            for s, cap, typ in rows])
        u = make_universe(c, ["2026-09-01", "2026-09-02", "2026-09-03"])
        self.assertEqual(set(u[u.effective_date.eq("2026-09-02")].symbol), {"000001", "000002", "000003"})
        self.assertEqual(set(u[u.effective_date.eq("2026-09-03")].symbol), {"000004", "000001", "000002"})
        self.assertTrue((u.asof_date < u.effective_date).all())

    def test_partial_cap_universe_rejected(self):
        c = pd.DataFrame({"date": ["2026-09-01"] * 3, "symbol": ["1", "2", "3"], "market_cap": [3, 2, 1], "market": "KOSPI", "security_type": "ordinary", "full_universe": False})
        with self.assertRaises(ValueError):
            make_universe(c, ["2026-09-01", "2026-09-02"])

    def test_investor_gate_uses_prev_day_time_series_and_correct_sign(self):
        dates = pd.bdate_range("2025-01-01", periods=256).strftime("%Y-%m-%d").tolist()
        f = pd.DataFrame({"date": dates, "foreign_net_amount": np.arange(256), "institution_net_amount": -np.arange(256), "scope": "KOSPI200_SPOT"})
        g = investor_gate(f, dates).set_index("date")
        self.assertFalse(g.loc[dates[252], "usable"])
        self.assertTrue(g.loc[dates[253], "investor_gate"])
        self.assertEqual(g.loc[dates[253], "flow_asof_date"], dates[252])
        f.loc[253:, ["foreign_net_amount", "institution_net_amount"]] = 0
        h = investor_gate(f, dates).set_index("date")
        self.assertEqual(g.loc[dates[253], "investor_gate"], h.loc[dates[253], "investor_gate"])

    def test_wrong_market_investor_flow_rejected(self):
        f = pd.DataFrame({"date": ["2026-01-01"], "foreign_net_amount": [1], "institution_net_amount": [-1], "scope": ["KOSPI"]})
        with self.assertRaises(ValueError):
            investor_gate(f, ["2026-01-01"])


if __name__ == "__main__":
    unittest.main()
