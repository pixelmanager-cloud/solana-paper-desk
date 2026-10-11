"""SYNTHETIC_TEST_ONLY: lean.route_check units. Paper only: also proves lean/ contains no signing or broadcast primitive."""
import ast
import base64
import json
import tempfile
import unittest
from pathlib import Path

from lean import route_check as R
from lean.providers import ProviderError
from lean.store import Store

LEAN = Path(__file__).resolve().parents[2] / 'lean'
SWAP = {'programId': 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA', 'accounts': [], 'data': 'AQID'}


def build(**over):
    body = {'routePlan': [{'percent': 100, 'swapInfo': {'label': 'Pump.fun Amm'}}], 'swapInstruction': SWAP,
            'computeBudgetInstructions': [], 'setupInstructions': []}
    body.update(over)
    return json.dumps(body).encode()


def budget(units=None, price=None):
    out = []
    if units is not None:
        out.append({'programId': R.COMPUTE_BUDGET, 'accounts': [], 'data': base64.b64encode(b'\x02' + units.to_bytes(4, 'little')).decode()})
    if price is not None:
        out.append({'programId': R.COMPUTE_BUDGET, 'accounts': [], 'data': base64.b64encode(b'\x03' + price.to_bytes(8, 'little')).decode()})
    return out


class Analyze(unittest.TestCase):
    def test_buildable_response_reports_route_compute_units_and_priority_fee(self):
        facts = R.analyze(build(computeBudgetInstructions=budget(units=200_000, price=5_000_000)))
        self.assertTrue(facts['buildable'])
        self.assertEqual((facts['route'], facts['compute_units'], facts['cu_price_micro_lamports']), (['Pump.fun Amm'], 200_000, 5_000_000))
        self.assertEqual(facts['priority_fee_lamports'], 1_000_000)               # 200k CU x 5 lamports/CU, rounded up
        self.assertEqual(facts['swap_program'], SWAP['programId'])
        self.assertEqual(facts['swap_data_len'], 4)                                # only the SIZE of the instruction data is kept
        self.assertNotIn('AQID', json.dumps(facts))                                # never the instruction bytes

    def test_top_level_fields_and_missing_budget_are_tolerated(self):
        facts = R.analyze(build(computeUnitLimit=150000, prioritizationFeeLamports=7))
        self.assertEqual((facts['compute_units'], facts['priority_fee_lamports']), (150000, 7))
        bare = R.analyze(build())
        self.assertTrue(bare['buildable']); self.assertIsNone(bare['compute_units']); self.assertIsNone(bare['priority_fee_lamports'])

    def test_not_buildable_cases_are_reported_not_raised(self):
        cases = {'no route': (build(routePlan=[]), 'NO_ROUTE'), 'no swap instruction': (build(swapInstruction=None), 'NO_SWAP_INSTRUCTION'),
                 'provider error': (b'{"error":"x","errorCode":"COULD_NOT_FIND_ANY_ROUTE"}', 'COULD_NOT_FIND_ANY_ROUTE'),
                 'malformed': (b'<html>', 'RESPONSE_MALFORMED'), 'array': (b'[]', 'RESPONSE_INVALID'), 'empty': (b'', 'RESPONSE_MALFORMED'),
                 'garbage budget': (build(computeBudgetInstructions=[{'programId': R.COMPUTE_BUDGET, 'data': '!!!'}]), None)}
        for name, (raw, code) in cases.items():
            with self.subTest(name):
                facts = R.analyze(raw)
                if code is None:
                    self.assertTrue(facts['buildable']); self.assertIsNone(facts['compute_units'])
                else:
                    self.assertFalse(facts['buildable']); self.assertEqual(facts['error_code'], code)

    def test_error_analysis_prefers_the_providers_own_code(self):
        facts = R.analyze_error(ProviderError('HTTP_400', False, raw=b'{"errorCode":"COULD_NOT_FIND_ANY_ROUTE"}'))
        self.assertEqual((facts['buildable'], facts['error_code'], facts['transient'], facts['conclusive']),
                         (False, 'COULD_NOT_FIND_ANY_ROUTE', False, True))
        facts = R.analyze_error(ProviderError('TIMEOUT', True))
        self.assertEqual((facts['error_code'], facts['transient'], facts['conclusive']), ('TIMEOUT', True, False))

    def test_only_evidence_about_the_route_is_conclusive(self):
        cases = {'a transport timeout': (ProviderError('TIMEOUT', True), False),
                 'rate limited': (ProviderError('HTTP_429', True, raw=b'{"errorCode":"COULD_NOT_FIND_ANY_ROUTE"}'), False),
                 'server error': (ProviderError('HTTP_503', True), False),
                 'bad key': (ProviderError('AUTH_REJECTED', False), False),
                 'bad key with a body code': (ProviderError('AUTH_REJECTED', False, raw=b'{"errorCode":"UNAUTHORIZED"}'), False),
                 'redirect': (ProviderError('REDIRECT_REFUSED', False), False),
                 'our own bad argument': (ProviderError('ARGUMENT_INVALID', False), False),
                 'unparseable body': (ProviderError('RESPONSE_MALFORMED', False), False),
                 'plain 400': (ProviderError('HTTP_400', False), True),
                 'plain 404': (ProviderError('HTTP_404', False), True),
                 'provider code on a 400': (ProviderError('HTTP_400', False, raw=b'{"errorCode":"TOKEN_NOT_TRADABLE"}'), True),
                 'a request timeout (408) is transient': (ProviderError('HTTP_408', True), False)}
        for name, (error, conclusive) in cases.items():
            with self.subTest(name):
                self.assertEqual(R.analyze_error(error)['conclusive'], conclusive)


class Checker(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / 'lean.sqlite')
        self.store = Store(self.path, initial_cash_sol='1', code_version='t', strategy_version='s')
        self.addCleanup(self.store.close)

    def rows(self):
        return self.store.rows('decisions', kind=R.KIND)

    def test_disabled_by_default_and_unknown_keys_refused(self):
        checker = R.RouteChecker(self.store, None)
        ref = self.store.add_observation('quote:buy', build())
        self.assertIsNone(checker.on_fill('entry', 'M', ref, trade=1))
        self.assertIsNone(checker.on_quote_error('entry', 'M', ProviderError('HTTP_400', False), trade=1))
        self.assertEqual(self.rows(), [])
        with self.assertRaises(ValueError):
            R.RouteChecker(self.store, {'enabled': True, 'enable': True})

    def test_records_routable_and_not_routable_with_side_and_never_signed_or_sent(self):
        checker = R.RouteChecker(self.store, {'enabled': True})
        checker.on_fill('entry', 'M1', self.store.add_observation('quote:buy', build()), trade=1)
        checker.on_fill('exit', 'M1', self.store.add_observation('quote:exit', build(routePlan=[])), trade=7)
        checker.on_quote_error('exit', 'M2', ProviderError('HTTP_400', False, raw=b'{"errorCode":"COULD_NOT_FIND_ANY_ROUTE"}'), trade=8)
        rows = self.rows()
        self.assertEqual([r['action'] for r in rows], ['ROUTABLE', 'NOT_ROUTABLE', 'NOT_ROUTABLE'])
        features = [json.loads(r['features']) for r in rows]
        self.assertEqual([f['side'] for f in features], ['entry', 'exit', 'exit'])
        self.assertTrue(all(f['signed'] is False and f['sent'] is False for f in features))
        self.assertEqual(json.loads(rows[2]['reasons']), ['COULD_NOT_FIND_ANY_ROUTE'])
        s = R.summary(rows)
        self.assertEqual((s['entry']['pct'], s['exit']['pct'], s['exit']['checked']), (100.0, 0.0, 2))
        self.assertEqual(s['not_routable_reasons'], {'NO_ROUTE': 1, 'COULD_NOT_FIND_ANY_ROUTE': 1})

    def test_funded_taker_requirement_is_recorded_once_disables_and_survives_restart(self):
        checker = R.RouteChecker(self.store, {'enabled': True})
        error = ProviderError('HTTP_400', False, raw=b'{"errorCode":"INSUFFICIENT_FUNDS","error":"taker balance too low"}')
        checker.on_quote_error('entry', 'M1', error, trade=1)
        checker.on_quote_error('entry', 'M2', error, trade=2)
        checker.on_fill('entry', 'M3', self.store.add_observation('quote:buy', build()), trade=3)
        self.assertEqual([r['action'] for r in self.rows()], ['UNSUPPORTED'])          # once; nothing after it
        self.assertFalse(checker.enabled)
        again = R.RouteChecker(self.store, {'enabled': True})
        self.assertFalse(again.enabled)                                                   # persisted, not re-probed
        self.assertTrue(R.summary(self.rows())['unsupported'])

    def test_malformed_stored_observation_is_recorded_not_raised(self):
        checker = R.RouteChecker(self.store, {'enabled': True})
        checker.on_fill('entry', 'M', 999999, trade=1)                                    # no such observation
        # L16F: was NOT_ROUTABLE. Our own failure to read the stored response says nothing about the route: INCONCLUSIVE.
        self.assertEqual([r['action'] for r in self.rows()], ['INCONCLUSIVE'])


    # -- review fixes (L16F) ------------------------------------------------------------------------------------
    def test_transient_errors_are_inconclusive_never_not_routable(self):
        checker = R.RouteChecker(self.store, {'enabled': True})
        for i, code in enumerate(('TIMEOUT', 'HTTP_429', 'HTTP_503', 'AUTH_REJECTED', 'RESPONSE_MALFORMED')):
            checker.on_quote_error('exit', 'M%d' % i, ProviderError(code, code in ('TIMEOUT', 'HTTP_429', 'HTTP_503')), trade=i)
        rows = self.rows()
        self.assertEqual([r['action'] for r in rows], ['INCONCLUSIVE'] * 5)
        s = R.summary(rows)
        self.assertEqual((s['exit']['checked'], s['exit']['routable'], s['exit']['pct'], s['inconclusive']), (0, 0, None, 5))
        self.assertEqual(s['not_routable_reasons'], {})

    def test_a_trade_is_counted_once_however_often_the_exit_is_retried(self):
        checker = R.RouteChecker(self.store, {'enabled': True})
        gone = ProviderError('HTTP_400', False, raw=b'{"errorCode":"COULD_NOT_FIND_ANY_ROUTE"}')
        for _ in range(5):
            checker.on_quote_error('exit', 'M', gone, trade=7)                           # the route is gone for five passes
        checker.on_fill('exit', 'M', self.store.add_observation('quote:exit', build()), trade=7)      # then it sells
        rows = self.rows()
        self.assertEqual([r['action'] for r in rows], ['NOT_ROUTABLE'])                  # the FIRST conclusive attempt is the check
        s = R.summary(rows)
        self.assertEqual((s['exit']['checked'], s['exit']['routable'], s['exit']['pct']), (1, 0, 0.0))

    def test_each_trade_has_its_own_check_and_a_restart_does_not_repeat_it(self):
        gone = ProviderError('HTTP_400', False, raw=b'{"errorCode":"COULD_NOT_FIND_ANY_ROUTE"}')
        first = R.RouteChecker(self.store, {'enabled': True})
        first.on_quote_error('exit', 'M', gone, trade=7)
        again = R.RouteChecker(self.store, {'enabled': True})                            # a fresh process on the same store
        again.on_quote_error('exit', 'M', gone, trade=7)
        again.on_fill('exit', 'M', self.store.add_observation('quote:exit', build()), trade=7)
        again.on_fill('exit', 'M', self.store.add_observation('quote:exit', build()), trade=9)       # a second lifecycle of the same mint
        self.assertEqual([(r['action'], json.loads(r['features'])['trade']) for r in self.rows()], [('NOT_ROUTABLE', 7), ('ROUTABLE', 9)])
        for side in ('entry', 'exit'):                                                   # entry and exit of one trade are separate checks
            again.on_fill(side, 'N', self.store.add_observation('q', build()), trade=1)
        self.assertEqual(len(self.rows()), 4)

    def test_an_inconclusive_attempt_is_recorded_once_and_does_not_hide_the_real_check(self):
        checker = R.RouteChecker(self.store, {'enabled': True})
        for _ in range(4):
            checker.on_quote_error('exit', 'M', ProviderError('TIMEOUT', True), trade=3)
        checker.on_fill('exit', 'M', self.store.add_observation('quote:exit', build()), trade=3)
        self.assertEqual([r['action'] for r in self.rows()], ['INCONCLUSIVE', 'ROUTABLE'])
        s = R.summary(self.rows())
        self.assertEqual((s['exit']['checked'], s['exit']['pct'], s['inconclusive']), (1, 100.0, 1))

    def test_unsupported_markers_are_exact_error_codes_not_substrings(self):
        for code in sorted(R.UNSUPPORTED_ERROR_CODES):
            with self.subTest(code=code):
                store = Store(str(Path(self.path).with_name('u-%s.sqlite' % code)), initial_cash_sol='1', code_version='t', strategy_version='s')
                self.addCleanup(store.close)
                checker = R.RouteChecker(store, {'enabled': True})
                checker.on_quote_error('entry', 'M', ProviderError('HTTP_400', False, raw=json.dumps({'errorCode': code}).encode()), trade=1)
                self.assertEqual([r['action'] for r in store.rows('decisions', kind=R.KIND)], ['UNSUPPORTED'])
        # the old substring match disabled the check on any of these
        for code, text in (('COULD_NOT_FIND_ANY_ROUTE', 'no route: pool balance too low'), ('SIMULATION_FAILED', 'simulation'),
                           ('insufficient_funds', 'x'), ('ROUTE_ACCOUNT_NOT_FOUND', 'x'), ('NO_FUNDS_ROUTE', 'x'), ('TAKER_NOT_ALLOWED', 'x')):
            with self.subTest(code=code):
                checker = R.RouteChecker(self.store, {'enabled': True})
                checker.on_quote_error('entry', 'M-' + code, ProviderError('HTTP_400', False, raw=json.dumps({'errorCode': code, 'error': text}).encode()), trade=1)
                self.assertTrue(checker.enabled)
        self.assertNotIn('UNSUPPORTED', [r['action'] for r in self.rows()])

    def test_config_must_be_an_object_with_a_boolean_enabled(self):
        for bad in ([], [1], 'on', 5, True, {'enabled': 'false'}, {'enabled': 1}, {'enabled': None}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                R.RouteChecker(self.store, bad)
        self.assertFalse(R.RouteChecker(self.store, {}).enabled)
        self.assertFalse(R.RouteChecker(self.store, {'enabled': False}).enabled)
        self.assertFalse(R.RouteChecker(self.store, None).enabled)

    def test_disabling_uses_only_the_stores_public_surface(self):
        class PublicOnly:
            code_version, strategy_version = 'c', 's'

            def __init__(self):
                self.events, self.decisions = [], []

            def latest_event(self, *kinds):
                return None

            def add_decision(self, *args, **kwargs):
                self.decisions.append((args, kwargs))
                return 1

            def record(self, kind, payload, *, code_version, strategy_version):
                self.events.append((kind, payload, code_version, strategy_version))
                return 1
        store = PublicOnly()
        checker = R.RouteChecker(store, {'enabled': True})
        checker.on_quote_error('entry', 'M', ProviderError('HTTP_400', False, raw=b'{"errorCode":"INSUFFICIENT_FUNDS"}'), trade=1)
        self.assertEqual(store.events, [(R.DISABLED_EVENT, {'reason': 'ROUTE_CHECK_UNSUPPORTED', 'error_code': 'INSUFFICIENT_FUNDS'}, 'c', 's')])

    def test_the_example_config_ships_the_check_disabled(self):
        example = json.loads((LEAN.parent / 'config' / 'lean' / 'lean.example.json').read_text())
        self.assertEqual(example['route_check'], {'enabled': False})

    def test_load_config_refuses_a_malformed_route_check(self):
        from lean import __main__ as entry
        raw = json.loads((LEAN.parent / 'config' / 'lean' / 'lean.example.json').read_text())
        with tempfile.TemporaryDirectory() as d:
            for value, ok in (({}, True), ({'enabled': True}, True), ({'enabled': 'yes'}, False), ([], False), ('on', False), ({'enable': True}, False)):
                path = Path(d) / 'lean.json'
                path.write_text(json.dumps({**raw, 'route_check': value, 'strategy_config': str(LEAN.parent / 'config' / 'lean' / 'strategy-default.json')}))
                with self.subTest(value=value):
                    if ok:
                        self.assertEqual(entry.load_config(path)['route_check'], value)
                    else:
                        with self.assertRaises(entry.ConfigError):
                            entry.load_config(path)


def forbidden_hits(source, filename='<source>'):
    """THE walker (used on lean/ and by its own self-test): [(file, line, what)] for every CALL of, ATTRIBUTE access to or IMPORT of a
    signing / broadcasting primitive. Parameters, keyword arguments, local names, string constants and docstrings are NOT hits, with
    one exception: a forbidden RPC method name passed as a string argument of a call (``rpc('sendTransaction', ...)``)."""
    names = NeverSignsOrSends.FORBIDDEN_NAMES
    modules = NeverSignsOrSends.FORBIDDEN_MODULES

    def bad_module(name):
        return any(name == m or name.startswith(m + '.') for m in modules)
    hits = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in names:
                hits.append((filename, node.lineno, 'call:' + func.id))
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value in names:
                    hits.append((filename, node.lineno, 'call-argument:' + arg.value))
        elif isinstance(node, ast.Attribute) and node.attr in names:
            hits.append((filename, node.lineno, 'attribute:' + node.attr))
        elif isinstance(node, ast.Import):
            hits += [(filename, node.lineno, 'import:' + a.name) for a in node.names if bad_module(a.name) or a.name.split('.')[-1] in names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ''
            hits += [(filename, node.lineno, 'import:%s.%s' % (module, a.name)) for a in node.names
                     if bad_module(module) or bad_module(module + '.' + a.name) or a.name in names]
    return hits


class NeverSignsOrSends(unittest.TestCase):
    FORBIDDEN_NAMES = {'sign', 'sign_message', 'sign_transaction', 'signTransaction', 'send_transaction', 'sendTransaction',
                       'send_raw_transaction', 'sendRawTransaction', 'send_and_confirm', 'Keypair', 'SigningKey', 'secret_key',
                       'private_key', 'simulateTransaction', 'simulate_transaction'}
    FORBIDDEN_MODULES = ('solders.keypair', 'nacl', 'cryptography.hazmat.primitives.asymmetric.ed25519', 'solana.keypair', 'solana.rpc')

    def test_no_signing_or_broadcast_primitive_anywhere_in_lean(self):
        files = sorted(LEAN.rglob('*.py'))
        self.assertGreater(len(files), 5)
        found = [hit for path in files for hit in forbidden_hits(path.read_text(), path.name)]
        self.assertEqual(found, [])

    def test_the_walker_fires_on_every_kind_of_offender(self):
        offenders = {'call': 'sign(tx)', 'method call': 'client.send_transaction(tx)', 'camel call': 'rpc.sendTransaction(tx)',
                     'attribute read': 'key = kp.secret_key', 'attribute on call result': 'make().private_key',
                     'import': 'import nacl.signing', 'from import': 'from solders.keypair import Keypair',
                     'from package import module': 'from solders import keypair', 'from import of a primitive': 'from foo import Keypair',
                     'aliased import': 'import solana.rpc.api as api', 'method name as call argument': "helius.rpc('sendTransaction', [tx])",
                     'method name as keyword-less argument': "post('simulateTransaction', body)", 'bare constructor': 'k = Keypair()'}
        for name, source in offenders.items():
            with self.subTest(name):
                self.assertTrue(forbidden_hits(source), source)

    def test_the_walker_ignores_parameters_keywords_locals_strings_and_docstrings(self):
        benign = {'parameter named sign': 'def f(sign, flag=False):\n    return sign',
                  'keyword argument named sign': 'f(sign=True, send_transaction=False)',
                  'local variable': 'sign = 1\nprint(sign)', 'lambda parameter': 'g = lambda sign: sign',
                  'string in a deny-list literal': "BLOCKED = {'sendTransaction', 'sign'}",
                  'docstring': 'def f():\n    """never calls sign() or send_transaction"""',
                  'string comparison': "ok = method == 'sendTransaction'", 'signature words': 'signatures = get_signatures()',
                  'safe import': 'from lean.providers import ProviderError', 'async parameter': 'async def f(*, Keypair=None):\n    pass'}
        for name, source in benign.items():
            with self.subTest(name):
                self.assertEqual(forbidden_hits(source), [], source)


if __name__ == '__main__':
    unittest.main()
