"""SYNTHETIC_TEST_ONLY (L20): a pool's swap tape in FAKE time, served as ``getSignaturesForAddress`` / ``getTransaction`` (jsonParsed).

Each pool has a list of trades ``(t, signer, side, sol_lamports, failed)``. The pool's reserves start at its fakeworld Token reserves and
evolve as a constant-product pool (fee ignored), so every body carries realistic pre/post token balances for the pool's two vaults
(owner = the pool) and the signer's token account, and the signer's lamports (fee 5000 paid by the signer, the fee payer). A signature
list only ever shows trades whose time is at or before the world clock ``now`` (newest first, ``before`` paging, ``limit``).
Not a test module (no ``test_`` prefix)."""
import bisect
import json
import random

from desk.security import base58

SOL = 'So11111111111111111111111111111111111111112'
FEE = 5000
START_LAMPORTS = 1000 * 10 ** 9
HELD = 10 ** 13                      # every signer's token balance before its trade (never negative after a sell)


def wallet(n):
    return base58(bytes([250, n % 256, n // 256 % 256]) + bytes([7]) * 29)


def signature(tag, i):
    return base58(bytes([tag % 255 + 1]) + i.to_bytes(4, 'big') + bytes([9]) * 59)


class Tape:
    def __init__(self, clock, *, lock=None):
        self.clock = clock
        self.pools = {}                  # pool -> {'mint', 'tag', 'times', 'trades', 'reserves', 'base_vault', 'quote_vault'}
        self.by_sig = {}                 # signature -> (pool, index)
        self.calls = []                  # (method, first param, fake time)

    def add(self, token, trades):
        """``trades``: [(t, signer, side, sol_lamports, failed)] in any order. Returns the sorted list."""
        trades = sorted(trades, key=lambda x: x[0])
        tok, sol = token.base_raw, token.quote_lamports
        reserves, sized = [], []
        for t, signer, side, amount, failed in trades:
            before = (tok, sol)
            if side == 'sell':
                amount = min(amount, sol // 5)                  # a sell never drains the pool (a heavy-sell tape stays valid)
            sized.append((t, signer, side, amount, failed))
            if not failed:
                if side == 'buy':
                    out = tok * amount // (sol + amount)
                    tok, sol = tok - out, sol + amount
                else:
                    tokens_in = tok * amount // max(1, sol - amount)
                    tok, sol = tok + tokens_in, sol - amount
            reserves.append((before, (tok, sol)))
        trades = sized
        tag = token.tag
        self.pools[token.pool] = {'mint': token.mint, 'tag': tag, 'times': [x[0] for x in trades], 'trades': trades, 'reserves': reserves,
                                  'base_vault': token.base_vault, 'quote_vault': token.quote_vault}
        for i in range(len(trades)):
            self.by_sig[signature(tag, i)] = (token.pool, i)
        return trades

    # -- RPC
    def handlers(self):
        return {'getSignaturesForAddress': self.signatures, 'getTransaction': self.transaction}

    @staticmethod
    def slot(t):
        return 300_000_000 + int((t - 1_700_000_000) / 0.4)

    def signatures(self, params, t):
        address, options = params[0], params[1]
        self.calls.append(('getSignaturesForAddress', address, t))
        pool = self.pools.get(address)
        if pool is None:
            return []
        n = bisect.bisect_right(pool['times'], t)               # only what has happened by now
        indexes = list(range(n - 1, -1, -1))                    # newest first
        if options.get('before'):
            _, at = self.by_sig[options['before']]
            indexes = [i for i in indexes if i < at]
        out = []
        for i in indexes[:options['limit']]:
            when, _signer, _side, _amount, failed = pool['trades'][i]
            out.append({'signature': signature(pool['tag'], i), 'slot': self.slot(when), 'err': {'InstructionError': [2, 'Custom']} if failed else None,
                        'blockTime': int(when), 'memo': None, 'confirmationStatus': 'finalized'})
        return out

    def transaction(self, params, t):
        assert params[1]['encoding'] == 'jsonParsed' and params[1]['maxSupportedTransactionVersion'] == 0
        self.calls.append(('getTransaction', params[0], t))
        address, i = self.by_sig[params[0]]
        pool = self.pools[address]
        when, signer, side, amount, failed = pool['trades'][i]
        assert when <= t, 'a transaction from the future was requested'
        (tok0, sol0), (tok1, sol1) = pool['reserves'][i]
        mint = pool['mint']
        ata = base58(bytes([251]) + bytes(signature(pool['tag'], i)[:31], 'ascii'))
        keys = [{'pubkey': signer, 'signer': True, 'writable': True, 'source': 'transaction'},
                {'pubkey': address, 'signer': False, 'writable': True, 'source': 'transaction'},
                {'pubkey': pool['base_vault'], 'signer': False, 'writable': True, 'source': 'transaction'},
                {'pubkey': pool['quote_vault'], 'signer': False, 'writable': True, 'source': 'transaction'},
                {'pubkey': ata, 'signer': False, 'writable': True, 'source': 'transaction'}]
        traded = abs(tok1 - tok0)
        mine0 = HELD + (traded if side == 'sell' else 0)        # a seller always held what it sells
        mine1 = mine0 if failed else (mine0 + traded if side == 'buy' else mine0 - traded)
        lamports0 = START_LAMPORTS
        lamports1 = lamports0 - FEE + (0 if failed else (-amount if side == 'buy' else amount))

        def balance(index, owner, token_mint, value):
            return {'accountIndex': index, 'mint': token_mint, 'owner': owner, 'programId': 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
                    'uiTokenAmount': {'amount': str(value), 'decimals': 6 if token_mint == mint else 9}}
        pre = [balance(2, address, mint, tok0), balance(3, address, SOL, sol0), balance(4, signer, mint, mine0)]
        post = [balance(2, address, mint, tok0 if failed else tok1), balance(3, address, SOL, sol0 if failed else sol1), balance(4, signer, mint, mine1)]
        return json.loads(json.dumps({
            'slot': self.slot(when), 'blockTime': int(when), 'version': 0,
            'meta': {'err': {'InstructionError': [2, 'Custom']} if failed else None, 'fee': FEE,
                     'preBalances': [lamports0, 0, 2_039_280, 2_039_280, 2_039_280],
                     'postBalances': [lamports1, 0, 2_039_280, 2_039_280, 2_039_280],
                     'preTokenBalances': pre, 'postTokenBalances': post, 'innerInstructions': [], 'logMessages': []},
            'transaction': {'signatures': [params[0]], 'message': {'accountKeys': keys, 'instructions': [], 'recentBlockhash': '1' * 32}}}))


# --------------------------------------------------------------------------------------------------- tape shapes
def organic(start, end, *, every=7.0, seed=1, base=0, sell_share=0.5, wallets=60):
    """Spread-out trades by many wallets: random sizes (0.05..2 SOL), buys and sells mixed."""
    rng = random.Random(seed)
    out, t = [], start
    while t < end:
        side = 'sell' if rng.random() < sell_share else 'buy'
        out.append((t, wallet(base + rng.randrange(wallets)), side, int(rng.uniform(0.05, 2.0) * 10 ** 9), False))
        t += every * rng.uniform(0.5, 1.5)
    return out


def bot_pump(start, end, *, every=0.2, size=50_000_000, base=500, wallets=3):
    """One bot (a few wallets) buying the same size every ``every`` seconds: a perfectly linear all-green staircase."""
    out, i = [], 0
    while start + i * every < end:
        out.append((start + i * every, wallet(base + i % wallets), 'buy', size, False))
        i += 1
    return out


def honeypot(start, end, *, every=30.0, base=800):
    """Buys by distinct wallets; every sell attempt FAILS on chain (a honeypot)."""
    out, t, i = [], start, 0
    while t < end:
        out.append((t, wallet(base + i), 'buy', 300_000_000, False))
        out.append((t + every / 2, wallet(base + 1000 + i), 'sell', 200_000_000, True))
        t += every
        i += 1
    return out


def only_by(start, end, seller, *, every=20.0, base=900):
    """Buys by distinct wallets; the only successful sells are by ``seller`` (e.g. the coin creator), two per buy."""
    out, t, i = [], start, 0
    while t < end:
        out.append((t, wallet(base + i), 'buy', 300_000_000, False))
        out.append((t + every / 3, seller, 'sell', 100_000_000, False))
        out.append((t + 2 * every / 3, seller, 'sell', 100_000_000, False))
        t += every
        i += 1
    return out
