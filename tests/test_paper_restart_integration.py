"""SYNTHETIC_TEST_ONLY: local restart/restore integration, never live entries.

Complements test_cloud_ledger_restart's serial delivery and fill crash matrix.
This file probes concurrent delivery, multi-outcome blocked exits, real offline
CLI replay/restore, and partial-schema-loss refusal. No provider or signer calls.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from desk.engine import initial_state, transition
from desk.ledger import Ledger
from desk.model import D, digest
from desk.storage import snapshot, verify
from tests.helpers import ROOT, T, config, control, event

CONCURRENT_APPLY = r'''
import json, sys
from desk.engine import initial_state, transition
from desk.ledger import Ledger
ledger = Ledger(sys.argv[1], must_exist=True)
sys.stdin.readline()
try:
    result = ledger.apply(json.loads(sys.argv[3]), json.loads(sys.argv[2]), transition, initial_state)
    print(json.dumps(result))
finally:
    ledger.close()
'''

BLOCKED_CRASH = r'''
import json, os, sys
from desk.engine import initial_state, transition
from desk.ledger import Ledger
ledger = Ledger(sys.argv[1], must_exist=True)
original = ledger.db
class Crash:
    count = 0
    def execute(self, sql, *args):
        result = original.execute(sql, *args)
        if sql.startswith('INSERT INTO outcomes('):
            self.count += 1
            if self.count == 2 and sys.argv[4] == 'second_outcome':
                os._exit(73)
        if sql == 'COMMIT' and sys.argv[4] == 'committed':
            os._exit(73)
        return result
ledger.db = Crash()
ledger.apply(json.loads(sys.argv[3]), json.loads(sys.argv[2]), transition, initial_state)
raise AssertionError('Synthetic crash boundary not reached')
'''


class PaperRestartIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'synthetic-paper.sqlite'
        self.cfg = config()
        self.ledger = Ledger(self.path)
        self.addCleanup(lambda: self.ledger.close())
        # Child interpreters receive no inherited credentials/provider settings.
        self.env = {'PATH': os.defpath, 'PYTHONPATH': str(ROOT)}

    def apply(self, e):
        return self.ledger.apply(e, self.cfg, transition, initial_state)

    def restart(self, path=None):
        self.ledger.close()
        self.path = path or self.path
        self.ledger = Ledger(self.path, must_exist=True)
        self.assertEqual(self.ledger.db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])

    def records(self):
        return {table:self.ledger.db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                for table in ('metadata','events','outcomes','state','raw_events','health')}

    def proof(self, at, quantity):
        return {'kind':'sell_simulation', 'mint':'SYNTHETIC_A', 'wallet':'synthetic-wallet',
                'transaction_hash':'a'*64, 'route_hash':'b'*64, 'slot':1,
                'observed_at':at, 'quantity_tokens':quantity, 'simulation_ok':True,
                'transaction_policy_ok':True, 'wallet_account_ok':True, 'net_proceeds_sol':'0.005'}

    def audit_saved_history(self):
        """Read-only deterministic replay plus accounting invariants, no repair."""
        state = initial_state(self.cfg)
        expected = []
        for identity, payload, fingerprint in self.ledger.db.execute(
                'SELECT event_id,payload,payload_hash FROM events ORDER BY seq'):
            observation = json.loads(payload)
            self.assertEqual(digest(observation), fingerprint)
            state, outcomes = transition(state, observation, self.cfg)
            actual = [json.loads(r[0]) for r in self.ledger.db.execute(
                'SELECT payload FROM outcomes WHERE event_id=? ORDER BY seq', (identity,))]
            self.assertEqual(actual, outcomes)
            expected.extend(outcomes)
        report = self.ledger.report()
        self.assertEqual(report['state'], state)
        self.assertEqual(report['outcomes'], expected)
        buys = [o for o in expected if o.get('side') == 'buy']
        sells = [o for o in expected if o.get('side') == 'sell']
        costs = sum((D(o['amount_sol'])+D(o['fee_sol']) for o in buys), D(0))
        receipts = sum((D(o['proceeds_sol']) for o in sells), D(0))
        basis = sum((D(p['cost_left']) for p in state['positions'].values()), D(0))
        self.assertAlmostEqual(D(state['cash']), D(self.cfg['initial_equity_sol'])-costs+receipts)
        self.assertAlmostEqual(D(state['realized_pnl']), receipts-costs+basis)
        for mint in {o['mint'] for o in buys}:
            bought = sum((D(o['quantity']) for o in buys if o['mint']==mint), D(0))
            sold = sum((D(o['quantity']) for o in sells if o['mint']==mint), D(0))
            remaining = D(state['positions'].get(mint, {}).get('qty', '0'))
            self.assertAlmostEqual(bought-sold, remaining)
        return report

    def cli(self, *args, clock=None):
        command = [sys.executable, '-m', 'desk', *map(str,args)]
        if clock is not None:
            # Execute the actual package entrypoint with only its local clock
            # replaced; production CLI does not expose a timestamp override.
            script = """import runpy,sys
from unittest.mock import patch
now=int(sys.argv[1]);sys.argv=['desk',*sys.argv[2:]]
with patch('desk.monitor.time.time',return_value=now):
    runpy.run_module('desk',run_name='__main__')
"""
            command = [sys.executable,'-c',script,str(clock),*map(str,args)]
        result = subprocess.run(command, cwd=ROOT,
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_simultaneous_duplicate_entry_partial_and_final_delivery(self):
        for observation in (event(), event(T+5,reserve_sol='160'),
                            event(T+10,danger=True,reserve_sol='160')):
            processes = [subprocess.Popen([sys.executable,'-c',CONCURRENT_APPLY,str(self.path),
                         json.dumps(self.cfg),json.dumps(observation)], cwd=ROOT,env=self.env,
                         stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                         for _ in range(2)]
            try:
                for process in processes:
                    process.stdin.write('\n'); process.stdin.flush()
                results = []
                for process in processes:
                    stdout, stderr = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, 0, stderr)
                    results.append(json.loads(stdout))
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill(); process.communicate(timeout=5)
            self.assertEqual(sum(result == [] for result in results), 1)
            self.assertEqual(sum(o['type']=='fill' for result in results for o in result), 1)
            self.restart()
            before = self.records()
            self.assertEqual(self.apply(observation), [])
            self.assertEqual(before, self.records())
            self.audit_saved_history()
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM events').fetchone()[0], 3)
        self.assertFalse(self.ledger.report()['state']['positions'])

    def test_partial_stale_proof_latch_is_atomic_across_multi_outcome_crash(self):
        for boundary in ('second_outcome','committed'):
            with self.subTest(boundary=boundary):
                path = self.root/f'{boundary}.sqlite'
                self.ledger.close()
                self.path = path
                self.ledger = Ledger(path)
                self.apply(event())
                self.apply(event(T+5,reserve_sol='160'))
                before = self.records()
                position = self.ledger.report()['state']['positions']['SYNTHETIC_A']
                # Current market price, but proof age=11 exceeds exact sell TTL=10.
                observation = event(T+20,danger=True,taker='synthetic-wallet',
                                    sellability=self.proof(T+9,position['qty']))
                result = subprocess.run([sys.executable,'-c',BLOCKED_CRASH,str(path),json.dumps(self.cfg),
                                         json.dumps(observation),boundary],cwd=ROOT,env=self.env,
                                         capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,73,result.stderr)
                self.restart()
                if boundary == 'second_outcome':
                    self.assertEqual(self.records(),before)
                    outcomes = self.apply(observation)
                else:
                    outcomes = self.ledger.report()['outcomes'][-2:]
                    self.assertEqual(self.apply(observation),[])
                self.assertEqual([o['type'] for o in outcomes],['blocked_exit','control'])
                self.assertIn('SELL_PROOF_STALE',outcomes[0]['reasons'])
                self.restart()
                report = self.audit_saved_history()
                self.assertEqual(report['state']['mode'],'EXIT_ONLY')
                for key in ('qty','cost_left','stage','mark_value','mark_at'):
                    self.assertEqual(report['state']['positions']['SYNTHETIC_A'][key],position[key])
                self.apply(control(T+21,'RESUME'))
                self.restart()
                self.assertEqual(self.audit_saved_history()['state']['mode'],'EXIT_ONLY')
                self.assertEqual(sum(o['type']=='fill' for o in self.ledger.report()['outcomes']),2)

    def test_actual_replay_cli_restore_and_net_exit_preserve_original_journal(self):
        entry, partial = event(), event(T+5,reserve_sol='160')
        initial = self.root/'synthetic-events.jsonl'
        initial.write_text('\n'.join(json.dumps(e) for e in (entry,entry,partial,partial))+'\n')
        replay = self.cli('replay','--input',initial,'--db',self.path,'--config',ROOT/'config/paper.json')
        self.assertEqual(sum(o['type']=='fill' for o in replay['outcomes']),2)
        before = self.records()
        backup = self.root/'synthetic-backup.sqlite'
        manifest = snapshot(self.path,backup)
        self.assertTrue(verify(backup,manifest['sha256']))
        restored = self.root/'synthetic-restored.sqlite'
        snapshot(backup,restored)
        self.restart(restored)
        self.assertEqual(self.records(),before)
        self.assertEqual(self.audit_saved_history()['replay_hash'],replay['replay_hash'])
        p = copy.deepcopy(replay['state']['positions']['SYNTHETIC_A'])
        stale = event(T+20,danger=True,taker='synthetic-wallet',sellability=self.proof(T+9,p['qty']))
        input_path = self.root/'synthetic-restored-delivery.jsonl'
        input_path.write_text('\n'.join(json.dumps(e) for e in (entry,partial,stale,stale,control(T+21,'RESUME')))+'\n')
        result = self.cli('replay','--input',input_path,'--db',restored,'--config',ROOT/'config/paper.json')
        self.assertEqual(result['state']['mode'],'EXIT_ONLY')
        self.assertEqual(result['state']['positions']['SYNTHETIC_A']['qty'],p['qty'])
        # Clock outage crosses midnight; it must retain the partial cost basis,
        # stale historical mark and day baseline, without inventing proceeds.
        monitor = self.cli('paper-monitor','--db',restored,'--config',ROOT/'config/paper.json',clock=T+86400)
        self.assertFalse(monitor['automatic_entry_enabled'])
        self.restart()
        state = self.audit_saved_history()['state']
        for key in ('cash','realized_pnl','day','day_start_equity','day_gross_losses'):
            self.assertEqual(state[key],replay['state'][key])
        self.assertEqual(state['positions']['SYNTHETIC_A']['mark_status'],'STALE')
        # Invented exact-size net proceeds are only a synthetic fixture. A fresh
        # full residual exit can close inventory while EXIT_ONLY stays latched.
        final = event(T+86401,danger=True,taker='synthetic-wallet',reserve_sol='160',
                      sellability=self.proof(T+86401,p['qty']))
        input_path.write_text(json.dumps(final)+'\n'+json.dumps(final)+'\n')
        closed = self.cli('replay','--input',input_path,'--db',restored,'--config',ROOT/'config/paper.json')
        self.restart()
        self.assertEqual(self.audit_saved_history(),closed)
        self.assertFalse(closed['state']['positions'])
        self.assertEqual(closed['state']['mode'],'EXIT_ONLY')
        self.assertEqual(D(closed['outcomes'][-1]['proceeds_sol']),D('0.005'))
        for table in ('events','outcomes'):
            self.assertEqual(self.records()[table][:len(before[table])],before[table])
        # Recovery/exit on the restored copy cannot rewrite the original ledger.
        original = Ledger(self.root/'synthetic-paper.sqlite',must_exist=True)
        try:
            self.assertEqual(original.report(),replay)
        finally:
            original.close()

    def test_missing_committed_checkpoint_refuses_duplicate_and_new_fills(self):
        self.apply(event())
        self.apply(event(T+5,reserve_sol='160'))
        self.ledger.db.execute('DELETE FROM state')  # Synthetic partial schema loss.
        self.restart()
        before = self.records()
        for observation in (event(),event(T+60,mint='SYNTHETIC_B'),control(T+61,'RESUME')):
            with self.subTest(event_id=observation['event_id']):
                with self.assertRaisesRegex(ValueError,'checkpoint missing'):
                    self.apply(observation)
                self.assertEqual(self.records(),before)

    def test_missing_committed_config_identity_cannot_rebind_costs_after_restart(self):
        self.apply(event())
        self.ledger.db.execute("DELETE FROM metadata WHERE key='config_hash'")
        self.restart()
        before = self.records()
        self.cfg['fixed_fee_sol'] = '.0002'
        with self.assertRaisesRegex(ValueError,'config.*unversioned'):
            self.apply(event(T+5,danger=True))
        self.assertEqual(self.records(),before)


if __name__ == '__main__':
    unittest.main()
