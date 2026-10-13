"""SYNTHETIC_TEST_ONLY: a fake launchpad chain for the census tests (signatures newest first with ``until`` / ``before`` /
``limit``, and transactions with program-invocation logs). Time-aware: a transaction is visible from its ``t``."""
SOL = 'So11111111111111111111111111111111111111112'
TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
LAUNCHLAB = 'LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj'
DBC = 'dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN'
AGGREGATOR = 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4'


CREATE_TEXT = {DBC: 'Instruction: InitializeVirtualPoolWithSplToken'}      # everything else logs 'Instruction: Initialize'


def logs(kind, program):
    text = CREATE_TEXT.get(program, 'Instruction: Initialize')
    if kind == 'create':
        return [f'Program {program} invoke [1]', 'Program log: ' + text, f'Program {TOKEN} invoke [2]',
                'Program log: Instruction: InitializeMint2', f'Program {TOKEN} success', f'Program {program} success']
    if kind == 'swap':
        return [f'Program {program} invoke [1]', 'Program log: Instruction: BuyExactIn', f'Program {program} success']
    if kind == 'token_only':       # the token program logs an Initialize-looking line, the launchpad program only transfers
        return [f'Program {program} invoke [1]', f'Program {TOKEN} invoke [2]', 'Program log: Instruction: Initialize',
                f'Program {TOKEN} success', f'Program {program} success']
    if kind == 'cpi_create':       # an aggregator calls the launchpad: the create log is the launchpad's
        return [f'Program {AGGREGATOR} invoke [1]', f'Program {program} invoke [2]', 'Program log: Instruction: Initialize',
                f'Program {program} success', f'Program {AGGREGATOR} success']
    if kind == 'outer_log':        # the outer program logs the pattern text, the launchpad does not
        return [f'Program {AGGREGATOR} invoke [1]', 'Program log: Instruction: Initialize', f'Program {program} invoke [2]',
                'Program log: Instruction: BuyExactIn', f'Program {program} success', f'Program {AGGREGATOR} success']
    raise ValueError(kind)


class LaunchChain:
    def __init__(self):
        self.txs = {}                  # program -> [tx dict], oldest first
        self.n = 0
        self.fail = {}                 # method -> callable(params) -> exception | None
        self.calls = []

    def add(self, program, t, kind, *, err=None, mint=None, sig=None):
        self.n += 1
        sig = sig or ('SIG%04d' % self.n) + 'x' * 40
        mint = mint or ('MINT%04d' % self.n) + 'm' * 38
        self.txs.setdefault(program, []).append({'sig': sig, 't': t, 'err': err, 'kind': kind, 'mint': mint, 'program': program})
        return sig

    def visible(self, program, t):
        return sorted((x for x in self.txs.get(program, []) if x['t'] <= t), key=lambda x: x['t'])      # oldest first, by time

    def handle(self, method, params, t):
        self.calls.append((method, t))
        hook = self.fail.get(method)
        if hook is not None:
            error = hook(params)
            if error is not None:
                raise error
        if method == 'getSignaturesForAddress':
            program, opts = params[0], params[1]
            rows = list(reversed(self.visible(program, t)))            # newest first
            sigs = [r['sig'] for r in rows]
            if opts.get('before') in sigs:
                rows = rows[sigs.index(opts['before']) + 1:]
            if opts.get('until') in [r['sig'] for r in rows]:
                rows = rows[:[r['sig'] for r in rows].index(opts['until'])]
            rows = rows[:opts.get('limit', 1000)]
            return [{'signature': r['sig'], 'slot': 300_000_000 + int(r['t']), 'err': r['err'], 'blockTime': int(r['t']),
                     'confirmationStatus': 'confirmed', 'memo': None} for r in rows]
        if method == 'getTransaction':
            for program, items in self.txs.items():
                for r in items:
                    if r['sig'] == params[0] and r['t'] <= t:
                        return {'slot': 300_000_000 + int(r['t']), 'blockTime': int(r['t']), 'transaction': {'signatures': [r['sig']]},
                                'meta': {'err': r['err'], 'logMessages': logs(r['kind'], program),
                                         'postTokenBalances': [{'accountIndex': 3, 'mint': r['mint'], 'owner': 'o'},
                                                               {'accountIndex': 4, 'mint': SOL, 'owner': 'o'}]}}
            return None
        raise AssertionError('unexpected RPC ' + method)

    def rpc_handlers(self):
        return {m: (lambda params, t, m=m: self.handle(m, params, t)) for m in ('getSignaturesForAddress', 'getTransaction')}


class ChainHelius:
    """``Helius.rpc`` convention: (result, raw bytes, meta) or a typed ProviderError; time comes from ``clock``."""

    def __init__(self, chain, clock):
        self.chain, self.clock = chain, clock

    def rpc(self, method, params):
        import json
        result = self.chain.handle(method, params, self.clock())
        return result, json.dumps(result, sort_keys=True).encode(), {'attempts': 1}
