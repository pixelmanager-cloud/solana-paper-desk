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
        facts, text = R.analyze_error(ProviderError('HTTP_400', False, raw=b'{"errorCode":"COULD_NOT_FIND_ANY_ROUTE"}'))
        self.assertEqual((facts['buildable'], facts['error_code'], facts['transient']), (False, 'COULD_NOT_FIND_ANY_ROUTE', False))
        facts, _ = R.analyze_error(ProviderError('TIMEOUT', True))
        self.assertEqual((facts['error_code'], facts['transient']), ('TIMEOUT', True))


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
        self.assertIsNone(checker.on_fill('entry', 'M', ref))
        self.assertIsNone(checker.on_quote_error('entry', 'M', ProviderError('HTTP_400', False)))
        self.assertEqual(self.rows(), [])
        with self.assertRaises(ValueError):
            R.RouteChecker(self.store, {'enabled': True, 'enable': True})

    def test_records_routable_and_not_routable_with_side_and_never_signed_or_sent(self):
        checker = R.RouteChecker(self.store, {'enabled': True})
        checker.on_fill('entry', 'M1', self.store.add_observation('quote:buy', build()))
        checker.on_fill('exit', 'M1', self.store.add_observation('quote:exit', build(routePlan=[])))
        checker.on_quote_error('exit', 'M2', ProviderError('HTTP_400', False, raw=b'{"errorCode":"COULD_NOT_FIND_ANY_ROUTE"}'))
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
        checker.on_quote_error('entry', 'M1', error)
        checker.on_quote_error('entry', 'M2', error)
        checker.on_fill('entry', 'M3', self.store.add_observation('quote:buy', build()))
        self.assertEqual([r['action'] for r in self.rows()], ['UNSUPPORTED'])          # once; nothing after it
        self.assertFalse(checker.enabled)
        again = R.RouteChecker(self.store, {'enabled': True})
        self.assertFalse(again.enabled)                                                   # persisted, not re-probed
        self.assertTrue(R.summary(self.rows())['unsupported'])

    def test_malformed_stored_observation_is_recorded_not_raised(self):
        checker = R.RouteChecker(self.store, {'enabled': True})
        checker.on_fill('entry', 'M', 999999)                                             # no such observation
        self.assertEqual([r['action'] for r in self.rows()], ['NOT_ROUTABLE'])


class NeverSignsOrSends(unittest.TestCase):
    FORBIDDEN_NAMES = {'sign', 'sign_message', 'sign_transaction', 'signTransaction', 'send_transaction', 'sendTransaction',
                       'send_raw_transaction', 'sendRawTransaction', 'send_and_confirm', 'Keypair', 'SigningKey', 'secret_key',
                       'private_key', 'simulateTransaction', 'simulate_transaction'}
    FORBIDDEN_MODULES = ('solders.keypair', 'nacl', 'cryptography.hazmat.primitives.asymmetric.ed25519', 'solana.keypair', 'solana.rpc')

    def test_no_signing_or_broadcast_primitive_anywhere_in_lean(self):
        files = sorted(LEAN.rglob('*.py'))
        self.assertGreater(len(files), 5)
        found = []
        for path in files:
            tree = ast.parse(path.read_text(), filename=str(path))
            docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                          if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
                          and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Name):
                    names = [node.id]
                elif isinstance(node, ast.Attribute):
                    names = [node.attr]
                elif isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                    names = [node.name]
                elif isinstance(node, ast.arg):
                    names = [node.arg]
                elif isinstance(node, ast.alias):
                    names = [node.name.split('.')[-1], node.name]
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    names = [node.value] if node.value in self.FORBIDDEN_NAMES else []
                for name in names:
                    if name in self.FORBIDDEN_NAMES or any(name == m or name.startswith(m + '.') for m in self.FORBIDDEN_MODULES):
                        found.append((path.name, getattr(node, 'lineno', 0), name))
                if isinstance(node, ast.ImportFrom) and node.module and any(node.module == m or node.module.startswith(m + '.') for m in self.FORBIDDEN_MODULES):
                    found.append((path.name, node.lineno, node.module))
        self.assertEqual(found, [])

    def test_the_scan_really_detects_what_it_forbids(self):
        # the same walker on a synthetic offender must fire (guards against a vacuous scan)
        tree = ast.parse('from solders.keypair import Keypair\nclient.send_transaction(tx)\nx = "sendTransaction"\n')
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in self.FORBIDDEN_NAMES:
                hits.append(node.attr)
            if isinstance(node, ast.alias) and node.name in self.FORBIDDEN_NAMES:
                hits.append(node.name)
            if isinstance(node, ast.Constant) and node.value in self.FORBIDDEN_NAMES:
                hits.append(node.value)
        self.assertEqual(sorted(hits), ['Keypair', 'sendTransaction', 'send_transaction'])


if __name__ == '__main__':
    unittest.main()
