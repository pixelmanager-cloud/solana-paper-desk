"""SYNTHETIC_TEST_ONLY: regime evidence producer (paper_regime_version=1). Fixture stores, no provider."""
import base64
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from desk import engine, regime, regime_producer as producer, kraken_usd_observation as ku
from desk.evidence import EvidenceStore
from desk.model import digest
from tests.helpers import T, config, event

NOW = 1_000_000


def raw_trade(price, at):
    return json.dumps({"error": [], "result": {"SOLUSD": [[price, "1.00000000", at - 0.5, "s", "l", "", 34299138]],
                                               "last": "1000249358226"}}).encode()


def attempt(price, at, scan="s1"):
    raw = raw_trade(price, at)
    return {"kind": "paper_read_attempt_v1", "scan_id": scan, "requests_used": 1, "source_id": ku.SOURCE,
            "method": ku.METHOD, "params": ku.PARAMS,
            "request_bytes_base64": base64.b64encode(urlencode(ku.PARAMS).encode()).decode(),
            "response_bytes_base64": base64.b64encode(raw).decode(), "observed_at": at, "http_status": 200,
            "failure_code": None, "acquired_at_decimal": str(at)}


class ProducerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=".")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()
        self.research = self.dir / "r.sqlite"
        with sqlite3.connect(self.research) as c:
            c.execute("CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)")
            for i in range(8):
                c.execute("INSERT INTO scans VALUES(?,?,?,?,?)", (f"s{i}", f"m{i}", NOW - 300 * i, "DONE", None))
            c.execute("INSERT INTO scans VALUES('old','mo',?,'DONE',NULL)", (NOW - 4000,))
        self.evidence = self.dir / "e.sqlite"
        self.store = EvidenceStore(self.evidence)

    def feed(self, *pairs):
        for price, at in pairs:
            self.store.save(attempt(price, at))

    def good(self):
        self.feed(("100.00", NOW - 3000), ("99.00", NOW - 1500), ("98.00", NOW - 100))

    def test_deterministic_evidence_from_fixture_stores(self):
        self.good()
        got = producer.evidence(self.research, self.evidence, NOW, 900)
        self.assertEqual(got, {"version": 1, "as_of": NOW - 100, "graduations_per_hour": "8",
                               "sol_usd_change_pct": "-2.0000"})
        self.assertEqual(got, producer.evidence(self.research, self.evidence, NOW, 900))
        regime.validate_record(got, NOW, regime.policy({}))

    def test_missing_short_stale_and_bad_history_give_none(self):
        self.assertIsNone(producer.evidence(self.research, self.evidence, NOW, 900))          # no Kraken records
        self.feed(("100.00", NOW - 600), ("99.00", NOW - 100))                                 # span < 30 min
        self.assertIsNone(producer.evidence(self.research, self.evidence, NOW, 900))
        self.feed(("100.00", NOW - 5000), ("98.00", NOW - 4000))                               # newest too old
        self.assertIsNone(producer.evidence(self.research, self.evidence, NOW + 3000, 900))
        self.assertIsNone(producer.evidence(self.dir / "absent.sqlite", self.evidence, NOW, 900))
        self.assertIsNone(producer.evidence(self.research, self.dir / "absent.sqlite", NOW, 900))
        self.assertIsNone(producer.evidence(self.research, self.evidence, "x", 900))

    def test_failed_malformed_or_foreign_records_are_ignored(self):
        bad = attempt("100.00", NOW - 3000)
        bad["failure_code"] = "HTTP"
        self.store.save(bad)
        foreign = attempt("100.00", NOW - 2900)
        foreign["source_id"] = "other"
        self.store.save(foreign)
        self.store.save({"kind": "paper_read_attempt_v1", "junk": 1})
        self.feed(("98.00", NOW - 100))
        self.assertIsNone(producer.evidence(self.research, self.evidence, NOW, 900))

    def test_read_only_and_never_charges_or_writes(self):
        self.good()
        before = [self.evidence.read_bytes(), self.research.read_bytes()]
        producer.evidence(self.research, self.evidence, NOW, 900)
        self.assertEqual(before, [self.evidence.read_bytes(), self.research.read_bytes()])

    def test_producer_output_drives_gate_and_failure_is_terminal_no_entry(self):
        self.good()
        cfg = config() | {"paper_regime_version": 1}
        e = event(ts=NOW)
        e["regime"] = producer.evidence(self.research, self.evidence, NOW, 900)
        reasons, mult, info = regime.gate(e, cfg, {})
        self.assertEqual(reasons, [])
        self.assertEqual(info["state"], regime.NORMAL if mult == 1 else regime.CAUTION)
        e2 = event(ts=NOW)                                   # producer failed: no evidence attached
        self.assertEqual(regime.gate(e2, cfg, {})[0], ["REGIME_EVIDENCE_REQUIRED"])

    def test_gate_rejects_overflow_and_huge_magnitudes(self):
        cfg = config() | {"paper_regime_version": 1}
        for value in ("1E+999", "-1E+999", "1E+13"):
            e = event(ts=NOW)
            e["regime"] = {"version": 1, "as_of": NOW, "graduations_per_hour": value, "sol_usd_change_pct": "0"}
            self.assertEqual(regime.gate(e, cfg, {})[0], ["REGIME_EVIDENCE_INVALID"], value)

    def test_event_id_recomputation_is_deterministic_and_binds_regime(self):
        self.good()
        e = event(ts=NOW)
        e["regime"] = producer.evidence(self.research, self.evidence, NOW, 900)
        body = {k: v for k, v in e.items() if k != "event_id"}
        a = "paper-market:" + digest(body)
        f = copy.deepcopy(e)
        f["regime"]["sol_usd_change_pct"] = "5.0000"
        self.assertNotEqual(a, "paper-market:" + digest({k: v for k, v in f.items() if k != "event_id"}))

    def test_flag_off_by_default_and_invalid_config_refused_at_load(self):
        from desk import paper_cycle
        base = config()
        self.assertFalse(regime.enabled(base))
        with self.assertRaises(ValueError):
            paper_cycle._config(base | {"paper_regime_version": 2})
        with self.assertRaises(ValueError):
            paper_cycle._config(base | {"paper_regime_version": 1, "paper_regime_policy": {"bogus": 1}})


if __name__ == "__main__":
    unittest.main()
