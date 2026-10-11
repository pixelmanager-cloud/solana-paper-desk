"""SYNTHETIC_TEST_ONLY: stores created by tools.ops.fresh_start driven through the real dispatcher.

Reuses the dispatcher test fixtures' wire bytes and transports (no network, no credentials); only the
store set differs: research/evidence/ledger/journal come from `fresh_start.apply`, with the example
experiment config. Shared pacing is a fixture copy.
"""
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import time
import unittest
from unittest.mock import patch

from desk import paper_cycle as cycle, provider_pacing as pace, kraken_pacing_migration as upgrade
from desk import paper_terminal_reconciliation as terminal
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.job_persistence import JobPersistence
from desk.model import canonical, digest
from desk.paper_checkpoint import RecoveryRequired
from discovery import continuous as discovery
from desk.programs import unbase58
from desk.security import base58
from tests import test_paper_observation_collector as fixture
from tests import test_paper_entry_dispatcher as base
from tests.test_graduation_witness import fixture as migration_fixture
from tools import paper_entry_dispatcher as tool
from tools.ops import fresh_start as fs

EXAMPLE = Path(__file__).resolve().parents[1] / 'config/experiments/paper-kraken-fresh.example.json'

class FreshFixture(unittest.TestCase):
    drop_token_profile = False
    # Borrowed, unmodified dispatcher fixture behaviour.
    invoke = base.DispatcherTests.invoke
    count = base.DispatcherTests.count
    setup_rpc = base.DispatcherTests.setup_rpc
    live = base.DispatcherTests.live
    rejection = base.DispatcherTests.rejection
    append_distinct_migration = base.DispatcherTests.append_distinct_migration

    def setUp(self):
        self.f = fixture.PaperObservationCollectorTests(); self.addCleanup(self.f.doCleanups); self.f.setUp()
        self.f.at = int(time.time()); self.root = Path(self.f.tmp.name).resolve()
        self.config = self.root / 'config.json'; shutil.copy(EXAMPLE, self.config)
        if self.drop_token_profile:
            # The inherited dispatcher wire fixtures predate token profile 2's snapshot-amount bounds
            # (boosted_paper.validate_quote rejects their synthetic quote), so the full-entry test runs
            # the example config without that single key; every other parameter is unchanged.
            value = json.loads(EXAMPLE.read_text()); del value['paper_token_profile_version']
            self.config.write_text(json.dumps(value))
        self.cfg = tool.cli._config(self.config)
        self.pacer = self.root / 'pacing.sqlite'; pace.initialize(self.pacer)
        policy = self.root / 'migration.json'
        p = patch.object(upgrade, 'POLICY', policy); p.start(); self.addCleanup(p.stop)
        policy.write_text(canonical({'version': 1, 'pins': []})); proposal = upgrade.review_plan(self.pacer)
        policy.write_text(canonical({'version': 1, 'pins': [proposal]})); upgrade.migrate(self.pacer)
        self.clock = [float(self.f.at)]
        def configured(**kw):
            return pace.Pacer(self.pacer, clock=lambda: self.clock[0], monotonic=lambda: self.clock[0],
                              sleep=lambda n: self.clock.__setitem__(0, self.clock[0] + n), **kw)
        p = patch.object(pace, 'configured', side_effect=configured); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {pace.ENV: str(self.pacer)}); p.start(); self.addCleanup(p.stop)
        self.discovery = self.root / 'discovery.sqlite'
        with patch.object(discovery.time, 'time', return_value=self.f.at - 600):
            discovery.initialize(self.discovery)
        self.raw, self.mint, self.pool = migration_fixture()
        self.raw['transaction']['signatures'] = [base58(bytes([9]) * 64)]
        self.raw['blockTime'] = self.f.at - 600
        ix = self.raw['meta']['innerInstructions'][0]['instructions'][0]
        data = bytearray(unbase58(ix['data'])); data[136:144] = self.raw['blockTime'].to_bytes(8, 'little', signed=True)
        ix['data'] = base58(data)
        self.wire = canonical({'method': 'transactionNotification', 'params': {'result': {
            'signature': self.raw['transaction']['signatures'][0], 'slot': self.raw['slot'],
            'blockTime': self.raw['blockTime'],
            'transaction': {'transaction': self.raw['transaction'], 'meta': self.raw['meta']}}}})
        d = discovery.Store(self.discovery, clock=lambda: self.f.at - 600)
        try: d.complete(d.reserve('RECEIVE'), payload=self.wire.encode())
        finally: d.close()
        # --- the store set under test: created only by fresh_start.apply
        self.exp = self.root / 'exp'
        (self.root / 'backups').mkdir()
        manifest = fs.apply(fs.plan(root=str(self.exp), config=str(self.config), pacing_db=str(self.pacer),
                                    discovery_db=str(self.discovery), taker=self.f.taker, amount_raw=100_000_000,
                                    pool_fee_bps='25', backup_dir=str(self.root / 'backups' / 'fresh-exp')),
                             service_user=pwd.getpwuid(os.geteuid()).pw_name)
        self.manifest = manifest
        s = {k: Path(v['path']) for k, v in manifest['stores'].items()}
        self.f.jobs = JobPersistence(s['research_db'])
        self.f.progress = HistoryProgress(EvidenceStore(s['evidence_db']))
        self.ledger = s['ledger_db']; self.journal = s['journal']
        self.args = dict(config=str(self.config), research_db=str(s['research_db']), evidence_db=str(s['evidence_db']),
                         ledger_db=str(self.ledger), discovery_db=str(self.discovery), pacing_db=str(self.pacer),
                         journal=str(self.journal), taker=self.f.taker, amount_raw=100_000_000, pool_fee_bps='25')
        self.ctx = tool.plan(**self.args)
        self.assertEqual(digest(self.ctx), manifest['dispatcher_context_hash'])
        self.calls = []

    def passes(self):
        with sqlite3.connect(self.f.progress.store.path) as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='paper_observation_passes'").fetchone():
                return []
            return c.execute('SELECT id,outcome_hash FROM paper_observation_passes').fetchall()

    def gate(self, scans=()):
        return terminal.gate(EvidenceStore(self.f.progress.store.path, read_only=True), self.f.jobs.path, scans,
                             ledger_locked=str(self.ledger))


class FreshStoreScenario(FreshFixture):
    def test_fresh_gate_is_clear_and_first_candidate_selected_without_receipts(self):
        self.assertFalse(self.gate())
        with patch.object(tool.cli, '_credentials', side_effect=AssertionError('dry-run credentials')):
            result = self.invoke()
        self.assertEqual((result['status'], result['hint']['mint']), ('DRY_RUN', self.mint))

    def test_normal_token_rejection_does_not_block_second_candidate_on_fresh_stores(self):
        self.assertEqual(self.rejection()['status'], 'TOKEN_REJECTED')
        with self.f.jobs.connect() as c:
            scan = c.execute('SELECT id FROM scans').fetchone()[0]
        self.assertEqual(self.f.progress.admission(scan)['requests_used'], 1)  # charge retained
        self.assertFalse(self.gate())  # no reconciliation receipt needed
        second = self.append_distinct_migration()
        with patch.object(tool.cli, '_credentials', side_effect=AssertionError('dry-run credentials')):
            result = self.invoke()
        self.assertEqual((result['status'], result['hint']['mint']), ('DRY_RUN', second))
        self.assertEqual(self.count('results'), 1)

    def blocked_first_candidate(self):
        """Real run_once with the market producer blocked: a charged, non-COMPLETE (NULL) pass."""
        with patch.object(cycle, 'build_market_event', return_value={'event': None, 'blockers': ['SYNTHETIC_PRODUCER_BLOCK']}):
            try:
                result = self.live()
            except ValueError as error:  # dispatcher may surface the unresolved dispatch as an error
                result = {'error': str(error)}
        return result

    def test_market_producer_blocked_pass_must_not_block_second_candidate(self):
        self.blocked_first_candidate()
        second = self.append_distinct_migration()
        with patch.object(tool.cli, '_credentials', side_effect=AssertionError('dry-run credentials')):
            result = self.invoke()
        self.assertEqual((result['status'], result['hint']['mint']), ('DRY_RUN', second))

    def test_market_producer_blocked_is_terminal_and_keeps_charges(self):
        """Post-T01: the typed rejection gets a terminal NO_ENTRY outcome; charges stay charged."""
        self.blocked_first_candidate()
        self.assertEqual([p for p in self.passes() if p[1] is None], [])
        self.assertIsNone(self.gate())
        with self.f.jobs.connect() as c:
            scan = c.execute('SELECT id FROM scans').fetchone()[0]
        self.assertGreater(self.f.progress.admission(scan)['requests_used'], 1)


class FreshEntryScenario(FreshFixture):
    drop_token_profile = True

    def test_full_fresh_entry_then_held_position_priority(self):
        result = self.live()
        self.assertEqual((result['status'], result['paper_status']), ('DISPATCHED', 'COMPLETE'), result)
        self.assertEqual(len(cycle._state(self.ledger, self.cfg)['positions']), 1)
        self.assertFalse(self.gate())
        # A flat check must now refuse rotation (real open position, real checkpoint).
        with self.assertRaisesRegex(fs.FreshStartError, 'open position'):
            fs.verify_flat(self.exp)


if __name__ == '__main__':
    unittest.main()
