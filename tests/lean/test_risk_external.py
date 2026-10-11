"""SYNTHETIC_TEST_ONLY: L21 risk_external unit tests. No network: every RugCheck answer comes from a fake opener behind the REAL
``lean.providers.Transport``. The report bodies are synthetic, shaped after the public ``/v1/tokens/<mint>/report`` answer (keys and
Go-style nulls as observed on 2026-10-11: ``creatorTokens`` / ``risks`` / ``insiderNetworks`` null when empty, nanosecond RFC 3339
times, ``topHolders[].pct`` in percent with the pool vault among them)."""
import json
import os
import queue
import socket
import tempfile
import threading
import types
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from desk.security import base58
from lean import learn as LN, providers, risk_external as R
from lean.candidates import Candidate, Screen
from lean.providers import ProviderError
from lean.store import Store

ROOT = Path(__file__).resolve().parents[2]
T0 = 1_800_000_000.0


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def mint(n):
    return base58(bytes([n]) * 32)


POOL = mint(200)
POOL_VAULT = mint(201)
SUPPLY = 10 ** 15


def iso(ts):
    import datetime
    whole = datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    return whole + '.123456789Z'


def report(m, *, score=1, norm=1, risks=None, holders=1200, insiders=0, networks=None, rugged=False, name='Super Token',
           symbol='SUPER', image='', creator=None, creator_tokens=None, lp_locked=100, top=None):
    top = top if top is not None else [(POOL_VAULT, POOL, 20.0)] + [(mint(150 + i), mint(170 + i), 3.0 - i * 0.1) for i in range(12)]
    return {'mint': m, 'tokenProgram': 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb', 'creator': creator or mint(99), 'creatorBalance': 0,
            'token': {'mintAuthority': None, 'supply': SUPPLY, 'decimals': 6, 'isInitialized': True, 'freezeAuthority': None},
            'token_extensions': None, 'tokenMeta': {'name': name, 'symbol': symbol, 'uri': 'https://ipfs.io/ipfs/bafkmeta' + m[:8],
                                                    'mutable': False, 'updateAuthority': '11111111111111111111111111111111'},
            'topHolders': [{'address': a, 'amount': 1, 'decimals': 6, 'pct': p, 'owner': o, 'insider': False} for a, o, p in top],
            'freezeAuthority': None, 'mintAuthority': None, 'risks': risks, 'score': score, 'score_normalised': norm,
            'fileMeta': {'description': '', 'name': name, 'symbol': symbol, 'image': image}, 'lockerOwners': {}, 'lockers': {},
            'markets': [{'pubkey': POOL, 'marketType': 'pump_fun_amm', 'mintA': m, 'liquidityAAccount': POOL_VAULT,
                         'liquidityBAccount': mint(202), 'lp': {'lpLockedPct': lp_locked}}],
            'totalMarketLiquidity': 86182.26, 'totalHolders': holders, 'rugged': rugged, 'graphInsidersDetected': insiders,
            'insiderNetworks': networks, 'knownAccounts': {POOL: {'name': 'Pump Fun AMM', 'type': 'AMM'}},
            'creatorTokens': creator_tokens, 'detectedAt': iso(T0), 'launchpad': {'name': 'Pump.Fun', 'platform': 'pump_fun'}}


class Resp:
    def __init__(self, body, status=200, headers=None):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status, self.headers = status, headers or {}

    def read(self, n=-1):
        return self.body if n is None or n < 0 else self.body[:n]

    def close(self):
        pass


class FakeClock:
    def __init__(self, t=T0):
        self.t = t
        self.sleeps = []

    def time(self):
        return self.t

    def monotonic(self):
        return self.t - T0 + 1000.0

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class FakeRugCheck:
    """The HTTP layer only: ``reports[mint]`` is a dict (served as JSON), bytes (served as is) or ``(status, body, headers)``."""

    def __init__(self):
        self.reports, self.requests = {}, []

    def opener(self, request, timeout):
        assert request.get_method() == 'GET', request.get_method()
        assert request.full_url.startswith('https://api.rugcheck.xyz/v1/tokens/') and request.full_url.endswith('/report'), request.full_url
        m = request.full_url[len('https://api.rugcheck.xyz/v1/tokens/'):-len('/report')]
        self.requests.append((m, dict(request.header_items())))
        answer = self.reports.get(m, (400, b'{"error":"invalid token mint"}', {}))
        if isinstance(answer, tuple):
            return Resp(answer[1], answer[0], answer[2])
        return Resp(answer)


def client(fake, clock, **kw):
    return providers.RugCheck(opener=fake.opener, clock=clock.time, monotonic=clock.monotonic, sleep=clock.sleep, **kw)


# ------------------------------------------------------------------------------------------------------------------ the client
class RugCheckClient(unittest.TestCase):
    def setUp(self):
        self.clock, self.fake = FakeClock(), FakeRugCheck()
        self.rc = client(self.fake, self.clock)

    def test_normal_report_is_a_keyless_get_of_the_report_url(self):
        self.fake.reports[mint(1)] = report(mint(1))
        parsed, raw, meta = self.rc.report(mint(1))
        self.assertEqual(parsed['mint'], mint(1))
        self.assertEqual((meta['provider'], meta['endpoint'], meta['attempts'], meta['http_status']), ('rugcheck', 'report', 1, 200))
        (asked, headers), = self.fake.requests
        self.assertEqual(asked, mint(1))
        self.assertEqual({k.lower() for k in headers}, {'accept'})              # no key, no auth header of any kind
        self.assertEqual(repr(self.rc), 'RugCheck()')

    def test_429_is_typed_transient_with_retry_after_and_is_not_retried(self):
        self.fake.reports[mint(1)] = (429, b'{"error":"rate limited"}', {'Retry-After': '7'})
        with self.assertRaises(ProviderError) as caught:
            self.rc.report(mint(1))
        e = caught.exception
        self.assertEqual((e.code, e.transient, e.provider, e.meta['retry_after'], e.meta['attempts']), ('HTTP_429', True, 'rugcheck', 7.0, 1))
        self.assertEqual(len(self.fake.requests), 1)
        self.assertNotIn('rugcheck.xyz', str(e))

    def test_5xx_is_typed_transient(self):
        self.fake.reports[mint(1)] = (503, b'upstream down', {})
        with self.assertRaises(ProviderError) as caught:
            self.rc.report(mint(1))
        self.assertEqual((caught.exception.code, caught.exception.transient), ('HTTP_503', True))

    def test_retries_only_when_configured(self):
        rc = client(self.fake, self.clock, max_attempts=2)
        self.fake.reports[mint(1)] = (502, b'', {})
        with self.assertRaises(ProviderError):
            rc.report(mint(1))
        self.assertEqual(len(self.fake.requests), 2)

    def test_malformed_bodies_are_typed(self):
        for body in (b'not json', b'{"mint": "a", "mint": "b"}', b'{"score": NaN}'):
            self.fake.reports[mint(1)] = (200, body, {})
            with self.assertRaises(ProviderError) as caught:
                self.rc.report(mint(1))
            self.assertEqual((caught.exception.code, caught.exception.transient), ('RESPONSE_MALFORMED', False), body)

    def test_not_an_object_for_this_mint_is_invalid(self):
        for body in ([report(mint(1))], report(mint(2)), {'error': 'x'}):
            self.fake.reports[mint(1)] = body
            with self.assertRaises(ProviderError) as caught:
                self.rc.report(mint(1))
            self.assertEqual(caught.exception.code, 'RESPONSE_INVALID')

    def test_unknown_mint_is_typed_non_transient(self):
        for answer in ((400, b'{"error":"invalid token mint"}', {}), (404, b'{"error":"not found"}', {})):
            self.fake.reports[mint(1)] = answer
            with self.assertRaises(ProviderError) as caught:
                self.rc.report(mint(1))
            self.assertEqual((caught.exception.code, caught.exception.transient, caught.exception.provider),
                             ('UNKNOWN_MINT', False, 'rugcheck'))

    def test_a_bad_argument_sends_nothing(self):
        for bad in ('../../etc', 'x' * 20, None, mint(1) + '/report?x='):
            with self.assertRaises(ProviderError) as caught:
                self.rc.report(bad)
            self.assertEqual(caught.exception.code, 'ARGUMENT_INVALID')
        self.assertEqual(self.fake.requests, [])

    def test_oversized_is_refused(self):
        rc = client(self.fake, self.clock, max_bytes=2048)
        self.fake.reports[mint(1)] = report(mint(1), name='x' * 4000)
        with self.assertRaises(ProviderError) as caught:
            rc.report(mint(1))
        self.assertEqual(caught.exception.code, 'RESPONSE_OVERSIZED')

    def test_its_own_bucket_paces_at_the_configured_rate_and_never_touches_a_helius_lane(self):
        before = providers.low_lane_stats('helius', clock=self.clock.monotonic, sleep=self.clock.sleep)
        self.fake.reports[mint(1)] = report(mint(1))
        for _ in range(3):
            self.rc.report(mint(1))
        self.assertEqual(self.clock.sleeps, [1.0, 1.0])                         # 1 req/s, burst 1
        self.assertEqual(providers.low_lane_stats('helius', clock=self.clock.monotonic, sleep=self.clock.sleep), before)
        with self.assertRaises(ValueError):
            client(self.fake, self.clock, max_bytes=10)


# ------------------------------------------------------------------------------------------------------------------ parsing
class Parsing(unittest.TestCase):
    def test_report_fields(self):
        networks = [{'id': 'a', 'size': 5, 'type': 'transfer', 'tokenAmount': SUPPLY // 20, 'activeAccounts': 5},
                    {'id': 'b', 'size': 2, 'type': 'transfer', 'tokenAmount': SUPPLY // 100, 'activeAccounts': 2}]
        risks = [{'name': 'Low Liquidity', 'value': '', 'description': '', 'score': 1, 'level': 'danger'},
                 {'name': 'Mutable metadata', 'value': '', 'description': '', 'score': 1, 'level': 'warn'}]
        f, m, ident, tokens = R.parse_report(report(mint(1), score=1234, norm=D('7.5'), risks=risks, insiders=232, networks=networks,
                                                    rugged=True, image='https://gateway.pinata.cloud/ipfs/bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi?x=1'))
        self.assertEqual(m, {})
        self.assertEqual((f['rc_score'], f['rc_score_norm'], f['rc_total_holders'], f['rc_graph_insiders']), (1234, '7.5000', 1200, 232))
        self.assertEqual(f['rc_risks'], [{'name': 'Low Liquidity', 'level': 'danger'}, {'name': 'Mutable metadata', 'level': 'warn'}])
        self.assertEqual((f['rc_danger_count'], f['rc_warn_count']), (1, 1))
        self.assertEqual((f['rc_insider_network_count'], f['rc_insider_largest_pct']), (2, '5.0000'))
        self.assertEqual(f['rc_top10_pct'], '25.5000')              # 3.0 + 2.9 + ... + 2.1; the 20 % pool vault is not a holder
        self.assertEqual((f['rc_lp_locked_pct'], f['rc_rugged']), ('100.0000', True))
        self.assertEqual(ident['image_key'], 'ipfs:bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi')
        self.assertEqual((ident['name_key'], ident['symbol_key'], ident['creator_tokens']), ('supertoken', 'super', 'null'))
        self.assertEqual(tokens, [])
        self.assertEqual(set(f) | set(m), set(R.REPORT_FIELDS))

    def test_go_nulls_are_empty_lists_and_bad_shapes_are_reasons(self):
        f, m, ident, tokens = R.parse_report(report(mint(1), risks=None, networks=None))
        self.assertEqual((f['rc_risks'], f['rc_danger_count'], f['rc_insider_network_count'], f['rc_insider_largest_pct']), ([], 0, 0, '0.0000'))
        bad = report(mint(1), risks='x', holders=-1, rugged='no', top=[(mint(3), mint(4), 'x')])
        bad.update(score=True, insiderNetworks=[{'tokenAmount': 'x'}], creatorTokens='x', markets=None)
        f, m, ident, tokens = R.parse_report(bad)
        self.assertEqual(f, {'rc_score_norm': 1, 'rc_insider_network_count': 1, 'rc_graph_insiders': 0})
        self.assertEqual(m['rc_score'], 'RC_SCORE_ABSENT')
        self.assertEqual(m['rc_risks'], 'RC_RISKS_MALFORMED')
        self.assertEqual(m['rc_top10_pct'], 'RC_HOLDERS_UNREADABLE')
        self.assertEqual(m['rc_insider_largest_pct'], 'RC_INSIDER_AMOUNTS_UNREADABLE')
        self.assertEqual((m['rc_rugged'], m['rc_lp_locked_pct'], m['rc_total_holders']), ('RC_RUGGED_ABSENT', 'RC_NO_MARKET_LP', 'RC_HOLDERS_ABSENT'))
        self.assertIsNone(tokens)
        self.assertEqual(ident['creator_tokens'], 'malformed')

    def test_times(self):
        self.assertEqual(R.parse_time('2026-09-08T15:26:59.363634079Z'), 1788881219.363634079)
        self.assertEqual(R.parse_time('2026-09-08T17:26:59+02:00'), 1788881219.0)
        for bad in ('2026-13-08T15:26:59Z', 'yesterday', None, 5, '2026-09-08 15:26:59'):
            self.assertIsNone(R.parse_time(bad), bad)

    def test_name_and_image_keys(self):
        self.assertEqual(R.name_key('$PEPE '), 'pepe')
        self.assertEqual(R.name_key('Ｐｅｐｅ 🐸'), 'pepe')                 # NFKC full-width, emoji dropped
        self.assertEqual(R.name_key('Pe-pe.'), 'pepe')
        self.assertIsNone(R.name_key(' 🐸 '))
        self.assertEqual(R.image_key('ipfs://QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG'), R.image_key('https://ipfs.io/ipfs/QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG'))
        self.assertEqual(R.image_key('HTTPS://Example.com/a.png?v=2#x'), 'example.com/a.png')
        self.assertIsNone(R.image_key(''))

    def test_creator_history_counts_only_tokens_before_the_decision(self):
        decision = T0
        tokens = [(mint(10), decision - 3600, D(500)), (mint(11), decision - 6 * R.DAY, D(50000)), (mint(12), decision - 20 * R.DAY, D(9000)),
                  (mint(13), decision - 40 * R.DAY, D(1)), (mint(14), decision + 60, D(1)), (mint(1), decision - 10, D(1)),
                  (mint(15), None, D(1)), (mint(16), decision - 2 * R.DAY, D(70000))]
        f, m, picks = R.creator_history(tokens, mint(1), decision, dead_mcap_usd=10000, sample=3)
        self.assertEqual(m, {})
        self.assertEqual((f['creator_tokens_7d'], f['creator_tokens_30d'], f['creator_dead_30d']), (3, 4, 2))
        self.assertEqual(picks, [mint(10), mint(16), mint(11)])                   # the newest three, never the future one
        f, m, picks = R.creator_history(tokens + [(mint(17), decision - 1, None)], mint(1), decision, dead_mcap_usd=10000, sample=0)
        self.assertEqual((m['creator_dead_30d'], picks, f['creator_tokens_30d']), ('RC_MARKET_CAP_UNKNOWN', [], 5))
        f, m, picks = R.creator_history(None, mint(1), decision, dead_mcap_usd=1, sample=3)
        self.assertEqual((f, set(m)), ({}, set(R.CREATOR_FIELDS)))

    def test_copycat_counts_distinct_earlier_tokens_inside_the_window(self):
        ident = {'name_key': 'pepe', 'symbol_key': 'pepe', 'image_key': 'ipfs:Q1'}
        recent = [(T0 - 100, mint(2), 'pepe', 'pp', None), (T0 - 50, mint(2), 'pepe', 'pp', None),     # one mint, counted once
                  (T0 - 90, mint(3), 'other', 'pepe', 'ipfs:Q1'), (T0 - 80, mint(4), 'x', 'y', 'ipfs:Q1'),
                  (T0 + 5, mint(5), 'pepe', 'pepe', 'ipfs:Q1'),                                         # after the decision: never
                  (T0 - R.DAY - 1, mint(6), 'pepe', 'pepe', 'ipfs:Q1'),                                 # outside the window
                  (T0 - 10, mint(1), 'pepe', 'pepe', 'ipfs:Q1')]                                        # itself
        f, m = R.copycat(ident, mint(1), T0, recent, R.DAY)
        self.assertEqual(f, {'copycat_name_count': 1, 'copycat_symbol_count': 1, 'copycat_of_recent': 2, 'same_image_count': 2})
        f, m = R.copycat({'name_key': None, 'symbol_key': None, 'image_key': None}, mint(1), T0, recent, R.DAY)
        self.assertEqual((f, m['copycat_of_recent'], m['same_image_count']), ({}, 'NO_NAME_OR_SYMBOL', 'NO_IMAGE'))


# ------------------------------------------------------------------------------------------------------------------ the recorder
SOFT = Screen(False, ('MARKET_CAP_ABOVE_MAX',), {}, (), ())
PASS = Screen(True, (), {}, (), ())
HAZARD = Screen(False, ('ACTIVE_MINT_AUTHORITY',), {}, (), ())
FAILED = Screen(False, ('HOLDERS_UNAVAILABLE',), {}, (), (), {'stage': 'holders', 'code': 'HTTP_503', 'transient': True})
FAILED_SOFT = Screen(False, ('MARKET_CAP_ABOVE_MAX',), {}, (), (), {'stage': 'holders', 'code': 'HTTP_503', 'transient': True})


class Recorder(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.clock, self.fake = FakeClock(), FakeRugCheck()
        self.store = Store(self.root / 'lean.sqlite', initial_cash_sol='10', code_version='t', strategy_version='s', clock=self.clock.time)
        self.addCleanup(self.store.close)
        self.rec = self.make()

    def make(self, **over):
        cfg = R.config({'enabled': True, **over})
        return R.RiskExternal(store=self.store, client=client(self.fake, self.clock, rate_per_s=cfg['rate_per_s']), cfg=cfg,
                              code_version='t', strategy_version='s', clock=self.clock.time, monotonic=self.clock.monotonic)

    def screened(self, n, screen=PASS, rec=None):
        """The runner's order: candidate row, screen decision, then the hand-over."""
        m = mint(n)
        cid = self.store.add_candidate(m, pool=POOL, ts=self.clock.time())
        self.store.add_decision('screen', 'PASS' if screen.passed else 'REJECT', mint=m, candidate_id=cid, reasons=screen.reasons,
                                ts=self.clock.time())
        candidate = Candidate(seq=n, mint=m, pool=POOL, signature='s', slot=1, migrated_at=T0, payload_hash='h')
        return m, cid, (rec or self.rec).submit(candidate, cid, screen)

    def rows(self, kind=R.KIND):
        return [json.loads(o['raw']) for o in self.store.rows('observations', kind=kind, limit=1000)]

    def drain(self, rec=None):
        rec = rec or self.rec
        while rec.process_one():
            pass

    def test_hazards_and_failed_screens_are_skipped_and_a_mint_is_collected_once_per_state(self):
        self.assertFalse(self.screened(1, HAZARD)[2])
        self.assertFalse(self.screened(2, FAILED)[2])
        self.assertFalse(self.screened(5, FAILED_SOFT)[2])                          # failed, even with only soft reasons: retried later
        self.assertTrue(self.screened(3, SOFT)[2])
        self.assertFalse(self.screened(3, SOFT)[2])                                 # a watchlist re-screen buys no new row
        self.assertTrue(self.screened(3, PASS)[2])                                  # ... until it passes
        self.assertTrue(self.screened(4, PASS)[2])
        self.assertEqual(self.rec.queue.qsize(), 3)
        self.assertFalse(self.rec.submit(None, None, object()))                    # never raises

    def test_a_row_is_the_report_as_of_collection_with_its_decision_and_lateness(self):
        self.fake.reports[mint(1)] = report(mint(1), score=500, rugged=False)
        m, cid, _ = self.screened(1)
        decision = self.clock.time()
        self.clock.t += 2.5
        self.drain()
        row, = self.rows()
        self.assertEqual((row['version'], row['mint'], row['candidate_id'], row['screen_passed']), (R.VERSION, m, cid, True))
        self.assertEqual((row['decision_ts'], row['collected_at'], row['late_s'], row['calls']), (decision, decision + 2.5, 2.5, 1))
        self.assertEqual(set(row['fields']), set(R.FIELDS))
        self.assertTrue(all((row['fields'][n] is None) == (n in row['missing']) for n in R.FIELDS))
        self.assertEqual(row['flags'], {n: 'AT_COLLECTION' for n in R.FIELDS if row['fields'][n] is not None})
        self.assertEqual((row['fields']['rc_score'], row['fields']['rc_rugged'], row['fields']['creator_tokens_30d']), (500, False, 0))
        self.assertEqual((row['fields']['creator_sampled'], row['fields']['creator_rugged_sampled']), (0, 0))
        self.assertEqual(row['missing']['same_image_count'], 'NO_IMAGE')
        raw, = self.store.rows('observations', kind=R.RAW_KIND, limit=10)          # small: the body is kept
        self.assertEqual(json.loads(raw['raw'])['mint'], m)
        self.assertEqual((row['raw']['kept'], row['raw']['bytes']), (True, len(raw['raw'])))
        stored, = self.store.rows('observations', kind=R.KIND, limit=10)
        self.assertEqual((stored['candidate_id'], stored['ts']), (cid, decision + 2.5))

    def test_the_decision_is_the_screen_handed_over_never_a_later_one(self):
        self.fake.reports[mint(1)] = report(mint(1))
        m, cid, _ = self.screened(1, SOFT)
        first = self.clock.time()
        self.clock.t += 5                                                           # a re-screen lands before the worker gets to it
        self.store.add_decision('screen', 'REJECT', mint=m, candidate_id=cid, reasons=('MARKET_CAP_ABOVE_MAX',), ts=self.clock.time())
        self.drain()
        row, = self.rows()
        self.assertEqual((row['decision_ts'], row['late_s']), (first, 5.0))
        other = self.store.add_candidate(mint(9), ts=self.clock.time())             # another candidate's decision is never borrowed
        self.store.add_decision('screen', 'PASS', mint=mint(9), candidate_id=other, ts=self.clock.time())
        self.assertIsNone(self.rec.decision_ts(other + 1, mint(9), self.clock.time()))

    def test_a_large_body_is_not_kept(self):
        rec = self.make(raw_max_bytes=100)
        self.fake.reports[mint(1)] = report(mint(1))
        self.screened(1, rec=rec)
        self.drain(rec)
        self.assertEqual(self.store.rows('observations', kind=R.RAW_KIND, limit=10), [])
        self.assertFalse(self.rows()[0]['raw']['kept'])

    def test_failures_are_reasons_and_429_opens_a_backoff_window(self):
        self.fake.reports[mint(1)] = (503, b'', {})
        self.fake.reports[mint2 := mint(2)] = (200, b'{"broken', {})
        self.fake.reports[mint(4)] = (429, b'', {'Retry-After': '90'})
        self.fake.reports[mint(5)] = report(mint(5))
        for n in (1, 2, 3, 4, 5):
            self.screened(n)
        self.drain()
        rows = {r['mint']: r for r in self.rows()}
        self.assertEqual(rows[mint(1)]['missing']['rc_score'], 'RC_HTTP_503')
        self.assertEqual(rows[mint2]['missing']['rc_score'], 'RC_RESPONSE_MALFORMED')
        self.assertEqual(rows[mint(3)]['missing']['rc_score'], 'RC_UNKNOWN_MINT')            # not in the fake: 400 invalid token mint
        self.assertEqual(rows[mint(4)]['missing']['rc_score'], 'RC_HTTP_429')
        self.assertEqual((rows[mint(5)]['missing']['rc_score'], rows[mint(5)]['calls']), ('RC_BACKOFF', 0))   # nothing sent in the window
        self.assertEqual([m for m, _ in self.fake.requests], [mint(1), mint(2), mint(3), mint(4)])
        self.assertTrue(all(set(r['missing']) == set(R.FIELDS) and r['flags'] == {} for r in rows.values()))
        h = self.rec.health()
        self.assertEqual((h['rows'], h['calls'], h['http_429'], h['unknown_mint'], h['backoff'], h['errors']), (5, 4, 1, 1, 1, 3))
        self.assertEqual(h['backoff_s'], 60.0)                                               # max(backoff_s, Retry-After capped at 30 s)
        self.clock.t += 59
        self.screened(7)
        self.drain()
        self.assertEqual(self.rows()[-1]['missing']['rc_score'], 'RC_BACKOFF')
        self.clock.t += 2
        self.screened(6)
        self.fake.reports[mint(6)] = report(mint(6))
        self.drain()
        self.assertEqual(self.rows()[-1]['fields']['rc_score'], 1)

    def test_a_candidate_already_too_late_costs_nothing(self):
        self.fake.reports[mint(1)] = report(mint(1))
        self.screened(1)
        self.clock.t += 31
        self.drain()
        row, = self.rows()
        self.assertEqual((row['calls'], row['missing']['rc_score'], row['late_s']), (0, 'COLLECTED_TOO_LATE', 31.0))
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(self.rec.stats['late'], 1)

    def test_creator_sample_reads_at_most_three_prior_tokens(self):
        prior = [(mint(30 + i), T0 - (i + 1) * 3600) for i in range(5)] + [(mint(40), T0 - 40 * R.DAY)]
        ct = [{'mint': m, 'marketCap': 500 if i % 2 else 50000, 'createdAt': iso(ts)} for i, (m, ts) in enumerate(prior)]
        self.fake.reports[mint(1)] = report(mint(1), creator_tokens=ct)
        for i, (m, _) in enumerate(prior[:3]):
            self.fake.reports[m] = report(m, rugged=i != 1)
        self.screened(1)
        self.drain()
        row, = self.rows()
        f = row['fields']
        self.assertEqual((f['creator_tokens_7d'], f['creator_tokens_30d'], f['creator_dead_30d']), (5, 5, 2))
        self.assertEqual((f['creator_sampled'], f['creator_rugged_sampled'], row['calls']), (3, 2, 4))
        self.assertEqual([m for m, _ in self.fake.requests], [mint(1)] + [m for m, _ in prior[:3]])
        self.assertEqual(row['identity']['creator_tokens'], 'listed')
        # one of the sampled reports fails: the rug count would be understated, so it is a reason, not a number
        self.fake.reports[prior[1][0]] = (503, b'', {})
        self.fake.reports[mint(2)] = report(mint(2), creator_tokens=ct)
        self.screened(2)
        self.drain()
        f, missing = self.rows()[-1]['fields'], self.rows()[-1]['missing']
        self.assertEqual((f['creator_sampled'], f['creator_rugged_sampled'], missing['creator_rugged_sampled']), (2, None, 'RC_HTTP_503'))
        # no sample configured: no extra call
        rec = self.make(creator_sample=0)
        self.fake.reports[mint(3)] = report(mint(3), creator_tokens=ct)
        self.screened(3, rec=rec)
        before = len(self.fake.requests)
        self.drain(rec)
        self.assertEqual(len(self.fake.requests) - before, 1)

    def test_copycat_uses_the_rows_written_before_and_survives_a_restart(self):
        self.fake.reports[mint(1)] = report(mint(1), name='PEPE', symbol='PEPE', image='ipfs://QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG')
        self.fake.reports[mint(2)] = report(mint(2), name='Pepe!', symbol='PEPE2', image='https://ipfs.io/ipfs/QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG')
        self.fake.reports[mint(3)] = report(mint(3), name='Other', symbol='$pepe')
        self.screened(1)
        self.drain()
        self.clock.t += 60
        self.screened(2)
        self.drain()
        rows = {r['mint']: r['fields'] for r in self.rows()}
        self.assertEqual((rows[mint(1)]['copycat_of_recent'], rows[mint(1)]['same_image_count']), (0, 0))
        self.assertEqual((rows[mint(2)]['copycat_of_recent'], rows[mint(2)]['copycat_name_count'], rows[mint(2)]['same_image_count']), (1, 1, 1))
        restarted = self.make()                                                    # a new process: the memory comes from the store
        self.clock.t += 60
        self.screened(3, rec=restarted)
        self.drain(restarted)
        f = self.rows()[-1]['fields']
        self.assertEqual((f['copycat_of_recent'], f['copycat_symbol_count'], f['copycat_name_count']), (1, 1, 0))
        self.clock.t += R.DAY + 120                                                # outside the window: forgotten
        self.fake.reports[mint(4)] = report(mint(4), name='pepe', symbol='pepe')
        self.screened(4, rec=restarted)
        self.drain(restarted)
        self.assertEqual(self.rows()[-1]['fields']['copycat_of_recent'], 0)

    def test_a_full_queue_drops_without_blocking_or_a_row(self):
        rec = self.make(queue_size=1)
        self.assertTrue(self.screened(1, rec=rec)[2])
        self.assertFalse(self.screened(2, rec=rec)[2])
        self.assertEqual((rec.stats['queue_full'], rec.stats['shed']), (1, 1))

    def test_stop_drops_the_queue_and_sends_nothing(self):
        stop = threading.Event()
        stop.set()
        self.fake.reports[mint(1)] = report(mint(1))
        self.screened(1)
        self.screened(2)
        self.rec.run(stop, tick=0.01)
        self.assertEqual((self.rows(), self.fake.requests, self.rec.stats['stopped']), ([], [], 2))
        self.rec._stop = stop                                                       # a stop seen just before a request: no row
        self.screened(3)
        self.rec.process_one()
        self.assertEqual((self.rows(), self.fake.requests), ([], []))

    def test_submit_never_waits_for_a_slow_provider(self):
        """The worker is stuck inside a RugCheck call; the runner's hand-over still returns at once."""
        entered, release = threading.Event(), threading.Event()
        real = self.fake.opener

        def slow(request, timeout):
            entered.set()
            release.wait(10)
            return real(request, timeout)
        self.fake.reports[mint(1)] = report(mint(1))
        self.rec.client.transport.opener = slow
        self.screened(1)
        worker = threading.Thread(target=self.rec.process_one, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(5))
        started = __import__('time').monotonic()
        for n in range(2, 40):
            self.screened(n)
        self.assertLess(__import__('time').monotonic() - started, 2.0)
        self.assertEqual(self.rec.queue.qsize(), 38)
        release.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())

    def test_a_recorder_bug_is_a_missing_row_never_an_exception(self):
        self.rec.client = types.SimpleNamespace(report=mock.Mock(side_effect=RuntimeError('bug')))
        self.screened(1)
        self.assertTrue(self.rec.process_one())
        self.assertEqual((self.rows(), self.rec.stats['dropped'], self.rec.stats['errors']), ([], 1, 1))
        self.rec.client = types.SimpleNamespace(report=mock.Mock(return_value=({'mint': mint(2), 'risks': 5, 'creatorTokens': [1]}, b'{}', {})))
        self.screened(2)
        self.rec.process_one()
        row, = self.rows()
        self.assertEqual(row['missing']['rc_risks'], 'RC_RISKS_MALFORMED')

    def test_health(self):
        h = self.rec.health()
        self.assertEqual(h['enabled'], True)
        for key in ('queued', 'rows', 'calls', 'errors', 'shed', 'last_error', 'backoff_s'):
            self.assertIn(key, h)


# ------------------------------------------------------------------------------------------------------------------ config
class Config(unittest.TestCase):
    def test_defaults_off_and_strict(self):
        self.assertFalse(R.config(None)['enabled'])
        self.assertFalse(R.config({})['enabled'])
        self.assertIsNone(R.build(R.config(None), store=None, code_version='x', strategy_version='y'))
        for bad in ({'enabled': 1}, {'rate_per_s': 5}, {'rate_per_s': 0}, {'creator_sample': 4}, {'max_attempts': 0}, {'queue_size': 1.5},
                    {'unknown': 1}, {'max_late_s': -1}, {'raw_max_bytes': True}, [], 'on'):
            with self.assertRaises(ValueError, msg=bad):
                R.config(bad)

    def test_the_example_turns_it_on_and_the_production_config_loads_unchanged(self):
        from lean.__main__ import load_config, CONFIG_KEYS
        example = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        self.assertEqual(example['risk_external'], {**R.DEFAULTS, 'enabled': True, 'rate_per_s': 1.0, 'timeout_s': 10.0, 'max_late_s': 30.0,
                                                    'dead_mcap_usd': 10000.0, 'copycat_window_s': 86400.0, 'backoff_s': 60.0})
        self.assertIn('risk_external', CONFIG_KEYS)
        # the production lean.json (deployed before L21) has no risk_external key: it must load unchanged, with the feature OFF
        with tempfile.TemporaryDirectory() as tmp:
            raw = json.loads((ROOT / 'config' / 'lean' / 'lean.example.json').read_text())
            del raw['risk_external']
            raw['strategy_config'] = str(ROOT / 'config' / 'lean' / 'strategy-default.json')
            path = Path(tmp) / 'lean.json'
            path.write_text(json.dumps(raw))
            prod = load_config(path)
            self.assertFalse(prod['risk_external']['enabled'])
            self.assertEqual({k: v for k, v in prod.items() if k != 'risk_external'},
                             {k: v for k, v in example.items() if k != 'risk_external'})
        baseline = load_config(ROOT / 'tests' / 'lean' / 'lean.baseline.json')
        self.assertFalse(baseline['risk_external']['enabled'])


# ------------------------------------------------------------------------------------------------------------------ learner guard
class LearnerGuard(unittest.TestCase):
    """``lean.learn.risk_values`` on rows built by the REAL ``risk_external.build_row``."""

    def row(self, **kw):
        base = dict(screen_passed=True, decision_ts=T0, collected_at=T0 + 3, calls=1,
                    fields={'rc_score': 900, 'rc_rugged': True, 'rc_top10_pct': '31.5000', 'copycat_of_recent': 2}, missing={})
        base.update(kw)
        return json.loads(R.canonical(R.build_row(mint(1), **base)))

    def test_values_on_time_are_used_and_late_or_unflagged_ones_are_not(self):
        counts, policy = LN._Counts(), LN.LookaheadPolicy(max_late_s=15)
        v = LN.risk_values(self.row(), T0, policy, counts)
        self.assertEqual((v['rc_score'], v['rc_rugged'], v['rc_top10_pct'], v['copycat_of_recent']), (D(900), D(1), D('31.5'), D(2)))
        self.assertIsNone(v['rc_total_holders'])
        late = LN.risk_values(self.row(collected_at=T0 + 16), T0, policy, counts)
        self.assertEqual({x for x in late.values()}, {None})
        self.assertEqual(counts.excluded['rc_score'], {'AT_COLLECTION_LATE': 1})
        unflagged = self.row()
        unflagged['flags'] = {}
        self.assertEqual(set(LN.risk_values(unflagged, T0, policy, counts).values()), {None})
        self.assertEqual(counts.excluded['rc_score']['FLAG_None'], 1)
        old = self.row()
        old['version'] = 'risk_external_v0'
        self.assertEqual(set(LN.risk_values(old, T0, policy, counts).values()), {None})
        self.assertEqual(counts.skipped['RISK_EXTERNAL_OLD_VERSION'], 1)
        self.assertEqual(set(LN.RISK_FEATURES) <= set(LN.FEATURE_NAMES), True)
        self.assertEqual(set(LN.RISK_FEATURES) - set(R.FIELDS), set())
        self.assertIn('wait_for_risk_external:rc_score', LN.runner_changes([LN.Rule((LN.Condition('rc_score', '>', D(1)),))]))


if __name__ == '__main__':
    unittest.main()
