"""L16: would a real bot have been able to BUILD this trade? Recorded at every paper BUY and every full exit.

PAPER ONLY: this module never signs, never sends and never touches a key. The lean quote already calls Jupiter's
``/swap/v2/build`` with the public paper taker, so the check re-uses THAT response (kept as an observation) instead of
spending a second call per trade: it asks "did the build answer contain a route and a swap instruction?" and records the
route, the compute-unit limit, the priority-fee suggestion and, when there was no route, the error code. The built
instructions themselves are not stored (only their sizes) and are never decoded beyond the two ComputeBudget fields.

If the endpoint demands a funded taker (an error about balance/funds/simulation), the check records
``ROUTE_CHECK_UNSUPPORTED`` ONCE, disables itself (persisted, survives restarts) and does not try to work around it.
"""
import base64
import json
import sqlite3

from lean import providers
from lean.store import StoreError

KIND = 'route_check'
DISABLED_EVENT = 'route_check_disabled'
COMPUTE_BUDGET = 'ComputeBudget111111111111111111111111111111'
UNSUPPORTED_MARKERS = ('INSUFFICIENT', 'NOT_ENOUGH', 'BALANCE', 'FUNDS', 'TAKER_NOT', 'SIMULATION', 'ACCOUNT_NOT_FOUND')
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
        return {'buildable': False, 'error_code': str(parsed.get('errorCode') or 'BUILD_ERROR')[:64]}
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


def analyze_error(error):
    """Facts about a failed quote call (a ProviderError): its code, and the provider's own errorCode when it sent one."""
    code = str(getattr(error, 'code', 'ERROR'))[:64]
    body = getattr(error, 'raw', None)
    text = code.upper()
    if isinstance(body, (bytes, bytearray)):
        try:
            parsed = json.loads(bytes(body))
            if isinstance(parsed, dict):
                code = str(parsed.get('errorCode') or code)[:64]
                text += ' ' + json.dumps(parsed).upper()[:600]
        except (ValueError, UnicodeError, RecursionError):
            pass
    return {'buildable': False, 'error_code': code, 'transient': bool(getattr(error, 'transient', False))}, text


class RouteChecker:
    def __init__(self, store, config=None, *, clock=None):
        config = config or {}
        unknown = set(config) - {'enabled'}
        if unknown:
            raise ValueError('unknown route_check keys: %s' % sorted(unknown))
        self.store, self.clock = store, clock
        self.enabled = bool(config.get('enabled', False))
        if self.enabled and store.latest_event(DISABLED_EVENT) is not None:
            self.enabled = False                                  # an earlier run found the endpoint unsupported

    # -- hooks (called by the runner; they never raise except for a store failure) ------------------------------
    def on_fill(self, side, mint, observation_ref, *, candidate_id=None):
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
        return self._record(side, mint, facts, candidate_id)

    def on_quote_error(self, side, mint, error, *, candidate_id=None):
        if not self.enabled:
            return None
        try:
            facts, text = analyze_error(error)
        except Exception:
            facts, text = {'buildable': False, 'error_code': 'CHECK_FAILED'}, ''
        if any(marker in text for marker in UNSUPPORTED_MARKERS):
            return self._unsupported(side, mint, facts, candidate_id)
        return self._record(side, mint, facts, candidate_id)

    # -- recording ---------------------------------------------------------------------------------------------
    def _record(self, side, mint, facts, candidate_id):
        action = 'ROUTABLE' if facts.get('buildable') else 'NOT_ROUTABLE'
        reasons = [] if facts.get('buildable') else [facts.get('error_code', 'UNKNOWN')]
        return self.store.add_decision(KIND, action, mint=mint, candidate_id=candidate_id, reasons=reasons,
                                       features={'side': side, **facts, 'signed': False, 'sent': False})

    def _unsupported(self, side, mint, facts, candidate_id):
        self.enabled = False
        self.store.add_decision(KIND, 'UNSUPPORTED', mint=mint, candidate_id=candidate_id, reasons=['ROUTE_CHECK_UNSUPPORTED'],
                                features={'side': side, **facts, 'signed': False, 'sent': False})
        code_version, strategy_version = self.store._versions(None, None)
        self.store.record(DISABLED_EVENT, {'reason': 'ROUTE_CHECK_UNSUPPORTED', 'error_code': facts.get('error_code')},
                          code_version=code_version, strategy_version=strategy_version)
        return None


def summary(rows):
    """Routable share at entry and at exit from ``decisions`` rows of kind route_check (dicts with action/features)."""
    result = {'entry': {'checked': 0, 'routable': 0, 'pct': None}, 'exit': {'checked': 0, 'routable': 0, 'pct': None},
              'unsupported': False, 'not_routable_reasons': {}}
    for row in rows:
        action = row['action']
        features = json.loads(row['features']) if isinstance(row['features'], str) else (row['features'] or {})
        if action == 'UNSUPPORTED':
            result['unsupported'] = True
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
