"""SYNTHETIC_TEST_ONLY: injected transport and fixtures; no provider calls, keys or live stores."""
import base64
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from desk.pools import ATA
from desk.providers import PUMP, PUMPSWAP, SOL
from desk.security import TOKEN_PROGRAM, base58
from tools.research import counterfactual as cf

T0 = 1_000_000.0


def pool_fixture(n):
    """Real PumpSwap pool bytes (same layout as tests/test_pools.py) for candidate n."""
    from solders.pubkey import Pubkey
    mint = Pubkey.from_bytes(bytes([n % 256, n // 256]) + bytes([7]) * 30)
    creator = Pubkey.find_program_address([b'pool-authority', bytes(mint)], Pubkey.from_string(PUMP))[0]
    pool, bump = Pubkey.find_program_address([b'pool', bytes(2), bytes(creator), bytes(mint), bytes(Pubkey.from_string(SOL))], Pubkey.from_string(PUMPSWAP))
    lp = Pubkey.find_program_address([b'pool_lp_mint', bytes(pool)], Pubkey.from_string(PUMPSWAP))[0]
    vaults = [Pubkey.find_program_address([bytes(pool), bytes(Pubkey.from_string(TOKEN_PROGRAM)), bytes(m)], Pubkey.from_string(ATA))[0]
              for m in (mint, Pubkey.from_string(SOL))]
    raw = (bytes([241, 154, 109, 4, 17, 177, 109, 188]) + bytes([bump]) + bytes(2)
           + b''.join(bytes(x) for x in [creator, mint, Pubkey.from_string(SOL), lp, *vaults]) + (1000).to_bytes(8, 'little') + bytes(32))
    return SimpleNamespace(mint=str(mint), pool=str(pool), base_vault=str(vaults[0]), quote_vault=str(vaults[1]), raw=raw)


def account(data, owner):
    return {'owner': owner, 'executable': False, 'data': [base64.b64encode(data).decode(), 'base64']}


def token_account(mint, owner, amount):
    data = bytearray(165)
    data[:32] = base58_decode(mint); data[32:64] = base58_decode(owner); data[64:72] = amount.to_bytes(8, 'little')
    return account(bytes(data), TOKEN_PROGRAM)


def base58_decode(value):
    from desk.programs import unbase58
    return bytes(unbase58(value))


class FakeChain:
    """Records every request; serves pool + vault accounts from mutable reserves."""

    def __init__(self, fixtures):
        self.fixtures = {f.pool: f for f in fixtures}
        self.by_vault = {}
        for f in fixtures:
            self.by_vault[f.base_vault] = (f, 'base'); self.by_vault[f.quote_vault] = (f, 'quote')
        self.reserves = {f.pool: (1_000_000, 2_000_000_000) for f in fixtures}
        self.calls = []; self.fail = None; self.mutate = None; self.slot = 500

    def __call__(self, method, params):
        assert method == 'getMultipleAccounts'
        addresses, options = params
        assert options == {'encoding': 'base64', 'commitment': 'confirmed'} and len(addresses) <= 100
        self.calls.append(list(addresses))
        if self.fail:
            raise cf.TransportError(self.fail)
        values = []
        for a in addresses:
            if a in self.fixtures:
                values.append(account(self.fixtures[a].raw, PUMPSWAP))
            else:
                f, side = self.by_vault[a]
                base, quote = self.reserves[f.pool]
                values.append(None if base is None else token_account(f.mint if side == 'base' else SOL, f.pool, base if side == 'base' else quote))
        if self.mutate:
            values = self.mutate(values)
        self.slot += 1
        return {'context': {'slot': self.slot}, 'value': values}


class CounterfactualTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / 'counterfactual.sqlite'
        cf.init(self.store, allowance_per_hour=300, now=T0 - 10)

    def candidates(self, n, offset=0):
        fixtures = [pool_fixture(offset + i + 1) for i in range(n)]
        for i, f in enumerate(fixtures):
            cf.add_candidate(self.store, mint=f.mint, pool=f.pool, signature=f'sig{offset + i}', slot=1, migrated_at=T0, seq=offset + i + 1, now=T0)
        return fixtures

    def rows(self, sql, args=()):
        with closing(sqlite3.connect(self.store)) as c:
            return c.execute(sql, args).fetchall()

    # ------------------------------------------------------------- price math
    def test_pumpswap_constant_product_price_known_answers(self):
        self.assertEqual(cf.price_from_reserves(1_000_000, 2_000_000_000), Decimal(2000))
        self.assertEqual(cf.price_from_reserves(3, 1), Decimal(1) / Decimal(3))
        self.assertIsNone(cf.price_from_reserves(0, 5)); self.assertIsNone(cf.price_from_reserves(5, 0))
        for bad in ((-1, 1), (1, -1), (1.5, 1), ('1', 1), (True, 1) if False else (None, 1)):
            with self.assertRaises(cf.CounterfactualError):
                cf.price_from_reserves(*bad)

    def test_token_account_amount_fails_closed(self):
        f = pool_fixture(1)
        good = token_account(f.mint, f.pool, 42)
        self.assertEqual(cf.token_account_amount(good, mint=f.mint, owner=f.pool), 42)
        other = pool_fixture(2)
        for bad in (token_account(other.mint, f.pool, 42), token_account(f.mint, other.pool, 42), {**good, 'owner': PUMPSWAP},
                    {**good, 'executable': True}, account(bytes(100), TOKEN_PROGRAM), {**good, 'data': ['!!!', 'base64']},
                    {**good, 'data': [good['data'][0], 'base58']}, None, []):
            with self.assertRaises((cf.CounterfactualError, ValueError)):
                cf.token_account_amount(bad, mint=f.mint, owner=f.pool)

    def test_metrics_known_answers(self):
        samples = [dict(horizon=h, status='OK', price=str(p), quote_raw=str(q)) for h, p, q in
                   ((300, 100, 10), (900, 150, 15), (1800, 60, 6), (3600, 90, 9))]
        m = cf.metrics(samples)
        self.assertEqual(m['returns'], {300: Decimal(0), 900: Decimal('0.5'), 1800: Decimal('-0.4'), 3600: Decimal('-0.1')})
        self.assertEqual(m['max_gain'], Decimal('0.5')); self.assertEqual(m['max_drawdown'], Decimal('-0.6'))
        self.assertEqual(m['liquidity_lamports'][900], 30); self.assertFalse(m['pool_died'])
        dead = cf.metrics(samples + [dict(horizon=7200, status='POOL_DEAD', price=None, quote_raw='0')])
        self.assertTrue(dead['pool_died']); self.assertEqual(dead['max_drawdown'], Decimal(-1))

    # ----------------------------------------------------- schedule / batching
    def test_schedule_batches_and_returns(self):
        fixtures = self.candidates(3); chain = FakeChain(fixtures)
        self.assertEqual(cf.sample(self.store, chain, now=T0 + 100)['requests'], 1)  # vault resolution only, nothing due yet
        self.assertEqual(self.rows('SELECT COUNT(*) FROM samples')[0][0], 0)
        summary = cf.sample(self.store, chain, now=T0 + 301)
        self.assertEqual((summary['requests'], summary['samples']), (1, 3))  # ONE request covers three candidates (6 accounts)
        self.assertEqual(len(chain.calls[-1]), 6)
        self.assertEqual(len(chain.calls), 2)
        chain.reserves = {f.pool: (1_000_000, 3_000_000_000) for f in fixtures}  # price x1.5
        summary = cf.sample(self.store, chain, now=T0 + 901)
        self.assertEqual((summary['requests'], summary['samples']), (1, 3))
        by_mint = {}
        for mint, h, price, status in self.rows('SELECT mint,horizon,price,status FROM samples ORDER BY horizon'):
            by_mint.setdefault(mint, []).append(dict(horizon=h, price=price, status=status, quote_raw='1'))
        for samples in by_mint.values():
            m = cf.metrics(samples)
            self.assertEqual([s['status'] for s in samples], ['OK', 'OK'])
            self.assertEqual(m['returns'][900], Decimal('0.5'))
        self.assertEqual(cf.sample(self.store, chain, now=T0 + 902)['requests'], 0)  # nothing due: no request at all

    def test_large_population_chunks_at_fifty_candidates_per_request(self):
        fixtures = self.candidates(55); chain = FakeChain(fixtures)
        summary = cf.sample(self.store, chain, now=T0 + 301)
        self.assertEqual(summary['samples'], 55)
        sizes = sorted(len(c) for c in chain.calls)
        self.assertEqual(sizes, [10, 55, 100])  # 55 pools; then 50 candidates*2 and 5 candidates*2 vault accounts
        self.assertEqual(self.rows('SELECT COUNT(*) FROM requests')[0][0], 3)

    def test_missed_slot_is_recorded_not_sampled_late(self):
        fixtures = self.candidates(1); chain = FakeChain(fixtures)
        cf.sample(self.store, chain, now=T0 + 100)
        summary = cf.sample(self.store, chain, now=T0 + 300 + 121 + 1)
        self.assertEqual((summary['missed'], summary['requests']), (1, 0))
        self.assertEqual(self.rows('SELECT status,code,price FROM samples'), [('MISSED', 'SCHEDULE_MISSED', None)])

    def test_cold_restart_resumes_without_duplicates(self):
        fixtures = self.candidates(2); chain = FakeChain(fixtures)
        cf.sample(self.store, chain, now=T0 + 301)
        before = self.rows('SELECT COUNT(*) FROM samples')[0][0]
        fresh_process_chain = FakeChain(fixtures)  # nothing in memory survives a restart
        cf.sample(self.store, fresh_process_chain, now=T0 + 305)
        self.assertEqual(fresh_process_chain.calls, [])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM samples')[0][0], before)
        cf.sample(self.store, fresh_process_chain, now=T0 + 901)
        self.assertEqual(self.rows('SELECT horizon,COUNT(*) FROM samples GROUP BY horizon ORDER BY horizon'), [(300, 2), (900, 2)])
        with self.assertRaises(sqlite3.DatabaseError), closing(sqlite3.connect(self.store)) as c:
            c.execute('INSERT INTO samples(mint,horizon,due_at,status) VALUES(?,?,?,?)', (fixtures[0].mint, 300, 1, 'OK'))

    # ------------------------------------------------------------- allowance
    def test_allowance_is_enforced_recorded_and_rolls(self):
        cf.set_allowance(self.store, 2, now=T0)
        fixtures = self.candidates(1); chain = FakeChain(fixtures)
        cf.sample(self.store, chain, now=T0 + 301)                      # pool + sample = 2 requests
        self.assertEqual(self.rows('SELECT COUNT(*) FROM requests')[0][0], 2)
        self.assertTrue(cf.sample(self.store, chain, now=T0 + 901)['allowance_exhausted'])  # +15m due, third request refused
        self.assertEqual(len(chain.calls), 2)
        self.assertEqual(self.rows('SELECT allowance_per_hour FROM policy ORDER BY id'), [(300,), (2,)])  # history kept
        late = cf.sample(self.store, chain, now=T0 + 21610)             # window rolled; only +6h is still on schedule
        self.assertFalse(late['allowance_exhausted']); self.assertEqual(late['requests'], 1)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM requests')[0][0], 3)
        self.assertEqual(self.rows("SELECT status FROM samples WHERE horizon=900"), [('MISSED',)])

    def test_failed_requests_are_charged_and_retried_later(self):
        cf.set_allowance(self.store, 3, now=T0)
        fixtures = self.candidates(1); chain = FakeChain(fixtures); chain.fail = 'HTTP_REJECTED'
        for _ in range(5):
            cf.sample(self.store, chain, now=T0 + 301)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM requests')[0][0], 3)  # allowance stops the hammering
        self.assertEqual(self.rows("SELECT status,code FROM request_results GROUP BY status,code"), [('FAILED', 'HTTP_REJECTED')])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM samples')[0][0], 0)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM vaults')[0][0], 0)  # transient failure never poisons the candidate

    def test_never_touches_entry_or_monitoring_budgets(self):
        source = Path(cf.__file__).read_text()
        for forbidden in ('MonitoringBudget', 'ownership_budgets', 'paper_monitoring', 'HistoryProgress', 'Ledger(', 'reserve(', 'desk.ledger', 'import paper_cycle '):
            self.assertNotIn(forbidden, source)

    # ----------------------------------------------- malformed / dead / closed
    def test_malformed_provider_data_fails_closed(self):
        fixtures = self.candidates(2); chain = FakeChain(fixtures)
        cf.sample(self.store, chain, now=T0 + 100)
        other = pool_fixture(99)
        def corrupt(values):
            values[0] = token_account(other.mint, other.pool, 5)  # wrong identity for candidate 0 base vault
            return values
        chain.mutate = corrupt
        cf.sample(self.store, chain, now=T0 + 301)
        rows = dict(((m, s), c) for m, s, c in self.rows('SELECT mint,status,code FROM samples'))
        self.assertEqual(rows[(fixtures[0].mint, 'FAILED')], 'VAULT_ACCOUNT_INVALID')
        self.assertEqual(self.rows("SELECT price FROM samples WHERE status='FAILED'"), [(None,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM samples WHERE status='OK'")[0][0], 1)
        # A response with the wrong shape records nothing and stays retryable.
        chain.mutate = lambda values: values[:-1]
        before = self.rows('SELECT COUNT(*) FROM samples')[0][0]
        cf.sample(self.store, chain, now=T0 + 901)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM samples')[0][0], before)
        self.assertIn(('FAILED', 'RESPONSE_INVALID'), self.rows('SELECT status,code FROM request_results'))

    def test_pool_account_invalid_marks_candidate_unresolved_once(self):
        fixtures = self.candidates(1); chain = FakeChain(fixtures)
        chain.mutate = lambda values: [account(bytes(300), PUMPSWAP)]
        cf.sample(self.store, chain, now=T0 + 301)
        self.assertEqual(self.rows('SELECT status,code FROM vaults'), [('UNRESOLVED', 'POOL_ACCOUNT_INVALID')])
        chain.mutate = None
        self.assertEqual(cf.sample(self.store, chain, now=T0 + 302)['requests'], 0)  # recorded, not hammered

    def test_dead_and_closed_pools_are_recorded(self):
        fixtures = self.candidates(2); chain = FakeChain(fixtures)
        cf.sample(self.store, chain, now=T0 + 100)
        chain.reserves[fixtures[0].pool] = (0, 0)
        chain.reserves[fixtures[1].pool] = (None, None)  # vaults closed
        cf.sample(self.store, chain, now=T0 + 301)
        self.assertEqual(sorted(self.rows('SELECT status,code FROM samples')), [('POOL_DEAD', 'VAULT_CLOSED'), ('POOL_DEAD', 'ZERO_RESERVE')])

    # ------------------------------------------------------------- report
    def put_sample(self, mint, horizon, price, quote=1):
        with closing(sqlite3.connect(self.store)) as c:
            c.execute("INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,'OK',NULL,NULL)", (mint, horizon, T0 + horizon, T0 + horizon, 1, '1', str(quote), str(price)))
            c.commit()

    def put_outcome(self, mint, payload):
        with closing(sqlite3.connect(self.store)) as c:
            c.execute('INSERT INTO outcomes(mint,at,payload) VALUES(?,?,?)', (mint, T0, json.dumps(payload)))
            c.commit()

    def test_report_known_answers_per_rejection_code(self):
        # T26F: outcome payloads are now the classified v2 form (BOUGHT / REJECTED:<stage>:<code> / NOT_DISPATCHED /
        # UNKNOWN); the old flat `dispatched/status/codes` payload misclassified token and engine rejections.
        fixtures = self.candidates(5)
        rejected = {'v': 2, 'class': 'REJECTED', 'stage': 'ENGINE', 'codes': ['COST_BUDGET'], 'detail': None}
        plan = [  # (outcome, +5m price, +1h price)
            (rejected, 100, 150),                                                                # rejected winner +50%
            (rejected, 100, 50),                                                                 # rejected loser -50%
            ({'v': 2, 'class': 'BOUGHT', 'stage': None, 'codes': [], 'detail': None}, 100, 200),   # bought +100%
            ({'v': 2, 'class': 'NOT_DISPATCHED', 'stage': None, 'codes': [], 'detail': None}, 100, 100),  # 0%
            (None, 100, 300)]                                                                    # outcome never recorded
        for f, (outcome, first, hour) in zip(fixtures, plan):
            self.put_sample(f.mint, 300, first); self.put_sample(f.mint, 3600, hour)
            if outcome:
                self.put_outcome(f.mint, outcome)
        r = cf.report(self.store)
        g = r['groups']
        self.assertEqual(set(g), {'REJECTED:ENGINE:COST_BUDGET', 'BOUGHT', 'NOT_DISPATCHED', 'UNKNOWN'})
        rej = g['REJECTED:ENGINE:COST_BUDGET']
        self.assertEqual((rej['candidates'], rej['with_return'], rej['baseline_missing']), (2, 2, 0))
        self.assertEqual(Decimal(rej['mean_return']), Decimal(0))
        self.assertEqual(Decimal(rej['median_return']), Decimal(0))
        self.assertEqual(Decimal(rej['p10_return']), Decimal('-0.5')); self.assertEqual(Decimal(rej['p90_return']), Decimal('0.5'))
        self.assertEqual(Decimal(rej['share_positive']), Decimal('0.5'))
        self.assertEqual(Decimal(g['BOUGHT']['median_return']), Decimal(1))
        self.assertEqual(Decimal(g['UNKNOWN']['median_return']), Decimal(2))
        self.assertEqual(r['unknown_breakdown'], {'NO_OUTCOME_RECORDED': 1})
        self.assertEqual((r['distinct_candidates'], r['label']), (5, cf.LABEL))

    def test_report_is_read_only_and_cli_runs(self):
        fixtures = self.candidates(1); self.put_sample(fixtures[0].mint, 300, 5); self.put_sample(fixtures[0].mint, 3600, 10)
        before = self.store.read_bytes()
        with patch('sys.stdout') as out:
            self.assertEqual(cf.main(['report', '--store', str(self.store)]), 0)
        self.assertEqual(self.store.read_bytes(), before)
        with patch('sys.stdout'):
            self.assertEqual(cf.main(['report', '--store', str(self.root / 'missing.sqlite')]), 2)

    # --------------------------------------------------------- store rules
    def test_store_is_append_only_and_init_refuses_existing(self):
        fixtures = self.candidates(1)
        self.put_outcome(fixtures[0].mint, {'dispatched': False, 'status': None, 'codes': []})
        chain = FakeChain(fixtures)
        cf.sample(self.store, chain, now=T0 + 301)  # populates vaults, requests, request_results, samples
        for table in ('candidates', 'outcomes', 'vaults', 'requests', 'request_results', 'samples', 'policy'):
            self.assertGreater(self.rows(f'SELECT COUNT(*) FROM {table}')[0][0], 0, table)  # triggers only fire on existing rows
        for sql in ('UPDATE candidates SET pool=pool', 'DELETE FROM candidates', 'UPDATE policy SET allowance_per_hour=1',
                    'DELETE FROM requests', 'UPDATE vaults SET status=status', 'DELETE FROM outcomes',
                    'UPDATE samples SET price=price', 'DELETE FROM samples', 'UPDATE request_results SET status=status', 'DELETE FROM request_results'):
            with closing(sqlite3.connect(self.store)) as c, self.assertRaises(sqlite3.DatabaseError):
                c.execute(sql)
        with self.assertRaises(cf.CounterfactualError):
            cf.init(self.store)
        self.assertEqual(oct(self.store.stat().st_mode & 0o777), '0o600')

    def test_real_transport_requires_shared_pacing_and_credential(self):
        with self.assertRaises(cf.TransportError):
            cf.HeliusTransport(None)
        with patch.dict('os.environ', {}, clear=True), self.assertRaises(cf.TransportError):
            cf.HeliusTransport(object())

    # ------------------------------------------------- discovery / journal
    def discovery_fixture(self, path=None):
        from discovery import continuous as discovery
        from tests.test_graduation_witness import fixture as migration_fixture
        from desk.model import canonical
        from desk.programs import unbase58
        at = int(T0)
        if path is None:
            path = self.root / 'discovery.sqlite'
            with patch.object(discovery.time, 'time', return_value=at - 600):
                discovery.initialize(path)
        raw, mint, pool = migration_fixture()
        raw['transaction']['signatures'] = [base58(bytes([9]) * 64)]
        raw['blockTime'] = at - 600
        ix = raw['meta']['innerInstructions'][0]['instructions'][0]
        data = bytearray(unbase58(ix['data'])); data[136:144] = raw['blockTime'].to_bytes(8, 'little', signed=True); ix['data'] = base58(data)
        wire = canonical({'method': 'transactionNotification', 'params': {'result': {
            'signature': raw['transaction']['signatures'][0], 'slot': raw['slot'], 'blockTime': raw['blockTime'],
            'transaction': {'transaction': raw['transaction'], 'meta': raw['meta']}}}})
        d = discovery.Store(path, clock=lambda: at - 600)
        try:
            d.complete(d.reserve('RECEIVE'), payload=wire.encode())
        finally:
            d.close()
        return path, str(mint), str(pool), raw

    def journal_with(self, mint, body, *, result=True, name='journal.sqlite', scan_id=None):
        from tools import paper_entry_dispatcher as dispatcher
        journal = self.root / name
        with closing(sqlite3.connect(journal)) as c:
            for sql in dispatcher.SCHEMAS.values():
                c.execute(sql)
            c.execute('INSERT INTO intents VALUES(?,?,?,?,?)', ('a' * 32, mint, 'sig', '{}', 'h'))
            if result:
                wrapper = {'result': body} if scan_id is None else {'scan_id': scan_id, 'result': body}
                c.execute('INSERT INTO results VALUES(?,?,?)', ('a' * 32, json.dumps(wrapper), 'h'))
            c.commit()
        return journal

    def test_ingest_from_discovery_and_journal_outcome(self):
        # T26F: a stored result is classified by its KIND. The previous version of this test fed a bare
        # {'status','blockers'} body and expected the blockers to be the rejection code; a typed cycle result
        # is what the dispatcher really writes.
        path, mint, pool, raw = self.discovery_fixture()
        result = cf.ingest(self.store, path, now=T0)
        self.assertEqual((result['candidates_added'], result['rows_skipped']), (1, 0))
        self.assertEqual(self.rows('SELECT mint,pool,signature FROM candidates'), [(mint, pool, raw['transaction']['signatures'][0])])
        self.assertEqual(cf.ingest(self.store, path, now=T0)['candidates_added'], 0)  # cursor + OR IGNORE: idempotent
        # T26G item 1 changed this fixture on purpose: the blocker used to be MAYHEM_POOL, which is not in T01's
        # NORMAL set (a bare BLOCKED MAYHEM_POOL with no terminal receipt is a latch, now UNRESOLVED). The rejection
        # example is MARKET_PRODUCER_BLOCKED, a NORMAL blocker; the assertion's strength is unchanged.
        journal = self.journal_with(mint, {'kind': 'paper_cycle_v1', 'status': 'BLOCKED', 'blockers': ['MARKET_PRODUCER_BLOCKED'], 'outcomes': []})
        cf.refresh_outcomes(self.store, journal, now=T0 + 1)
        cf.refresh_outcomes(self.store, journal, now=T0 + 2)  # unchanged outcome: no duplicate row
        last = json.loads(self.rows('SELECT payload FROM outcomes ORDER BY id DESC LIMIT 1')[0][0])
        self.assertEqual(last, {'v': 2, 'class': 'REJECTED', 'stage': 'OBSERVATIONS', 'codes': ['MARKET_PRODUCER_BLOCKED'], 'detail': None, 'source': 'JOURNAL'})
        self.assertEqual(self.rows('SELECT COUNT(*) FROM outcomes')[0][0], 2)    # NOT_DISPATCHED/UNKNOWN first, then the rejection
        self.assertEqual(cf._outcome(self.root / 'nope.sqlite', mint)['detail'], 'JOURNAL_UNREADABLE')

    def test_altered_discovery_original_is_skipped_not_learned(self):
        path, mint, pool, raw = self.discovery_fixture()
        with closing(sqlite3.connect(path)) as c:
            for name, in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='raw_events'").fetchall():
                c.execute(f'DROP TRIGGER {name}')
            c.execute("UPDATE raw_events SET payload_hash=?", ('0' * 64,)); c.commit()
        result = cf.ingest(self.store, path, now=T0)
        self.assertEqual((result['candidates_added'], result['rows_skipped']), (0, 1))


class CounterfactualBase(unittest.TestCase):
    """Fixtures shared by the T26F test classes (the same helpers, without re-running the original tests)."""


for _name in ('setUp', 'candidates', 'rows', 'put_sample', 'put_outcome', 'discovery_fixture', 'journal_with'):
    setattr(CounterfactualBase, _name, getattr(CounterfactualTests, _name))


class OutcomeClassificationTests(CounterfactualBase):
    """T26F item 1: every result shape, with the real dispatcher writers where they exist."""

    # known-answer table: stored body -> classification
    SHAPES = [
        ({'kind': 'dispatcher_token_rejection_v1', 'token_policy': {'decision': 'SKIP', 'reasons': ['ACTIVE_FREEZE_AUTHORITY', 'ACTIVE_MINT_AUTHORITY']}},
         ('REJECTED', 'TOKEN', ['ACTIVE_FREEZE_AUTHORITY', 'ACTIVE_MINT_AUTHORITY'])),
        ({'kind': 'dispatcher_migration_no_entry_v1', 'reason': 'UNSUPPORTED_QUOTE'}, ('REJECTED', 'MIGRATION', ['UNSUPPORTED_QUOTE'])),
        ({'kind': 'history_preparation_no_entry_v1', 'reason': 'INSUFFICIENT_HISTORY'}, ('REJECTED', 'HISTORY', ['INSUFFICIENT_HISTORY'])),
        ({'kind': 'paper_cycle_v1', 'status': 'COMPLETE', 'outcomes': [{'type': 'reject', 'reason': 'COST_BUDGET', 'reasons': ['COST_BUDGET', 'MAX_POSITIONS']}]},
         ('REJECTED', 'ENGINE', ['COST_BUDGET', 'MAX_POSITIONS'])),
        ({'kind': 'paper_cycle_v1', 'status': 'BLOCKED', 'blockers': ['MARKET_PRODUCER_BLOCKED'], 'outcomes': []},
         ('REJECTED', 'OBSERVATIONS', ['MARKET_PRODUCER_BLOCKED'])),
        # T26G item 1: only T01's NORMAL blockers (or a certified/terminal result) are rejections; any other
        # BLOCKED result is a latch, shown as UNRESOLVED and never counted as a filter's rejection.
        ({'kind': 'paper_cycle_v1', 'status': 'BLOCKED', 'blockers': ['USD_ORIGINAL_BINDING_INVALID'], 'outcomes': []},
         ('UNRESOLVED', 'OBSERVATIONS', ['USD_ORIGINAL_BINDING_INVALID'])),                    # category (b)
        ({'kind': 'paper_cycle_v1', 'status': 'BLOCKED', 'blockers': ['MARKET_PRODUCER_BLOCKED', 'USD_ORIGINAL_BINDING_INVALID'], 'outcomes': []},
         ('UNRESOLVED', 'OBSERVATIONS', ['MARKET_PRODUCER_BLOCKED', 'USD_ORIGINAL_BINDING_INVALID'])),   # one latch poisons the set
        ({'kind': 'paper_cycle_v1', 'status': 'BLOCKED', 'blockers': ['MAYHEM_POOL'], 'outcomes': []}, ('UNRESOLVED', 'OBSERVATIONS', ['MAYHEM_POOL'])),
        ({'kind': 'paper_cycle_v1', 'status': 'BLOCKED', 'blockers': ['MAYHEM_POOL'], 'outcomes': [], 'terminal_receipt_hash': 'ab' * 32},
         ('REJECTED', 'OBSERVATIONS', ['MAYHEM_POOL'])),                                        # certified terminal hazard rejection
        ({'kind': 'paper_cycle_v1', 'status': 'BLOCKED', 'blockers': ['MAYHEM_POOL'], 'outcomes': [], 'terminal_receipt_hash': 'not-a-hash'},
         ('UNRESOLVED', 'OBSERVATIONS', ['MAYHEM_POOL'])),
        ({'kind': 'paper_cycle_v1', 'status': 'COMPLETE', 'outcomes': [{'type': 'fill', 'side': 'buy', 'mint': 'm'}]}, ('BOUGHT', None, [])),
        ({'kind': 'paper_cycle_v1', 'status': 'RECOVERY_REQUIRED', 'blockers': ['X']}, ('UNKNOWN', None, [])),
        ({'kind': 'paper_cycle_v1', 'status': 'COMPLETE', 'outcomes': []}, ('UNKNOWN', None, [])),
        ({'kind': 'paper_cycle_v1', 'status': 'BLOCKED', 'blockers': []}, ('UNKNOWN', None, [])),
        ({'kind': 'something_new_v9'}, ('UNKNOWN', None, [])),
        ({'status': 'COMPLETE'}, ('UNKNOWN', None, [])),      # the shape the old code called "admitted"
        ('not an object', ('UNKNOWN', None, [])),
        (None, ('UNKNOWN', None, [])),
    ]

    def test_every_result_shape_known_answers(self):
        for body, (klass, stage, codes) in self.SHAPES:
            with self.subTest(body=body):
                got = cf.classify_result(body)
                self.assertEqual((got['class'], got['stage'], got['codes']), (klass, stage, codes))
                if klass in ('UNKNOWN', 'UNRESOLVED'):
                    self.assertTrue(got['detail'])   # never an unexplained unknown

    def test_report_groups_one_per_code_and_not_a_rejection_when_unknown(self):
        names, detail = cf.groups_of({'v': 2, 'class': 'REJECTED', 'stage': 'TOKEN', 'codes': ['A', 'B'], 'detail': None})
        self.assertEqual((names, detail), (['REJECTED:TOKEN:A', 'REJECTED:TOKEN:B'], None))
        self.assertEqual(cf.groups_of({'v': 2, 'class': 'UNKNOWN', 'detail': 'RECOVERY_REQUIRED'}), (['UNKNOWN'], 'RECOVERY_REQUIRED'))
        self.assertEqual(cf.groups_of({'dispatched': True, 'status': 'COMPLETE'}), (['UNKNOWN'], 'LEGACY_OUTCOME_FORMAT'))

    def dispatcher(self):
        from tests import test_paper_entry_dispatcher as fixtures
        d = fixtures.DispatcherTests('test_dry_run_no_admission_credentials_or_io_and_context_activation')
        d.setUp(); self.addCleanup(d.doCleanups)
        return d

    def test_real_dispatcher_writers_buy_token_rejection_and_engine_rejection(self):
        buy = self.dispatcher()
        self.assertEqual(buy.live()['paper_status'], 'COMPLETE')
        journal_only = cf._outcome(buy.journal, buy.mint)
        self.assertEqual((journal_only['class'], journal_only['source']), ('BOUGHT', 'JOURNAL'))
        with_ledger = cf._outcome(buy.journal, buy.mint, ledger_db=buy.ledger)
        self.assertEqual((with_ledger['class'], with_ledger['source']), ('BOUGHT', 'LEDGER'))
        self.assertEqual(cf._outcome(buy.journal, 'SOME_OTHER_MINT')['class'], 'NOT_DISPATCHED')

        token = self.dispatcher()
        self.assertEqual(token.rejection(mint_tag=1)['status'], 'TOKEN_REJECTED')
        got = cf._outcome(token.journal, token.mint)
        self.assertEqual((got['class'], got['stage'], got['codes']), ('REJECTED', 'TOKEN', ['ACTIVE_MINT_AUTHORITY']))
        both = self.dispatcher()
        self.assertEqual(both.rejection(mint_tag=1, freeze_tag=1)['status'], 'TOKEN_REJECTED')
        self.assertEqual(set(cf._outcome(both.journal, both.mint)['codes']), {'ACTIVE_MINT_AUTHORITY', 'ACTIVE_FREEZE_AUTHORITY'})

        engine = self.dispatcher()
        self.assertEqual(engine.live(roundtrip_output=10_000_000)['paper_status'], 'COMPLETE')   # status COMPLETE, but a REJECTION
        got = cf._outcome(engine.journal, engine.mint)
        self.assertEqual((got['class'], got['stage'], got['codes']), ('REJECTED', 'ENGINE', ['COST_BUDGET']))
        # Even a ledger that has no BUY cannot turn that rejection into BOUGHT.
        self.assertEqual(cf._outcome(engine.journal, engine.mint, ledger_db=engine.ledger)['class'], 'REJECTED')

    def test_ledger_buy_wins_over_the_journal_and_a_corrupt_row_is_unknown(self):
        mint = pool_fixture(7).mint
        journal = self.journal_with(mint, {'kind': 'history_preparation_no_entry_v1', 'reason': 'X'})
        ledger = self.root / 'ledger.sqlite'
        from desk.ledger import Ledger
        Ledger(ledger).close()
        with closing(sqlite3.connect(ledger)) as c:
            c.execute("INSERT INTO events(event_id,ts,payload,payload_hash) VALUES('e',1,'{}','h')")
            c.execute('INSERT INTO outcomes(event_id,payload) VALUES(?,?)', ('e', json.dumps({'type': 'fill', 'side': 'buy', 'mint': mint})))
            c.commit()
        self.assertEqual(cf._outcome(journal, mint, ledger_db=ledger)['class'], 'BOUGHT')
        broken = self.journal_with(pool_fixture(8).mint, None, result=False, name='broken.sqlite')       # intent without any result row
        self.assertEqual(cf._outcome(broken, pool_fixture(8).mint)['detail'], 'UNRESOLVED_NO_RESULT')
        with closing(sqlite3.connect(journal)) as c:
            c.execute("UPDATE results SET payload='not json'"); c.commit()
        self.assertEqual(cf._outcome(journal, mint)['detail'], 'OUTCOME_UNREADABLE')

    def decisions_db(self, rows):
        from desk import decision_runner
        path = self.root / 'decisions.sqlite'
        with closing(sqlite3.connect(path)) as c:
            decision_runner._initialize_journal(c)       # the real DDL
            for scan, mint, decision in rows:
                c.execute('INSERT INTO decisions VALUES(?,?,?,?)', (scan, 'h', '{}', json.dumps(
                    {'kind': 'paper_candidate_decision', 'scan_id': scan, 'mint': mint, 'decision': decision, 'reasons': ['INVESTIGATION_X', 'FLOW_WEAK']})))
            c.commit()
        return path

    def test_decision_store_join_and_failure_modes(self):
        rejected, accepted, other = pool_fixture(21).mint, pool_fixture(22).mint, pool_fixture(23).mint
        decisions = self.decisions_db([('s1', rejected, 'REJECT'), ('s2', accepted, 'ACCEPT')])
        journal = self.journal_with(other, {'kind': 'history_preparation_no_entry_v1', 'reason': 'Z'})
        got = cf._outcome(journal, rejected, decisions_db=decisions)
        self.assertEqual((got['class'], got['stage'], got['codes'], got['source']), ('REJECTED', 'DECISION', ['INVESTIGATION_X', 'FLOW_WEAK'], 'DECISIONS'))
        self.assertEqual(cf._outcome(journal, accepted, decisions_db=decisions)['class'], 'NOT_DISPATCHED')   # not a rejection record
        self.assertEqual(cf._outcome(journal, other, decisions_db=decisions)['stage'], 'HISTORY')              # the typed journal result wins
        # An unreadable decision store must not be reported as "never dispatched".
        self.assertEqual(cf._outcome(journal, rejected, decisions_db=self.root / 'missing.sqlite')['detail'], 'DECISIONS_UNREADABLE')
        # Without any journal the candidate is UNKNOWN, never NOT_DISPATCHED.
        self.assertEqual(cf._outcome(None, rejected)['detail'], 'NO_OUTCOME_SOURCE')
        self.assertEqual(cf._outcome(None, rejected, decisions_db=decisions)['class'], 'REJECTED')

    def test_refresh_reuses_one_journal_view_and_never_rereads_final_outcomes(self):
        fixtures = self.candidates(4)
        journal = self.root / 'journal.sqlite'
        from tools import paper_entry_dispatcher as dispatcher
        with closing(sqlite3.connect(journal)) as c:
            for sql in dispatcher.SCHEMAS.values():
                c.execute(sql)
            for n, (f, body) in enumerate(zip(fixtures[:3], [
                    {'kind': 'history_preparation_no_entry_v1', 'reason': 'A'}, {'kind': 'history_preparation_no_entry_v1', 'reason': 'B'},
                    {'kind': 'paper_cycle_v1', 'status': 'COMPLETE', 'outcomes': [{'type': 'fill', 'side': 'buy'}]}])):
                c.execute('INSERT INTO intents VALUES(?,?,?,?,?)', (f'{n}' * 32, f.mint, f'sig{n}', '{}', 'h'))
                c.execute('INSERT INTO results VALUES(?,?,?)', (f'{n}' * 32, json.dumps({'result': body}), 'h'))
            c.commit()
        opened = []
        real = cf._connect
        def spy(path, **kw):
            opened.append((str(path), kw.get('readonly', False)))
            return real(path, **kw)
        with patch.object(cf, '_connect', side_effect=spy):
            cf.refresh_outcomes(self.store, journal, now=T0 + 10)
        self.assertEqual([o for o in opened if o[0] == str(journal) and o[1]], [(str(journal), True)])   # ONE journal connection for 4 candidates
        classes = [json.loads(p)['class'] for (p,) in self.rows('SELECT payload FROM outcomes ORDER BY mint')]
        self.assertEqual(sorted(classes), ['BOUGHT', 'NOT_DISPATCHED', 'REJECTED', 'REJECTED'])
        # Typed results are final: nothing is read again; NOT_DISPATCHED is re-checked until the dispatch window closes.
        calls = []
        real_classify = cf.OutcomeReader.classify
        def counting(self_, mint):
            calls.append(mint)
            return real_classify(self_, mint)
        with patch.object(cf.OutcomeReader, 'classify', counting):
            summary = cf.refresh_outcomes(self.store, journal, now=T0 + 20)
        self.assertEqual(calls, [fixtures[3].mint])
        self.assertEqual((summary['final'], summary['appended']), (3, 0))
        calls.clear()
        with patch.object(cf.OutcomeReader, 'classify', counting):             # window closed but the grace (120 s) is not over
            cf.refresh_outcomes(self.store, journal, now=T0 + cf.WINDOW_MAX_SECONDS + 60)
        self.assertEqual(calls, [fixtures[3].mint])
        calls.clear()
        with patch.object(cf.OutcomeReader, 'classify', counting):             # window and grace over: NOT_DISPATCHED is final too
            summary = cf.refresh_outcomes(self.store, journal, now=T0 + cf.WINDOW_MAX_SECONDS + cf.DEFAULT_GRACE + 600)
        self.assertEqual((calls, summary['final']), ([], 4))


class LatchDecisionAndBackoffTests(CounterfactualBase):
    """T26G: latches are not filter rejections; decision results are provisional; null-pool backoff keeps the +5m window."""

    LATCH = {'kind': 'paper_cycle_v1', 'status': 'BLOCKED', 'blockers': ['USD_ORIGINAL_BINDING_INVALID'], 'outcomes': []}

    def evidence_with_no_entry(self, scan_id, *, name='evidence.sqlite'):
        from desk import paper_cycle_no_entry as no_entry
        path = self.root / name
        with closing(sqlite3.connect(path)) as c:
            c.execute(no_entry.SQL)
            c.execute(f'INSERT INTO {no_entry.TABLE} VALUES(?,?,?,?)', ('p' * 32, scan_id, 'i' * 64, 'o' * 64))
            c.commit()
        return path

    def test_item1_a_latch_is_unresolved_and_a_no_entry_row_or_normal_blocker_makes_it_a_rejection(self):
        from desk.paper_cycle_no_entry import NORMAL
        mint = pool_fixture(31).mint
        journal = self.journal_with(mint, self.LATCH, scan_id='scan-31')
        got = cf._outcome(journal, mint)
        self.assertEqual((got['class'], got['stage'], got['codes'], got['source']),
                         ('UNRESOLVED', 'OBSERVATIONS', ['USD_ORIGINAL_BINDING_INVALID'], 'JOURNAL'))
        self.assertFalse(cf.groups_of(got)[0][0].startswith('REJECTED'))
        self.assertEqual(cf.groups_of(got), (['UNRESOLVED:USD_ORIGINAL_BINDING_INVALID'], None))
        # A paper_cycle_no_entry row for exactly this scan proves the pass was retired as a normal rejection.
        proven = self.evidence_with_no_entry('scan-31')
        self.assertEqual(cf._outcome(journal, mint, evidence_db=proven)['class'], 'REJECTED')
        other = self.evidence_with_no_entry('another-scan', name='other.sqlite')
        self.assertEqual(cf._outcome(journal, mint, evidence_db=other)['class'], 'UNRESOLVED')
        # An unreadable or table-less evidence store can prove nothing: still UNRESOLVED (fail closed).
        unreadable = cf._outcome(journal, mint, evidence_db=self.root / 'missing.sqlite')
        self.assertEqual((unreadable['class'], unreadable['detail']), ('UNRESOLVED', 'LATCH_EVIDENCE_UNREADABLE'))
        self.assertEqual(got['detail'], 'LATCH_NOT_A_NORMAL_REJECTION')
        empty = self.root / 'empty.sqlite'
        sqlite3.connect(empty).close()
        self.assertEqual(cf._outcome(journal, mint, evidence_db=empty)['class'], 'UNRESOLVED')
        # Every NORMAL blocker stays a rejection without any row (the imported set, not a copy).
        for code in sorted(NORMAL):
            body = {'kind': 'paper_cycle_v1', 'status': 'BLOCKED', 'blockers': [code], 'outcomes': []}
            self.assertEqual(cf.classify_result(body)['class'], 'REJECTED', code)

    def test_item1_report_shows_latches_separately_and_never_as_a_rejection_group(self):
        fixtures = self.candidates(2)
        self.put_outcome(fixtures[0].mint, {'v': 2, 'class': 'UNRESOLVED', 'stage': 'OBSERVATIONS',
                                            'codes': ['USD_ORIGINAL_BINDING_INVALID'], 'detail': 'LATCH_NOT_A_NORMAL_REJECTION', 'source': 'JOURNAL'})
        self.put_outcome(fixtures[1].mint, {'v': 2, 'class': 'REJECTED', 'stage': 'OBSERVATIONS',
                                            'codes': ['MARKET_PRODUCER_BLOCKED'], 'detail': None, 'source': 'JOURNAL'})
        groups = cf.report(self.store)['groups']
        self.assertEqual(sorted(groups), ['REJECTED:OBSERVATIONS:MARKET_PRODUCER_BLOCKED', 'UNRESOLVED:USD_ORIGINAL_BINDING_INVALID'])

    def test_item1_unresolved_is_never_final_so_a_later_no_entry_row_can_reclassify_it(self):
        mint = pool_fixture(32).mint
        self.candidates(1, offset=31)
        journal = self.journal_with(mint, self.LATCH, scan_id='scan-32')
        self.assertEqual(cf.refresh_outcomes(self.store, journal, now=T0 + cf.WINDOW_MAX_SECONDS + cf.DEFAULT_GRACE + 10_000)['final'], 0)
        cf.refresh_outcomes(self.store, journal, now=T0 + 20_000)
        last = lambda: json.loads(self.rows('SELECT payload FROM outcomes ORDER BY id DESC LIMIT 1')[0][0])['class']
        self.assertEqual(last(), 'UNRESOLVED')
        proven = self.evidence_with_no_entry('scan-32')
        cf.refresh_outcomes(self.store, journal, evidence_db=proven, now=T0 + 20_001)
        self.assertEqual(last(), 'REJECTED')

    def test_item1_cli_ingest_accepts_the_evidence_store(self):
        path, mint, pool, raw = CounterfactualTests.discovery_fixture(self)
        journal = self.journal_with(mint, self.LATCH, scan_id='scan-cli')
        proven = self.evidence_with_no_entry('scan-cli')
        with patch.object(cf.time, 'time', return_value=T0), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cf.main(['ingest', '--store', str(self.store), '--discovery-db', str(path), '--journal', str(journal),
                                      '--evidence-db', str(proven)]), 0)
        self.assertEqual(json.loads(out.getvalue())['outcomes_appended'], 1)
        self.assertEqual(json.loads(self.rows('SELECT payload FROM outcomes')[0][0])['class'], 'REJECTED')

    # ---------------------------------------------------------------- item 2
    def decisions(self, mint, decision='REJECT'):
        from desk import decision_runner
        path = self.root / 'decisions.sqlite'
        with closing(sqlite3.connect(path)) as c:
            decision_runner._initialize_journal(c)
            c.execute('INSERT INTO decisions VALUES(?,?,?,?)', ('s1', 'h', '{}', json.dumps(
                {'kind': 'paper_candidate_decision', 'scan_id': 's1', 'mint': mint, 'decision': decision, 'reasons': ['FLOW_WEAK']})))
            c.commit()
        return path

    def ledger_with_buy(self, mint):
        from desk.ledger import Ledger
        ledger = self.root / 'ledger.sqlite'
        Ledger(ledger).close()
        with closing(sqlite3.connect(ledger)) as c:
            c.execute("INSERT INTO events(event_id,ts,payload,payload_hash) VALUES('e',1,'{}','h')")
            c.execute('INSERT INTO outcomes(event_id,payload) VALUES(?,?)', ('e', json.dumps({'type': 'fill', 'side': 'buy', 'mint': mint})))
            c.commit()
        return ledger

    def test_item2_a_decision_reject_is_provisional_until_the_dispatch_window_closes_and_a_later_buy_overrides_it(self):
        (fixture,) = self.candidates(1)
        journal = self.journal_with(pool_fixture(99).mint, {'kind': 'history_preparation_no_entry_v1', 'reason': 'OTHER'})
        decisions = self.decisions(fixture.mint)
        last = lambda: json.loads(self.rows('SELECT payload FROM outcomes ORDER BY id DESC LIMIT 1')[0][0])
        cf.refresh_outcomes(self.store, journal, decisions_db=decisions, now=T0 + 10)
        self.assertEqual((last()['class'], last()['stage'], last()['source']), ('REJECTED', 'DECISION', 'DECISIONS'))
        # Still provisional well after the first sample, so the next pass re-reads it...
        calls = []
        real = cf.OutcomeReader.classify
        with patch.object(cf.OutcomeReader, 'classify', lambda self_, m: (calls.append(m), real(self_, m))[1]):
            summary = cf.refresh_outcomes(self.store, journal, decisions_db=decisions, now=T0 + 3600)
        self.assertEqual((calls, summary['final']), ([fixture.mint], 0))
        # ...and a later ledger BUY (priority ledger > journal > decision) overrides the decision rejection.
        ledger = self.ledger_with_buy(fixture.mint)
        cf.refresh_outcomes(self.store, journal, ledger_db=ledger, decisions_db=decisions, now=T0 + 3700)
        self.assertEqual((last()['class'], last()['source']), ('BOUGHT', 'LEDGER'))
        self.assertEqual(self.rows('SELECT COUNT(*) FROM outcomes')[0][0], 2)        # append-only: the rejection row is kept

    def test_item2_decision_result_becomes_final_only_after_window_and_grace(self):
        (fixture,) = self.candidates(1)
        journal = self.journal_with(pool_fixture(98).mint, {'kind': 'history_preparation_no_entry_v1', 'reason': 'OTHER'})
        decisions = self.decisions(fixture.mint)
        cf.refresh_outcomes(self.store, journal, decisions_db=decisions, now=T0 + 10)
        edge = T0 + cf.WINDOW_MAX_SECONDS + cf.DEFAULT_GRACE
        self.assertEqual(cf.refresh_outcomes(self.store, journal, decisions_db=decisions, now=edge)['final'], 0)
        self.assertEqual(cf.refresh_outcomes(self.store, journal, decisions_db=decisions, now=edge + 1)['final'], 1)
        # A typed journal rejection (not a decision) is final immediately, as before.
        typed = self.candidates(1, offset=5)[0]
        journal2 = self.journal_with(typed.mint, {'kind': 'history_preparation_no_entry_v1', 'reason': 'OTHER'}, name='j2.sqlite')
        cf.refresh_outcomes(self.store, journal2, now=T0 + 10)
        self.assertEqual(cf.refresh_outcomes(self.store, journal2, now=T0 + 11)['final'], 1)

    # ---------------------------------------------------------------- item 3
    def test_item3_null_pool_delay_never_overshoots_a_sample_window(self):
        m, g = T0, cf.DEFAULT_GRACE
        self.assertEqual(cf.null_pool_delay(1), 60)                       # legacy call shape unchanged
        self.assertEqual(cf.null_pool_delay(9), 900)
        self.assertEqual(cf.null_pool_delay(1, last=m + 10, migrated=m), 60)         # far from the window: base backoff
        self.assertEqual(cf.null_pool_delay(3, last=m + 210, migrated=m), 90)        # 240 s base capped at the +5m window start
        self.assertEqual(cf.null_pool_delay(4, last=m + 299.5, migrated=m), 1)       # never zero, never negative
        for last in (m + 300, m + 330, m + 300 + g):                                  # inside the +5m window
            self.assertEqual(cf.null_pool_delay(7, last=last, migrated=m), 30)
        self.assertEqual(cf.null_pool_delay(7, last=m + 300 + g + 1, migrated=m), 900 - (300 + g + 1))   # capped at the +15m window start
        self.assertEqual(cf.null_pool_delay(7, last=m + 900 + 50, migrated=m), 30)   # inside the +15m window
        self.assertEqual(cf.null_pool_delay(7, last=m + cf.HORIZONS[-1] + g + 5, migrated=m), 900)   # after every window

    def test_item3_a_pool_visible_late_in_the_first_window_is_still_resolved_and_sampled_at_plus_5m(self):
        (fixture,) = self.candidates(1)
        chain = FakeChain([fixture])
        visible_from = T0 + 340                                           # becomes visible inside the +5m window (300..420 s)
        state = {'now': None}

        def call(method, params):
            values = chain(method, params)
            if state['now'] < visible_from and params[0] == [fixture.pool]:
                return {**values, 'value': [None]}
            return values
        t = T0 + 10
        while t <= T0 + 430:                                              # a 10 s timer
            state['now'] = t
            cf.sample(self.store, call, now=t)
            t += 10
        self.assertEqual(self.rows('SELECT status FROM vaults'), [('OK',)])
        (row,) = self.rows('SELECT status,sampled_at FROM samples WHERE horizon=300')
        self.assertEqual(row[0], 'OK')
        self.assertLessEqual(row[1], T0 + 300 + cf.DEFAULT_GRACE)
        attempts = self.rows('SELECT at FROM vault_attempts ORDER BY id')
        self.assertTrue(all(b - a <= 120 for a, b in zip([x for (x,) in attempts], [x for (x,) in attempts][1:])), attempts)
        in_window = [a for (a,) in attempts if T0 + 300 <= a <= T0 + 420]
        self.assertLessEqual(len(in_window), 5)                           # 30 s cadence inside the window: bounded cost

    def test_item3_the_old_schedule_would_have_missed_the_window(self):
        """Documents the defect: pure doubling (60,120,240,480) tries at +10,+70,+190,+430, after the window."""
        attempt, delays = T0 + 10, []
        for n in range(1, 5):
            delays.append(min(60 * 2 ** (n - 1), 900))
            attempt += delays[-1]
        self.assertGreater(attempt, T0 + 300 + cf.DEFAULT_GRACE)


class BaselineAndSurvivorshipTests(CounterfactualBase):
    """T26F items 2 and 3."""

    def sample_rows(self, *specs):
        return [dict(horizon=h, status=st, price=None if p is None else str(p), quote_raw=None if p is None else '1') for h, st, p in specs]

    def test_dead_pool_is_minus_100_percent_at_its_horizon_and_after(self):
        m = cf.metrics(self.sample_rows((300, 'OK', 100), (900, 'OK', 150), (3600, 'POOL_DEAD', None)))
        self.assertEqual(m['baseline'], 'OK')
        self.assertEqual(m['returns'], {300: Decimal(0), 900: Decimal('0.5'), 3600: Decimal(-1), 7200: Decimal(-1), 21600: Decimal(-1)})
        self.assertEqual((m['pool_died'], m['died_at'], m['max_drawdown'], m['max_gain']), (True, 3600, Decimal(-1), Decimal('0.5')))
        revived = cf.metrics(self.sample_rows((300, 'OK', 100), (900, 'POOL_DEAD', None), (3600, 'OK', 120)))
        self.assertEqual(revived['returns'], {300: Decimal(0), 900: Decimal(-1), 1800: Decimal(-1), 3600: Decimal('0.2')})   # -1 until a priced sample, then trusted

    def test_dead_at_baseline_is_a_total_loss_not_a_missing_value(self):
        m = cf.metrics(self.sample_rows((300, 'POOL_DEAD', None)))
        self.assertEqual((m['baseline'], m['died_at'], m['max_drawdown']), ('DEAD', 300, Decimal(-1)))
        self.assertEqual(set(m['returns']), set(cf.HORIZONS))
        self.assertTrue(all(r == -1 for r in m['returns'].values()))

    def test_missing_or_failed_baseline_never_rebaselines_to_a_later_horizon(self):
        for label, specs in (('no baseline row', ((900, 'OK', 100), (3600, 'OK', 150))),
                             ('baseline missed', ((300, 'MISSED', None), (3600, 'OK', 150))),
                             ('baseline failed', ((300, 'FAILED', None), (3600, 'OK', 150))),
                             ('baseline unpriced', ((300, 'OK', None), (3600, 'OK', 150)))):
            with self.subTest(label):
                m = cf.metrics(self.sample_rows(*specs))
                self.assertEqual((m['baseline'], m['returns'], m['max_gain']), ('MISSING', {}, None))

    def test_report_counts_missing_baselines_and_keeps_the_dead(self):
        fixtures = self.candidates(4)
        group = {'v': 2, 'class': 'REJECTED', 'stage': 'ENGINE', 'codes': ['COST_BUDGET'], 'detail': None}
        for f in fixtures:
            self.put_outcome(f.mint, group)
        self.put_sample(fixtures[0].mint, 300, 100); self.put_sample(fixtures[0].mint, 3600, 200)      # +100%
        self.put_sample(fixtures[1].mint, 300, 100)                                                       # dies by +1h
        with closing(sqlite3.connect(self.store)) as c:
            c.execute("INSERT INTO samples(mint,horizon,due_at,sampled_at,status,code) VALUES(?,3600,0,0,'POOL_DEAD','VAULT_CLOSED')", (fixtures[1].mint,))
            c.execute("INSERT INTO samples(mint,horizon,due_at,status,code) VALUES(?,300,0,'MISSED','SCHEDULE_MISSED')", (fixtures[2].mint,))
            c.commit()
        self.put_sample(fixtures[2].mint, 3600, 300)          # a +1h price but NO baseline: must not become its own baseline
        with closing(sqlite3.connect(self.store)) as c:       # dead before the first sample
            c.execute("INSERT INTO samples(mint,horizon,due_at,sampled_at,status,code) VALUES(?,300,0,0,'POOL_DEAD','ZERO_RESERVE')", (fixtures[3].mint,))
            c.commit()
        g = cf.report(self.store)['groups']['REJECTED:ENGINE:COST_BUDGET']
        self.assertEqual((g['candidates'], g['baseline_missing'], g['with_return']), (4, 1, 3))
        self.assertEqual((g['dead_at_horizon'], g['dead_at_baseline'], g['pool_died']), (2, 1, 2))
        self.assertEqual(Decimal(g['mean_return']), (Decimal(1) + Decimal(-1) + Decimal(-1)) / 3)       # the dead are in the mean
        self.assertEqual(Decimal(g['median_return']), Decimal(-1))
        self.assertEqual(Decimal(g['share_positive']), Decimal(1) / 3)


class IngestBackfillAndRetryTests(CounterfactualBase):
    """T26F items 5 and 6."""

    def discovery_with_history(self, old_ages):
        from discovery import continuous as discovery
        from desk.model import canonical
        at = int(T0)
        path = self.root / 'discovery-history.sqlite'
        with patch.object(discovery.time, 'time', return_value=at - max(old_ages) - 100):
            discovery.initialize(path)
        for age in sorted(old_ages, reverse=True):           # oldest first: the store's clock only moves forward
            d = discovery.Store(path, clock=lambda age=age: at - age)
            try:
                d.complete(d.reserve('RECEIVE'), payload=canonical({'method': 'transactionNotification', 'params': {'result': {'signature': f'stale-{age}', 'slot': age}}}).encode())
            finally:
                d.close()
        real_path, mint, pool, raw = self.discovery_fixture(path=path)
        return path, mint

    def test_first_start_never_backfills_old_discovery_history(self):
        path, mint = self.discovery_with_history([30 * 3600, 10 * 3600])
        decoded = []
        from desk import decode as decode_module
        real = decode_module.decode
        with patch.object(decode_module, 'decode', side_effect=lambda raw: (decoded.append(1), real(raw))[1]):
            result = cf.ingest(self.store, path, now=T0)
        self.assertEqual(len(decoded), 1, 'frames older than 6 h + grace are skipped without being decoded')
        self.assertEqual((result['candidates_added'], result['rows_skipped']), (1, 0))
        self.assertEqual(result['cursor_start'], 2)           # two stale frames skipped
        self.assertEqual(self.rows('SELECT mint FROM candidates'), [(mint,)])
        decoded.clear()
        with patch.object(decode_module, 'decode', side_effect=lambda raw: (decoded.append(1), real(raw))[1]):
            self.assertEqual(cf.ingest(self.store, path, now=T0 + 60)['candidates_added'], 0)
        self.assertEqual(decoded, [])                         # the cursor is persisted: nothing is read twice

    def test_restart_after_a_long_outage_skips_stale_frames_without_decoding_them(self):
        # The cursor already exists (an earlier run), but the service was down for hours: frames received since
        # then that are too old to have a horizon left are skipped before decoding, not learned from.
        path, mint = self.discovery_with_history([30 * 3600, 10 * 3600])
        with closing(sqlite3.connect(self.store)) as c:
            c.execute("INSERT OR REPLACE INTO meta VALUES('last_discovery_seq','1')"); c.commit()
        decoded = []
        from desk import decode as decode_module
        real = decode_module.decode
        with patch.object(decode_module, 'decode', side_effect=lambda raw: (decoded.append(1), real(raw))[1]):
            result = cf.ingest(self.store, path, now=T0)
        self.assertEqual((len(decoded), result['cursor_start'], result['candidates_added'], result['rows_skipped']), (1, 1, 1, 0))
        self.assertEqual(self.rows('SELECT mint FROM candidates'), [(mint,)])

    def test_first_start_with_only_stale_frames_starts_after_all_of_them(self):
        from discovery import continuous as discovery
        from desk.model import canonical
        at = int(T0)
        path = self.root / 'stale-discovery.sqlite'
        with patch.object(discovery.time, 'time', return_value=at - 40 * 3600):
            discovery.initialize(path)
        d = discovery.Store(path, clock=lambda: at - 30 * 3600)
        try:
            d.complete(d.reserve('RECEIVE'), payload=canonical({'method': 'transactionNotification', 'params': {'result': {'signature': 'stale-only', 'slot': 5}}}).encode())
        finally:
            d.close()
        result = cf.ingest(self.store, path, now=T0)
        self.assertEqual((result['cursor_start'], result['candidates_added'], result['last_seq']), (1, 0, 1))
        self.assertEqual(self.rows("SELECT value FROM meta WHERE key='last_discovery_seq'"), [('1',)])

    def test_null_pool_account_is_retried_with_backoff_until_the_window_passes(self):
        fixtures = self.candidates(1); chain = FakeChain(fixtures)
        real_values = chain.__call__
        state = {'null': True}
        def call(method, params):
            values = real_values(method, params)
            return {**values, 'value': [None] if state['null'] and params[0] == [fixtures[0].pool] else values['value']}
        calls = lambda: len(chain.calls)
        self.assertEqual(cf.null_pool_delay(1), 60); self.assertEqual(cf.null_pool_delay(2), 120); self.assertEqual(cf.null_pool_delay(9), 900)
        cf.sample(self.store, call, now=T0 + 10)                                  # null: attempt 1
        self.assertEqual((calls(), self.rows('SELECT COUNT(*) FROM vault_attempts')[0][0], self.rows('SELECT COUNT(*) FROM vaults')[0][0]), (1, 1, 0))
        cf.sample(self.store, call, now=T0 + 40)                                  # inside the 60 s backoff: no request
        self.assertEqual(calls(), 1)
        cf.sample(self.store, call, now=T0 + 80)                                  # attempt 2 (+120 s backoff)
        self.assertEqual((calls(), self.rows('SELECT COUNT(*) FROM vault_attempts')[0][0]), (2, 2))
        cf.sample(self.store, call, now=T0 + 150)                                 # still backing off
        self.assertEqual(calls(), 2)
        state['null'] = False
        cf.sample(self.store, call, now=T0 + 210)                                 # visible now: resolved, then sampled normally
        self.assertEqual(self.rows('SELECT status FROM vaults'), [('OK',)])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM request_results WHERE status=?', ('OK',))[0][0], 3)   # every retry is a charged request

    def test_pool_that_never_appears_is_abandoned_once_after_the_window(self):
        fixtures = self.candidates(1)
        chain = FakeChain(fixtures)
        null = lambda method, params: {'context': {'slot': 1}, 'value': [None] * len(params[0])}
        cf.sample(self.store, null, now=T0 + 10)
        late = T0 + cf.HORIZONS[-1] + cf.DEFAULT_GRACE + 1
        summary = cf.sample(self.store, null, now=late)
        self.assertEqual(self.rows('SELECT status,code FROM vaults'), [('UNRESOLVED', 'POOL_ACCOUNT_NULL_WINDOW_PASSED')])
        self.assertEqual((summary['abandoned'], summary['requests']), (1, 0))
        before = self.rows('SELECT COUNT(*) FROM requests')[0][0]
        cf.sample(self.store, null, now=late + 100)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM requests')[0][0], before)      # recorded once, never hammered
        # a candidate that was never even tried before the window passed is recorded with its own code
        self.candidates(1, offset=5)
        cf.sample(self.store, null, now=late + 200)
        self.assertIn(('UNRESOLVED', 'WINDOW_PASSED_BEFORE_RESOLUTION'), self.rows('SELECT status,code FROM vaults'))

    def test_vault_attempts_table_is_append_only_and_added_to_old_stores(self):
        fixtures = self.candidates(1)
        null = lambda method, params: {'context': {'slot': 1}, 'value': [None] * len(params[0])}
        cf.sample(self.store, null, now=T0 + 10)
        for sql in ('UPDATE vault_attempts SET code=code', 'DELETE FROM vault_attempts'):
            with closing(sqlite3.connect(self.store)) as c, self.assertRaises(sqlite3.DatabaseError):
                c.execute(sql)
        old = self.root / 'old-store.sqlite'
        cf.init(old, now=T0 - 10)
        with closing(sqlite3.connect(old)) as c:                   # simulate a store created before vault_attempts existed
            c.execute('DROP TRIGGER vault_attempts_update'); c.execute('DROP TRIGGER vault_attempts_delete'); c.execute('DROP TABLE vault_attempts'); c.commit()
        cf.add_candidate(old, mint=fixtures[0].mint, pool=fixtures[0].pool, signature='s', slot=1, migrated_at=T0, seq=1, now=T0)
        cf.sample(old, null, now=T0 + 10)
        with closing(sqlite3.connect(old)) as c, self.assertRaises(sqlite3.DatabaseError):
            c.execute('DELETE FROM vault_attempts')

    def test_set_allowance_is_range_checked(self):
        for bad in (0, -1, 3601, True, 5.0, '5', None):
            with self.subTest(bad=bad), self.assertRaises(cf.CounterfactualError):
                cf.set_allowance(self.store, bad)
        for bad in (-1, 3601, True):
            with self.subTest(grace=bad), self.assertRaises(cf.CounterfactualError):
                cf.set_allowance(self.store, 5, grace_seconds=bad)
        cf.set_allowance(self.store, 1, now=T0); cf.set_allowance(self.store, 3600, now=T0 + 1)
        self.assertEqual(self.rows('SELECT allowance_per_hour FROM policy ORDER BY id'), [(300,), (1,), (3600,)])
        with patch('sys.stdout'):
            self.assertEqual(cf.main(['set-allowance', '--store', str(self.store), '--allowance-per-hour', '0']), 2)
            self.assertEqual(cf.main(['set-allowance', '--store', str(self.store), '--allowance-per-hour', '3601']), 2)


class OpenModeAndServiceTests(CounterfactualBase):
    """T26F item 4: read-only opens follow the T02F rule; the unit template is self-consistent."""

    def test_open_mode_rule(self):
        wal = self.root / 'wal.sqlite'
        with closing(sqlite3.connect(wal, isolation_level=None)) as c:
            c.execute('PRAGMA journal_mode=WAL'); c.execute('CREATE TABLE t(x)'); c.execute('INSERT INTO t VALUES(1)')
        self.assertEqual(sorted(p.name for p in self.root.glob('wal.sqlite*')), ['wal.sqlite'])
        self.assertEqual(cf.open_mode(wal), 'immutable')
        with closing(cf._connect(wal, readonly=True)) as c:
            self.assertEqual(c.execute('SELECT x FROM t').fetchall(), [(1,)])
        self.assertEqual(sorted(p.name for p in self.root.glob('wal.sqlite*')), ['wal.sqlite'], 'no -wal/-shm created beside a quiet WAL store')
        self.assertEqual(cf.open_mode(self.store), 'ro', 'a rollback-journal store is never immutable')
        writer = sqlite3.connect(wal, isolation_level=None); self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL'); writer.execute('PRAGMA wal_autocheckpoint=0'); writer.execute('INSERT INTO t VALUES(2)')
        self.assertEqual(cf.open_mode(wal), 'ro')
        with closing(cf._connect(wal, readonly=True)) as c:
            self.assertEqual(c.execute('SELECT x FROM t ORDER BY x').fetchall(), [(1,), (2,)])
            with self.assertRaises(sqlite3.OperationalError):
                c.execute('INSERT INTO t VALUES(9)')
        link = self.root / 'link.sqlite'; link.symlink_to(self.store)
        with self.assertRaises(cf.CounterfactualError):
            cf._connect(link, readonly=True)

    def unit(self, name):
        text = (Path(__file__).resolve().parents[1] / 'deploy' / 'fresh' / name).read_text()
        values = {}
        for line in text.splitlines():
            if '=' in line and not line.startswith(('#', '[')):
                key, _, value = line.partition('=')
                values.setdefault(key, []).append(value)
        return text, values

    def test_service_template_isolation_and_restart_policy(self):
        text, v = self.unit('desk-counterfactual.service')
        store = '/var/lib/solana-desk/exp-FRESH/counterfactual/counterfactual.sqlite'
        for key in ('ExecStartPre', 'ExecStart'):
            self.assertIn('--store ' + store, v[key][0])                         # the store lives in its OWN subdirectory
        self.assertNotIn('--store /var/lib/solana-desk/exp-FRESH/counterfactual.sqlite', text)
        rw = v['ReadWritePaths'][0].split()
        self.assertIn('/var/lib/solana-desk/exp-FRESH/counterfactual', rw)
        self.assertIn('/var/lib/solana-desk', rw)                                 # rollback journal of the shared pacing database
        self.assertNotIn('/var/lib/solana-desk/exp-FRESH', rw)                    # the experiment root itself is never writable
        ro = v['ReadOnlyPaths'][0].split()
        self.assertTrue({'/var/lib/solana-desk/exp-FRESH', '/var/lib/solana-desk/discovery'} <= set(ro))
        self.assertEqual(v['Environment'], ['DESK_PROVIDER_PACING_DB=/var/lib/solana-desk/provider-pacing.sqlite'])
        self.assertIn('--ledger', v['ExecStartPre'][0]); self.assertIn('--decisions-db', v['ExecStartPre'][0])
        self.assertEqual(v['Type'], ['oneshot'])
        self.assertNotIn('\n[Install]', text)
        self.assertIn('provider-pacing', text)

    def test_timer_template_is_the_restart_policy(self):
        text, v = self.unit('desk-counterfactual.timer')
        self.assertEqual((v['Unit'], v['OnUnitInactiveSec'], v['Persistent']), (['desk-counterfactual.service'], ['60s'], ['false']))
        self.assertNotIn('\n[Install]', text)



if __name__ == '__main__':
    unittest.main()
