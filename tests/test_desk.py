import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desk.bundles import audit
from desk.engine import initial_state, transition
from desk.ledger import Ledger
from desk.model import D, load_config, validate_event
from desk.providers import backfill, record_notification, subscription
from desk.strategy import size, swap_quote
from tests.helpers import T, bundled_evidence, clean_evidence, config, control, event


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.sqlite"
        self.ledger = Ledger(self.path)
        self.cfg = config()

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def apply(self, e):
        return self.ledger.apply(e, self.cfg, transition, initial_state)

    def test_real_exit_cannot_reuse_synthetic_sell_evidence(self):
        self.apply(event())
        before=self.ledger.report()["state"]
        result=self.apply(event(T+1,danger=True,provenance="MAINNET_OBSERVATION"))
        self.assertTrue(any(x.get("reason")=="EXIT_SELLABILITY_UNVERIFIED" for x in result))
        after=self.ledger.report()["state"]
        self.assertEqual(before["cash"],after["cash"])
        self.assertEqual(before["positions"]["SYNTHETIC_A"]["qty"],after["positions"]["SYNTHETIC_A"]["qty"])

    def test_unresolved_exit_blocks_resume_until_new_evidence(self):
        self.apply(event())
        self.apply(event(T+1,danger=True,provenance="MAINNET_OBSERVATION"))
        self.assertEqual(self.ledger.report()["state"]["mode"],"EXIT_ONLY")
        self.apply(control(T+2,"RESUME"))
        self.assertEqual(self.ledger.report()["state"]["mode"],"EXIT_ONLY")
        self.apply(event(T+3))
        self.assertIsNone(self.ledger.report()["state"]["positions"]["SYNTHETIC_A"]["exit_blocked"])
        self.assertEqual(self.ledger.report()["state"]["mode"],"EXIT_ONLY")
        self.apply(control(T+4,"RESUME"))
        self.assertEqual(self.ledger.report()["state"]["mode"],"RUNNING")

    def test_restart_and_redelivery_do_not_duplicate_fill(self):
        self.apply(event())
        before = self.ledger.report()["replay_hash"]
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.assertEqual(self.apply(event()), [])
        self.assertEqual(before, self.ledger.report()["replay_hash"])
        self.assertEqual(len(self.ledger.report()["state"]["positions"]), 1)

    def test_crash_during_transition_rolls_back(self):
        def crash(state, e, cfg):
            raise RuntimeError("simulated crash")
        with self.assertRaises(RuntimeError):
            self.ledger.apply(event(), self.cfg, crash, initial_state)
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        self.assertEqual(self.apply(event())[0]["side"], "buy")

    def test_changed_event_id_is_not_silently_ignored(self):
        self.apply(event())
        with self.assertRaises(ValueError):
            self.apply(event(flow="0"))

    def test_config_drift_rejected(self):
        self.apply(event())
        self.cfg["min_order_sol"] = ".02"
        with self.assertRaises(ValueError):
            self.apply(event(T + 1))

    def test_stale_and_future_inputs(self):
        results = self.apply(event(holder_at=T - 121))
        self.assertIn("STALE_HOLDER", results[0]["reasons"])
        with self.assertRaises(ValueError):
            self.apply(event(T + 1, holder_at=T + 2))

    def test_nonfinite_numbers_and_missing_safety_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.assertRaises(ValueError):
                validate_event(event(reserve_sol=value))
        with self.assertRaises(ValueError):
            validate_event(event(lp_verified=None))

    def test_bundle_gate_overrides_high_score(self):
        result = self.apply(event(bundle_evidence=bundled_evidence()))
        self.assertEqual(result[0]["type"], "reject")
        self.assertIn("EARLY_LINKED_SUPPLY_GE_8PCT", result[0]["reasons"])
        self.assertGreater(D(result[0]["scores"]["entry"]), 68)

    def test_missing_bundle_evidence_fails_closed(self):
        result = self.apply(event(bundle_evidence=None))
        self.assertIn("BUNDLE_EVIDENCE_MISSING", result[0]["reasons"])

    def test_unsupported_token_program_cannot_be_overridden_by_safe_boolean(self):
        from desk.security import TOKEN_2022
        e = event()
        e["token_evidence"]["account"]["owner"] = TOKEN_2022
        self.assertIn("TOKEN_2022_NOT_ALLOWED", self.apply(e)[0]["reasons"])

    def test_missing_sellability_proof_prevents_entry(self):
        self.assertEqual(self.apply(event(sellability=None))[0]["reason"], "SELLABILITY_UNKNOWN")

    def test_unrealized_drawdown_triggers_liquidation(self):
        from desk.engine import risk
        state = initial_state(self.cfg)
        state["cash"] = "4.4"
        out = []
        risk(state, self.cfg, out)
        self.assertEqual(state["mode"], "LIQUIDATING")

    def test_pause_threshold_includes_unrealized_loss(self):
        from desk.engine import risk
        state = initial_state(self.cfg)
        state["cash"] = "4.65"
        out = []
        risk(state, self.cfg, out)
        self.assertEqual(state["mode"], "EXIT_ONLY")

    def test_pause_still_allows_exit(self):
        self.apply(event())
        self.apply(control(T + 1, "PAUSE_ENTRY"))
        result = self.apply(event(T + 2, reserve_sol="60"))
        self.assertEqual(result[0]["reason"], "STOP")
        self.assertFalse(self.ledger.report()["state"]["positions"])
        self.assertEqual(self.ledger.report()["state"]["mode"], "ENTRY_PAUSED")

    def test_stale_exit_is_not_a_fake_fill(self):
        self.apply(event())
        result = self.apply(event(T + 60, price_at=T, danger=True))
        self.assertEqual(result[0]["type"], "blocked_exit")
        self.assertTrue(self.ledger.report()["state"]["positions"])

    def test_no_route_preserves_position(self):
        self.apply(event())
        result = self.apply(event(T + 5, route_available=False, danger=True))
        self.assertEqual(result[0]["type"], "blocked_exit")
        self.assertEqual(sum(x["type"]=="fill" for x in self.ledger.report()["outcomes"]), 1)
        self.assertEqual(self.ledger.report()["state"]["mode"], "EXIT_ONLY")

    def test_ladder_sells_initial_quantity_and_accounting_conserves(self):
        self.apply(event())
        initial = self.ledger.report()["state"]["positions"]["SYNTHETIC_A"]
        initial_qty = D(initial["initial_qty"])
        for dt, reserve in [(5, "160"), (10, "240"), (15, "350")]:
            self.apply(event(T + dt, reserve_sol=reserve))
        p = self.ledger.report()["state"]["positions"]["SYNTHETIC_A"]
        self.assertAlmostEqual(D(p["qty"]), initial_qty * D(".2"))
        self.assertEqual(p["stage"], 3)
        self.apply(event(T + 20, danger=True, reserve_sol="250"))
        state = self.ledger.report()["state"]
        self.assertFalse(state["positions"])
        self.assertAlmostEqual(D(state["cash"]) - D(self.cfg["initial_equity_sol"]), D(state["realized_pnl"]))

    def test_time_stop_does_not_need_new_entry_signal(self):
        self.apply(event())
        result = self.apply(event(T + 2700, flow="0"))
        self.assertEqual(result[0]["reason"], "TIME_STOP")

    def test_cooldown_and_out_of_order(self):
        self.apply(event())
        self.apply(event(T + 5, danger=True))
        result = self.apply(event(T + 60))
        self.assertIn("COOLDOWN", result[0]["reasons"])
        self.assertEqual(self.apply(event(T + 30))[0]["reason"], "OUT_OF_ORDER")

    def test_liquidation_mode_remains_latched(self):
        self.apply(event())
        self.apply(control(T + 1, "LIQUIDATE"))
        self.apply(event(T + 2))
        self.assertEqual(self.ledger.report()["state"]["mode"], "STOPPED")
        self.apply(event(T + 86400, mint="NEXT_DAY"))
        self.assertEqual(self.ledger.report()["state"]["mode"], "STOPPED")

    def test_small_equity_size_and_cost_filter(self):
        e = event()
        amount, _ = size(e, self.cfg, D(68), D(5), D(5), D(0), D(".3"), D(0))
        self.assertEqual(amount, D(".02"))
        self.cfg["fixed_fee_sol"] = ".01"
        self.assertEqual(self.apply(e)[0]["reason"], "COST_BUDGET")

    def test_price_impact_increases_with_order_size(self):
        e = event()
        small = swap_quote(e, D(".01"), "buy", self.cfg) / D(".01")
        large = swap_quote(e, D("10"), "buy", self.cfg) / D("10")
        self.assertLess(large, small)


class BundleTests(unittest.TestCase):
    def test_unknown_history_skips(self):
        e = clean_evidence()
        e["launch_history_complete"] = False
        self.assertEqual(audit(e, T)["decision"], "SKIP")

    def test_hub_funding_does_not_create_cluster(self):
        e = bundled_evidence()
        for f in e["funding"]:
            f["source_kind"] = "exchange"
        result = audit(e, T)
        self.assertEqual(result["decision"], "PASS_SCREEN")
        self.assertEqual(result["largest_linked_supply_pct"], "0")
        self.assertEqual(result["excluded_hub_or_unknown_funding_edges"], 12)

    def test_distributed_buys_still_caught_by_funding(self):
        e = bundled_evidence()
        for b in e["early_buys"]:
            b["slot"] += 100
        result = audit(e, T)
        self.assertIn("LINKED_SUPPLY_GE_10PCT", result["reasons"])

    def test_dust_does_not_link_holders(self):
        e = clean_evidence()
        e["early_buys"][0]["slot"] = 1000
        e["transfers"] = [{"source": "w000", "destination": f"w{i:03}", "supply_pct": ".00001", "ts": T - 1}
                          for i in range(1, 30)]
        self.assertEqual(audit(e, T)["clusters"], [])

    def test_future_evidence_rejected(self):
        e = bundled_evidence()
        e["funding"][0]["ts"] = T + 1
        with self.assertRaises(ValueError):
            audit(e, T)

    def test_unknown_funding_is_uncertainty_not_clean_bill(self):
        e = bundled_evidence()
        for f in e["funding"]:
            f["source_kind"] = "unknown"
        self.assertIn("UNRESOLVED_FUNDING_GE_5PCT", audit(e, T)["reasons"])

    def test_coverage_attestation_must_match_supplied_holdings(self):
        e = clean_evidence()
        e["holders"] = e["holders"][:10]
        self.assertIn("HOLDER_COVERAGE_INCONSISTENT", audit(e, T)["reasons"])


class ProviderTests(unittest.TestCase):
    def test_subscription_filters_and_versions(self):
        req = subscription(["pool"])
        self.assertEqual(req["params"][0]["accountInclude"], ["pool"])
        self.assertEqual(req["params"][1]["maxSupportedTransactionVersion"], 1)

    def test_notification_duplicate_and_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "a.sqlite")
            msg = {"method": "transactionNotification", "params": {"result": {"signature": "s", "slot": 1}}}
            self.assertTrue(record_notification(ledger, msg, T))
            self.assertFalse(record_notification(ledger, msg, T))
            with self.assertRaises(ValueError):
                record_notification(ledger, {"method": "transactionNotification"}, T)
            ledger.close()

    def test_backfill_resumes_after_page_limit_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "a.sqlite")
            def rpc(method, params):
                if "paginationToken" not in params[1]:
                    return {"data": [{"signature": "s1", "slot": 1}], "paginationToken": "1:1"}
                return {"data": [{"signature": "s1", "slot": 1}, {"signature": "s2", "slot": 2}]}
            result = backfill(ledger, "addr", 1, 100, max_pages=1, rpc=rpc)
            self.assertFalse(result["complete"])
            result = backfill(ledger, "addr", 1, 100, max_pages=1, rpc=rpc)
            self.assertTrue(result["complete"])
            self.assertEqual(ledger.report()["raw_event_count"], 2)
            ledger.close()


if __name__ == "__main__":
    unittest.main()
