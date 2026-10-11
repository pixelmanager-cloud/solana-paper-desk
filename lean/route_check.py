"""L16: would a real bot have been able to BUILD this trade? Recorded at every paper BUY and every full exit.

PAPER ONLY: this module never signs, never sends and never touches a key. The lean quote already calls Jupiter's
``/swap/v2/build`` with the public paper taker, so the check re-uses THAT response (kept as an observation) instead of
spending a second call per trade: it asks "did the build answer contain a route and a swap instruction?" and records the
route, the compute-unit limit, the priority-fee suggestion and, when there was no route, the error code. The built
instructions themselves are not stored (only their sizes) and are never decoded beyond the two ComputeBudget fields.

Only evidence about the route counts. A transient failure (429, timeout, 5xx), a rejected key, a redirect, our own bad
argument or an unparseable body say nothing about whether a route exists: they are recorded as ``INCONCLUSIVE`` and never
as ``NOT_ROUTABLE``. Every trade is checked ONCE per side: the first conclusive attempt is the check (an exit retried every
pass while the route is gone is one NOT_ROUTABLE, not one per pass; the failed quotes themselves are in ``errors``).

If the endpoint demands a funded taker (the provider's own ``errorCode`` is one of ``UNSUPPORTED_ERROR_CODES``, matched
exactly), the check records ``ROUTE_CHECK_UNSUPPORTED`` ONCE, disables itself (persisted, survives restarts) and does not try
to work around it.
"""
import base64
import json
import sqlite3

from lean import providers
from lean.store import StoreError

KIND = 'route_check'
DISABLED_EVENT = 'route_check_disabled'
COMPUTE_BUDGET = 'ComputeBudget111111111111111111111111111111'
# The provider's own ``errorCode`` values that mean "the taker must hold funds / exist on chain", matched EXACTLY (case-sensitive).
# A substring match disabled the check on any route error that merely mentioned a balance or a simulation.
UNSUPPORTED_ERROR_CODES = frozenset({'INSUFFICIENT_FUNDS', 'INSUFFICIENT_BALANCE', 'TAKER_NOT_FUNDED', 'TAKER_ACCOUNT_NOT_FOUND'})
# Codes that are about OUR call or the transport, never about the route: never evidence of "no route".
INCONCLUSIVE_CODES = frozenset({'AUTH_REJECTED', 'REDIRECT_REFUSED', 'ARGUMENT_INVALID', 'RESPONSE_MALFORMED', 'RESPONSE_INVALID',
                                'RESPONSE_OVERSIZED', 'RESPONSE_TRUNCATED', 'RESPONSE_HEADERS_INVALID', 'TRANSPORT_ERROR',
                                'CONNECTION_ERROR', 'TIMEOUT', 'RATE_LIMITED', 'DEADLINE_EXCEEDED', 'CHECK_FAILED'})
MAX_LABELS = 8


def _int(value):
    return value if type(value) is int and not isinstance(value, bool) and 0 <= value < 2 ** 64 else None


def _compute_budget(parsed):
    """(compute unit limit, micro-lamports per CU, priority fee lamports) from the build response; None when absent."""
    units, price = _int(parsed.get('computeUnitLimit')), None
    fee = _int(parsed.get('prioritizationFeeLamports'))
    instructions = parsed.get('computeBudgetInstructions')
    for ix in instructions if isinstance(instructions, list) else ():
        if not isinstance(ix, dict) or ix.get('programId') != COMPUTE_BUDGET or not isinstance(ix.get('data'), str):
            continue
        try:
            data = base64.b64decode(ix['data'], validate=True)
        except (ValueError, TypeError):
            continue
        if data[:1] == b'\x02' and len(data) == 5:
            units = int.from_bytes(data[1:], 'little')
        elif data[:1] == b'\x03' and len(data) == 9:
            price = int.from_bytes(data[1:], 'little')
    if fee is None and units is not None and price is not None:
        fee = -(-units * price // 10 ** 6)
    return units, price, fee


def analyze(raw):
    """Facts about one build response (bytes). Never raises on bad input: it reports it."""
    try:
        parsed = providers.parse_json(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError, ArithmeticError):
        return {'buildable': False, 'error_code': 'RESPONSE_MALFORMED'}
    if not isinstance(parsed, dict):
        return {'buildable': False, 'error_code': 'RESPONSE_INVALID'}
    if parsed.get('errorCode') or parsed.get('error'):
        return {'buildable': False, 'error_code': _code(parsed.get('errorCode') or 'BUILD_ERROR')}
    route = parsed.get('routePlan')
    labels = [str((step.get('swapInfo') or {}).get('label'))[:32] for step in route if isinstance(step, dict)][:MAX_LABELS] if isinstance(route, list) else []
    swap = parsed.get('swapInstruction')
    has_swap = isinstance(swap, dict) and isinstance(swap.get('programId'), str) and isinstance(swap.get('data'), str)
    units, price, fee = _compute_budget(parsed)
    facts = {'buildable': bool(route) and has_swap, 'route': labels, 'compute_units': units, 'cu_price_micro_lamports': price,
             'priority_fee_lamports': fee, 'swap_program': swap.get('programId') if has_swap else None,
             'swap_data_len': len(swap['data']) if has_swap else None}
    if not facts['buildable']:
        facts['error_code'] = 'NO_ROUTE' if not route else 'NO_SWAP_INSTRUCTION'
    return facts


def _code(value):
    return str(value)[:64]


def analyze_error(error):
    """Facts about a failed quote call (a ProviderError): its code (the provider's own ``errorCode`` when it sent one) and
    ``conclusive``: True only when the failure is evidence about the ROUTE (the provider answered a 4xx, or named its own
    error code), False for anything transient or about our call / the transport."""
    code = _code(getattr(error, 'code', 'ERROR'))
    transient = bool(getattr(error, 'transient', False))
    own_code = None
    body = getattr(error, 'raw', None)
    if isinstance(body, (bytes, bytearray)):
        try:
            parsed = json.loads(bytes(body))
            if isinstance(parsed, dict) and isinstance(parsed.get('errorCode'), str) and parsed['errorCode']:
                own_code = _code(parsed['errorCode'])
        except (ValueError, UnicodeError, RecursionError):
            pass
    client_error = code.startswith('HTTP_4') and code not in ('HTTP_408', 'HTTP_429')
    conclusive = not transient and code not in INCONCLUSIVE_CODES and (own_code is not None or client_error)
    return {'buildable': False, 'error_code': own_code or code, 'transient': transient, 'conclusive': conclusive}


class RouteChecker:
    def __init__(self, store, config=None, *, clock=None):
        if config is None:
            config = {}
        if not isinstance(config, dict):
            raise ValueError('route_check must be an object')
        unknown = set(config) - {'enabled'}
        if unknown:
            raise ValueError('unknown route_check keys: %s' % sorted(unknown))
        if not isinstance(config.get('enabled', False), bool):
            raise ValueError('route_check.enabled must be true or false')
        self.store, self.clock = store, clock
        self.enabled = config.get('enabled', False)
        if self.enabled and store.latest_event(DISABLED_EVENT) is not None:
            self.enabled = False                                  # an earlier run found the endpoint unsupported

    # -- hooks (called by the runner; they never raise except for a store failure) ------------------------------
    # ``trade`` identifies the trade a check belongs to: the candidate id for an entry, the opening fill id for an exit.
    def on_fill(self, side, mint, observation_ref, *, candidate_id=None, trade=None):
        if not self.enabled:
            return None
        try:
            observation = self.store.observation(observation_ref)
            facts = analyze(observation['raw'] if observation else b'')
        except (StoreError, sqlite3.Error):
            raise
        except Exception:
            facts = {'buildable': False, 'error_code': 'CHECK_FAILED'}
        facts['quote_ref'] = observation_ref
        if not facts['buildable']:
            facts['conclusive'] = facts.get('error_code') not in INCONCLUSIVE_CODES
        return self._record(side, mint, facts, candidate_id, trade)

    def on_quote_error(self, side, mint, error, *, candidate_id=None, trade=None):
        if not self.enabled:
            return None
        try:
            facts = analyze_error(error)
        except Exception:
            facts = {'buildable': False, 'error_code': 'CHECK_FAILED', 'conclusive': False}
        if facts['error_code'] in UNSUPPORTED_ERROR_CODES:
            return self._unsupported(side, mint, facts, candidate_id, trade)
        return self._record(side, mint, facts, candidate_id, trade)

    # -- recording ---------------------------------------------------------------------------------------------
    def _already(self, side, mint, trade, actions):
        """True when this trade already has a row of one of ``actions`` for ``side`` (durable: it reads the store)."""
        for row in self.store.rows('decisions', mint=mint, kind=KIND, limit=10000):
            features = json.loads(row['features'])
            if features.get('side') == side and features.get('trade') == trade and row['action'] in actions:
                return True
        return False

    def _record(self, side, mint, facts, candidate_id, trade):
        if facts.get('buildable'):
            action = 'ROUTABLE'
        elif facts.get('conclusive', True):
            action = 'NOT_ROUTABLE'
        else:
            action = 'INCONCLUSIVE'                                # transient / about our call: says nothing about the route
        if trade is not None:
            seen = ('ROUTABLE', 'NOT_ROUTABLE') if action != 'INCONCLUSIVE' else ('ROUTABLE', 'NOT_ROUTABLE', 'INCONCLUSIVE')
            if self._already(side, mint, trade, seen):
                return None                                        # once per trade: later attempts add nothing
        reasons = [] if facts.get('buildable') else [facts.get('error_code', 'UNKNOWN')]
        features = {'side': side, 'trade': trade, **{k: v for k, v in facts.items() if k != 'conclusive'}, 'signed': False, 'sent': False}
        return self.store.add_decision(KIND, action, mint=mint, candidate_id=candidate_id, reasons=reasons, features=features)

    def _unsupported(self, side, mint, facts, candidate_id, trade):
        self.enabled = False
        features = {'side': side, 'trade': trade, **{k: v for k, v in facts.items() if k != 'conclusive'}, 'signed': False, 'sent': False}
        self.store.add_decision(KIND, 'UNSUPPORTED', mint=mint, candidate_id=candidate_id, reasons=['ROUTE_CHECK_UNSUPPORTED'], features=features)
        self.store.record(DISABLED_EVENT, {'reason': 'ROUTE_CHECK_UNSUPPORTED', 'error_code': facts.get('error_code')},
                          code_version=self.store.code_version, strategy_version=self.store.strategy_version)
        return None


def summary(rows):
    """Routable share at entry and at exit from ``decisions`` rows of kind route_check (dicts with action/features)."""
    result = {'entry': {'checked': 0, 'routable': 0, 'pct': None}, 'exit': {'checked': 0, 'routable': 0, 'pct': None},
              'unsupported': False, 'inconclusive': 0, 'not_routable_reasons': {}}
    for row in rows:
        action = row['action']
        features = json.loads(row['features']) if isinstance(row['features'], str) else (row['features'] or {})
        if action == 'UNSUPPORTED':
            result['unsupported'] = True
            continue
        if action == 'INCONCLUSIVE':
            result['inconclusive'] += 1                    # a provider/transport problem: not part of the routable percentage
            continue
        side = features.get('side')
        if side not in ('entry', 'exit'):
            continue
        result[side]['checked'] += 1
        if action == 'ROUTABLE':
            result[side]['routable'] += 1
        else:
            code = str(features.get('error_code') or 'UNKNOWN')
            result['not_routable_reasons'][code] = result['not_routable_reasons'].get(code, 0) + 1
    for side in ('entry', 'exit'):
        n = result[side]['checked']
        result[side]['pct'] = None if n == 0 else round(100.0 * result[side]['routable'] / n, 1)
    return result
