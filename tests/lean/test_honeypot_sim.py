"""SYNTHETIC_TEST_ONLY: lean.honeypot_sim (L22) behind a fake HTTP layer. The REAL lean.providers Helius / Jupiter clients parse
these bytes. The Jupiter sell build is the repo's captured mainnet ``/swap/v2/build`` response
(fixtures/mainnet-roundtrip-simulation.json, quote_responses[1], provenance UNSIGNED_UNSUBMITTED) re-keyed to the requested taker
and amount; the successful simulation logs are the captured fixtures/mainnet-sell-simulation.json. Failure logs are synthetic in
the shape of agave's program log collector. No network, no credentials; nothing is signed or sent.
"""
import base64
import hashlib
import json
import socket
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from lean import honeypot_sim as HP
from lean import providers as P
from lean.candidates import Screen
from lean.store import Store

ROOT = Path(__file__).resolve().parents[2]
SELL_BUILD = json.loads((ROOT / 'fixtures' / 'mainnet-roundtrip-simulation.json').read_text())['quote_responses'][1]
SIM_OK = json.loads((ROOT / 'fixtures' / 'mainnet-sell-simulation.json').read_text())['simulation']
FIXTURE_TAKER = 'FzULv8pR9Rd7cyVKjVkzmJ1eqEmgwDnzjYyNUcEJtoG9'
MINT = SELL_BUILD['inputMint']
SOL = P.SOL_MINT
SWAP_INDEX = len(SELL_BUILD['computeBudgetInstructions']) + len(SELL_BUILD['setupInstructions'])      # 2 in the fixture
JUP = 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4'
HOOK = 'HookProgram11111111111111111111111111111111'
POOL = 'Poo1111111111111111111111111111111111111111'


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def key(seed, on_curve=True):
    from solders.pubkey import Pubkey
    for i in range(1000):
        pk = Pubkey.from_bytes(hashlib.sha256(('%s/%d' % (seed, i)).encode()).digest())
        if pk.is_on_curve() is on_curve:
            return str(pk)
    raise AssertionError('no key')


VAULT = key('pool-vault', False)


# ------------------------------------------------------------------------------------------------- account bytes
def legacy_mint(*, mint_authority=False, freeze_authority=False):
    d = bytearray(82)
    d[36:44] = (10 ** 15).to_bytes(8, 'little'); d[44] = 6; d[45] = 1
    if mint_authority:
        d[0:4] = (1).to_bytes(4, 'little')
    if freeze_authority:
        d[46:50] = (1).to_bytes(4, 'little')
    return bytes(d)


def t22_mint(*extensions):
    d = bytearray(legacy_mint()) + bytes(83) + bytes([1])
    for tag, body in extensions:
        d += tag.to_bytes(2, 'little') + len(body).to_bytes(2, 'little') + body
    return bytes(d)


def fee_ext(bps):
    body = bytearray(108)
    body[88:90] = bps.to_bytes(2, 'little'); body[106:108] = bps.to_bytes(2, 'little')
    return (1, bytes(body))


HOOK_EXT = (14, bytes(64))
METADATA_POINTER = (18, bytes(64))


def b58(pubkey):
    from solders.pubkey import Pubkey
    return bytes(Pubkey.from_string(pubkey))


def token_account(owner, amount, *, program=HP.TOKEN_PROGRAM, mint=MINT, frozen=False):
    d = bytearray(165)
    d[:32] = b58(mint); d[32:64] = b58(owner); d[64:72] = int(amount).to_bytes(8, 'little'); d[108] = 2 if frozen else 1
    return {'data': [base64.b64encode(bytes(d)).decode(), 'base64'], 'executable': False, 'lamports': 2039280, 'owner': program}


def rpc_body(result):
    return json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result}).encode()


def screen(mint_data, program, *, holders=None, keep_holders=True, pool_account=None, token_program=None):
    mint_account = {'data': [base64.b64encode(mint_data).decode(), 'base64'], 'executable': False, 'lamports': 1, 'owner': program}
    raw = [('accounts_mint_pool', rpc_body({'context': {'slot': 1}, 'value': [mint_account, pool_account]}))]
    if keep_holders and holders is not None:
        raw.append(('holders', rpc_body({'context': {'slot': 1}, 'value': holders})))
    features = {'mint': MINT, 'pool': POOL, 'token_program': token_program or program, 'decimals': 6,
                'pool_base_token_account': VAULT, 'base_reserve_raw': str(2 * 10 ** 14), 'quote_effective_raw': str(80 * 10 ** 9)}
    return Screen(True, (), features, (), tuple(raw))


GATE = SimpleNamespace(size_sol=Decimal('0.1'))
FULL = 2 * 10 ** 14 * 10 ** 8 // (80 * 10 ** 9 + 10 ** 8)         # our paper size in raw tokens (no fee)


# ------------------------------------------------------------------------------------------------- the fake HTTP layer
class Resp:
    def __init__(self, body, status=200):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status, self.headers = status, {}

    def read(self, n=-1):
        return self.body if n is None or n < 0 else self.body[:n]

    def close(self):
        pass


class Net:
    """Helius JSON-RPC + Jupiter swap/v2/build. ``sims`` is the queue of simulateTransaction ``value`` objects (the last repeats);
    ``build`` is 'ok' | 'no_route' | 'zero_out' | 'http_503' | a callable(query) -> Resp."""

    def __init__(self, *, accounts=None, largest=None, sims=None, build='ok'):
        self.accounts, self.largest = dict(accounts or {}), largest or []
        self.sims, self.build = list(sims or [dict(err=None, logs=SIM_OK['logs'], unitsConsumed=SIM_OK['unitsConsumed'])]), build
        self.calls, self.simulated, self.requested = [], [], []

    def opener(self, request, timeout):
        url = urlsplit(request.full_url)
        if url.hostname == 'api.jup.ag':
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            self.calls.append(('jupiter', 'build', q['taker'], int(q['amount'])))
            return self._build(q)
        body = json.loads(request.data)
        method, params = body['method'], body['params']
        self.calls.append(('helius', method))
        if method == 'getTokenLargestAccounts':
            result = {'context': {'slot': 5}, 'value': self.largest}
        elif method == 'getMultipleAccounts':
            self.requested.append(list(params[0]))
            result = {'context': {'slot': 5}, 'value': [self.accounts.get(k) for k in params[0]]}
        elif method == 'simulateTransaction':
            self.simulated.append(self._inspect(params))
            value = self.sims.pop(0) if len(self.sims) > 1 else self.sims[0]
            if isinstance(value, Resp):
                return value
            result = {'context': {'slot': 6}, 'value': {'accounts': None, 'returnData': None, **value}}
        else:
            raise AssertionError('unexpected RPC ' + method)
        return Resp({'jsonrpc': '2.0', 'id': body['id'], 'result': result})

    @staticmethod
    def _inspect(params):
        from solders.transaction import VersionedTransaction
        tx = VersionedTransaction.from_bytes(base64.b64decode(params[0]))
        return {'options': params[1], 'signatures': [bytes(s) for s in tx.signatures], 'payer': str(tx.message.account_keys[0]),
                'size': len(base64.b64decode(params[0]))}

    def _build(self, q):
        if callable(self.build):
            return self.build(q)
        if self.build == 'no_route':
            return Resp(b'{"error":"Could not find any route","errorCode":"COULD_NOT_FIND_ANY_ROUTE"}', 400)
        if self.build == 'http_503':
            return Resp(b'{"error":"unavailable"}', 503)
        text = json.dumps(SELL_BUILD).replace(FIXTURE_TAKER, q['taker'])
        body = json.loads(text)
        body['inAmount'] = q['amount']
        if self.build == 'zero_out':                    # a 100 % transfer fee: the seller would receive nothing
            body['outAmount'] = body['otherAmountThreshold'] = '0'
        return Resp(body)


def providers(net):
    def transport(name):
        return P.Transport(name, opener=net.opener, clock=lambda: 1000.0, monotonic=lambda: 1000.0, sleep=lambda s: None,
                           rng=lambda: 0.5, limiter=P.RateLimiter(1000, 1000, clock=lambda: 1000.0, sleep=lambda s: None))
    return P.Providers(P.Helius('TEST-HELIUS-KEY', transport('helius')), P.Jupiter('TEST-JUPITER-KEY', transport('jupiter')), None)


# A transfer hook (a program invoked BY Token-2022) rejecting the outbound transfer of the swap.
HOOK_LOGS = ['Program ComputeBudget111111111111111111111111111111 invoke [1]', 'Program ComputeBudget111111111111111111111111111111 success',
             'Program ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL invoke [1]', 'Program ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL success',
             'Program %s invoke [1]' % JUP, 'Program log: Instruction: SharedAccountsRouteV2',
             'Program %s invoke [2]' % HP.TOKEN_2022, 'Program log: Instruction: TransferChecked',
             'Program %s invoke [3]' % HOOK, 'Program log: AnchorError: transfer blocked: seller not whitelisted',
             'Program %s consumed 4000 of 500000 compute units' % HOOK, 'Program %s failed: custom program error: 0x1770' % HOOK,
             'Program %s consumed 9000 of 510000 compute units' % HP.TOKEN_2022,
             'Program %s failed: custom program error: 0x1770' % HP.TOKEN_2022,
             'Program %s consumed 20000 of 560000 compute units' % JUP, 'Program %s failed: custom program error: 0x1770' % JUP]
HOOK_SIM = dict(err={'InstructionError': [SWAP_INDEX, {'Custom': 6000}]}, logs=HOOK_LOGS, unitsConsumed=33000)
FROZEN_LOGS = ['Program %s invoke [1]' % JUP, 'Program log: Instruction: SharedAccountsRouteV2',
               'Program %s invoke [2]' % HP.TOKEN_PROGRAM, 'Program log: Instruction: Transfer', 'Program log: Error: Account is frozen',
               'Program %s consumed 1300 of 500000 compute units' % HP.TOKEN_PROGRAM,
               'Program %s failed: custom program error: 0x11' % HP.TOKEN_PROGRAM,
               'Program %s failed: custom program error: 0x11' % JUP]
FROZEN_SIM = dict(err={'InstructionError': [SWAP_INDEX, {'Custom': 17}]}, logs=FROZEN_LOGS, unitsConsumed=9000)
SLIPPAGE_SIM = dict(err={'InstructionError': [SWAP_INDEX, {'Custom': 6001}]}, unitsConsumed=90000,
                    logs=['Program %s invoke [1]' % JUP, 'Program log: Error: SlippageToleranceExceeded',
                          'Program %s failed: custom program error: 0x1771' % JUP])
FEE_SIM = dict(err='InsufficientFundsForFee', logs=[], unitsConsumed=0)
OK_SIM = dict(err=None, logs=SIM_OK['logs'], unitsConsumed=SIM_OK['unitsConsumed'])


class Base(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [5000.0]
        self.store = Store(str(Path(self.tmp.name) / 'lean.sqlite'), initial_cash_sol='1', code_version='t', strategy_version='s',
                           clock=lambda: self.now[0])
        self.owners = [key('holder-%d' % i) for i in range(4)]
        self.accounts = [key('holder-ata-%d' % i, False) for i in range(4)]

    def world(self, *, balances=(10 ** 13, 5 * 10 ** 12, 10 ** 12, 10 ** 11), program=HP.TOKEN_PROGRAM, **net):
        largest = [{'address': VAULT, 'amount': str(2 * 10 ** 14)}]
        accounts = {}
        for owner, address, amount in zip(self.owners, self.accounts, balances):
            largest.append({'address': address, 'amount': str(amount)})
            accounts[address] = token_account(owner, amount, program=program)
        net.setdefault('largest', largest)
        return Net(accounts={**accounts, **net.pop('accounts', {})}, **net), largest

    def sim(self, cfg=None):
        return HP.HoneypotSim(HP.config({'enabled': True, **(cfg or {})}), self.store, clock=lambda: self.now[0])

    def run_one(self, net, scr, *, cfg=None, source=None, extra_sources=None, sim=None):
        sim = sim or self.sim(cfg)
        notes = []
        runner = SimpleNamespace(providers=providers(net), store=self.store, _source_of={MINT: source} if source else {},
                                 extra_sources=extra_sources, _l07r_note=lambda **k: notes.append(k))
        cid = self.store.add_candidate(MINT, pool=POOL, signature='s' * 88, slot=1, migrated_at=1.0, hint_seq=1)
        rejected = sim.reject(runner, SimpleNamespace(mint=MINT), cid, scr, GATE)
        rows = self.store.rows('observations', kind=HP.KIND, limit=100)
        record = json.loads(rows[-1]['raw']) if rows else None
        return rejected, record, notes

    def entry_decisions(self):
        return [(r['action'], json.loads(r['reasons'])) for r in self.store.rows('decisions', kind='entry', limit=100)]


# ------------------------------------------------------------------------------------------------- the outcomes
class Outcomes(Base):
    def test_ok_small_then_full_size_by_a_real_holder(self):
        net, largest = self.world()
        rejected, record, _ = self.run_one(net, screen(t22_mint(METADATA_POINTER), HP.TOKEN_2022, holders=largest))
        self.assertFalse(rejected)
        self.assertEqual((record['outcome'], record['decision'], record['reject_reason']), (HP.OK, 'CONTINUE', None))
        self.assertEqual([(a['size'], a['outcome'], a['holder']) for a in record['attempts']],
                         [('small', HP.OK, self.owners[0]), ('full', HP.OK, self.owners[0])])
        self.assertEqual([a['amount'] for a in record['attempts']], [FULL // 10, FULL])
        # the holder rows came from the screen: ONE getMultipleAccounts, then build + simulate per size
        self.assertEqual([c[:2] for c in net.calls], [('helius', 'getMultipleAccounts'), ('jupiter', 'build'), ('helius', 'simulateTransaction'),
                                                      ('jupiter', 'build'), ('helius', 'simulateTransaction')])
        self.assertEqual(record['calls'], 5)
        for request in net.simulated:                 # unsigned, never broadcast-shaped, payer = the real holder
            self.assertEqual(request['signatures'], [bytes(64)])
            self.assertEqual(request['payer'], self.owners[0])
            self.assertEqual((request['options']['sigVerify'], request['options']['replaceRecentBlockhash'], request['options']['encoding']),
                             (False, True, 'base64'))
        self.assertEqual(self.entry_decisions(), [])
        row = self.store.rows('observations', kind=HP.KIND, limit=10)[0]
        meta = json.loads(row['meta'])
        self.assertEqual((meta['outcome'], meta['decision_ts'], row['ts']), (HP.OK, 5000.0, 5000.0))
        self.assertEqual((record['signed'], record['sent'], record['decision_ts']), (False, False, 5000.0))

    def test_transfer_hook_rejecting_the_sell_is_blocked_with_logs(self):
        net, largest = self.world(sims=[HOOK_SIM], program=HP.TOKEN_2022)
        rejected, record, notes = self.run_one(net, screen(t22_mint(METADATA_POINTER, HOOK_EXT), HP.TOKEN_2022, holders=largest))
        self.assertTrue(rejected)
        self.assertEqual((record['outcome'], record['reject_reason']), (HP.BLOCKED, HP.REJECT_BLOCKED))
        self.assertTrue(record['why'].startswith('TRANSFER_HOOK_REJECTED:' + HOOK), record['why'])
        self.assertIn('Program %s failed: custom program error: 0x1770' % HOOK, record['attempts'][0]['logs_excerpt'])
        self.assertIn('Program log: AnchorError: transfer blocked: seller not whitelisted', record['attempts'][0]['logs_excerpt'])
        self.assertEqual(len(record['attempts']), 1)                  # blocked on the small sell: no full-size attempt
        self.assertEqual(self.entry_decisions(), [('SKIP', [HP.REJECT_BLOCKED])])
        self.assertEqual(notes, [{'entry_reasons': [HP.REJECT_BLOCKED]}])

    def test_frozen_account_is_blocked(self):
        net, largest = self.world(sims=[FROZEN_SIM])
        rejected, record, _ = self.run_one(net, screen(legacy_mint(), HP.TOKEN_PROGRAM, holders=largest), source='raydium_new')
        self.assertTrue(rejected)
        self.assertEqual((record['outcome'], record['why']), (HP.BLOCKED, 'ACCOUNT_FROZEN'))

    def test_hundred_percent_transfer_fee_build_gives_nothing_and_rejects(self):
        net, largest = self.world(build='zero_out', program=HP.TOKEN_2022)
        rejected, record, _ = self.run_one(net, screen(t22_mint(METADATA_POINTER, fee_ext(10000)), HP.TOKEN_2022, holders=largest))
        self.assertTrue(rejected)
        self.assertEqual((record['outcome'], record['why'], record['reject_reason']),
                         (HP.NO_BUILD, 'BUILD_FAILED:QUOTE_INVALID', HP.REJECT_RISKY))
        self.assertEqual(record['scope']['transfer_fee_bps'], 10000)
        self.assertEqual(net.simulated, [])
        self.assertEqual(self.entry_decisions(), [('SKIP', [HP.REJECT_RISKY])])

    def test_hundred_percent_fee_sell_that_fails_in_the_swap_rejects_too(self):
        net, largest = self.world(sims=[SLIPPAGE_SIM], program=HP.TOKEN_2022)
        rejected, record, _ = self.run_one(net, screen(t22_mint(fee_ext(10000)), HP.TOKEN_2022, holders=largest))
        self.assertTrue(rejected)
        self.assertEqual((record['outcome'], record['reject_reason']), (HP.INCONCLUSIVE, HP.REJECT_RISKY))
        self.assertTrue(record['why'].startswith('SWAP_FAILED:' + JUP))

    def test_an_unverified_sell_of_a_low_fee_token_continues(self):
        net, largest = self.world(sims=[SLIPPAGE_SIM], program=HP.TOKEN_2022)
        rejected, record, _ = self.run_one(net, screen(t22_mint(fee_ext(500)), HP.TOKEN_2022, holders=largest))
        self.assertFalse(rejected)
        self.assertEqual((record['outcome'], record['decision']), (HP.INCONCLUSIVE, 'CONTINUE'))
        self.assertFalse(record['scope']['risky'])
        net, largest = self.world(sims=[SLIPPAGE_SIM], program=HP.TOKEN_2022)
        self.assertTrue(self.run_one(net, screen(t22_mint(fee_ext(1001)), HP.TOKEN_2022, holders=largest))[0])   # > 10 %

    def test_insufficient_fee_retries_once_with_another_holder(self):
        net, largest = self.world(sims=[FEE_SIM, OK_SIM])
        rejected, record, _ = self.run_one(net, screen(t22_mint(), HP.TOKEN_2022, holders=largest))
        self.assertFalse(rejected)
        self.assertEqual(record['outcome'], HP.OK)
        self.assertEqual([(a['size'], a['outcome'], a['holder']) for a in record['attempts']],
                         [('small', 'RETRY', self.owners[0]), ('small', HP.OK, self.owners[1]), ('full', HP.OK, self.owners[1])])
        self.assertEqual([s['payer'] for s in net.simulated], [self.owners[0], self.owners[1], self.owners[1]])

    def test_retry_only_once_then_inconclusive(self):
        for err in ('InsufficientFundsForFee', 'BlockhashNotFound', 'AccountNotFound', {'InsufficientFundsForRent': {'account_index': 0}}):
            with self.subTest(err=err):
                self.setUp()
                net, largest = self.world(sims=[dict(err=err, logs=[])])
                rejected, record, _ = self.run_one(net, screen(t22_mint(HOOK_EXT), HP.TOKEN_2022, holders=largest))
                self.assertEqual(record['outcome'], HP.INCONCLUSIVE)
                self.assertEqual(len(net.simulated), 2)                     # one retry, never a third holder
                self.assertEqual(len({s['payer'] for s in net.simulated}), 2)
                self.assertTrue(rejected)                                   # a hook mint whose sell could not be verified

    def test_build_failure_is_no_build(self):
        net, largest = self.world(build='no_route')
        rejected, record, _ = self.run_one(net, screen(t22_mint(METADATA_POINTER), HP.TOKEN_2022, holders=largest))
        self.assertFalse(rejected)                                          # plain metadata-only Token-2022: recorded, continues
        self.assertEqual((record['outcome'], record['why']), (HP.NO_BUILD, 'BUILD_FAILED:COULD_NOT_FIND_ANY_ROUTE'))
        self.assertEqual(net.simulated, [])
        net, largest = self.world(build='no_route')
        self.assertTrue(self.run_one(net, screen(t22_mint(HOOK_EXT), HP.TOKEN_2022, holders=largest))[0])

    def test_transient_build_failure_is_inconclusive_not_no_build(self):
        net, largest = self.world(build='http_503')
        _rejected, record, _ = self.run_one(net, screen(t22_mint(), HP.TOKEN_2022, holders=largest))
        self.assertEqual((record['outcome'], record['why']), (HP.INCONCLUSIVE, 'BUILD_UNAVAILABLE:HTTP_503'))

    def test_taker_must_be_funded_answer_retries_with_another_holder(self):
        answers = [Resp(b'{"error":"taker has no SOL","errorCode":"INSUFFICIENT_FUNDS"}', 400), None]

        def build(q):
            item = answers.pop(0)
            return item if item is not None else Net(build='ok')._build(q)
        net, largest = self.world(build=build)
        _rejected, record, _ = self.run_one(net, screen(t22_mint(), HP.TOKEN_2022, holders=largest), cfg={'full_size': False})
        self.assertEqual(record['outcome'], HP.OK)
        self.assertEqual([a['outcome'] for a in record['attempts']], ['RETRY', HP.OK])

    def test_simulation_transport_and_shape_failures_are_inconclusive(self):
        for sims, why in (([Resp(b'{"error":"busy"}', 503)], 'SIM_UNAVAILABLE:HTTP_503'),
                          ([Resp({'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32602, 'message': 'bad'}})], 'SIM_UNAVAILABLE:RPC_ERROR'),
                          ([dict(logs=[])], 'SIM_RESPONSE_INVALID')):
            with self.subTest(why=why):
                self.setUp()
                net, largest = self.world(sims=sims)
                rejected, record, _ = self.run_one(net, screen(t22_mint(), HP.TOKEN_2022, holders=largest))
                self.assertFalse(rejected)
                self.assertEqual((record['outcome'], record['why']), (HP.INCONCLUSIVE, why))

    def test_size_dependent_honeypot_blocks_at_full_size(self):
        net, largest = self.world(sims=[OK_SIM, HOOK_SIM])
        rejected, record, _ = self.run_one(net, screen(t22_mint(), HP.TOKEN_2022, holders=largest))
        self.assertTrue(rejected)
        self.assertEqual(record['outcome'], HP.BLOCKED)
        self.assertTrue(record['why'].startswith('FULL_SIZE:TRANSFER_HOOK_REJECTED'))

    def test_full_size_skipped_when_the_holder_holds_less(self):
        net, largest = self.world(balances=(FULL - 1, 10, 10, 10))
        _rejected, record, _ = self.run_one(net, screen(t22_mint(), HP.TOKEN_2022, holders=largest))
        self.assertEqual(record['outcome'], HP.OK)
        self.assertEqual([a['outcome'] for a in record['attempts']], [HP.OK, 'SKIPPED_BALANCE'])
        self.assertEqual(len(net.simulated), 1)
        net, largest = self.world()
        _rejected, record, _ = self.run_one(net, screen(t22_mint(), HP.TOKEN_2022, holders=largest), cfg={'full_size': False})
        self.assertEqual([a['size'] for a in record['attempts']], ['small'])


# ------------------------------------------------------------------------------------------------- scope, holders, cap
class ScopeAndHolders(Base):
    def test_plain_pump_spl_with_revoked_authorities_is_skipped_with_zero_calls(self):
        net, largest = self.world()
        for source in (None, 'pump_graduation', 'pump_b'):
            with self.subTest(source=source):
                extra = SimpleNamespace(sources=[_pump_source('pump_b')])
                rejected, record, _ = self.run_one(net, screen(legacy_mint(), HP.TOKEN_PROGRAM, holders=largest), source=source,
                                                   extra_sources=extra)
                self.assertFalse(rejected)
                self.assertEqual((record['outcome'], record['calls'], record['simulated']), (HP.SKIPPED, 0, False))
        self.assertEqual(net.calls, [])
        self.assertEqual(self.entry_decisions(), [])

    def test_in_scope_cases(self):
        cases = {'token2022': (t22_mint(METADATA_POINTER), HP.TOKEN_2022, None),
                 'legacy with a live freeze authority': (legacy_mint(freeze_authority=True), HP.TOKEN_PROGRAM, None),
                 'legacy with a live mint authority': (legacy_mint(mint_authority=True), HP.TOKEN_PROGRAM, None),
                 'plain SPL from a non-pump source': (legacy_mint(), HP.TOKEN_PROGRAM, 'raydium_new'),
                 'unreadable mint bytes': (b'\x00' * 90, HP.TOKEN_PROGRAM, None)}
        for name, (data, program, source) in cases.items():
            with self.subTest(name):
                scoped = HP.scope(screen(data, program), program, source is None)
                self.assertTrue(scoped['in_scope'])
        self.assertTrue(HP.scope(screen(b'\x00' * 90, HP.TOKEN_PROGRAM), HP.TOKEN_PROGRAM, True)['risky'])     # fails closed
        self.assertFalse(HP.scope(screen(legacy_mint(), HP.TOKEN_PROGRAM), HP.TOKEN_PROGRAM, True)['in_scope'])
        self.assertTrue(HP.scope(screen(t22_mint(HOOK_EXT), HP.TOKEN_2022), HP.TOKEN_2022, True)['risky'])
        self.assertFalse(HP.scope(screen(t22_mint(fee_ext(1000)), HP.TOKEN_2022), HP.TOKEN_2022, True)['risky'])   # exactly 10 %
        self.assertTrue(HP.scope(screen(t22_mint(fee_ext(1001)), HP.TOKEN_2022), HP.TOKEN_2022, True)['risky'])

    def test_holder_choice_skips_vault_burn_creator_pda_and_frozen_is_kept(self):
        from lean.features import burn_accounts
        from tests.lean.fakeworld import Token
        token = Token(77, coin_creator_tag=12)                 # bytes([12]) * 32 is on the curve
        creator = token.coin_creator
        burn = sorted(burn_accounts(MINT, HP.TOKEN_PROGRAM))[0]
        pda_owner = key('some-pda', False)
        rows = [{'address': VAULT, 'amount': str(9 * 10 ** 14)}, {'address': burn, 'amount': str(8 * 10 ** 13)},
                {'address': 'CreatorAta11111111111111111111111111111111', 'amount': str(7 * 10 ** 13)},
                {'address': 'PdaAta1111111111111111111111111111111111111', 'amount': str(6 * 10 ** 13)},
                {'address': self.accounts[0], 'amount': str(10 ** 12)}]
        accounts = {'CreatorAta11111111111111111111111111111111': token_account(creator, 7 * 10 ** 13),
                    'PdaAta1111111111111111111111111111111111111': token_account(pda_owner, 6 * 10 ** 13),
                    self.accounts[0]: token_account(self.owners[0], 10 ** 12)}
        net = Net(accounts=accounts, largest=rows)
        pool = Token.account(token.pool_raw, 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA')
        scr = screen(t22_mint(), HP.TOKEN_2022, holders=rows, pool_account=pool, token_program=HP.TOKEN_PROGRAM)
        _rejected, record, _ = self.run_one(net, scr)
        self.assertEqual({a['holder'] for a in record['attempts'] if 'holder' in a}, {self.owners[0]})
        # ONE getMultipleAccounts, and the pool vault / burn account are never even asked for (largest first)
        self.assertEqual(net.requested, [['CreatorAta11111111111111111111111111111111', 'PdaAta1111111111111111111111111111111111111',
                                          self.accounts[0]]])
        # the frozen holder is NOT skipped: its failing sell is the evidence
        self.setUp()
        net, largest = self.world(sims=[FROZEN_SIM], accounts={})
        net.accounts[self.accounts[0]] = token_account(self.owners[0], 10 ** 13, frozen=True)
        self.assertTrue(self.run_one(net, screen(t22_mint(), HP.TOKEN_2022, holders=largest))[0])

    def test_holders_are_fetched_only_when_the_screen_did_not_keep_them(self):
        net, largest = self.world()
        _rejected, record, _ = self.run_one(net, screen(t22_mint(), HP.TOKEN_2022, holders=largest, keep_holders=False),
                                            cfg={'full_size': False})
        self.assertEqual([c[:2] for c in net.calls][:2], [('helius', 'getTokenLargestAccounts'), ('helius', 'getMultipleAccounts')])
        self.assertEqual(record['outcome'], HP.OK)

    def test_no_eligible_holder_is_inconclusive(self):
        net = Net(largest=[{'address': VAULT, 'amount': '5'}])
        rejected, record, _ = self.run_one(net, screen(t22_mint(HOOK_EXT), HP.TOKEN_2022, holders=net.largest))
        self.assertEqual((record['outcome'], record['why']), (HP.INCONCLUSIVE, 'NO_ELIGIBLE_HOLDER'))
        self.assertTrue(rejected)
        self.assertEqual(net.calls, [])

    def test_hourly_cap_is_hard_and_survives_a_restart(self):
        sim = self.sim({'max_per_hour': 2})
        for i in range(3):
            net, largest = self.world()
            _rejected, record, _ = self.run_one(net, screen(t22_mint(HOOK_EXT), HP.TOKEN_2022, holders=largest), sim=sim)
            self.now[0] += 1
        self.assertEqual((record['outcome'], record['why'], record['calls'], record['simulated']), (HP.INCONCLUSIVE, 'HOURLY_CAP', 0, False))
        self.assertEqual(net.calls, [])
        self.assertEqual(self.entry_decisions()[-1], ('SKIP', [HP.REJECT_RISKY]))       # a risky mint under the cap is not let through
        restarted = self.sim({'max_per_hour': 2})                                       # a new process on the same store
        net, largest = self.world()
        _rejected, record, _ = self.run_one(net, screen(t22_mint(), HP.TOKEN_2022, holders=largest), sim=restarted)
        self.assertEqual(record['why'], 'HOURLY_CAP')
        self.now[0] += 3600
        net, largest = self.world()
        _rejected, record, _ = self.run_one(net, screen(t22_mint(), HP.TOKEN_2022, holders=largest), sim=restarted)
        self.assertEqual(record['outcome'], HP.OK)

    def test_an_internal_error_is_inconclusive_never_a_crash(self):
        net, largest = self.world()
        with mock.patch.object(HP, 'compile_unsigned', side_effect=RuntimeError('boom')):
            _rejected, record, _ = self.run_one(net, screen(t22_mint(), HP.TOKEN_2022, holders=largest))
        self.assertEqual((record['outcome'], record['why']), (HP.INCONCLUSIVE, 'SIM_INTERNAL_ERROR:RuntimeError'))


def _pump_source(name):
    from lean.sources import PumpGraduationSource
    return PumpGraduationSource('/nonexistent.sqlite', name=name)


# ------------------------------------------------------------------------------------------------- pure mapping
class Classify(unittest.TestCase):
    def test_mapping(self):
        token_fail = lambda code, program=HP.TOKEN_PROGRAM: ['Program %s invoke [1]' % JUP, 'Program %s invoke [2]' % program,
                                                            'Program %s failed: custom program error: %s' % (program, hex(code)),
                                                            'Program %s failed: custom program error: %s' % (JUP, hex(code))]
        ix = lambda code: {'InstructionError': [3, {'Custom': code}]}
        cases = [((None, [], 3), HP.OK),
                 (('InsufficientFundsForFee', [], 3), HP.RETRY), (('BlockhashNotFound', [], 3), HP.RETRY),
                 (({'InsufficientFundsForRent': {'account_index': 0}}, [], 3), HP.RETRY),
                 ((ix(1), token_fail(1), 3), HP.RETRY),                                   # the holder's balance moved
                 ((ix(17), token_fail(17), 3), HP.BLOCKED),
                 ((ix(37), token_fail(37, HP.TOKEN_2022), 3), HP.BLOCKED),               # a non-benign Token-2022 code
                 ((ix(4), token_fail(4), 3), HP.INCONCLUSIVE),                            # owner mismatch: our plumbing
                 ((ix(6000), HOOK_LOGS, 3), HP.BLOCKED),
                 ((ix(17), token_fail(17), 1), HP.INCONCLUSIVE),                          # a setup instruction failed, not the swap
                 (({'InstructionError': [3, 'InvalidAccountData']}, token_fail(0)[:2], 3), HP.INCONCLUSIVE),
                 ((ix(6001), SLIPPAGE_SIM['logs'], 3), HP.INCONCLUSIVE),
                 (('ProgramAccountNotFound', [], 3), HP.INCONCLUSIVE),
                 ((ix(6000), FROZEN_LOGS[:5], 0), HP.BLOCKED)]                             # 'Account is frozen' in a program log
        for (err, logs, swap), expected in cases:
            with self.subTest(err=err, swap=swap):
                self.assertEqual(HP.classify(err, logs, swap)[0], expected)

    def test_decide(self):
        risky, plain = {'risky': True}, {'risky': False}
        self.assertEqual(HP.decide(HP.BLOCKED, plain), HP.REJECT_BLOCKED)
        self.assertEqual(HP.decide(HP.INCONCLUSIVE, risky), HP.REJECT_RISKY)
        self.assertEqual(HP.decide(HP.NO_BUILD, risky), HP.REJECT_RISKY)
        for outcome in (HP.OK, HP.INCONCLUSIVE, HP.NO_BUILD):
            self.assertIsNone(HP.decide(outcome, plain))
        self.assertIsNone(HP.decide(HP.OK, risky))

    def test_blocked_reasons_are_hazards_for_the_watchlist(self):
        from lean.watchlist import classify
        for reason in (HP.REJECT_BLOCKED, HP.REJECT_RISKY):
            self.assertEqual(classify('entry', 'SKIP', [reason]), 'HAZARD')

    def test_mint_facts(self):
        self.assertEqual(HP.mint_facts(t22_mint(fee_ext(10000), HOOK_EXT))['extensions'], [1, 14])
        self.assertEqual(HP.mint_facts(t22_mint(fee_ext(10000)))['transfer_fee_bps'], 10000)
        self.assertEqual(HP.mint_facts(legacy_mint())['extensions'], [])
        for bad in (b'', b'\x00' * 90, legacy_mint() + bytes(83) + bytes([2]), t22_mint((1, bytes(10))), t22_mint() + b'\x01\x00\xff\x00'):
            with self.assertRaises(ValueError):
                HP.mint_facts(bad)

    def test_compiled_transaction_is_unsigned_and_the_tip_is_left_out(self):
        from solders.transaction import VersionedTransaction
        build = dict(SELL_BUILD, tipInstruction={'programId': '11111111111111111111111111111111', 'data': 'AA==',
                                                 'accounts': [{'pubkey': FIXTURE_TAKER, 'isSigner': True, 'isWritable': True}]})
        raw, swap = HP.compile_unsigned(build, FIXTURE_TAKER)
        tx = VersionedTransaction.from_bytes(raw)
        self.assertEqual([bytes(s) for s in tx.signatures], [bytes(64)])
        self.assertEqual((str(tx.message.account_keys[0]), swap, len(tx.message.instructions)), (FIXTURE_TAKER, SWAP_INDEX, 4))
        self.assertLessEqual(len(raw), HP.MAX_TX_BYTES)
        other = key('someone-else')
        with self.assertRaisesRegex(ValueError, 'UNEXPECTED_SIGNER'):
            HP.compile_unsigned(SELL_BUILD, other)                   # the build's signer is not the payer we asked for
        with self.assertRaisesRegex(ValueError, 'NO_SWAP_INSTRUCTION'):
            HP.compile_unsigned({'routePlan': []}, FIXTURE_TAKER)


# ------------------------------------------------------------------------------------------------- the provider gate
class ProviderGate(unittest.TestCase):
    def tx(self, signature=bytes(64)):
        raw, _ = HP.compile_unsigned(SELL_BUILD, FIXTURE_TAKER)
        return base64.b64encode(raw[:1] + signature + raw[65:]).decode()

    def test_only_unsigned_simulation_reaches_the_wire(self):
        net = Net()
        helius = providers(net).helius
        good = {'encoding': 'base64', 'sigVerify': False, 'replaceRecentBlockhash': True, 'commitment': 'confirmed'}
        bad = [[self.tx(), dict(good, sigVerify=True)], [self.tx(), {k: v for k, v in good.items() if k != 'sigVerify'}],
               [self.tx(), dict(good, replaceRecentBlockhash=False)], [self.tx(), {k: v for k, v in good.items() if k != 'replaceRecentBlockhash'}],
               [self.tx(), dict(good, sigVerify=0)], [self.tx(), dict(good, encoding='base58')], [self.tx(), dict(good, skipPreflight=True)],
               [self.tx(b'\x01' * 64), good], [self.tx(bytes(63) + b'\x01'), good], ['not base64!', good], [self.tx()], [self.tx(), good, 1],
               [base64.b64encode(bytes(2000)).decode(), good], [base64.b64encode(b'\x00').decode(), good], [[], good], ['', good]]
        for params in bad:
            with self.subTest(params=str(params)[-80:]):
                with self.assertRaises(P.ProviderError) as cm:
                    helius.rpc('simulateTransaction', params)
                self.assertEqual(cm.exception.code, 'METHOD_NOT_ALLOWED')
        self.assertEqual(net.calls, [])
        result, _raw, _meta = helius.rpc('simulateTransaction', [self.tx(), good])
        self.assertIsNone(result['value']['err'])
        self.assertEqual(net.calls, [('helius', 'simulateTransaction')])
        for method in ('sendTransaction', 'sendRawTransaction', 'sendBundle', 'requestAirdrop'):
            with self.assertRaises(P.ProviderError):
                helius.rpc(method, [self.tx(), good])
        self.assertEqual(len(net.calls), 1)


# ------------------------------------------------------------------------------------------------- config
class Config(unittest.TestCase):
    def test_strict_and_off_by_default(self):
        self.assertEqual(HP.config(None), {'enabled': False, 'max_per_hour': 30, 'full_size': True})
        self.assertIsNone(HP.build({}, store=None))
        self.assertIsNone(HP.build(None, store=None))
        for bad in ([], {'enabled': 'yes'}, {'enable': True}, {'enabled': True, 'max_per_hour': 0}, {'max_per_hour': 1.5},
                    {'max_per_hour': True}, {'full_size': 1}, {'max_per_hour': 3601}):
            with self.subTest(bad=bad):
                with self.assertRaises(HP.HoneypotConfigError):
                    HP.config(bad)

    def test_loader_example_on_production_shape_unchanged(self):
        from lean import __main__ as entry
        example = entry.load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        self.assertEqual(example['honeypot_sim'], {'enabled': True, 'max_per_hour': 30, 'full_size': True})
        # the deployed (pre-L22) config has no honeypot_sim key: it loads unchanged and the filter is OFF
        raw = json.loads((ROOT / 'config' / 'lean' / 'lean.example.json').read_text())
        raw.pop('honeypot_sim')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'lean.json'
            raw['strategy_config'] = str(ROOT / 'config' / 'lean' / 'strategy-default.json')
            path.write_text(json.dumps(raw))
            production = entry.load_config(path)
            self.assertEqual(production['honeypot_sim']['enabled'], False)
            self.assertEqual({k: v for k, v in production.items() if k != 'honeypot_sim'},
                             {k: v for k, v in example.items() if k != 'honeypot_sim'})
            raw['honeypot_sim'] = {'enabled': True, 'typo': 1}
            path.write_text(json.dumps(raw))
            with self.assertRaises(entry.ConfigError):
                entry.load_config(path)
        self.assertFalse(entry.load_config(ROOT / 'tests' / 'lean' / 'lean.baseline.json')['honeypot_sim']['enabled'])


if __name__ == '__main__':
    unittest.main()
