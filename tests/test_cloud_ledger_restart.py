"""Synthetic-only issue #7 regression probes; never activate a runner/provider."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from desk.engine import initial_state, transition
from desk.ledger import Ledger
from desk.model import D
from desk.monitor import tick
from tests.helpers import ROOT, T, config, control, event


# Kill a separate interpreter without unwinding Ledger.apply: SQLite must recover
# the actual uncommitted WAL, rather than relying on its Python exception handler.
CRASH_CHILD = r'''
import json, os, sys
from desk.engine import initial_state, transition
from desk.ledger import Ledger
path, boundary, cfg_json, event_json = sys.argv[1:]
ledger = Ledger(path, must_exist=True)
class CrashConnection:
    def execute(self, sql, *args):
        if boundary == "before_commit" and sql == "COMMIT":
            os._exit(73)
        result = ledger_connection.execute(sql, *args)
        targets = {
            "after_event": "INSERT INTO events(",
            "after_outcome": "INSERT INTO outcomes(",
            "after_state": "INSERT OR REPLACE INTO state",
            "after_commit": "COMMIT",
        }
        if boundary in targets and sql.startswith(targets[boundary]):
            os._exit(73)
        return result
ledger_connection = ledger.db
ledger.db = CrashConnection()
def crash_transition(state, e, cfg):
    result = transition(state, e, cfg)
    if boundary == "after_transition":
        os._exit(73)
    return result
ledger.apply(json.loads(event_json), json.loads(cfg_json), crash_transition, initial_state)
raise AssertionError("crash boundary was not reached")
'''


class CloudLedgerRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "paper.sqlite"
        self.cfg = config()
        self.ledger = Ledger(self.path)
        self.addCleanup(lambda: self.ledger.close())

    def apply(self, e):
        return self.ledger.apply(e, self.cfg, transition, initial_state)

    def restart(self):
        self.ledger.close()
        self.ledger = Ledger(self.path, must_exist=True)
        self.assertEqual(self.ledger.db.execute("PRAGMA integrity_check").fetchone(), ("ok",))

    def records(self):
        return {table: self.ledger.db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                for table in ("metadata", "events", "outcomes", "state", "raw_events", "health")}

    def test_process_crash_boundaries_recover_entry_partial_and_final_exit(self):
        boundaries = ("after_transition", "after_event", "after_outcome", "after_state",
                      "before_commit", "after_commit")
        for phase in ("entry", "partial", "final"):
            for boundary in boundaries:
                with self.subTest(phase=phase, boundary=boundary):
                    path = Path(self.tmp.name) / f"{phase}-{boundary}.sqlite"
                    ledger = Ledger(path)
                    try:
                        if phase != "entry":
                            ledger.apply(event(), self.cfg, transition, initial_state)
                        if phase == "final":
                            ledger.apply(event(T+5, reserve_sol="160"), self.cfg, transition, initial_state)
                        candidate = {"entry": event(), "partial": event(T+5, reserve_sol="160"),
                                     "final": event(T+10, danger=True, reserve_sol="160")}[phase]
                        before = ledger.report()
                        expected_state, expected_outcomes = transition(
                            copy.deepcopy(before["state"]) if before["state"] else initial_state(self.cfg),
                            candidate, self.cfg)
                    finally:
                        ledger.close()
                    # No inherited credentials, proxy, provider or signer settings.
                    result = subprocess.run(
                        [sys.executable, "-c", CRASH_CHILD, str(path), boundary,
                         json.dumps(self.cfg), json.dumps(candidate)], cwd=ROOT,
                        env={"PATH": os.defpath, "PYTHONPATH": str(ROOT)},
                        capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 73, result.stderr)
                    ledger = Ledger(path, must_exist=True)
                    try:
                        recovered = ledger.report()
                        self.assertEqual(ledger.db.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                        if boundary == "after_commit":
                            self.assertNotEqual(before["replay_hash"], recovered["replay_hash"])
                            self.assertEqual(ledger.apply(candidate, self.cfg, transition, initial_state), [])
                        else:
                            self.assertEqual(before, recovered)
                            fills = ledger.apply(candidate, self.cfg, transition, initial_state)
                            self.assertEqual(sum(o["type"] == "fill" for o in fills), 1)
                        committed = ledger.report()
                        self.assertEqual(committed["state"], expected_state)
                        self.assertEqual(committed["outcomes"], before["outcomes"] + expected_outcomes)
                        self.assertEqual(ledger.apply(candidate, self.cfg, transition, initial_state), [])
                        self.assertEqual(committed, ledger.report())
                        expected_events = {"entry": 1, "partial": 2, "final": 3}[phase]
                        self.assertEqual(ledger.db.execute("SELECT count(*) FROM events").fetchone()[0], expected_events)
                        self.assertEqual(sum(o["type"] == "fill" for o in committed["outcomes"]), expected_events)
                    finally:
                        ledger.close()

    def test_nonadjacent_replay_and_collision_preserve_original_records(self):
        observations = [event(), event(T+5, reserve_sol="160"),
                        event(T+10, danger=True, reserve_sol="160")]
        self.ledger.record_raw("synthetic-source", T, 1, {"fixture": "original"})
        self.ledger.health(T, "SYNTHETIC_TEST", {"provider_calls": 0})
        for e in observations:
            self.apply(e)
        self.restart()
        before = self.records()
        for e in reversed(observations):
            self.assertEqual(self.apply(e), [])
        collision = copy.deepcopy(observations[1])
        collision["reserve_sol"] = "999"
        with self.assertRaisesRegex(ValueError, "event_id collision"):
            self.apply(collision)
        with self.assertRaisesRegex(ValueError, "Conflicting raw"):
            self.ledger.record_raw("synthetic-source", T+1, 1, {"fixture": "changed"})
        self.assertEqual(before, self.records())

    def test_restart_each_ladder_rung_conserves_inventory_basis_and_net_cash(self):
        buy = self.apply(event())[0]
        initial = self.ledger.report()["state"]["positions"]["SYNTHETIC_A"]
        initial_qty, initial_cost = D(initial["qty"]), D(initial["cost_left"])
        self.assertEqual(initial_cost, D(buy["amount_sol"]) + D(buy["fee_sol"]))
        sold = D(0)
        proceeds = D(0)
        for stage, (dt, reserve, fraction) in enumerate(
                [(5, "160", ".3"), (10, "240", ".3"), (15, "350", ".2")], 1):
            self.restart()
            quote = event(T+dt, reserve_sol=reserve)
            fills = [o for o in self.apply(quote) if o["type"] == "fill"]
            self.assertEqual(len(fills), 1)
            fill = fills[0]
            self.assertAlmostEqual(D(fill["quantity"]), initial_qty * D(fraction))
            self.assertEqual(D(fill["fee_sol"]), D(self.cfg["fixed_fee_sol"]))
            sold += D(fill["quantity"])
            proceeds += D(fill["proceeds_sol"])
            state = self.ledger.report()["state"]
            p = state["positions"]["SYNTHETIC_A"]
            self.assertEqual(p["stage"], stage)
            self.assertAlmostEqual(D(p["qty"]) + sold, initial_qty)
            self.assertAlmostEqual(D(p["cost_left"]), initial_cost * D(p["qty"]) / initial_qty)
            self.assertAlmostEqual(D(state["cash"]), D(self.cfg["initial_equity_sol"]) - initial_cost + proceeds)
            before = self.records()
            self.assertEqual(self.apply(quote), [])
            self.assertEqual(before, self.records())
        self.restart()
        exit_fill = self.apply(event(T+20, danger=True, reserve_sol="250"))[0]
        self.assertAlmostEqual(D(exit_fill["quantity"]), initial_qty * D(".2"))
        self.restart()
        state = self.ledger.report()["state"]
        self.assertFalse(state["positions"])
        self.assertAlmostEqual(D(state["cash"]) - D(self.cfg["initial_equity_sol"]), D(state["realized_pnl"]))
        self.assertEqual(len([o for o in self.ledger.report()["outcomes"] if o["type"] == "fill"]), 5)
        self.assertGreater(state["cooldowns"]["SYNTHETIC_A"], T+20)

    def test_stale_partial_position_survives_midnight_and_resume_without_fills(self):
        self.apply(event())
        self.apply(event(T+5, reserve_sol="160"))
        before = self.ledger.report()["state"]
        self.ledger.close()
        result = tick(self.path, self.cfg, now=T+86400)
        self.assertEqual(result["status"], "MARKS_EXPIRED")
        self.assertFalse(result["automatic_entry_enabled"])
        self.ledger = Ledger(self.path, must_exist=True)
        self.restart()
        self.apply(control(T+86401, "RESUME"))
        after = self.ledger.report()["state"]
        for key in ("cash", "realized_pnl", "day", "day_start_equity", "day_gross_losses"):
            self.assertEqual(before[key], after[key])
        for key in ("qty", "cost_left", "stage", "mark_value", "mark_at"):
            self.assertEqual(before["positions"]["SYNTHETIC_A"][key], after["positions"]["SYNTHETIC_A"][key])
        self.assertEqual(after["mode"], "EXIT_ONLY")
        self.assertEqual(after["positions"]["SYNTHETIC_A"]["mark_status"], "STALE")
        self.assertEqual(sum(o["type"] == "fill" for o in self.ledger.report()["outcomes"]), 2)

    def test_unsellable_partial_exit_and_recovery_remain_latched_after_restart(self):
        self.apply(event())
        self.apply(event(T+5, reserve_sol="160"))
        for dt, changes, reason in [(6, {"route_available": False}, "STALE_OR_UNAVAILABLE_EXIT"),
                                    (7, {"sellability": None}, "EXIT_SELLABILITY_UNVERIFIED"),
                                    (8, {"reserve_sol": "0.000001"}, "STUCK_POSITION")]:
            with self.subTest(reason=reason):
                before = self.ledger.report()["state"]
                quote = event(T+dt, danger=True, **changes)
                outcomes = self.apply(quote)
                self.assertTrue(any(o.get("reason") == reason for o in outcomes))
                self.restart()
                after = self.ledger.report()["state"]
                self.assertEqual(before["cash"], after["cash"])
                self.assertEqual(before["realized_pnl"], after["realized_pnl"])
                for key in ("qty", "cost_left", "stage"):
                    self.assertEqual(before["positions"]["SYNTHETIC_A"][key], after["positions"]["SYNTHETIC_A"][key])
                self.assertEqual(after["mode"], "EXIT_ONLY")
                self.assertEqual(self.apply(quote), [])
        # Fresh evidence clears the blocked position, but cannot resume entries.
        self.apply(event(T+9, reserve_sol="160"))
        self.restart()
        self.assertEqual(self.ledger.report()["state"]["mode"], "EXIT_ONLY")
        self.assertIsNone(self.ledger.report()["state"]["positions"]["SYNTHETIC_A"]["exit_blocked"])
        self.apply(event(T+10, danger=True, reserve_sol="160"))
        self.restart()
        self.assertFalse(self.ledger.report()["state"]["positions"])
        self.assertEqual(self.ledger.report()["state"]["mode"], "EXIT_ONLY")

    def test_full_position_proof_cannot_authorize_partial_rung(self):
        self.apply(event())
        before = self.ledger.report()["state"]
        p = before["positions"]["SYNTHETIC_A"]
        proof = {"kind": "sell_simulation", "mint": "SYNTHETIC_A", "wallet": "fixture-wallet",
                 "transaction_hash": "a" * 64, "route_hash": "b" * 64, "slot": 1,
                 "observed_at": T+5, "quantity_tokens": p["qty"], "simulation_ok": True,
                 "transaction_policy_ok": True, "wallet_account_ok": True, "net_proceeds_sol": "1"}
        outcomes = self.apply(event(T+5, reserve_sol="160", taker="fixture-wallet", sellability=proof))
        self.assertTrue(any("SELL_PROOF_SIZE_MISMATCH" in o.get("reasons", []) for o in outcomes))
        self.assertFalse(any(o["type"] == "fill" for o in outcomes))
        self.restart()
        after = self.ledger.report()["state"]
        self.assertEqual(before["cash"], after["cash"])
        for key in ("qty", "cost_left", "stage"):
            self.assertEqual(p[key], after["positions"]["SYNTHETIC_A"][key])
        self.assertEqual(after["mode"], "EXIT_ONLY")

    def test_simulated_net_exit_caps_model_proceeds_across_restart(self):
        self.apply(event())
        before = self.ledger.report()["state"]
        p = before["positions"]["SYNTHETIC_A"]
        proof = {"kind": "sell_simulation", "mint": "SYNTHETIC_A", "wallet": "fixture-wallet",
                 "transaction_hash": "a" * 64, "route_hash": "b" * 64, "slot": 1,
                 "observed_at": T+1, "quantity_tokens": p["qty"], "simulation_ok": True,
                 "transaction_policy_ok": True, "wallet_account_ok": True, "net_proceeds_sol": "0.005"}
        quote = event(T+1, danger=True, taker="fixture-wallet", sellability=proof)
        fill = self.apply(quote)[0]
        self.assertEqual(D(fill["proceeds_sol"]), D("0.005"))
        self.assertEqual(D(fill["realized_pnl_sol"]), D("0.005") - D(p["cost_left"]))
        self.restart()
        state = self.ledger.report()["state"]
        self.assertFalse(state["positions"])
        self.assertEqual(D(state["cash"]), D(before["cash"]) + D("0.005"))
        self.assertEqual(D(state["day_gross_losses"]), -D(fill["realized_pnl_sol"]))
        self.assertEqual(self.apply(quote), [])

    def test_configuration_and_implementation_drift_fail_closed_on_duplicate(self):
        self.apply(event())
        self.restart()
        before = self.records()
        changed = copy.deepcopy(self.cfg)
        changed["fixed_fee_sol"] = "0.01"
        with self.assertRaisesRegex(ValueError, "config changed"):
            self.ledger.apply(event(), changed, transition, initial_state)
        self.assertEqual(before, self.records())
        # Alter only a disposable fixture database, never production source.
        self.ledger.db.execute("UPDATE metadata SET value='fixture-other-version' WHERE key='implementation_hash'")
        before = self.records()
        with self.assertRaisesRegex(ValueError, "implementation changed"):
            self.apply(event())
        self.assertEqual(before, self.records())

    def test_cost_budget_rejection_is_durable_and_does_not_charge_a_fill(self):
        self.cfg["fixed_fee_sol"] = ".01"
        quote = event()
        self.assertEqual(self.apply(quote)[0]["reason"], "COST_BUDGET")
        self.restart()
        before = self.records()
        self.assertEqual(self.apply(quote), [])
        self.assertEqual(before, self.records())
        state = self.ledger.report()["state"]
        self.assertEqual(state["cash"], self.cfg["initial_equity_sol"])
        self.assertFalse(state["positions"])
        self.assertEqual(D(state["realized_pnl"]), 0)


if __name__ == "__main__":
    unittest.main()
