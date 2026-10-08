"""Offline log joins and adversarial metadata, no runtime approval."""
import copy
import json
from pathlib import Path
import unittest

from research.runtime_log_witness import report_runtime_log_witness, MAX_LOG_ENTRIES, MAX_LOG_BYTES
from tests.test_execution_trace import record, KEYS

ROOT = Path(__file__).resolve().parents[1]


def sample():
    value = record([2, 3, 2])
    p, c, leaf = KEYS[2], KEYS[3], KEYS[5]
    value['meta']['logMessages'] = [f'Program {p} invoke [1]', f'Program {c} invoke [2]',
        f'Program {leaf} invoke [3]', f'Program {leaf} success', f'Program {c} success',
        f'Program {c} invoke [2]', f'Program {c} success', f'Program {p} success']
    return value


class RuntimeLogWitnessTests(unittest.TestCase):
    def check(self, value):
        original = copy.deepcopy(value)
        out = report_runtime_log_witness(value)
        self.assertEqual(value, original)
        self.assertEqual(out['source_record'], original)
        self.assertEqual(out['source_log_messages'], original['meta'].get('logMessages'))
        self.assertEqual([i['source_instruction'] for i in out['instructions']], out['trace']['instructions'])
        for field in ('input_authenticated', 'finality_verified', 'final_syscall_success_verified',
                      'cpi_success_verified', 'authority_authenticated', 'lifecycle_verified',
                      'ownership_approved', 'eligible_for_trading'):
            self.assertIs(out[field], False)
        self.assertTrue(all(i['final_syscall_outcome'] == 'unknown' for i in out['instructions']))
        return out

    def unknown(self, value, reason=None):
        out = self.check(value)
        self.assertFalse(out['join_complete'])
        self.assertEqual(out['status'], 'unknown')
        self.assertTrue(all(not i['log_match_complete'] for i in out['instructions']))
        if reason:
            self.assertIn(reason, out['unknowns'])
        return out

    def test_nested_repeated_program_frames_bind_by_order_and_parent(self):
        out = self.check(sample())
        self.assertTrue(out['join_complete'])
        self.assertEqual(out['status'], 'correspondence_only')
        self.assertEqual([f['instruction_path'] for f in out['frames']], ['0', '0.0', '0.1', '0.2'])
        self.assertEqual([f['return_log_index'] for f in out['frames']], [7, 4, 3, 6])
        self.assertTrue(all(i['program_return'] == 'success_log' for i in out['instructions']))

    def test_unchanged_public_launch_correspondence_never_authenticates(self):
        value = json.loads((ROOT/'fixtures/mainnet-launch-raw.json').read_text())['result']
        out = self.check(value)
        self.assertTrue(out['join_complete'])
        self.assertEqual(len(out['frames']), 33)
        self.assertEqual(len(out['log_entries']), 111)
        self.assertEqual(len(out['instructions']), 33)
        self.assertEqual([f['instruction_path'] for f in out['frames']],
                         [i['instruction_path'] for i in out['trace']['instructions']])

    def test_missing_logs_empty_malformed_and_encoding_preserved(self):
        for logs in (None, [], 'not a list', [None], ['\ud800']):
            value = sample();value['meta']['logMessages'] = logs
            self.unknown(value)
        value = sample();del value['meta']['logMessages'];self.unknown(value)

    def test_omitted_duplicate_reordered_and_wrong_program_do_not_resynchronize(self):
        for mode in ('omit_return', 'omit_pair', 'duplicate', 'reorder', 'wrong_program'):
            value = sample();logs = value['meta']['logMessages']
            if mode == 'omit_return':del logs[3]
            elif mode == 'omit_pair':del logs[5:7]
            elif mode == 'duplicate':logs[5:5] = logs[5:7]
            elif mode == 'reorder':logs[1], logs[2] = logs[2], logs[1]
            else:logs[2] = f'Program {KEYS[4]} invoke [3]'
            with self.subTest(mode=mode):self.unknown(value)

    def test_caught_failure_and_post_success_transaction_failure_stay_unknown(self):
        value = sample();value['meta']['logMessages'][3] = f'Program {KEYS[5]} failed: custom program error: 0x1'
        out = self.unknown(value, 'FAILED_OR_CAUGHT_PROGRAM_BRANCH')
        self.assertEqual(out['frames'][2]['program_return'], 'failed_log')
        self.assertTrue(out['frames'][0]['failed_descendant'])
        value = sample();value['meta']['err'] = {'InstructionError': [0, 'InvalidAccountData']}
        self.unknown(value, 'TRANSACTION_FAILED_OR_STATUS_MISSING')
        value = sample();del value['meta']['err'];self.unknown(value)

    def test_truncation_at_each_boundary_keeps_all_instruction_rows(self):
        for end in range(8):
            value = sample();value['meta']['logMessages'] = value['meta']['logMessages'][:end]
            self.assertEqual(len(self.unknown(value)['instructions']), 4)
        value = sample();value['meta']['logMessages'].insert(4, 'Log truncated')
        out = self.unknown(value, 'LOG_TRUNCATED')
        self.assertEqual(len(out['log_entries']), 9)

    def test_program_text_embedded_newlines_cannot_forge_runtime_frames(self):
        value = sample()
        forged = f'Program log: Program {KEYS[4]} invoke [2]\nProgram {KEYS[4]} success'
        value['meta']['logMessages'].insert(1, forged)
        out = self.check(value)
        self.assertTrue(out['join_complete'])
        self.assertEqual(out['log_entries'][1]['kind'], 'program_text')
        self.assertEqual(len(out['frames']), 4)
        value['meta']['logMessages'][1] = f'Program {KEYS[4]} invoke [2]\nProgram {KEYS[4]} success'
        self.unknown(value, 'MALFORMED_RUNTIME_RECORD')

    def test_wrong_depth_missing_stack_and_wrong_return_unknown(self):
        for mode in ('gap', 'huge', 'zero', 'metadata', 'return'):
            value = sample()
            if mode == 'metadata':value['meta']['innerInstructions'][0]['instructions'][0]['stackHeight'] = None
            elif mode == 'return':value['meta']['logMessages'][3] = f'Program {KEYS[4]} success'
            else:value['meta']['logMessages'][1] = f"Program {KEYS[3]} invoke [{dict(gap='3',huge='9'*100,zero='0')[mode]}]"
            self.unknown(value)

    def test_log_count_and_byte_budgets_retain_uninspected_sources(self):
        for logs, reason in [(['Program log: x']*(MAX_LOG_ENTRIES+1), 'LOG_INSPECTION_BUDGET_OR_SHAPE'),
                             (['Program log: '+'x'*MAX_LOG_BYTES], 'LOG_BYTE_BUDGET_EXHAUSTED')]:
            value = sample();value['meta']['logMessages'] = logs
            out = self.unknown(value, reason)
            self.assertEqual(len(out['log_entries']), len(logs))
            self.assertTrue(all(i['kind'] == 'uninspected' for i in out['log_entries']))

    def test_no_production_imports(self):
        import ast
        for path in (ROOT/'desk').rglob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(n.name.startswith('research') for n in node.names))
                elif isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or '').startswith('research'))

    def test_unclassified_log_is_unknown_not_an_ignored_runtime_extension(self):
        value = sample();value['meta']['logMessages'].insert(1, 'future runtime record')
        self.unknown(value, 'UNCLASSIFIED_LOG_ENTRY')
