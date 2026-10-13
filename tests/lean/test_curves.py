"""SYNTHETIC_TEST_ONLY: lean.curves (C2 bonding-curve recorder). Fake RPC objects, a real curves.sqlite, no network, no keys."""
import base64
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from lean import curves as C
from lean.providers import LANE_SHED, ProviderError

ROOT = Path(__file__).resolve().parents[2]
T0 = 1_800_000_000.0
SOL = C.SOL_MINT
CREATOR = bytes(range(1, 33))
INITIAL = C.INITIAL_REAL_TOKEN_RESERVES


def curve_bytes(*, vtr=1_000_000_000_000_000, vsr=40_000_000_000, rtr=None, rsr=10_000_000_000, supply=10 ** 15, complete=False,
                length=125, quote=None, mayhem=0, cashback=0, tail=b''):
    """A BondingCurve account body (desk/schemas/pump.json layout); ``length`` truncates to an older layout."""
    rtr = INITIAL // 4 if rtr is None else rtr                  # progress 0.75
    body = (C.DISCRIMINATOR + vtr.to_bytes(8, 'little') + vsr.to_bytes(8, 'little') + rtr.to_bytes(8, 'little')
            + rsr.to_bytes(8, 'little') + supply.to_bytes(8, 'little') + bytes([int(complete)]) + CREATOR + bytes([mayhem, cashback])
            + _b58decode(quote or SOL) + (25).to_bytes(8, 'little') + bytes([0, 0]) + tail)
    return body[:length] if length < 125 else body


def _b58decode(text):
    from desk.programs import unbase58
    return unbase58(text)


def key(n):
    return C.b58encode(bytes([n]) * 32)


def entry(pubkey, data, owner=C.PUMP):
    return {'pubkey': pubkey, 'account': {'data': [base64.b64encode(data).decode(), 'base64'], 'owner': owner, 'executable': False,
                                          'lamports': 1_000_000, 'space': len(data)}}


class Clock:
    def __init__(self, t=T0):
        self.t = float(t)

    def __call__(self):
        return self.t


class FakeRPC:
    """Stands in for the LOW-lane Helius client. ``script`` is a list of results/exceptions consumed one per call."""
    def __init__(self):
        self.calls, self.script, self.default = [], [], None

    def rpc(self, method, params):
        self.calls.append((method, params))
        item = self.script.pop(0) if self.script else self.default
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(method, params)
        return item, json.dumps(item).encode(), {'attempts': 1}


def v1(entries, slot=500):
    return {'context': {'slot': slot}, 'value': entries}


def v2(entries, slot=500, key=None):
    out = {'accounts': entries, 'context': {'slot': slot}}
    if key is not None:
        out['paginationKey'] = key
    return out


class Base(unittest.TestCase):
    method = 'getProgramAccounts'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.clock = Clock()
        self.rpc = FakeRPC()
        self.raw = {'enabled': True, 'method': self.method, 'min_free_gb': 0, 'stale_s': 600}
        self.recorder = self.make()

    def make(self, **overrides):
        cfg = C.config({**self.raw, **overrides})
        self.db = C.CurveStore(self.dir / 'curves.sqlite', clock=self.clock)
        self.addCleanup(self.db.close)
        return C.CurveRecorder(db=self.db, helius=self.rpc, cfg=cfg, code_version='test', clock=self.clock)

    def rows(self, sql, *args):
        return self.db.db.execute(sql, args).fetchall()

    def poll(self, entries, dt=60, **kw):
        self.clock.t += dt
        self.rpc.script.append(v1(entries, **kw) if self.method == 'getProgramAccounts' else v2(entries, **kw))
        return self.recorder.poll()


class DecoderTests(unittest.TestCase):
    def test_offsets_are_pinned_to_the_pump_schema(self):
        schema = json.loads((ROOT / 'desk' / 'schemas' / 'pump.json').read_text())
        fields = next(t for t in schema['types'] if t['name'] == 'BondingCurve')['type']['fields']
        size = {'u64': 8, 'bool': 1, 'pubkey': 32}
        offset, seen = 8, {}
        for field in fields:
            seen[field['name']] = offset
            offset += size[field['type']]
        self.assertEqual(seen['virtual_token_reserves'], C.OFFSETS['virtual_token_reserves'])
        for ours, theirs in (('virtual_quote_reserves', 'virtual_quote_reserves'), ('real_token_reserves', 'real_token_reserves'),
                             ('real_quote_reserves', 'real_quote_reserves'), ('token_total_supply', 'token_total_supply'),
                             ('complete', 'complete'), ('creator', 'creator'), ('is_mayhem_mode', 'is_mayhem_mode'),
                             ('is_cashback_coin', 'is_cashback_coin'), ('quote_mint', 'quote_mint'), ('creator_fee_bps', 'creator_fee_bps'),
                             ('can_edit_creator_fee', 'can_edit_creator_fee'), ('is_holder_reward', 'is_holder_reward')):
            self.assertEqual(seen[theirs], C.OFFSETS[ours], ours)
        self.assertEqual(offset, C.CURRENT_LEN)
        account = next(a for a in schema['accounts'] if a['name'] == 'BondingCurve')
        self.assertEqual(bytes(account['discriminator']), C.DISCRIMINATOR)
        self.assertEqual(schema['address'], C.PUMP)

    def test_round_trip_of_every_known_layout(self):
        full = C.decode_curve(curve_bytes())
        self.assertEqual((full['virtual_token_reserves'], full['virtual_quote_reserves'], full['real_quote_reserves']),
                         (1_000_000_000_000_000, 40_000_000_000, 10_000_000_000))
        self.assertEqual((full['complete'], full['account_len'], full['quote_mint']), (False, 125, SOL))
        self.assertEqual(full['creator'], C.b58encode(CREATOR))
        for length, creator, quote, flags in ((49, False, None, None), (81, True, None, None), (82, True, None, False),
                                              (83, True, None, False), (115, True, SOL, False), (125, True, SOL, False)):
            d = C.decode_curve(curve_bytes(length=length))
            self.assertEqual(d['account_len'], length)
            self.assertEqual(d['creator'] is not None, creator, length)
            self.assertEqual(d['quote_mint'], quote, length)
            self.assertEqual(d['is_mayhem_mode'], flags, length)
            self.assertTrue(C.is_sol_quoted(d))
        self.assertEqual(C.decode_curve(curve_bytes(complete=True))['complete'], True)

    def test_unknown_appended_bytes_are_ignored_and_other_quote_mints_are_not_sol(self):
        self.assertEqual(C.decode_curve(curve_bytes(tail=b'\x07' * 40))['account_len'], 165)
        other = C.decode_curve(curve_bytes(quote=key(9)))
        self.assertFalse(C.is_sol_quoted(other))

    def test_malformed_accounts_raise_a_code(self):
        good = curve_bytes()
        cases = [(b'', 'TOO_SHORT'), (good[:48], 'TOO_SHORT'), (b'\x00' * 125, 'BAD_DISCRIMINATOR'),
                 (good[:48] + b'\x02' + good[49:], 'INVALID_COMPLETE_FLAG'), (good[:81] + b'\x05' + good[82:], 'INVALID_FLAG'),
                 ('not bytes', 'NOT_BYTES'), (None, 'NOT_BYTES')]
        for data, code in cases:
            with self.assertRaises(C.CurveError, msg=code) as caught:
                C.decode_curve(data)
            self.assertEqual(caught.exception.code, code)

    def test_price_and_progress_known_values(self):
        start = C.decode_curve(curve_bytes(vtr=1_073_000_000_000_000, vsr=30_000_000_000, rtr=INITIAL, rsr=0))
        self.assertAlmostEqual(C.price_sol(start), Decimal(30) / Decimal(1_073_000_000), places=30)      # pump.fun's launch price
        self.assertEqual(C.progress(start), Decimal(0))
        half = C.decode_curve(curve_bytes(rtr=INITIAL // 2))
        self.assertEqual(C.progress(half), Decimal('0.5'))
        done = C.decode_curve(curve_bytes(rtr=0, complete=True))
        self.assertEqual(C.progress(done), Decimal(1))
        self.assertEqual(C.progress(C.decode_curve(curve_bytes(rtr=INITIAL * 2))), Decimal(0))      # clamped
        self.assertIsNone(C.price_sol(C.decode_curve(curve_bytes(vtr=0))))
        self.assertIsNone(C.price_sol(C.decode_curve(curve_bytes(vsr=0))))

    def test_curve_address_is_the_pump_pda_of_the_mint(self):
        from solders.pubkey import Pubkey
        mint = Pubkey.from_bytes(bytes([7]) * 32)
        expected = str(Pubkey.find_program_address([b'bonding-curve', bytes(mint)], Pubkey.from_string(C.PUMP))[0])
        self.assertEqual(C.curve_address(str(mint)), expected)
        with self.assertRaises(ValueError):
            C.curve_address('not-a-mint')

    def test_discriminator_filter_is_its_base58(self):
        from desk.programs import unbase58
        self.assertEqual(unbase58(C.DISCRIMINATOR_B58), C.DISCRIMINATOR)


class ConfigTests(unittest.TestCase):
    def test_absent_is_disabled_and_defaults_are_idempotent(self):
        cfg = C.config(None)
        self.assertFalse(cfg['enabled'])
        self.assertEqual(C.config(cfg), cfg)
        self.assertEqual((cfg['poll_s'], cfg['method'], cfg['min_progress']), (10.0, 'getProgramAccountsV2', 0.5))   # LINT5: v1 cannot list mainnet

    def test_refusals(self):
        bad = [{'typo': 1}, [], 'x', {'enabled': 1}, {'method': 'getAccountInfo'}, {'method': None}, {'poll_s': 0}, {'poll_s': True},
               {'min_progress': 1.5}, {'min_progress': -0.1}, {'max_tracked': 0}, {'max_tracked': 2.5}, {'page_limit': 0}, {'max_pages': 0},
               {'stale_s': 1}, {'credit_per_call': 0}, {'max_credits_month': 0}, {'data_slice_len': 10}, {'data_slice_len': True},
               {'db': ''}, {'db': 5}, {'changed_since': 'yes'}, {'initial_real_token_reserves': 0}, {'min_free_gb': -1}]
        for raw in bad:
            with self.assertRaises(C.ConfigError, msg=str(raw)):
                C.config(raw)

    def test_default_budget_stays_under_half_a_million_credits(self):
        e = C.estimate(C.config(None))
        self.assertEqual((e['requested_poll_s'], e['effective_poll_s']), (10.0, 57.6))        # LINT5: 450k/month default
        self.assertEqual(e['max_calls_per_hour_by_budget'], 62)
        self.assertLess(e['credits_per_month'], 500_000)
        self.assertLessEqual(e['calls_per_hour'], 62.5)

    def test_estimate_follows_the_config(self):
        slow = C.estimate(C.config({'poll_s': 300}))
        self.assertEqual((slow['effective_poll_s'], slow['calls_per_hour']), (300.0, 12.0))
        self.assertEqual(slow['credits_per_month'], 12 * 10 * 720)
        rich = C.estimate(C.config({'max_credits_month': 5_000_000}))
        self.assertEqual(rich['effective_poll_s'], 10.0)
        v2 = C.estimate(C.config({'method': 'getProgramAccountsV2', 'max_pages': 4}))
        self.assertEqual(v2['worst_case_pages_per_poll'], 4)


class RequestShapeTests(Base):
    def test_v1_request(self):
        self.poll([])
        method, params = self.rpc.calls[0]
        self.assertEqual(method, 'getProgramAccounts')
        self.assertEqual(params[0], C.PUMP)
        opts = params[1]
        self.assertEqual(opts['filters'], [{'memcmp': {'offset': 0, 'bytes': C.DISCRIMINATOR_B58}}])
        self.assertEqual((opts['encoding'], opts['withContext'], opts['dataSlice']), ('base64', True, {'offset': 0, 'length': 125}))
        self.assertNotIn('limit', opts)
        self.assertNotIn('complete', json.dumps(opts))          # graduation must stay visible: no server-side complete filter
        self.assertEqual(set(opts), {'filters', 'encoding', 'commitment', 'withContext', 'dataSlice'})     # no dataSize (every layout version)

    def test_data_slice_can_be_disabled(self):
        r = self.make(data_slice_len=None)
        self.rpc.script.append(v1([]))
        r.poll()
        self.assertNotIn('dataSlice', self.rpc.calls[-1][1][1])


class TrackingTests(Base):
    def test_first_sight_starts_a_path_with_a_mark(self):
        self.assertEqual(self.poll([entry(key(1), curve_bytes())]), 1)
        path = self.rows('SELECT curve, segment, creator, quote_mint, account_len, start_progress, code_version FROM curve_paths')
        self.assertEqual(path, [(key(1), 0, C.b58encode(CREATOR), SOL, 125, '0.750000', 'test')])
        mark = self.rows('SELECT curve, segment, ts, slot, virtual_token, virtual_quote, real_token, real_quote, supply, complete, price_sol, '
                         'progress, sol_raised FROM curve_marks')[0]
        self.assertEqual(mark[:3], (key(1), 0, T0 + 60))
        self.assertEqual(mark[3:10], (500, 10 ** 15, 40_000_000_000, INITIAL // 4, 10_000_000_000, 10 ** 15, 0))
        self.assertEqual((Decimal(mark[10]), mark[11], mark[12]), (Decimal(40) / Decimal(10 ** 9), '0.750000', '10'))
        self.assertEqual(self.recorder.health()['tracked'], 1)

    def test_progress_threshold_complete_non_sol_and_capacity(self):
        low = curve_bytes(rtr=INITIAL * 6 // 10)                # progress 0.4
        edge = curve_bytes(rtr=INITIAL // 2)                    # progress exactly 0.5: tracked
        self.poll([entry(key(1), low), entry(key(2), edge), entry(key(3), curve_bytes(rtr=0, complete=True)),
                   entry(key(4), curve_bytes(quote=key(77)))])
        self.assertEqual([r[0] for r in self.rows('SELECT curve FROM curve_paths')], [key(2)])
        s = self.recorder.stats
        self.assertEqual((s['below_min_progress'], s['complete_untracked'], s['non_sol']), (1, 1, 1))
        small = self.make(max_tracked=2)
        small.live.clear()
        self.rpc.script.append(v1([entry(key(10 + i), curve_bytes()) for i in range(5)]))
        small.poll()
        self.assertEqual(small.stats['capacity_dropped'], 3)
        self.assertEqual(len(small.live), 2)

    def test_bad_entries_are_counted_and_never_stop_the_good_ones(self):
        bad = [entry(key(1), curve_bytes(), owner=C.PUMP[:-1] + '7'), entry('not-a-key', curve_bytes()),
               {'pubkey': key(2)}, 'junk', {'pubkey': key(3), 'account': {'owner': C.PUMP, 'data': ['!!', 'base64']}},
               {'pubkey': key(4), 'account': {'owner': C.PUMP, 'data': 'raw'}}, entry(key(5), curve_bytes()[:20]),
               entry(key(6), b'\x01' * 125), entry(key(7), curve_bytes(), owner=None)]
        marks = self.poll(bad + [entry(key(8), curve_bytes())])
        self.assertEqual(marks, 1)
        self.assertEqual([r[0] for r in self.rows('SELECT curve FROM curve_paths')], [key(8)])
        self.assertEqual(self.recorder.stats['decode_errors'] + self.recorder.stats['bad_owner'], 9)
        self.assertIn('DECODE_TOO_SHORT', self.recorder.errors_by_code)

    def test_unchanged_curve_writes_nothing_and_a_change_writes_one_mark(self):
        self.poll([entry(key(1), curve_bytes())])
        self.assertEqual(self.poll([entry(key(1), curve_bytes())]), 0)
        self.assertEqual(self.recorder.stats['unchanged'], 1)
        self.assertEqual(self.poll([entry(key(1), curve_bytes(rtr=INITIAL // 5, rsr=14_000_000_000, vsr=44_000_000_000))]), 1)
        marks = self.rows('SELECT progress, sol_raised, price_sol FROM curve_marks ORDER BY ts')
        self.assertEqual([m[0] for m in marks], ['0.750000', '0.800000'])
        self.assertEqual(marks[1][1], '14')
        self.assertGreater(Decimal(marks[1][2]), Decimal(marks[0][2]))

    def test_duplicate_entries_in_one_response_count_once(self):
        self.assertEqual(self.poll([entry(key(1), curve_bytes()), entry(key(1), curve_bytes())]), 1)
        self.assertEqual(self.recorder.stats['accounts'], 1)

    def test_graduation_writes_the_final_mark_then_ends_and_is_not_restarted(self):
        self.poll([entry(key(1), curve_bytes())])
        done = curve_bytes(rtr=0, rsr=85_000_000_000, complete=True)
        self.assertEqual(self.poll([entry(key(1), done)]), 1)
        self.assertEqual(self.rows('SELECT curve, segment, why, marks FROM curve_ends'), [(key(1), 0, 'GRADUATED', 2)])
        self.assertEqual(self.rows('SELECT complete, progress FROM curve_marks ORDER BY ts DESC LIMIT 1'), [(1, '1.000000')])
        self.assertEqual(self.recorder.health()['tracked'], 0)
        self.assertEqual(self.poll([entry(key(1), done)]), 0)               # still there next poll: untracked, not a new path
        self.assertEqual(self.rows('SELECT COUNT(*) FROM curve_paths'), [(1,)])
        self.assertEqual(self.recorder.stats['graduated'], 1)

    def test_stale_curve_ends_and_a_later_change_starts_a_new_segment(self):
        self.poll([entry(key(1), curve_bytes())])
        self.poll([entry(key(1), curve_bytes())], dt=300)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM curve_ends'), [(0,)])
        self.poll([entry(key(1), curve_bytes())], dt=300)                    # 660 s without a change >= stale_s (600)
        self.assertEqual(self.rows('SELECT curve, segment, why, marks FROM curve_ends'), [(key(1), 0, 'STALE', 1)])
        self.assertEqual(self.recorder.health()['tracked'], 0)
        self.poll([entry(key(1), curve_bytes(rtr=INITIAL // 6))])
        self.assertEqual(self.rows('SELECT curve, segment FROM curve_paths ORDER BY segment'), [(key(1), 0), (key(1), 1)])
        self.assertEqual(self.recorder.health()['tracked'], 1)

    def test_curves_missing_from_a_poll_are_not_ended_by_absence(self):
        self.poll([entry(key(1), curve_bytes())])
        self.poll([])                                                         # V2 returns only changed curves: absence is not an end
        self.assertEqual(self.recorder.health()['tracked'], 1)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM curve_ends'), [(0,)])


class GapAndBudgetTests(Base):
    def test_shed_is_a_skip_with_a_gap_and_no_credits(self):
        self.rpc.script.append(ProviderError(LANE_SHED, True, meta={'why': 'no_token'}))
        self.clock.t += 60
        self.assertEqual(self.recorder.poll(), 0)
        self.assertEqual(self.rows('SELECT cause FROM curve_poll_gaps'), [('shed',)])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM curve_polls'), [(0,)])
        self.assertEqual((self.recorder.stats['shed'], self.recorder.stats['credits']), (1, 0.0))
        self.assertEqual(len(self.rpc.calls), 1)                              # no retry inside the poll

    def test_provider_error_is_a_gap_and_costs_its_attempts(self):
        self.rpc.script.append(ProviderError('HTTP_503', True, meta={'attempts': 2}))
        self.clock.t += 60
        self.recorder.poll()
        self.assertEqual(self.rows('SELECT cause FROM curve_poll_gaps'), [('HTTP_503',)])
        self.assertEqual(self.rows('SELECT calls, credits, ok FROM curve_polls'), [(1, 20.0, 0)])
        self.assertIn('HTTP_503', self.recorder.errors_by_code)

    def test_malformed_responses_are_gaps_not_exceptions(self):
        for bad in ('text', 5, {'value': 'x'}, {'value': [], 'context': {'slot': True}}, {'accounts': [], 'paginationKey': ''}, None):
            self.rpc.script.append(bad)
            self.clock.t += 60
            self.assertEqual(self.recorder.poll(), 0)
        self.assertEqual(self.rows('SELECT DISTINCT cause FROM curve_poll_gaps'), [('MALFORMED_RESPONSE',)])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM curve_marks'), [(0,)])

    def test_unexpected_exceptions_never_escape_poll(self):
        self.rpc.script.append(RuntimeError('boom'))
        self.assertEqual(self.recorder.poll(), 0)
        self.assertEqual(self.recorder.last_error, 'POLL_FAILED')

    def test_hourly_credit_budget_blocks_before_the_call_and_survives_a_restart(self):
        r = self.make(max_credits_month=7200)                                  # 10 credits/hour = one call per hour
        self.rpc.script.append(v1([]))
        self.clock.t += 1
        r.poll()
        self.assertEqual(len(self.rpc.calls), 1)
        self.clock.t += 600
        self.rpc.script.append(v1([]))
        r.poll()
        self.assertEqual(len(self.rpc.calls), 1)                               # skipped: the hour's budget is spent
        self.assertEqual(self.rows('SELECT cause FROM curve_poll_gaps'), [('budget',)])
        self.assertEqual(r.stats['budget_skips'], 1)
        restarted = C.CurveRecorder(db=self.db, helius=self.rpc, cfg=C.config({**self.raw, 'max_credits_month': 7200}),
                                    code_version='test', clock=self.clock)
        self.rpc.script.append(v1([]))
        self.clock.t += 60
        restarted.poll()
        self.assertEqual(len(self.rpc.calls), 1)                               # the restart did not reset the budget
        self.clock.t += 3600
        self.rpc.script.append(v1([]))
        restarted.poll()
        self.assertEqual(len(self.rpc.calls), 2)                               # a new hour

    def test_effective_interval_is_stretched_by_the_budget(self):
        self.assertAlmostEqual(self.recorder.effective_interval_s(), 57.6)
        fast = self.make(max_credits_month=5_000_000)
        self.assertEqual(fast.effective_interval_s(), 10.0)
        slow = self.make(poll_s=120)
        self.assertEqual(slow.effective_interval_s(), 120.0)

    def test_disk_low_writes_nothing(self):
        r = self.make(min_free_gb=10)
        with mock.patch.object(self.db, 'free_bytes', return_value=1024):
            self.rpc.script.append(v1([entry(key(1), curve_bytes())]))
            self.assertEqual(r.poll(), 0)
        self.assertEqual(len(self.rpc.calls), 0)
        self.assertEqual(r.stats['disk_low'], 1)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM curve_paths'), [(0,)])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM curve_poll_gaps'), [(0,)])

    def test_a_failed_write_leaves_no_phantom_state_and_the_next_poll_records(self):
        with mock.patch.object(self.db, 'write', side_effect=sqlite3.OperationalError('disk I/O error')):
            self.assertEqual(self.poll([entry(key(1), curve_bytes())]), 0)
        self.assertEqual(self.recorder.health()['tracked'], 0)
        self.assertEqual(self.recorder.live, {})
        self.assertEqual((self.recorder.stats['started'], self.recorder.stats['marks'], self.recorder.stats['write_failures']), (0, 0, 1))
        self.assertEqual(self.poll([entry(key(1), curve_bytes())]), 1)
        self.assertEqual(self.recorder.health()['tracked'], 1)


class PersistenceTests(Base):
    def test_resume_restores_live_paths_dedupes_and_marks_downtime(self):
        self.poll([entry(key(1), curve_bytes()), entry(key(2), curve_bytes(rtr=INITIAL // 3))])
        self.clock.t += 5000
        again = C.CurveRecorder(db=self.db, helius=self.rpc, cfg=C.config(self.raw), code_version='test', clock=self.clock)
        self.assertEqual(again.health()['tracked'], 2)
        self.assertEqual(self.rows('SELECT cause FROM curve_poll_gaps'), [('DOWNTIME',)])
        self.rpc.script.append(v1([entry(key(1), curve_bytes())]))
        self.clock.t += 10
        self.assertEqual(again.poll(), 0)                                      # unchanged since before the restart
        self.assertEqual(self.rows('SELECT COUNT(*) FROM curve_paths'), [(2,)])

    def test_rollover_carries_live_paths_and_their_last_mark(self):
        self.poll([entry(key(1), curve_bytes()), entry(key(2), curve_bytes())])
        self.poll([entry(key(2), curve_bytes(rtr=0, complete=True))])
        archive = self.db.rollover()
        self.assertTrue(archive.name.startswith('curves-') and archive.exists())
        self.assertEqual(self.rows('SELECT curve, carried_from FROM curve_paths'), [(key(1), archive.name)])
        self.assertEqual(self.rows('SELECT curve FROM curve_marks'), [(key(1),)])
        with sqlite3.connect(archive) as old:
            self.assertEqual(old.execute('SELECT COUNT(*) FROM curve_ends').fetchone(), (1,))
        self.recorder.live.clear()
        again = C.CurveRecorder(db=self.db, helius=self.rpc, cfg=C.config(self.raw), code_version='test', clock=self.clock)
        self.assertEqual(again.health()['tracked'], 1)

    def test_size_triggered_rollover_in_the_poll(self):
        r = self.make(max_db_gb=0.001)
        with mock.patch.object(self.db, 'size_bytes', side_effect=[2 * 1024 ** 3, 100]):
            self.rpc.script.append(v1([entry(key(1), curve_bytes())]))
            self.clock.t += 60
            r.poll()
        self.assertEqual(r.stats['rollovers'], 1)
        self.assertEqual(len(list(self.dir.glob('curves-*.sqlite'))), 1)
        self.assertEqual(self.rows('SELECT curve FROM curve_paths'), [(key(1),)])


class V2Tests(Base):
    method = 'getProgramAccountsV2'

    def test_request_pagination_and_cursor(self):
        e1, e2, e3 = (entry(key(i), curve_bytes()) for i in (1, 2, 3))
        self.rpc.script += [v2([e1], slot=100, key='k1'), v2([e2], slot=101, key='k2'), v2([e3], slot=102)]
        self.clock.t += 60
        self.recorder.poll()
        first = self.rpc.calls[0][1][1]
        self.assertEqual((first['limit'], 'changedSinceSlot' in first, 'paginationKey' in first, 'withContext' in first), (1000, False, False, False))
        self.assertEqual([c[1][1].get('paginationKey') for c in self.rpc.calls], [None, 'k1', 'k2'])
        self.assertEqual(self.recorder.cursor, 100)                          # LINT5: the LOWEST slot of the poll's pages
        self.assertEqual(self.db.meta('changed_since_slot'), '100')
        self.assertEqual(len(self.rows('SELECT curve FROM curve_paths')), 3)
        self.rpc.script.append(v2([], slot=110))
        self.clock.t += 60
        self.recorder.poll()
        self.assertEqual(self.rpc.calls[-1][1][1]['changedSinceSlot'], 100)
        self.assertEqual(self.recorder.cursor, 110)

    def test_cursor_survives_a_restart_and_changed_since_can_be_off(self):
        self.rpc.script.append(v2([], slot=300))
        self.clock.t += 60
        self.recorder.poll()
        again = C.CurveRecorder(db=self.db, helius=self.rpc, cfg=C.config(self.raw), code_version='test', clock=self.clock)
        self.assertEqual(again.cursor, 300)
        off = C.CurveRecorder(db=self.db, helius=self.rpc, cfg=C.config({**self.raw, 'changed_since': False}), code_version='test', clock=self.clock)
        self.rpc.script.append(v2([], slot=310))
        self.clock.t += 60
        off.poll()
        self.assertNotIn('changedSinceSlot', self.rpc.calls[-1][1][1])

    def test_a_failure_on_page_two_keeps_page_one_but_not_the_cursor(self):
        self.rpc.script += [v2([entry(key(1), curve_bytes())], slot=100, key='k1'), ProviderError('HTTP_503', True)]
        self.clock.t += 60
        self.assertEqual(self.recorder.poll(), 1)
        self.assertIsNone(self.recorder.cursor)
        self.assertIsNone(self.db.meta('changed_since_slot'))
        self.assertEqual(self.rows('SELECT cause FROM curve_poll_gaps'), [('HTTP_503',)])

    def test_running_out_of_pages_is_a_gap_and_still_advances_the_cursor(self):
        # LINT5: the first (cursor-less) poll of mainnet can never list millions of curves in max_pages: it must move on
        self.rpc.script += [v2([], slot=100 + i, key='k%d' % i) for i in range(5)]
        self.clock.t += 60
        self.recorder.poll()
        self.assertEqual(len(self.rpc.calls), 3)                              # max_pages
        self.assertEqual(self.recorder.cursor, 100)
        self.assertEqual(self.rows('SELECT cause FROM curve_poll_gaps'), [('max_pages',)])
        self.rpc.script[:] = [v2([], slot=200)]
        self.clock.t += 60
        self.recorder.poll()
        self.assertEqual(self.rpc.calls[-1][1][1]['changedSinceSlot'], 100)  # incremental from then on
        self.assertEqual(self.recorder.cursor, 200)

    def test_avg_pages_stretches_the_interval(self):
        self.rpc.script += [v2([], slot=1, key='a'), v2([], slot=2, key='b'), v2([], slot=3)]
        self.clock.t += 60
        self.recorder.poll()
        self.assertGreater(self.recorder.avg_pages, 1.3)
        self.assertGreater(self.recorder.effective_interval_s(), 51.84 * 1.3)
        self.assertAlmostEqual(self.recorder.health()['effective_poll_s'], self.recorder.effective_interval_s(), places=2)

    def test_response_without_a_slot_keeps_the_old_cursor(self):
        self.rpc.script += [v2([], slot=200)]
        self.clock.t += 60
        self.recorder.poll()
        self.rpc.script += [{'accounts': []}]
        self.clock.t += 60
        self.recorder.poll()
        self.assertEqual(self.recorder.cursor, 200)

    def test_normalize_shapes(self):
        n = C.CurveRecorder.normalize
        self.assertEqual(n([]), ([], None, None))
        self.assertEqual(n({'value': [1], 'context': {'slot': 5}}), ([1], 5, None))
        self.assertEqual(n({'accounts': [], 'slot': 9, 'paginationKey': 'x'}), ([], 9, 'x'))
        for bad in ('x', None, 1, {}, {'accounts': 'no'}, {'accounts': [], 'context': {'slot': -1}},
                    {'accounts': [], 'paginationKey': 5}, {'accounts': [], 'paginationKey': 'x' * 5000}):
            with self.assertRaises(ValueError, msg=str(bad)):
                n(bad)


class LoopTests(Base):
    def test_run_loop_polls_until_stop_and_never_raises(self):
        self.recorder.stop = threading.Event()

        def stopping(method, params):
            self.recorder.stop.set()
            raise RuntimeError('boom')
        self.rpc.script.append(stopping)
        done = threading.Thread(target=self.recorder.run_loop, daemon=True)
        done.start()
        done.join(5)
        self.assertFalse(done.is_alive())
        self.assertEqual(self.recorder.last_error, 'POLL_FAILED')

    def test_stop_before_the_first_page_makes_no_call(self):
        self.recorder.stop.set()
        self.recorder.poll()
        self.assertEqual(self.rpc.calls, [])


class JoinAndReportTests(Base):
    def setUp(self):
        super().setUp()
        from solders.pubkey import Pubkey
        self.mint = str(Pubkey.from_bytes(bytes([7]) * 32))
        self.curve = C.curve_address(self.mint)

    def pool_db(self, marks):
        from lean import paths as L
        store = L.PathStore(self.dir / 'paths.sqlite', clock=self.clock)
        self.addCleanup(store.close)
        store.add(marks=marks)
        return self.dir / 'paths.sqlite'

    def test_one_continuous_series_for_a_mint(self):
        self.poll([entry(self.curve, curve_bytes())])
        self.poll([entry(self.curve, curve_bytes(rtr=INITIAL // 8))])
        self.poll([entry(self.curve, curve_bytes(rtr=0, complete=True))])
        grad = self.clock.t
        marks = [(self.mint, grad + 30 * i, 10 + i, 4 * 10 ** 14, 50 * 10 ** 9 + i, '1', '100', 'OK') for i in range(1, 4)]
        marks.append((self.mint, grad + 200, 20, None, None, None, None, 'ACCOUNT_MISSING'))
        marks.append(('OTHERMINT' + '1' * 30, grad + 10, 1, 1, 1, '1', '1', 'OK'))
        joined = C.join_by_mint(self.db.path, self.pool_db(marks), self.mint)
        self.assertEqual(joined['curve'], self.curve)
        sources = [s['source'] for s in joined['series']]
        self.assertEqual(sources, ['curve'] * 3 + ['pool'] * 3)                 # missing-account mark and the other mint are excluded
        times = [s['ts'] for s in joined['series']]
        self.assertEqual(times, sorted(times))
        self.assertEqual(joined['ends'][0]['why'], 'GRADUATED')
        self.assertLessEqual(joined['ends'][0]['ts'], joined['series'][3]['ts'])
        pool = joined['series'][3]
        expected = (Decimal(50 * 10 ** 9 + 1) / Decimal(10 ** 9)) / (Decimal(4 * 10 ** 14) / Decimal(10 ** 6))
        self.assertAlmostEqual(Decimal(pool['price_sol']), expected, places=15)                  # stored to 18 decimals
        self.assertEqual((joined['curve_marks'], joined['pool_marks']), (3, 3))

    def test_join_without_a_paths_db_or_for_an_unknown_mint(self):
        self.poll([entry(self.curve, curve_bytes())])
        self.assertEqual(C.join_by_mint(self.db.path, None, self.mint)['pool_marks'], 0)
        from solders.pubkey import Pubkey
        other = C.join_by_mint(self.db.path, None, str(Pubkey.from_bytes(bytes([8]) * 32)))
        self.assertEqual(other['series'], [])

    def test_join_is_read_only_and_refuses_links(self):
        self.poll([entry(self.curve, curve_bytes())])
        before = self.db.path.read_bytes()
        C.join_by_mint(self.db.path, None, self.mint)
        link = self.dir / 'link.sqlite'
        link.symlink_to(self.db.path)
        with self.assertRaises(ValueError):
            C.join_by_mint(link, None, self.mint)
        with self.assertRaises(ValueError):
            C.join_by_mint(self.dir / 'missing.sqlite', None, self.mint)
        self.db.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.assertEqual(self.db.path.read_bytes()[:16], before[:16])

    def test_summary_and_cli_report(self):
        self.poll([entry(self.curve, curve_bytes())])
        self.poll([entry(self.curve, curve_bytes(rtr=0, complete=True))])
        self.rpc.script.append(ProviderError(LANE_SHED, True))
        self.clock.t += 60
        self.recorder.poll()
        self.db.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        s = C.summary(self.db.path)
        self.assertEqual((s['paths'], s['marks'], s['live'], s['ends'], s['gaps'], s['polls'], s['credits'], s['graduated_with_final_mark']),
                         (1, 2, 0, {'GRADUATED': 1}, {'shed': 1}, 2, 20.0, 1))
        out = subprocess.run([sys.executable, '-m', 'lean.curves', 'report', '--db', str(self.db.path)], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(out.returncode, 0, out.stderr)
        report = json.loads(out.stdout)
        self.assertEqual(report['status'], 'OK')
        self.assertEqual(report['estimate']['effective_poll_s'], 57.6)
        bad = subprocess.run([sys.executable, '-m', 'lean.curves', 'report', '--db', str(self.dir / 'none.sqlite')], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(bad.returncode, 2)


class StoreSafetyTests(unittest.TestCase):
    def test_symlink_and_directory_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(os.path.realpath(tmp))
            real = tmp / 'real.sqlite'
            real.touch()
            (tmp / 'link.sqlite').symlink_to(real)
            with self.assertRaises(ValueError):
                C.CurveStore(tmp / 'link.sqlite')
            (tmp / 'dir').mkdir()
            with self.assertRaises(ValueError):
                C.CurveStore(tmp / 'dir')
            with self.assertRaises(ValueError):
                C.CurveStore(tmp / 'missing' / 'curves.sqlite')

    def test_file_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = C.CurveStore(Path(tmp) / 'curves.sqlite')
            self.addCleanup(store.close)
            self.assertEqual(oct((Path(tmp) / 'curves.sqlite').stat().st_mode & 0o777), '0o600')

    def test_build_is_off_when_disabled_and_uses_the_low_lane_otherwise(self):
        self.assertIsNone(C.build(C.config(None), state_dir='/nonexistent', keys={}, code_version='x'))
        with tempfile.TemporaryDirectory() as tmp:
            fake = mock.Mock()
            with mock.patch('lean.providers.low', return_value=mock.Mock(helius=fake)) as low:
                recorder = C.build(C.config({'enabled': True}), state_dir=tmp, keys={'helius': 'k', 'jupiter': 'k'}, code_version='x')
            low.assert_called_once()
            self.assertIs(recorder.helius, fake)
            self.assertEqual(recorder.db.path, Path(tmp) / 'curves.sqlite')
            recorder.db.close()


if __name__ == '__main__':
    unittest.main()
