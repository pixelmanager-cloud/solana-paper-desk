"""SYNTHETIC_TEST_ONLY: a fake Solana/Jupiter/Kraken world behind the HTTP layer ONLY.

Everything above urllib is real: lean.providers parses these bytes exactly as it would parse the live services.
Response shapes follow the public APIs and the repo's captured fixtures (fixtures/mainnet-roundtrip-simulation.json
for Jupiter swap/v2/build, the Helius JSON-RPC envelope with base64 accounts, Kraken public Trades). Accounts are the
desk's PumpSwap/SPL layouts (tests.test_pools / tests.test_candidates), one canonical migrated pool per token.

Prices move by scaling a pool's SOL reserve with a multiplier path keyed on the seconds since the token's FIRST buy
quote (so a scenario does not depend on exactly when the runner enters).
"""
import base64
import copy
import json
import sqlite3
from decimal import Decimal
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from desk.model import canonical, digest
from desk.pools import ATA
from desk.programs import unbase58
from desk.providers import PUMP, PUMPSWAP
from desk.security import TOKEN_PROGRAM, base58
from discovery import continuous as discovery

SOL = 'So11111111111111111111111111111111111111112'
T0 = 1_800_000_000.0                      # a UTC minute boundary, far from midnight
SUPPLY = 10 ** 15                         # 1e9 tokens at 6 decimals
DECIMALS = 6
POOL_FEE_BPS = 25


class FakeTime:
    """One clock for everything: wall time, monotonic time and sleep (sleeping advances both)."""

    def __init__(self, start=T0):
        self.t = float(start)

    def time(self):
        return self.t

    def monotonic(self):
        return self.t - T0 + 1000.0

    def sleep(self, seconds):
        self.t += max(0.0, float(seconds))

    def advance(self, seconds):
        self.t += seconds


def _pda(seeds, program):
    from solders.pubkey import Pubkey
    return Pubkey.find_program_address(seeds, Pubkey.from_string(program))


class Token:
    """One migrated PumpSwap pool. ``hazard`` / ``route_fee_bps`` / ``path`` shape the scenario."""

    def __init__(self, tag, *, quote_sol=80, base_raw=2 * 10 ** 14, hazard=None, route_fee_bps=0, path=None,
                 no_route=False):
        from solders.pubkey import Pubkey
        self.tag = tag
        self.mint_pk = Pubkey.from_bytes(bytes([tag]) * 32)
        self.mint = str(self.mint_pk)
        sol_pk = Pubkey.from_string(SOL)
        creator = _pda([b'pool-authority', bytes(self.mint_pk)], PUMP)[0]
        pool, bump = _pda([b'pool', bytes(2), bytes(creator), bytes(self.mint_pk), bytes(sol_pk)], PUMPSWAP)
        lp = _pda([b'pool_lp_mint', bytes(pool)], PUMPSWAP)[0]
        vaults = [_pda([bytes(pool), bytes(Pubkey.from_string(TOKEN_PROGRAM)), bytes(m)], ATA)[0] for m in (self.mint_pk, sol_pk)]
        self.pool, self.lp = str(pool), str(lp)
        self.base_vault, self.quote_vault = str(vaults[0]), str(vaults[1])
        self._pool_pk, self._vault_pks = pool, vaults
        self.pool_raw = (bytes([241, 154, 109, 4, 17, 177, 109, 188]) + bytes([bump]) + bytes(2)
                         + b''.join(bytes(x) for x in (creator, self.mint_pk, sol_pk, lp, *vaults))
                         + (1000).to_bytes(8, 'little') + bytes(32))
        self.quote_lamports, self.base_raw = int(quote_sol * 10 ** 9), int(base_raw)
        self.hazard, self.route_fee_bps, self.no_route = hazard, route_fee_bps, no_route
        self.path = path or (lambda age: Decimal(1))
        self.anchor = None                 # time of the first buy quote

    # -- state at time t
    def multiplier(self, t):
        return Decimal(1) if self.anchor is None else Decimal(self.path(t - self.anchor))

    def reserves(self, t):
        return self.base_raw, int(Decimal(self.quote_lamports) * self.multiplier(t))

    # -- accounts
    @staticmethod
    def account(data, owner):
        return {'data': [base64.b64encode(bytes(data)).decode(), 'base64'], 'executable': False, 'lamports': 2_039_280,
                'owner': owner, 'rentEpoch': 18446744073709551615, 'space': len(data)}

    def mint_account(self):
        d = bytearray(82)
        d[36:44] = SUPPLY.to_bytes(8, 'little'); d[44] = DECIMALS; d[45] = 1
        if self.hazard == 'mint_authority':
            d[0:4] = (1).to_bytes(4, 'little'); d[4:36] = bytes([3]) * 32
        if self.hazard == 'freeze_authority':
            d[46:50] = (1).to_bytes(4, 'little'); d[50:82] = bytes([4]) * 32
        return self.account(d, TOKEN_PROGRAM)

    def vault_account(self, which, amount):
        from solders.pubkey import Pubkey
        d = bytearray(165)
        d[:32] = bytes(self.mint_pk if which == 'base' else Pubkey.from_string(SOL))
        d[32:64] = bytes(self._pool_pk); d[64:72] = int(amount).to_bytes(8, 'little'); d[108] = 1
        return self.account(d, TOKEN_PROGRAM)

    def lp_account(self):
        d = bytearray(82)
        d[:4] = (1).to_bytes(4, 'little'); d[4:36] = bytes(self._pool_pk); d[45] = 1
        if self.hazard == 'lp_outstanding':
            d[36:44] = (10 ** 9).to_bytes(8, 'little')
        return self.account(d, TOKEN_PROGRAM)

    def accounts(self, t):
        base, quote = self.reserves(t)
        return {self.mint: self.mint_account(), self.pool: self.account(self.pool_raw, PUMPSWAP),
                self.base_vault: self.vault_account('base', base), self.quote_vault: self.vault_account('quote', quote),
                self.lp: self.lp_account()}

    def holders(self):
        rows = [{'address': self.base_vault, 'amount': str(self.base_raw)}]
        if self.hazard == 'concentrated':
            rows.append({'address': base58(bytes([200]) * 32), 'amount': str(SUPPLY * 7 // 10)})
        rows += [{'address': base58(bytes([100 + i]) * 32), 'amount': str(SUPPLY // 100)} for i in range(10)]
        return [dict(r, decimals=DECIMALS, uiAmountString=str(Decimal(r['amount']) / 10 ** DECIMALS)) for r in rows]

    # -- Jupiter constant-product quote (pool fee + any extra route fee)
    def quote(self, t, input_mint, amount):
        base, quote = self.reserves(t)
        bps = POOL_FEE_BPS + self.route_fee_bps
        if input_mint == SOL:
            fee = amount * bps // 10000
            out = base * (amount - fee) // (quote + amount - fee)
        else:
            gross = quote * amount // (base + amount)
            fee = gross * bps // 10000
            out = gross - fee
        return out, fee


class World:
    """The HTTP opener plus the discovery database. ``outage`` = {provider: (start, end)} answers 503 inside the window."""

    def __init__(self, root, tokens, clock):
        self.root, self.tokens, self.clock = root, {t.mint: t for t in tokens}, clock
        self.order = list(tokens)
        self.outages = {}
        self.extra_rpc = {}                                  # L13: {method: fn(params, t) -> result} for methods the base world lacks
        self.calls = []                                      # (provider, method, t)
        self.slot = 300_000_000
        self.discovery_db = root / 'continuous.sqlite'
        with patch.object(discovery.time, 'time', return_value=T0 - 10_000):
            discovery.initialize(self.discovery_db)

    # -- discovery frames (the desk's migration transaction, re-keyed per token; see tests.test_candidates)
    def add_frames(self, ready_at):
        """One migration frame per token; token i becomes old enough (min_age 300 s) at ``ready_at[i]``."""
        from tests.test_graduation_witness import fixture as migration_fixture
        from desk import graduation_witness as g
        raw0, mint0, pool0 = migration_fixture()
        with sqlite3.connect(self.discovery_db) as c:
            for seq, (token, ready) in enumerate(zip(self.order, ready_at), start=1):
                raw = copy.deepcopy(raw0)
                authority = g._pda([b'pool-authority', unbase58(token.mint)], g.PUMP)
                curve = g._pda([b'bonding-curve', unbase58(token.mint)], g.PUMP)
                replacements = {mint0: token.mint, pool0: token.pool,
                                g._pda([b'pool-authority', unbase58(mint0)], g.PUMP): authority,
                                g._pda([b'bonding-curve', unbase58(mint0)], g.PUMP): curve}
                for ix in raw['transaction']['message']['instructions']:
                    ix['accounts'] = [replacements.get(x, x) for x in ix['accounts']]
                ix = raw['meta']['innerInstructions'][0]['instructions'][0]
                data = unbase58(ix['data'])
                for old, new in replacements.items():
                    data = data.replace(unbase58(old), unbase58(new))
                ix['data'] = base58(data)
                signature = base58(bytes([token.tag + 50]) * 64)
                raw['transaction']['signatures'] = [signature]
                wire = {'method': 'transactionNotification', 'params': {'result': {
                    'signature': signature, 'slot': raw['slot'], 'blockTime': raw['blockTime'],
                    'transaction': {'transaction': raw['transaction'], 'meta': raw['meta']}}}}
                c.execute('INSERT INTO raw_events(seq,source_id,received_at,slot,payload,payload_hash) VALUES(?,?,?,?,?,?)',
                          (seq, 'confirmed:%d' % seq, ready - 300, raw['slot'], canonical(wire), digest(wire)))

    # -- HTTP
    def opener(self, request, timeout):
        url = urlsplit(request.full_url)
        host, t = url.hostname, self.clock.time()
        provider = {'mainnet.helius-rpc.com': 'helius', 'api.jup.ag': 'jupiter', 'api.kraken.com': 'kraken'}[host]
        window = self.outages.get(provider)
        if window and window[0] <= t < window[1]:
            self.calls.append((provider, 'OUTAGE', t))
            return Resp(b'{"error":"service unavailable"}', 503)
        if provider == 'helius':
            return self._helius(json.loads(request.data), t)
        if provider == 'jupiter':
            return self._jupiter(url.path, {k: v[0] for k, v in parse_qs(url.query).items()}, t)
        self.calls.append(('kraken', 'Trades', t))
        trade = format(Decimal(repr(t)) - 2, 'f')
        return Resp(('{"error":[],"result":{"SOLUSD":[["150.00000","0.50000000",%s,"b","l","",123456]],"last":"1"}}'
                     % trade).encode())

    def _helius(self, body, t):
        method, params = body['method'], body['params']
        self.calls.append(('helius', method, t))
        self.slot += 1
        if method == 'getMultipleAccounts':
            known = {}
            for token in self.tokens.values():
                known.update(token.accounts(t))
            result = {'context': {'apiVersion': '2.2.7', 'slot': self.slot}, 'value': [copy.deepcopy(known.get(k)) for k in params[0]]}
        elif method == 'getTokenLargestAccounts':
            result = {'context': {'apiVersion': '2.2.7', 'slot': self.slot}, 'value': self.tokens[params[0]].holders()}
        elif method in self.extra_rpc:
            result = self.extra_rpc[method](params, t)
        else:
            raise AssertionError('unexpected RPC ' + method)
        return Resp({'jsonrpc': '2.0', 'id': body['id'], 'result': result})

    def _jupiter(self, path, q, t):
        assert path == '/swap/v2/build', path
        assert q['taker'] and q['transactionVersion'] == '0'
        self.calls.append(('jupiter', 'quote', t))
        input_mint, output_mint, amount = q['inputMint'], q['outputMint'], int(q['amount'])
        token = self.tokens[output_mint if input_mint == SOL else input_mint]
        if token.no_route:
            return Resp(b'{"error":"Could not find any route","errorCode":"COULD_NOT_FIND_ANY_ROUTE"}', 400)
        if input_mint == SOL and token.anchor is None:
            token.anchor = t
        out, fee = token.quote(t, input_mint, amount)
        body = {'inputMint': input_mint, 'outputMint': output_mint, 'inAmount': str(amount), 'outAmount': str(out),
                'otherAmountThreshold': str(out * 9900 // 10000), 'swapMode': 'ExactIn', 'slippageBps': int(q['slippageBps']),
                'priceImpactPct': '0.004', 'platformFee': None,
                'routePlan': [{'percent': 100, 'bps': 10000, 'swapInfo': {
                    'ammKey': token.pool, 'label': 'Pump.fun Amm', 'inputMint': input_mint, 'outputMint': output_mint,
                    'inAmount': str(amount), 'outAmount': str(out), 'feeAmount': str(fee), 'feeMint': SOL}}],
                'contextSlot': self.slot, 'timeTaken': 0.01,
                'computeBudgetInstructions': [], 'setupInstructions': [], 'swapInstruction': {'programId': PUMPSWAP, 'accounts': [], 'data': ''},
                'addressLookupTableAddresses': []}
        return Resp(body)


class Resp:
    def __init__(self, body, status=200):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status, self.headers = status, {}

    def read(self, n=-1):
        return self.body if n is None or n < 0 else self.body[:n]

    def close(self):
        pass


def unreachable_opener(request, timeout):
    raise URLError('network disabled in tests')
