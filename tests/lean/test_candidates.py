"""SYNTHETIC_TEST_ONLY: lean candidate intake + screening. Fixtures only, no network, no credentials.

Discovery frames: the desk's migration transaction fixture (tests.test_graduation_witness) written into a real
discovery schema (discovery.continuous.initialize); pool/vault/mint accounts: tests.test_pools.PoolTests bytes.
"""
import base64
import copy
import json
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from desk.model import canonical, digest
from desk.programs import unbase58
from desk.security import TOKEN_2022, TOKEN_PROGRAM, base58
from discovery import continuous as discovery
from lean import candidates as lc
from tests.test_graduation_witness import fixture as migration_fixture
from tests.test_pools import PoolTests

NOW = 1_800_000_000.0


# ------------------------------------------------------------------------------------------- discovery
class DiscoveryFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.db = self.root / 'continuous.sqlite'
        with patch.object(discovery.time, 'time', return_value=NOW - 10_000):
            discovery.initialize(self.db)
        self.raw0, self.mint0, self.pool0 = migration_fixture()
        self.next_seq = 1

    def frame(self, tag, *, no_op=False, non_sol=False):
        """A distinct real migration transaction (mint/pool/signature derived from ``tag``)."""
        from solders.pubkey import Pubkey
        from desk import graduation_witness as g
        raw = copy.deepcopy(self.raw0)
        if non_sol:
            raw, _, _ = migration_fixture('migrate_v2')
        mint = str(Pubkey.from_bytes(bytes([tag]) * 32))
        authority = g._pda([b'pool-authority', unbase58(mint)], g.PUMP)
        curve = g._pda([b'bonding-curve', unbase58(mint)], g.PUMP)
        quote = str(Pubkey.from_bytes(bytes([tag + 1]) * 32)) if non_sol else g.SOL
        pool = g._pda([b'pool', b'\0\0', unbase58(authority), unbase58(mint), unbase58(quote)], g.AMM)
        old_authority = g._pda([b'pool-authority', unbase58(self.mint0)], g.PUMP)
        old_curve = g._pda([b'bonding-curve', unbase58(self.mint0)], g.PUMP)
        replacements = {self.mint0: mint, self.pool0: pool, old_authority: authority, old_curve: curve}
        if non_sol:
            replacements[g.SOL] = quote
        for ix in raw['transaction']['message']['instructions']:
            ix['accounts'] = [replacements.get(x, x) for x in ix['accounts']]
        ix = raw['meta']['innerInstructions'][0]['instructions'][0]
        data = unbase58(ix['data'])
        for old, new in replacements.items():
            data = data.replace(unbase58(old), unbase58(new))
        ix['data'] = base58(data)
        if no_op:
            raw['meta']['innerInstructions'] = []
        signature = base58(bytes([tag + 50]) * 64)
        raw['transaction']['signatures'] = [signature]
        wire = {'method': 'transactionNotification', 'params': {'result': {
            'signature': signature, 'slot': raw['slot'], 'blockTime': raw['blockTime'],
            'transaction': {'transaction': raw['transaction'], 'meta': raw['meta']}}}}
        return wire, mint, pool, signature, raw['slot']

    def add(self, wire, received, *, slot=100, payload=None, stored_hash=None):
        text = canonical(wire) if payload is None else payload
        seq = self.next_seq; self.next_seq += 1
        with sqlite3.connect(self.db) as c:
            c.execute('INSERT INTO raw_events(seq,source_id,received_at,slot,payload,payload_hash) VALUES(?,?,?,?,?,?)',
                      (seq, 'confirmed:%d' % seq, received, slot, text, stored_hash or digest(wire)))
        return seq

    def add_migration(self, tag, age, **kw):
        wire, mint, pool, signature, slot = self.frame(tag, **{k: v for k, v in kw.items() if k in ('no_op', 'non_sol')})
        seq = self.add(wire, NOW - age, slot=slot)
        return seq, mint, pool, signature, slot


class IntakeTests(DiscoveryFixture):
    def test_returns_migration_candidate_with_exact_identity(self):
        seq, mint, pool, signature, slot = self.add_migration(21, 900)
        result = lc.scan_new(self.db, 0, now=NOW)
        self.assertEqual(result.candidates, [lc.Candidate(seq, mint, pool, signature, slot, NOW - 900, result.candidates[0].payload_hash)])
        self.assertEqual(result.next_cursor, seq)
        self.assertEqual(lc.iter_new_candidates(self.db, 0, now=NOW)[0].mint, mint)

    def test_cursor_makes_scan_incremental_and_new_frames_appear_later(self):
        first = self.add_migration(21, 900)
        result = lc.scan_new(self.db, 0, now=NOW)
        self.assertEqual(lc.scan_new(self.db, result.next_cursor, now=NOW).candidates, [])
        second = self.add_migration(23, 800)
        again = lc.scan_new(self.db, result.next_cursor, now=NOW)
        self.assertEqual([c.mint for c in again.candidates], [second[1]])
        self.assertEqual(again.next_cursor, second[0])
        self.assertNotEqual(first[1], second[1])

    def test_age_window_young_frame_blocks_without_advancing_old_frame_skipped(self):
        old = self.add_migration(21, 20_000)
        ready = self.add_migration(23, 900)
        young = self.add_migration(25, 60)
        result = lc.scan_new(self.db, 0, now=NOW)
        self.assertEqual([c.mint for c in result.candidates], [ready[1]])
        self.assertEqual(result.stats['too_old'], 1)
        self.assertTrue(result.stats['waiting_for_age'])
        self.assertEqual(result.next_cursor, ready[0])          # stops before the young frame
        later = lc.scan_new(self.db, result.next_cursor, now=NOW + 400)
        self.assertEqual([c.mint for c in later.candidates], [young[1]])   # not lost: age caught up
        self.assertEqual(old[1] != young[1], True)

    def test_altered_frame_never_used_and_cursor_moves_past_it(self):
        wire, mint, *_ = self.frame(21)
        bad = self.add(wire, NOW - 900, stored_hash='0' * 64)
        good = self.add_migration(23, 900)
        result = lc.scan_new(self.db, 0, now=NOW)
        self.assertEqual([c.mint for c in result.candidates], [good[1]])
        self.assertEqual(result.stats['altered'], 1)
        self.assertGreater(result.next_cursor, bad)

    def test_garbage_noop_and_non_sol_frames_are_counted_not_candidates(self):
        junk = {'method': 'x'}
        self.add(junk, NOW - 900)
        self.add_migration(21, 900, no_op=True)
        self.add_migration(23, 900, non_sol=True)
        result = lc.scan_new(self.db, 0, now=NOW)
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.stats['frames'], 3)
        self.assertEqual(result.stats['undecodable'] + result.stats['not_migration'] + result.stats['unsupported_pool'], 3)

    def test_duplicate_mint_in_batch_is_returned_once(self):
        wire, mint, pool, signature, slot = self.frame(21)
        self.add(wire, NOW - 900, slot=slot)
        again = copy.deepcopy(wire)
        again['params']['result']['signature'] = base58(bytes([99]) * 64)
        again['params']['result']['transaction']['transaction']['signatures'] = [again['params']['result']['signature']]
        self.add(again, NOW - 880, slot=slot)
        result = lc.scan_new(self.db, 0, now=NOW)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.stats['duplicate_mint'], 1)

    def test_discovery_database_is_read_only_and_unchanged(self):
        self.add_migration(21, 900)
        before = self.db.read_bytes()
        lc.scan_new(self.db, 0, now=NOW)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['continuous.sqlite'])

    def test_symlinked_or_missing_discovery_refused(self):
        link = self.root / 'link.sqlite'; link.symlink_to(self.db)
        with self.assertRaises(ValueError):
            lc.scan_new(link, 0, now=NOW)
        with self.assertRaises(OSError):
            lc.scan_new(self.root / 'missing.sqlite', 0, now=NOW)

    def test_argument_bounds(self):
        for bad in ({'cursor': -1}, {'cursor': True}, {'cursor': 0, 'limit': 0}):
            with self.assertRaises(ValueError):
                lc.scan_new(self.db, bad.get('cursor'), now=NOW, **({'limit': bad['limit']} if 'limit' in bad else {}))

    def test_missing_corrupt_or_locked_discovery_is_typed_unavailable(self):
        with self.assertRaises(lc.DiscoveryUnavailable) as cm:
            lc.scan_new(self.root / 'missing.sqlite', 0, now=NOW)
        self.assertEqual((cm.exception.code, cm.exception.transient), ('DISCOVERY_UNAVAILABLE', True))
        junk = self.root / 'junk.sqlite'; junk.write_bytes(b'not a database' * 100)
        with self.assertRaises(lc.DiscoveryUnavailable):
            lc.scan_new(junk, 0, now=NOW)
        locked = self.root / 'locked.sqlite'
        holder = sqlite3.connect(locked, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute('CREATE TABLE raw_events(seq INTEGER PRIMARY KEY, received_at REAL, slot INTEGER, payload TEXT, payload_hash TEXT)')
        holder.execute('BEGIN EXCLUSIVE')
        with patch.object(lc, 'DISCOVERY_TIMEOUT', 0.05), self.assertRaises(lc.DiscoveryUnavailable):
            lc.scan_new(locked, 0, now=NOW)
        holder.execute('ROLLBACK')
        self.assertEqual(lc.scan_new(locked, 0, now=NOW).candidates, [])     # retried next pass: works again

    def test_live_wal_discovery_is_readable_with_a_read_only_shm_and_directory(self):
        """The systemd unit keeps the discovery dir read-only (ProtectSystem=strict): SQLite must read a live WAL
        database through the EXISTING -wal/-shm without being able to write either (deploy/lean/desk-lean.service)."""
        seq, mint, *_ = self.add_migration(21, 900)
        writer = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(writer.close)
        self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        later, later_mint, *_ = self.add_migration(23, 800)                 # committed into the -wal, not checkpointed
        writer.execute('BEGIN'); writer.execute('SELECT COUNT(*) FROM raw_events').fetchone(); writer.execute('COMMIT')
        shm = Path(str(self.db) + '-shm')
        self.assertTrue(shm.exists() and Path(str(self.db) + '-wal').exists())
        shm.chmod(0o444); self.root.chmod(0o555)
        self.addCleanup(shm.chmod, 0o644); self.addCleanup(self.root.chmod, 0o755)
        result = lc.scan_new(self.db, 0, now=NOW)
        self.assertEqual([c.mint for c in result.candidates], [mint, later_mint])

    def test_iter_new_candidates_keeps_the_cursor_past_batches_without_candidates(self):
        for _ in range(4):
            self.add({'method': 'x'}, NOW - 900)
        first = lc.iter_new_candidates(self.db, 0, now=NOW, limit=2)
        self.assertEqual((list(first), first.next_cursor), ([], 2))         # nothing found, cursor still advances
        second = lc.iter_new_candidates(self.db, first.next_cursor, now=NOW, limit=2)
        self.assertEqual(second.next_cursor, 4)
        seq, mint, *_ = self.add_migration(21, 900)
        third = lc.iter_new_candidates(self.db, second.next_cursor, now=NOW)
        self.assertEqual(([c.mint for c in third], len(third), third[0].seq, third.next_cursor), ([mint], 1, seq, seq))

    def test_limit_truncates_and_cursor_resumes(self):
        for i in range(5):
            self.add_migration(21 + 2 * i, 900)
        first = lc.scan_new(self.db, 0, now=NOW, limit=2)
        self.assertTrue(first.stats['truncated']); self.assertEqual(len(first.candidates), 2)
        rest = lc.scan_new(self.db, first.next_cursor, now=NOW)
        self.assertEqual(len(rest.candidates), 3)
        self.assertFalse({c.mint for c in first.candidates} & {c.mint for c in rest.candidates})


class CursorFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'cursor.json'

    def test_roundtrip_absent_zero_forward_only_and_corrupt(self):
        self.assertEqual(lc.read_cursor(self.path), 0)
        lc.write_cursor(self.path, 7)
        self.assertEqual(lc.read_cursor(self.path), 7)
        lc.write_cursor(self.path, 7)
        with self.assertRaises(ValueError):
            lc.write_cursor(self.path, 6)
        self.assertEqual(lc.read_cursor(self.path), 7)
        self.path.write_text('{"seq": -1}')
        with self.assertRaises(ValueError):
            lc.read_cursor(self.path)
        self.path.write_text('not json')
        with self.assertRaises(ValueError):
            lc.read_cursor(self.path)
        with self.assertRaises(ValueError):
            lc.write_cursor(self.path, True)

    def test_no_temp_file_left_and_mode_private(self):
        lc.write_cursor(self.path, 3)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ['cursor.json'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


# --------------------------------------------------------------------------------------------- screening
class ProviderError(Exception):
    def __init__(self, code, transient):
        super().__init__(code); self.code = code; self.transient = transient


class World:
    """Accounts for one synthetic canonical migrated pool (PoolTests bytes); tests mutate it."""
    def __init__(self, base_raw=10**13, quote_lamports=50 * 10**9, supply=10**15, decimals=6):
        self.t = PoolTests('test_exact_pool_pdas_and_vaults'); self.t.setUp()
        t = self.t
        self.mint, self.pool, self.lp = str(t.mint), str(t.pool), str(t.lp)
        self.base_vault, self.quote_vault = str(t.vaults[0]), str(t.vaults[1])
        mint = bytearray(82); mint[36:44] = supply.to_bytes(8, 'little'); mint[44] = decimals; mint[45] = 1
        self.accounts = {
            self.mint: t.account(bytes(mint), TOKEN_PROGRAM),
            self.pool: t.account(t.raw, t.owner),
            self.base_vault: self.vault(t.mint, base_raw),
            self.quote_vault: self.vault(t.Pubkey.from_string('So11111111111111111111111111111111111111112'), quote_lamports),
            self.lp: self.lp_account(0),
        }
        self.holders = [{'address': base58(bytes([i + 1]) * 32), 'amount': str(10**12), 'decimals': 6} for i in range(5)]
        self.slot = 100

    def vault(self, mint, amount, *, delegate=False, frozen=False, owner=TOKEN_PROGRAM):
        d = bytearray(165); d[:32] = bytes(mint); d[32:64] = bytes(self.t.pool); d[64:72] = amount.to_bytes(8, 'little')
        d[108] = 0 if frozen else 1; d[72] = int(delegate)
        return self.t.account(bytes(d), owner)

    def lp_account(self, supply, authority=None):
        d = bytearray(82); d[:4] = (1).to_bytes(4, 'little'); d[4:36] = bytes(authority or self.t.pool)
        d[36:44] = supply.to_bytes(8, 'little'); d[45] = 1
        return self.t.account(bytes(d), TOKEN_PROGRAM)

    def patch_bytes(self, key, offset, value):
        a = self.accounts[key]; data = bytearray(base64.b64decode(a['data'][0])); data[offset:offset + len(value)] = value
        a['data'][0] = base64.b64encode(bytes(data)).decode()


class FakeHelius:
    def __init__(self, world, log):
        self.world, self.log, self.fail, self.holder_fail, self.mutate = world, log, None, None, None

    def get_multiple_accounts(self, keys):
        self.log.append(('accounts', tuple(keys)))
        if self.fail:
            raise self.fail
        values = [copy.deepcopy(self.world.accounts.get(k)) for k in keys]
        result = {'context': {'slot': self.world.slot + len(self.log)}, 'value': values}
        if self.mutate:
            result = self.mutate(keys, result)
        return result, json.dumps(result).encode(), {'status': 200}

    def rpc(self, method, params):
        self.log.append((method, tuple(params[:1])))
        assert method == 'getTokenLargestAccounts'
        if self.holder_fail:
            raise self.holder_fail
        rows = [dict(r) for r in self.world.holders]
        # the pool's base vault is the largest account; it must be excluded from concentration
        rows.insert(0, {'address': self.world.base_vault, 'amount': str(8 * 10**14), 'decimals': 6})
        result = {'context': {'slot': 200}, 'value': rows}
        return result, json.dumps(result).encode(), {}


class FakeKraken:
    def __init__(self, log, price='100'):
        self.log, self.price, self.fail = log, price, None

    def sol_usd(self):
        self.log.append(('sol_usd', ()))
        if self.fail:
            raise self.fail
        return self.price, b'{"result":{"SOLUSD":[["100"]]}}', {}


class Providers:
    def __init__(self, world):
        self.log = []
        self.helius, self.kraken = FakeHelius(world, self.log), FakeKraken(self.log)


class ScreenBase(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.providers = Providers(self.world)
        self.candidate = lc.Candidate(1, self.world.mint, self.world.pool, base58(bytes([9]) * 64), 90, NOW - 900, '0' * 64)

    def screen(self, **kw):
        return lc.screen(self.candidate, self.providers, kw.pop('cfg', None), now=kw.pop('now', NOW), **kw)

    def assertRejected(self, result, reason):
        self.assertFalse(result.passed, result)
        self.assertIn(reason, result.reasons)
        self.assertIsNone(result.error)


class ScreenPassTests(ScreenBase):
    def test_clean_candidate_passes_with_features_raw_and_exact_call_budget(self):
        result = self.screen()
        self.assertTrue(result.passed, result.reasons)
        self.assertEqual([c[0] for c in self.providers.log], ['sol_usd', 'accounts', 'accounts', 'getTokenLargestAccounts'])
        self.assertEqual(self.providers.log[1][1], (self.world.mint, self.world.pool))
        f = result.features
        # price = (50 SOL)/(1e7 tokens) = 5e-6 SOL; supply 1e9 tokens; SOL=$100  -> $500k cap, $10k liquidity
        self.assertEqual(f['market_cap_usd'], '500000.0000000000000000000000000000'[:len(f['market_cap_usd'])])
        self.assertEqual(float(f['market_cap_usd']), 500_000.0)
        self.assertEqual(float(f['liquidity_usd']), 10_000.0)
        self.assertEqual(f['holder_check'], 'OK')
        self.assertEqual(float(f['top10_pct_excluding_pool']), 0.5)     # 5 holders x 1e12 / 1e15 supply
        self.assertEqual([label for label, _ in result.raw], ['sol_usd', 'accounts_mint_pool', 'accounts_vaults_lp', 'holders'])
        self.assertTrue(all(isinstance(b, bytes) and b for _, b in result.raw))
        self.assertIn('OWNERSHIP_AND_FUNDING_HISTORY', result.unknowns)     # what is NOT checked is explicit

    def test_cached_sol_usd_avoids_the_kraken_call(self):
        result = self.screen(sol_usd='100')
        self.assertTrue(result.passed)
        self.assertNotIn('sol_usd', [c[0] for c in self.providers.log])

    def test_holder_check_can_be_disabled_and_spends_no_call(self):
        result = self.screen(cfg={'holder_check': False})
        self.assertTrue(result.passed)
        self.assertNotIn('getTokenLargestAccounts', [c[0] for c in self.providers.log])


class ScreenRejectionTests(ScreenBase):
    def test_age_and_binding_reject_before_any_provider_call(self):
        self.assertRejected(self.screen(now=NOW - 800), 'TOO_YOUNG')
        self.assertRejected(self.screen(now=NOW + 10_000), 'TOO_OLD')
        self.candidate = lc.Candidate(1, self.world.mint, self.world.quote_vault, 'sig', 1, NOW - 900, '0' * 64)
        self.assertRejected(self.screen(), 'POOL_BINDING_INVALID')
        self.candidate = lc.Candidate(1, 'not-an-address', self.world.pool, 'sig', 1, NOW - 900, '0' * 64)
        self.assertRejected(self.screen(), 'CANDIDATE_ADDRESS_INVALID')
        self.assertEqual(self.providers.log, [])

    def test_mint_authorities_and_supply(self):
        for offset, value, reason in ((0, (1).to_bytes(4, 'little'), 'ACTIVE_MINT_AUTHORITY'),
                                      (46, (1).to_bytes(4, 'little'), 'ACTIVE_FREEZE_AUTHORITY'),
                                      (36, bytes(8), 'ZERO_SUPPLY')):
            self.setUp()
            self.world.patch_bytes(self.world.mint, offset, value)
            self.assertRejected(self.screen(), reason)

    def test_unknown_or_token2022_mint_program_without_valid_profile(self):
        self.world.accounts[self.world.mint]['owner'] = 'Unknown1111111111111111111111111111111111111'
        self.assertRejected(self.screen(), 'UNKNOWN_TOKEN_PROGRAM')
        self.setUp()
        self.world.accounts[self.world.mint]['owner'] = TOKEN_2022      # 82-byte body is not a valid profile-2 mint
        self.assertRejected(self.screen(), 'TOKEN2022_PROFILE_REJECTED')

    def test_missing_accounts(self):
        for key, reason in ((self.world.mint, 'MINT_ACCOUNT_MISSING'), (self.world.pool, 'POOL_ACCOUNT_MISSING')):
            self.setUp()
            del self.world.accounts[key]
            self.assertRejected(self.screen(), reason)
        for key, reason in ((self.world.base_vault, 'VAULT_ACCOUNT_MISSING'), (self.world.lp, 'LP_MINT_UNAVAILABLE')):
            self.setUp()
            del self.world.accounts[key]
            self.assertRejected(self.screen(), reason)

    def test_pool_not_owned_by_pumpswap_or_truncated_is_malformed_not_an_exception(self):
        self.world.accounts[self.world.pool]['owner'] = TOKEN_PROGRAM
        self.assertRejected(self.screen(), 'POOL_EVIDENCE_MALFORMED')
        self.setUp()
        self.world.accounts[self.world.pool]['data'][0] = base64.b64encode(b'\x00' * 40).decode()
        self.assertRejected(self.screen(), 'POOL_EVIDENCE_MALFORMED')

    def test_pool_for_a_different_mint_or_bump_is_rejected(self):
        self.world.patch_bytes(self.world.pool, 8, bytes([1]))          # pool_bump
        self.assertRejected(self.screen(), 'POOL_PDA_MISMATCH')

    def test_lp_supply_authority_and_freeze(self):
        self.world.accounts[self.world.lp] = self.world.lp_account(5)
        self.assertRejected(self.screen(), 'OUTSTANDING_WITHDRAWABLE_LP_SUPPLY')
        self.setUp()
        self.world.accounts[self.world.lp] = self.world.lp_account(0, authority=self.world.t.mint)
        self.assertRejected(self.screen(), 'UNEXPECTED_LP_MINT_AUTHORITY')
        self.setUp()
        self.world.patch_bytes(self.world.lp, 46, (1).to_bytes(4, 'little'))
        self.assertRejected(self.screen(), 'LP_FREEZE_AUTHORITY')

    def test_vault_delegate_frozen_wrong_owner_and_wrong_program(self):
        t = self.world.t
        self.world.accounts[self.world.base_vault] = self.world.vault(t.mint, 10**13, delegate=True)
        self.assertRejected(self.screen(), 'VAULT_TOKEN_ACCOUNT_DELEGATE')
        self.setUp()
        self.world.accounts[self.world.base_vault] = self.world.vault(t.mint, 10**13, frozen=True)
        self.assertRejected(self.screen(), 'VAULT_FROZEN_OR_UNINITIALIZED_HOLDING')
        self.setUp()
        self.world.patch_bytes(self.world.base_vault, 32, bytes(32))     # not owned by the pool
        self.assertRejected(self.screen(), 'VAULT_HOLDING_IDENTITY_MISMATCH')
        self.setUp()
        self.world.accounts[self.world.quote_vault]['owner'] = 'Unknown1111111111111111111111111111111111111'
        self.assertRejected(self.screen(), 'UNSUPPORTED_VAULT_PROGRAM')

    def test_vault_at_a_non_ata_address_is_rejected(self):
        """A pool pointing at an attacker-made, identity-consistent token account that is not the pool's ATA."""
        fake = base58(bytes([77]) * 32)
        self.world.patch_bytes(self.world.pool, 139, bytes([77]) * 32)          # pool_base_token_account
        self.world.accounts[fake] = self.world.accounts[self.world.base_vault]
        self.assertRejected(self.screen(), 'VAULT_ATA_MISMATCH')

    def test_market_cap_and_liquidity_bounds(self):
        self.world = World(base_raw=10**13, quote_lamports=500 * 10**9)         # $5M cap
        self.providers = Providers(self.world)
        self.assertRejected(self.screen(), 'MARKET_CAP_ABOVE_MAX')
        self.world = World(base_raw=10**13, quote_lamports=5 * 10**9)           # $50k cap, $1k liquidity
        self.providers = Providers(self.world)
        result = self.screen()
        self.assertRejected(result, 'LIQUIDITY_BELOW_MIN')
        self.assertRejected(self.screen(cfg={'min_market_cap_usd': 60_000, 'min_liquidity_usd': 1}), 'MARKET_CAP_BELOW_MIN')

    def test_snapshot_slot_drift_rejected(self):
        def mutate(keys, result):
            if len(keys) == 3:
                result['context']['slot'] += 1000
            return result
        self.providers.helius.mutate = mutate
        self.assertRejected(self.screen(), 'SNAPSHOT_SLOT_DRIFT')

    def extended_pool(self):
        """Documented 300-byte PumpSwap pool: appended fields at fixed offsets (verified against desk.pools.parse_pool)."""
        raw = bytes(self.world.t.raw) + bytes(300 - len(self.world.t.raw))
        self.world.accounts[self.world.pool] = self.world.t.account(raw, self.world.t.owner)

    def test_extended_zero_pool_passes(self):
        self.extended_pool()
        self.assertTrue(self.screen().passed)

    def test_accrued_fees_reduce_physical_liquidity_but_not_pricing(self):
        self.extended_pool()
        self.world.patch_bytes(self.world.pool, 271, (11 * 10**9).to_bytes(8, 'little'))     # protocol fees
        result = self.screen()
        self.assertRejected(result, 'LIQUIDITY_BELOW_MIN')                                  # 39 SOL physical -> $7.8k
        self.assertEqual(result.features['quote_spendable_raw'], str(39 * 10**9))
        self.assertEqual(float(result.features['market_cap_usd']), 500_000.0)
        self.assertTrue(result.features['boosted'])
        self.world.patch_bytes(self.world.pool, 271, (10 * 10**9).to_bytes(8, 'little'))     # exactly $8k: allowed
        self.assertTrue(self.screen().passed)
        self.world.patch_bytes(self.world.pool, 271, (60 * 10**9).to_bytes(8, 'little'))     # fees exceed the vault
        self.assertRejected(self.screen(), 'POOL_RESERVES_INVALID')

    def test_virtual_reserves_price_the_pool_but_never_count_as_liquidity(self):
        self.extended_pool()
        self.world.patch_bytes(self.world.pool, 245, (50 * 10**9).to_bytes(8, 'little'))
        result = self.screen()
        self.assertTrue(result.passed, result.reasons)
        self.assertEqual(float(result.features['market_cap_usd']), 1_000_000.0)
        self.assertEqual(float(result.features['liquidity_usd']), 10_000.0)
        self.assertTrue(result.features['boosted'])

    def test_pool_flags_and_unknown_layout_are_rejected(self):
        cases = ((243, b'\x01', 'MAYHEM_POOL'), (244, b'\x01', 'CASHBACK_POOL'), (270, b'\x01', 'HOLDER_REWARD_POOL'),
                 (269, b'\x01', 'MUTABLE_CREATOR_FEE'), (261, (25).to_bytes(8, 'little'), 'POOL_CREATOR_FEE_OVERRIDE'),
                 (290, b'\x01', 'POOL_LAYOUT_HAS_UNKNOWN_EXTENSION'))
        for offset, value, reason in cases:
            self.setUp()
            self.extended_pool()
            self.world.patch_bytes(self.world.pool, offset, value)
            self.assertRejected(self.screen(), reason)


class ScreenRobustnessTests(ScreenBase):
    def test_malformed_provider_shapes_reject_without_raising(self):
        for bad in ({'value': 'x'}, {'context': {'slot': 1}, 'value': []}, {'context': {}, 'value': [None, None]}, [], None, 5):
            self.setUp()
            self.providers.helius.mutate = lambda keys, result, bad=bad: bad
            result = self.screen()
            self.assertFalse(result.passed)
            self.assertIn('MALFORMED_PROVIDER_RESPONSE', result.reasons)

    def test_invalid_base64_and_wrong_types_reject_without_raising(self):
        self.world.accounts[self.world.mint]['data'] = ['!!!not-base64!!!', 'base64']
        result = self.screen()
        self.assertFalse(result.passed)
        self.assertIsNone(result.error)
        self.setUp()
        self.world.accounts[self.world.mint] = 'a string, not an account'
        self.assertFalse(self.screen().passed)
        self.setUp()
        self.world.accounts[self.world.mint]['data'] = None
        self.assertFalse(self.screen().passed)

    def test_provider_errors_fail_the_candidate_with_typed_error_and_do_not_raise(self):
        self.providers.helius.fail = ProviderError('HTTP_429', True)
        result = self.screen()
        self.assertFalse(result.passed)
        self.assertEqual((result.error['code'], result.error['transient']), ('HTTP_429', True))
        self.assertEqual(result.error['stage'], 'accounts_mint_pool')
        self.setUp()
        self.providers.kraken.fail = ProviderError('MALFORMED_BODY', False)
        result = self.screen()
        self.assertEqual((result.error['code'], result.error['transient'], result.error['stage']), ('MALFORMED_BODY', False, 'sol_usd'))
        self.assertEqual([c[0] for c in self.providers.log], ['sol_usd'])      # no further spend after the failure

    def test_unexpected_exception_is_isolated_as_internal_error(self):
        self.providers.helius.fail = RuntimeError('boom')
        result = self.screen()
        self.assertFalse(result.passed)
        self.assertIn('SCREEN_INTERNAL_ERROR:RuntimeError', result.reasons)
        self.assertNotIn('boom', ' '.join(result.reasons))
        self.assertIsNone(result.error)

    def test_non_exception_base_errors_are_not_swallowed(self):
        self.providers.helius.fail = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.screen()

    def test_sol_usd_garbage_rejects(self):
        for bad in ('abc', '-1', '0', None, True, float('nan'), {'x': 1}):
            self.setUp()
            self.providers.kraken.price = bad
            result = self.screen()
            self.assertFalse(result.passed)
            self.assertIn('SOL_USD_INVALID', result.reasons)

    def test_sol_usd_dict_price_accepted(self):
        self.providers.kraken.price = {'price': '100'}
        self.assertTrue(self.screen().passed)

    def test_holders_concentrated_malformed_and_unavailable(self):
        self.world.holders = [{'address': 'h%d' % i, 'amount': str(10**14), 'decimals': 6} for i in range(10)]    # 100% of supply
        self.world.holders = [dict(h, address=base58(bytes([i + 1]) * 32)) for i, h in enumerate(self.world.holders)]
        self.assertRejected(self.screen(), 'HOLDER_CONCENTRATION_ABOVE_MAX')
        self.setUp()
        self.world.holders = [{'address': 'not-an-address', 'amount': '1'}]
        self.assertRejected(self.screen(), 'HOLDER_EVIDENCE_MALFORMED')
        self.setUp()
        self.world.holders = [{'address': base58(bytes([1]) * 32), 'amount': str(10**16)}]                        # more than supply
        self.assertRejected(self.screen(), 'HOLDER_EVIDENCE_MALFORMED')
        self.setUp()
        for failure in (ProviderError('TIMEOUT', True), ProviderError('HTTP_400', False)):
            self.setUp()
            self.providers.helius.holder_fail = failure
            result = self.screen()
            # L09: a holder-lookup failure is a typed transient reject (retried by the runner), never a pass
            self.assertFalse(result.passed)
            self.assertEqual(result.reasons, ('HOLDERS_UNAVAILABLE',))
            self.assertEqual(result.error, {'code': 'HOLDERS_UNAVAILABLE', 'transient': True, 'stage': 'holders',
                                            'provider_code': failure.code})
            self.assertEqual(result.features['holder_check'], 'UNAVAILABLE:' + failure.code)

    def test_pool_vault_excluded_from_holder_concentration(self):
        result = self.screen()          # the fake lists the pool vault with 80% of supply
        self.assertTrue(result.passed)
        self.assertLess(float(result.features['top10_pct_excluding_pool']), 1)

    def test_seeded_single_byte_corruption_never_raises_and_never_passes_silently(self):
        rng = random.Random(20261011)
        keys = [self.world.mint, self.world.pool, self.world.base_vault, self.world.quote_vault, self.world.lp]
        passed_after_mutation = 0
        for _ in range(300):
            self.setUp()
            key = rng.choice(keys)
            data = bytearray(base64.b64decode(self.world.accounts[key]['data'][0]))
            index = rng.randrange(len(data)); original = data[index]
            data[index] = (original + rng.randrange(1, 256)) % 256
            self.world.accounts[key]['data'][0] = base64.b64encode(bytes(data)).decode()
            result = self.screen()
            self.assertIsInstance(result, lc.Screen)
            self.assertIsInstance(result.reasons, tuple)
            if result.passed:
                passed_after_mutation += 1
                self.assertEqual(result.reasons, ())
                self.assertIn('market_cap_usd', result.features)
        # most single-byte flips land in checked identity/authority/amount fields; the rest are inert padding/amounts
        self.assertLess(passed_after_mutation, 300)

    def test_screen_is_idempotent_and_does_not_mutate_inputs(self):
        before = copy.deepcopy(self.world.accounts)
        first = self.screen(); second = self.screen()
        self.assertEqual(first.passed, second.passed)
        self.assertEqual(first.reasons, second.reasons)
        self.assertEqual(self.world.accounts, before)


class IsolationTests(unittest.TestCase):
    def test_module_imports_no_desk_gates_or_pacing(self):
        repo = str(Path(__file__).resolve().parents[2])
        code = ("import sys; sys.path.insert(0, %r)\n"
                "import lean.candidates\n"
                "bad=[m for m in sys.modules if m.startswith('desk.') and any(k in m for k in "
                "('pacing','terminal','reconcil','monitoring','paper_cycle','receipt','runtime','retirement',"
                "'successor','ledger','job_persistence','history_progress'))]\n"
                "print(bad)\n") % repo
        out = subprocess.check_output([sys.executable, '-I', '-c', code], text=True, cwd=repo)
        self.assertEqual(out.strip(), '[]')

    def test_module_has_no_write_path(self):
        source = Path(lc.__file__).read_text()
        for needle in ('INSERT ', 'DELETE ', 'executescript', 'mode=rw', 'PRAGMA journal_mode'):
            self.assertNotIn(needle, source)


if __name__ == '__main__':
    unittest.main()
