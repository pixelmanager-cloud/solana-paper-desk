"""SYNTHETIC_TEST_ONLY: injected transport and fixtures; no provider calls, keys or live stores."""
import base64
import json
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
        fixtures = self.candidates(5)
        plan = [  # (outcome, +5m price, +1h price)
            ({'dispatched': True, 'status': 'BLOCKED', 'codes': ['MAYHEM_POOL']}, 100, 150),   # rejected winner +50%
            ({'dispatched': True, 'status': 'BLOCKED', 'codes': ['MAYHEM_POOL']}, 100, 50),    # rejected loser -50%
            ({'dispatched': True, 'status': 'COMPLETE', 'codes': []}, 100, 200),                # admitted +100%
            ({'dispatched': False, 'status': None, 'codes': []}, 100, 100),                     # never dispatched 0%
            (None, 100, 300)]                                                                   # outcome unknown
        for f, (outcome, first, hour) in zip(fixtures, plan):
            self.put_sample(f.mint, 300, first); self.put_sample(f.mint, 3600, hour)
            if outcome:
                self.put_outcome(f.mint, outcome)
        r = cf.report(self.store)
        g = r['groups']
        self.assertEqual(set(g), {'MAYHEM_POOL', 'ADMITTED:COMPLETE', 'NOT_DISPATCHED', 'UNKNOWN_OUTCOME'})
        self.assertEqual((g['MAYHEM_POOL']['candidates'], g['MAYHEM_POOL']['with_return']), (2, 2))
        self.assertEqual(Decimal(g['MAYHEM_POOL']['mean_return']), Decimal(0))
        self.assertEqual(Decimal(g['MAYHEM_POOL']['median_return']), Decimal(0))
        self.assertEqual(Decimal(g['MAYHEM_POOL']['p10_return']), Decimal('-0.5')); self.assertEqual(Decimal(g['MAYHEM_POOL']['p90_return']), Decimal('0.5'))
        self.assertEqual(Decimal(g['MAYHEM_POOL']['share_positive']), Decimal('0.5'))
        self.assertEqual(Decimal(g['ADMITTED:COMPLETE']['median_return']), Decimal(1))
        self.assertEqual(Decimal(g['UNKNOWN_OUTCOME']['median_return']), Decimal(2))
        self.assertEqual(r['label'], cf.LABEL)

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
    def discovery_fixture(self):
        from discovery import continuous as discovery
        from tests.test_graduation_witness import fixture as migration_fixture
        from desk.model import canonical
        from desk.programs import unbase58
        at = int(T0)
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

    def test_ingest_from_discovery_and_journal_outcome(self):
        from tools import paper_entry_dispatcher as dispatcher
        path, mint, pool, raw = self.discovery_fixture()
        result = cf.ingest(self.store, path, now=T0)
        self.assertEqual((result['candidates_added'], result['rows_skipped']), (1, 0))
        self.assertEqual(self.rows('SELECT mint,pool,signature FROM candidates'), [(mint, pool, raw['transaction']['signatures'][0])])
        self.assertEqual(cf.ingest(self.store, path, now=T0)['candidates_added'], 0)  # cursor + OR IGNORE: idempotent
        journal = self.root / 'journal.sqlite'
        with closing(sqlite3.connect(journal)) as c:
            for sql in dispatcher.SCHEMAS.values():
                c.execute(sql)
            c.execute('INSERT INTO intents VALUES(?,?,?,?,?)', ('a' * 32, mint, 'sig', '{}', 'h'))
            c.execute('INSERT INTO results VALUES(?,?,?)', ('a' * 32, json.dumps({'result': {'status': 'BLOCKED', 'blockers': ['MAYHEM_POOL']}}), 'h'))
            c.commit()
        cf.refresh_outcomes(self.store, journal, now=T0 + 1)
        cf.refresh_outcomes(self.store, journal, now=T0 + 2)  # unchanged outcome: no duplicate row
        last = json.loads(self.rows('SELECT payload FROM outcomes ORDER BY id DESC LIMIT 1')[0][0])
        self.assertEqual(last, {'dispatched': True, 'status': 'BLOCKED', 'codes': ['MAYHEM_POOL']})
        self.assertEqual(self.rows('SELECT COUNT(*) FROM outcomes')[0][0], 2)
        self.assertEqual(cf._outcome(self.root / 'nope.sqlite', mint)['status'], 'OUTCOME_UNREADABLE')

    def test_altered_discovery_original_is_skipped_not_learned(self):
        path, mint, pool, raw = self.discovery_fixture()
        with closing(sqlite3.connect(path)) as c:
            for name, in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='raw_events'").fetchall():
                c.execute(f'DROP TRIGGER {name}')
            c.execute("UPDATE raw_events SET payload_hash=?", ('0' * 64,)); c.commit()
        result = cf.ingest(self.store, path, now=T0)
        self.assertEqual((result['candidates_added'], result['rows_skipped']), (0, 1))


if __name__ == '__main__':
    unittest.main()
