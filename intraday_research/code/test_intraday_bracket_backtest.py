"""Behavioral tests of causal normalization and bracket execution, not fitted returns."""
import copy
import unittest

import numpy as np
import pandas as pd

import intraday_bracket_backtest as bt


def execution_fixture():
    times = pd.date_range("2026-01-05 14:55", "2026-01-05 15:19", freq="min", tz="Asia/Seoul")
    tape = pd.DataFrame({"bar_start": times, "bar_end": times + pd.Timedelta(minutes=1),
                         "trade_date": "2026-01-05", "open": 10000.0, "high": 10005.0,
                         "low": 9995.0, "close": 10000.0, "volume": 1000.0})
    joint = tape[["bar_start"]].copy()
    joint["signal_segment"] = 1
    joint["feature_eligible"] = True
    row = {"strategy": "A_continuation", "setup_id": "A:fixture",
           "setup_start": times[0] - pd.Timedelta(minutes=5), "level_frozen_at": times[0],
           "context_start": times[0] - pd.Timedelta(minutes=20), "signal_bar_start": times[0],
           "signal_available_at": times[1], "trade_date": "2026-01-05", "signal_segment": 1,
           "target": 10200.0, "stop": 9900.0,
           "entry_ceiling": bt.entry_ceiling(10200, 9900, 6), "signal_etf_close": 10000.0,
           "normalization_max_date": "2026-01-02", "impulse_etf_krw": 200.0, "shock_fut_bp": -5.0}
    signals = pd.DataFrame([row], columns=bt.SIGNAL_COLUMNS)
    spec = bt.load_spec()
    case = copy.deepcopy(spec["cases"]["primary"])
    return signals, joint, tape, spec, case


def signal_fixture(kind="A"):
    times = pd.date_range("2026-01-05 09:00", periods=35, freq="min", tz="Asia/Seoul")
    rows = []
    for i, t in enumerate(times):
        if kind == "A":
            if i < 15:
                eo, ec, fo, fc = 10000 + i * 5, 10005 + i * 5, 500 + i * .1, 500.1 + i * .1
                eh, el, fh, fl, v = ec, eo, fc + .01, fo - .01, 1000
            elif i < 20:
                eo = ec = 10075
                fo = fc = 501.5
                eh, el, fh, fl, v = 10080, 10070, 501.52, 501.48, 1000
            else:
                eo, ec, fo, fc = 10080, 10090, 501.5, 501.6
                eh, el, fh, fl, v = 10095, 10075, 501.61, 501.49, 1000
        else:
            if i < 15:
                eo = ec = 10000
                fo = fc = 500.0
                eh, el, fh, fl, v = 10005, 9995, 500.01, 499.99, 1000
            elif i < 20:
                k = i - 15
                eo, ec = 10000 - k * 20, 9980 - k * 20
                fo, fc = 500 - k * .2, 499.8 - k * .2
                eh, el, fh, fl, v = eo, ec - 5, fo + .01, fc - .01, 1000
            else:
                eo, ec, fo, fc = 9905, 9930, 499.0, 499.3
                eh, el, fh, fl, v = 9935, 9900, 499.31, 499.0, 50
        rows.append({"bar_start": t, "bar_end": t + pd.Timedelta(minutes=1),
                     "trade_date": t.strftime("%Y-%m-%d"), "contract_code": "fixture",
                     "fut_open": fo, "fut_high": fh, "fut_low": fl, "fut_close": fc,
                     "fut_volume": v, "etf_open": eo, "etf_high": eh, "etf_low": el,
                     "etf_close": ec, "etf_volume": 1000})
    x = bt.add_features(pd.DataFrame(rows), bt.load_spec())
    constants = {"n_fvol1_med": 500, "n_fvol5_med": 3000, "n_frange5_med": 10,
                 "n_fret5_q20": -5, "n_fvol15_med": 10000,
                 "n_fabs15_med": 5, "n_frange15_med": 10}
    for c, value in constants.items():
        x[c] = float(value)
    x["normalization_max_date"] = "2026-01-02"
    return x


class BracketRules(unittest.TestCase):
    def test_adverse_gap_uses_open_not_stop(self):
        r = bt.resolve_exit(95, 97, 94, 98, 110, "stop_first")
        self.assertEqual((r.price, r.reason, r.at_open), (95, "stop_gap", True))

    def test_favorable_gap_has_no_free_price_improvement(self):
        r = bt.resolve_exit(112, 115, 111, 98, 110, "stop_first")
        self.assertEqual((r.price, r.reason), (110, "target_gap"))

    def test_open_gap_precedes_unordered_intrabar_extremes(self):
        r = bt.resolve_exit(112, 115, 90, 98, 110, "stop_first")
        self.assertEqual(r.reason, "target_gap")

    def test_double_touch_primary_stops_first(self):
        r = bt.resolve_exit(100, 112, 97, 98, 110, "stop_first")
        self.assertEqual(r.price, 98)
        self.assertTrue(r.ambiguous)
        self.assertFalse(r.at_open)

    def test_double_touch_diagnostic_targets_first(self):
        r = bt.resolve_exit(100, 112, 97, 98, 110, "target_first")
        self.assertEqual(r.price, 110)
        self.assertTrue(r.ambiguous)

    def test_neither_touch_remains_open(self):
        self.assertIsNone(bt.resolve_exit(100, 105, 99, 98, 110, "stop_first"))

    def test_commissions_apply_to_both_notionals(self):
        a = bt.trade_accounting(10000, 10000, 100, 1, 2)
        expected = 100 * (9999 - 10001) - 100 * 10001 * .0002 - 100 * 9999 * .0002
        self.assertAlmostEqual(a["net_pnl"], expected)
        self.assertAlmostEqual(a["net_pnl"], -600)
        self.assertEqual(a["gross_bp"], 0)

    def test_entry_ceiling_is_cost_adjusted_payoff_identity(self):
        p = bt.entry_ceiling(50150, 49945, 6)
        g, loss = (50150 / p - 1) * 1e4, (1 - 49945 / p) * 1e4
        self.assertAlmostEqual((g - loss) / 2 - 6, 0, places=8)


class ExecutionRules(unittest.TestCase):
    def run_fixture(self, signals, joint, tape, spec, case):
        return bt.simulate(signals, joint, tape, ["2026-01-05"],
                           ["A_continuation", "B_shock_recovery"], case, spec)

    def test_entry_and_intrabar_double_touch_are_causal(self):
        s, x, e, spec, case = execution_fixture()
        e.loc[2, ["high", "low"]] = [10300, 9800]
        t, marks, rejects = self.run_fixture(s, x, e, spec, case)
        self.assertEqual(len(t), 1)
        self.assertEqual(t.iloc[0].exit_reason, "stop_ambiguous")
        self.assertEqual(t.iloc[0].exit_time, e.iloc[2].bar_end)
        self.assertEqual(t.iloc[0].entry_time, e.iloc[2].bar_start)
        self.assertEqual(t.iloc[0].exit_raw, 9900)

    def test_cannot_reenter_same_open_after_intrabar_exit(self):
        s, x, e, spec, case = execution_fixture()
        s2 = s.copy()
        s2["strategy"], s2["setup_id"] = "B_shock_recovery", "B:fixture"
        s = pd.concat([s, s2], ignore_index=True)
        e.loc[2, ["high", "low"]] = [10300, 9800]
        t, _, rejected = self.run_fixture(s, x, e, spec, case)
        self.assertEqual(len(t), 1)
        self.assertEqual(rejected["position_busy_at_open"], 1)

    def test_tp_or_sl_during_wait_cancels_order(self):
        s, x, e, spec, case = execution_fixture()
        e.loc[1, "low"] = 9800
        t, _, rejected = self.run_fixture(s, x, e, spec, case)
        self.assertEqual(len(t), 0)
        self.assertEqual(rejected["bracket_touched_before_arrival"], 1)

    def test_daily_flat_is_calendar_management_not_holding_cut(self):
        s, x, e, spec, case = execution_fixture()
        t, _, _ = self.run_fixture(s, x, e, spec, case)
        self.assertEqual(t.iloc[0].exit_reason, "daily_flat")
        self.assertEqual(t.iloc[0].exit_time.strftime("%H:%M"), "15:15")
        self.assertEqual(t.iloc[0].holding_minutes, 18)

    def test_zero_volume_has_no_fictitious_stop_fill(self):
        s, x, e, spec, case = execution_fixture()
        e.loc[3, ["volume", "high", "low"]] = [0, 12000, 8000]
        t, _, _ = self.run_fixture(s, x, e, spec, case)
        self.assertEqual(t.iloc[0].exit_reason, "daily_flat")
        self.assertEqual(t.iloc[0].held_zero_volume_bars, 1)

    def test_zero_volume_entry_is_unfilled(self):
        s, x, e, spec, case = execution_fixture()
        e.loc[2, "volume"] = 0
        t, _, rejected = self.run_fixture(s, x, e, spec, case)
        self.assertEqual(len(t), 0)
        self.assertEqual(rejected["no_etf_trade_at_entry"], 1)

    def test_futures_gap_does_not_remove_etf_stop(self):
        s, x, e, spec, case = execution_fixture()
        x = x.drop(index=3)
        e.loc[3, "low"] = 9800
        t, _, _ = self.run_fixture(s, x, e, spec, case)
        self.assertEqual(t.iloc[0].exit_reason, "stop")
        self.assertEqual(t.iloc[0].exit_bar_start, e.iloc[3].bar_start)

    def test_missing_execution_path_is_flagged_and_not_backfilled(self):
        s, x, e, spec, case = execution_fixture()
        e = e.drop(index=3).reset_index(drop=True)
        t, _, _ = self.run_fixture(s, x, e, spec, case)
        self.assertEqual(t.iloc[0].exit_reason, "unobserved_execution_gap")
        self.assertTrue(t.iloc[0].execution_gap_unknown)
        self.assertEqual(t.iloc[0].exit_time, e.iloc[3].bar_start)

    def test_gap_stop_executes_worse_than_planned_stop(self):
        s, x, e, spec, case = execution_fixture()
        e.loc[3, ["open", "high", "low", "close"]] = [9700, 9705, 9695, 9700]
        t, _, _ = self.run_fixture(s, x, e, spec, case)
        self.assertEqual(t.iloc[0].exit_reason, "stop_gap")
        self.assertEqual(t.iloc[0].exit_raw, 9700)


class SignalCausality(unittest.TestCase):
    def test_continuation_target_and_stop_use_past_setup(self):
        x, spec = signal_fixture("A"), bt.load_spec()
        s = bt.build_signals(x, ["2026-01-05"], spec)
        a = s[s.strategy.eq("A_continuation")]
        self.assertGreater(len(a), 0)
        self.assertEqual(a.iloc[0].target, 10155)
        self.assertEqual(a.iloc[0].stop, 10065)
        self.assertLessEqual(a.iloc[0].level_frozen_at, a.iloc[0].signal_available_at)

    def test_recovery_target_is_pre_shock_median(self):
        x, spec = signal_fixture("B"), bt.load_spec()
        s = bt.build_signals(x, ["2026-01-05"], spec)
        b = s[s.strategy.eq("B_shock_recovery")]
        self.assertGreater(len(b), 0)
        self.assertEqual(b.iloc[0].target, 10000)
        self.assertEqual(b.iloc[0].stop, 9890)

    def test_future_bar_changes_do_not_rewrite_earlier_signals(self):
        x, spec = signal_fixture("A"), bt.load_spec()
        before = bt.build_signals(x, ["2026-01-05"], spec)
        mutated = x.copy()
        mutated.loc[25:, ["fut_close", "etf_close"]] *= 2
        after = bt.build_signals(mutated, ["2026-01-05"], spec)
        cutoff = x.iloc[24].bar_end
        pd.testing.assert_frame_equal(before[before.signal_available_at <= cutoff].reset_index(drop=True),
                                      after[after.signal_available_at <= cutoff].reset_index(drop=True))

    def test_current_day_not_used_for_normalization(self):
        spec = bt.load_spec()
        frames = []
        for date in pd.bdate_range("2025-09-22", periods=61):
            part = signal_fixture("A")
            times = pd.date_range(date.strftime("%Y-%m-%d") + " 09:00", periods=len(part), freq="min", tz="Asia/Seoul")
            part["bar_start"], part["bar_end"] = times, times + pd.Timedelta(minutes=1)
            part["trade_date"] = date.strftime("%Y-%m-%d")
            frames.append(part)
        x = pd.concat(frames, ignore_index=True)
        current = x.trade_date.iloc[-1]
        original, _ = bt.add_prior_day_normalization(x, spec)
        x.loc[x.trade_date.eq(current), ["fut_volume", "f_volume5", "f_ret5_bp"]] *= 100
        changed, _ = bt.add_prior_day_normalization(x, spec)
        cols = [v[0] for v in bt.NORM_SPECS.values()]
        pd.testing.assert_frame_equal(original.loc[original.trade_date.eq(current), cols],
                                      changed.loc[changed.trade_date.eq(current), cols])
        self.assertTrue((changed.loc[changed.trade_date.eq(current), "normalization_max_date"] < current).all())

    def test_rolling_features_do_not_cross_missing_minute(self):
        x = signal_fixture("A").drop(index=10).reset_index(drop=True)
        x = bt.add_features(x, bt.load_spec())
        after_gap = x[x.bar_start.dt.strftime("%H:%M").eq("09:11")].iloc[0]
        self.assertTrue(np.isnan(after_gap.f_volume5))
        self.assertTrue(np.isnan(after_gap.f_ret15_bp))

    def test_bootstrap_uses_trade_count_weighted_estimator(self):
        daily = pd.DataFrame({"trades": [1, 9], "gross_bp_sum": [10, 0], "net_bp_sum": [4, -54]})
        spec = bt.load_spec()
        interval = bt.block_intervals(daily, spec)
        # The only full two-day block: 10 / 10 = 1, not (10 + 0) / 2 = 5.
        self.assertAlmostEqual(interval["gross_ci_low"], 1)
        self.assertAlmostEqual(interval["net_ci_low"], -5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
