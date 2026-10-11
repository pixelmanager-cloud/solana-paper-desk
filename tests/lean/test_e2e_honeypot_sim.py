"""SYNTHETIC_TEST_ONLY: L22 end to end on tests/lean/fakeworld.py with the REAL lean modules (only HTTP is faked).

A  The 20-candidate scenario of test_e2e_real (all plain pump-graduated SPL mints) with the filter ON has IDENTICAL fills, entry
   decisions and provider calls to the same scenario with the filter OFF: every candidate is SKIPPED_STANDARD_SPL with no call.
B  A Token-2022 honeypot (a transfer hook rejecting the sell in simulation) is rejected as a hazard and never re-screened by the
   watchlist; a sellable Token-2022 mint and a plain SPL mint are entered; the exit loop never calls the filter.
"""
import base64
import hashlib
import json
import unittest

from desk.pools import ATA
from desk.security import TOKEN_2022
from lean import honeypot_sim as HP
from tests.lean import test_e2e_real as E
from tests.lean.fakeworld import DECIMALS, T0, Token, World, _pda


def setUpModule():
    E.setUpModule()


def _key(seed, on_curve=True):
    from solders.pubkey import Pubkey
    for i in range(1000):
        pk = Pubkey.from_bytes(hashlib.sha256(('%s/%d' % (seed, i)).encode()).digest())
        if pk.is_on_curve() is on_curve:
            return str(pk)
    raise AssertionError('no key')


HOOK = 'HookProgram11111111111111111111111111111111'


class Token22(Token):
    """A migrated PumpSwap pool whose base mint is a metadata-only Token-2022 mint (the screen's profile 2), with four real holders
    (on-curve owners) of Token-2022 token accounts. ``honeypot`` makes a simulated sell fail in a transfer hook."""

    def __init__(self, tag, *, honeypot=False, **kwargs):
        super().__init__(tag, **kwargs)
        from solders.pubkey import Pubkey
        old = self._vault_pks[0]
        new = _pda([bytes(self._pool_pk), bytes(Pubkey.from_string(TOKEN_2022)), bytes(self.mint_pk)], ATA)[0]
        self.pool_raw = self.pool_raw.replace(bytes(old), bytes(new))
        self.base_vault, self._vault_pks = str(new), [new, self._vault_pks[1]]
        self.honeypot = honeypot
        self.holder_owners = [_key('t%d-owner-%d' % (tag, i)) for i in range(4)]
        self.holder_accounts = [_key('t%d-ata-%d' % (tag, i), False) for i in range(4)]

    def _t22_account(self, owner_bytes, amount, mint_bytes):
        d = bytearray(165)
        d[:32] = mint_bytes; d[32:64] = owner_bytes; d[64:72] = int(amount).to_bytes(8, 'little'); d[108] = 1
        return self.account(bytes(d) + bytes([2]) + (7).to_bytes(2, 'little') + bytes(2), TOKEN_2022)

    def mint_account(self, t=None):
        mint = bytes(self.mint_pk)
        d = bytearray(self.SUPPLY_BYTES()) + bytes(83) + bytes([1])
        d += (18).to_bytes(2, 'little') + (64).to_bytes(2, 'little') + bytes(32) + mint
        metadata = bytes(32) + mint + b''.join(len(s).to_bytes(4, 'little') + s for s in (b'Hook', b'HK', b'')) + bytes(4)
        d += (19).to_bytes(2, 'little') + len(metadata).to_bytes(2, 'little') + metadata
        return self.account(bytes(d), TOKEN_2022)

    @staticmethod
    def SUPPLY_BYTES():
        from tests.lean.fakeworld import SUPPLY
        d = bytearray(82)
        d[36:44] = SUPPLY.to_bytes(8, 'little'); d[44] = DECIMALS; d[45] = 1
        return bytes(d)

    def vault_account(self, which, amount):
        if which != 'base':
            return super().vault_account(which, amount)
        return self._t22_account(bytes(self._pool_pk), amount, bytes(self.mint_pk))

    def holders(self):
        from decimal import Decimal
        rows = [{'address': self.base_vault, 'amount': str(self.base_raw)}]
        rows += [{'address': a, 'amount': str(10 ** 13)} for a in self.holder_accounts]
        return [dict(r, decimals=DECIMALS, uiAmountString=str(Decimal(r['amount']) / 10 ** DECIMALS)) for r in rows]

    def accounts(self, t):
        from solders.pubkey import Pubkey
        out = super().accounts(t)
        for owner, address in zip(self.holder_owners, self.holder_accounts):
            out[address] = self._t22_account(bytes(Pubkey.from_string(owner)), 10 ** 13, bytes(self.mint_pk))
        return out


def simulator(world):
    """simulateTransaction for the fake world: the payer (account key 0) tells which token's holder is selling."""
    def simulate(params, t):
        from solders.transaction import VersionedTransaction
        tx = VersionedTransaction.from_bytes(base64.b64decode(params[0]))
        assert [bytes(s) for s in tx.signatures] == [bytes(64)], 'a signed transaction reached the simulator'
        assert params[1]['sigVerify'] is False and params[1]['replaceRecentBlockhash'] is True
        payer = str(tx.message.account_keys[0])
        token = next(tok for tok in world.tokens.values() if payer in getattr(tok, 'holder_owners', ()))
        world.simulated.append((token.mint, payer, t))
        if not token.honeypot:
            return {'context': {'slot': world.slot}, 'value': {'err': None, 'logs': ['Program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA invoke [1]',
                                                                                       'Program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA success'],
                                                              'unitsConsumed': 60000, 'accounts': None}}
        logs = ['Program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA invoke [1]', 'Program %s invoke [2]' % TOKEN_2022,
                'Program log: Instruction: TransferChecked', 'Program %s invoke [3]' % HOOK, 'Program log: sells are disabled',
                'Program %s failed: custom program error: 0x1770' % HOOK, 'Program %s failed: custom program error: 0x1770' % TOKEN_2022,
                'Program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA failed: custom program error: 0x1770']
        return {'context': {'slot': world.slot}, 'value': {'err': {'InstructionError': [0, {'Custom': 6000}]}, 'logs': logs,
                                                          'unitsConsumed': 20000, 'accounts': None}}
    return simulate


class PumpCandidatesUnchanged(E.EndToEndRealModulesTest):
    """A: identical fills, decisions and provider calls with the filter on, for plain pump candidates."""
    test_twenty_candidates_end_to_end = None

    def capture(self, r):
        fills = [{k: v for k, v in f.items() if k != 'quote_ref'} for f in r.store.rows('fills', limit=10000)]  # quote_ref = observation id
        decisions = [(d['kind'], d['action'], d['mint'], d['reasons']) for d in r.store.rows('decisions', limit=100000)]
        states = [(s['mint'], s['event'], s['open_fill_id'], s['fill_id']) for s in r.store.rows('position_state', limit=100000)]
        return fills, decisions, states, r.store.cash()

    def test_identical_fills_and_zero_extra_calls(self):
        r, _ = self.run_scenario()
        baseline, calls = self.capture(r), list(self.world.calls)
        r.store.close()
        self.setUp()                                                       # a fresh world, the same scenario, the filter ON
        self.cfg = dict(self.cfg, honeypot_sim=HP.config({'enabled': True}))
        r, _ = self.run_scenario()
        self.assertIsNotNone(r.honeypot)
        self.assertEqual(self.capture(r), baseline)
        self.assertEqual(self.world.calls, calls)
        self.assertFalse([c for c in self.world.calls if c[1] == 'simulateTransaction'])
        rows = r.store.rows('observations', kind=HP.KIND, limit=10000)
        self.assertTrue(rows)
        self.assertEqual({json.loads(o['meta'])['outcome'] for o in rows}, {HP.SKIPPED})
        self.assertEqual({json.loads(o['raw'])['calls'] for o in rows}, {0})
        self.assertTrue(baseline[0])                                       # the scenario did trade


class TokenTwentyTwoHoneypot(unittest.TestCase):
    """B: a Token-2022 honeypot is rejected; a sellable Token-2022 mint and a plain SPL mint are entered."""

    def setUp(self):
        import os
        import tempfile
        from pathlib import Path
        from lean.__main__ import load_config
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.clock = E.FakeTime(T0)
        self.honeypot, self.good, self.plain = Token22(61, honeypot=True), Token22(63), Token(65)
        self.world = World(self.root, [self.honeypot, self.good, self.plain], self.clock)
        self.world.add_frames([T0, T0 + 60, T0 + 120])
        self.world.simulated = []
        self.world.extra_rpc['simulateTransaction'] = simulator(self.world)
        cfg = load_config(E.ROOT / 'tests' / 'lean' / 'lean.baseline.json')
        self.cfg = dict(cfg, honeypot_sim=HP.config({'enabled': True}), watchlist={'enabled': True})

    def runner(self):
        return E.build_runner(self.cfg, state_dir=self.root / 'state', discovery_db=self.world.discovery_db, keys=E.KEYS,
                              code_version='e2e-test', clock=self.clock.time,
                              transport_kwargs={'opener': self.world.opener, 'clock': self.clock.time,
                                                'monotonic': self.clock.monotonic, 'sleep': self.clock.sleep, 'rng': lambda: 0.5})

    def test_honeypot_rejected_others_entered_exits_untouched(self):
        r = self.runner()
        for _ in range(12):
            r.position_pass()
            r.candidate_pass()
            self.assertIsNone(r.halted)
            self.clock.advance(60)
        store = r.store
        bought = {f['mint'] for f in store.rows('fills', limit=1000) if f['side'] == 'buy'}
        self.assertEqual(bought, {self.good.mint, self.plain.mint})
        obs = {o['mint']: json.loads(o['raw']) for o in store.rows('observations', kind=HP.KIND, limit=100)}
        self.assertEqual({m: o['outcome'] for m, o in obs.items()},
                         {self.honeypot.mint: HP.BLOCKED, self.good.mint: HP.OK, self.plain.mint: HP.SKIPPED})
        self.assertTrue(obs[self.honeypot.mint]['why'].startswith('TRANSFER_HOOK_REJECTED:' + HOOK))
        self.assertIn('Program log: sells are disabled', obs[self.honeypot.mint]['attempts'][0]['logs_excerpt'])
        entry = [(d['action'], json.loads(d['reasons'])) for d in store.rows('decisions', mint=self.honeypot.mint, limit=100) if d['kind'] == 'entry']
        self.assertEqual(entry, [('SKIP', [HP.REJECT_BLOCKED])])
        # the honeypot is a HAZARD: never put on the watchlist, never re-screened
        watched = [json.loads(e['payload']).get('mint') for e in store.rows('events', kind='watch_add', limit=100)]
        self.assertNotIn(self.honeypot.mint, watched)
        self.assertEqual(len([d for d in store.rows('decisions', mint=self.honeypot.mint, limit=100) if d['kind'] == 'screen']), 1)
        # calls: the honeypot blocked on the small sell (1 simulation), the good mint small + full size (2), the SPL mint none
        sims = [s[0] for s in self.world.simulated]
        self.assertEqual((sims.count(self.honeypot.mint), sims.count(self.good.mint), sims.count(self.plain.mint)), (1, 2, 0))
        payers = {s[1] for s in self.world.simulated}
        self.assertTrue(payers <= set(self.honeypot.holder_owners) | set(self.good.holder_owners))
        self.assertTrue(store.check_invariants())
        # the exit loop never reaches the filter
        calls = []
        r.honeypot.reject = lambda *a, **k: calls.append(a) or True
        before = len(self.world.simulated)
        for _ in range(60):
            r.position_pass()
            self.clock.advance(60)
        self.assertEqual(calls, [])
        self.assertEqual(len(self.world.simulated), before)
        self.assertTrue(store.rows('fills', limit=1000))
        self.assertTrue([f for f in store.rows('fills', limit=1000) if f['side'] == 'sell'])     # exits still run
        self.assertIsNone(r.halted)
        health = r.health()['honeypot_sim']
        self.assertEqual((health['blocked'], health['ok'], health['skipped'], health['rejected']), (1, 1, 1, 1))


if __name__ == '__main__':
    unittest.main()
