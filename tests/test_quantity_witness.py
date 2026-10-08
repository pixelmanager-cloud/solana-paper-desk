"""Original public raw effects plus explicitly synthetic transport/unit attacks."""
import base64
import copy
import hashlib
import json
from decimal import Decimal, localcontext, Inexact, Rounded, Overflow, Underflow
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desk.evidence import EvidenceStore
from desk.model import digest
from desk.quantity import quantity_witness, diagnostic_quantity
from desk.replay_sell import replay_sell
from desk.security import TOKEN_PROGRAM, base58, sellability_gate
from desk.simulate import simulate_sell
from tests import test_simulate as sell_fixtures
from tests import test_roundtrip as roundtrip_fixtures

ROOT = Path(__file__).resolve().parents[1]


class QuantityWitnessTests(unittest.TestCase):
    def setUp(self):
        self.public = json.loads((ROOT / 'fixtures/mainnet-roundtrip-simulation.json').read_text())
        r = self.public['result']; s = r['sequence']; row = s['result']['value']['transactionResults'][1]
        self.mint = r['mint']; self.wallet = r['wallet']; self.amount = r['sell_quantity_raw']
        self.tx = s['transaction_hashes'][1]; self.keys = self.public['legs'][1]['keys']
        self.source = {'kind': 'sequence_pre_execution_accounts', 'keys': s['watch'],
                       'result': row, 'slot': s['slot'], 'transaction_hash': self.tx}
        self.sim = {**row, 'accounts': [row['postExecutionAccounts'][s['watch'].index(k)] for k in self.keys]}

    def witness(self, amount=None, human=None):
        return quantity_witness(self.mint, self.wallet, self.amount if amount is None else amount,
            mint_source=self.source, simulation=self.sim, keys=self.keys,
            transaction_hash=self.tx, human_quantity=human)

    def test_original_public_raw_debit_is_exact_human_quantity_not_approval(self):
        before = copy.deepcopy(self.public)
        result = self.witness(human='2.523452')
        self.assertEqual((result['raw_u64'], result['mint_decimals'], result['human_decimal']),
                         ('2523452', 6, '2.523452'))
        self.assertEqual(result['transaction_hash'], self.tx)
        self.assertEqual(result['mint_source_hash'], digest(self.source))
        self.assertEqual(result['simulation_hash'], digest(self.sim))
        key = result.pop('witness_hash'); self.assertEqual(key, digest(result))
        self.assertFalse(result['eligible_for_trading']); self.assertFalse(result['transaction_policy_ok'])
        self.assertEqual(self.public, before)
        self.assertEqual(sellability_gate({'sellability':result,'mint':self.mint,'taker':self.wallet,
            'provenance':'SYNTHETIC_TEST_ONLY','ts':0},Decimal('2.523452')),['SELL_SIMULATION_REQUIRED'])

    def test_raw_u64_and_fractional_or_float_inputs_reject(self):
        for amount in (True, 1.0, Decimal('1'), '1.0', '1e0', '-1', '01', '１', str(2**64), -1, 0):
            with self.subTest(amount=amount), self.assertRaises(ValueError): self.witness(amount)
        with self.assertRaisesRegex(ValueError, 'EXACT_RAW_DEBIT'): self.witness(int(self.amount)+1)

    def test_human_mismatch_and_floats_reject(self):
        for human in ('2.523453', '.2523452', 'NaN', 'Infinity', 2.523452, True):
            with self.subTest(human=human), self.assertRaises(ValueError): self.witness(human=human)

    def test_missing_conflicting_and_out_of_range_decimals_reject(self):
        for value in (None, True, -1, 256, 7):
            with self.subTest(value=value):
                self.setUp()
                row = next(r for r in self.sim['preTokenBalances'] if r['mint'] == self.mint)
                row['uiTokenAmount']['decimals'] = value
                with self.assertRaisesRegex(ValueError, 'UNIT_CONFLICT'): self.witness()

    def test_mint_bytes_units_program_and_state_tampering_reject(self):
        for attack in ('decimals', 'initialized', 'authority', 'program', 'identity', 'transaction'):
            with self.subTest(attack=attack):
                self.setUp(); at = self.source['keys'].index(self.mint)
                account = self.source['result']['preExecutionAccounts'][at]
                raw = bytearray(base64.b64decode(account['data'][0]))
                if attack == 'decimals': raw[44] = 7
                elif attack == 'initialized': raw[45] = 0
                elif attack == 'authority': raw[:4] = (1).to_bytes(4, 'little')
                elif attack == 'program': account['owner'] = 'unknown'
                elif attack == 'identity': self.source['keys'][at] = base58(bytes([19])*32)
                else: self.source['transaction_hash'] = '0'*64
                account['data'][0] = base64.b64encode(raw).decode()
                with self.assertRaises(ValueError): self.witness()

    def test_missing_owner_duplicate_index_program_and_human_metadata_reject(self):
        for attack in ('owner', 'duplicate', 'program', 'human', 'amount', 'missing'):
            with self.subTest(attack=attack):
                self.setUp()
                row = next(r for r in self.sim['preTokenBalances'] if r['owner'] == self.wallet and r['mint'] == self.mint)
                if attack == 'owner': del row['owner']
                elif attack == 'duplicate': self.sim['preTokenBalances'].append(copy.deepcopy(row))
                elif attack == 'program': row['programId'] = 'unknown'
                elif attack == 'human': row['uiTokenAmount']['uiAmountString'] = '1'
                elif attack == 'amount': row['uiTokenAmount']['amount'] = '1.5'
                else: del self.sim['preTokenBalances']
                with self.assertRaises((ValueError, KeyError)): self.witness()

    def test_float_ui_amount_is_ignored_without_rounding(self):
        baseline = self.witness()['human_decimal']
        for row in self.sim['preTokenBalances']: row['uiTokenAmount']['uiAmount'] = .1
        self.assertEqual(self.witness()['human_decimal'], baseline)

    def test_extreme_valid_u64_and_u8_decimals_ignore_ambient_precision(self):
        mint = base58(bytes([1])*32); wallet = base58(bytes([2])*32); token = base58(bytes([3])*32)
        for decimals in (0, 6, 19, 255):
            with self.subTest(decimals=decimals):
                raw = bytearray(82); raw[36:44] = (2**64-1).to_bytes(8,'little'); raw[44] = decimals; raw[45] = 1
                account = {'owner':TOKEN_PROGRAM,'executable':False,'data':[base64.b64encode(raw).decode(),'base64']}
                source = {'method':'getMultipleAccounts','params':[[mint],{'encoding':'base64','commitment':'confirmed'}],
                          'result':{'context':{'slot':1},'value':[account]}}
                row = lambda n: {'accountIndex':1,'owner':wallet,'mint':mint,'programId':TOKEN_PROGRAM,
                                  'uiTokenAmount':{'amount':str(n),'decimals':decimals}}
                sim = {'err':None,'preBalances':[1,1],'postBalances':[1,1],
                       'preTokenBalances':[row(2**64-1)],'postTokenBalances':[row(0)]}
                outputs=[]
                for precision in (1, 9, 28, 100):
                    with localcontext() as ctx:
                        ctx.prec=precision; ctx.Emax=9; ctx.Emin=-9
                        for trap in (Inexact,Rounded,Overflow,Underflow): ctx.traps[trap]=True
                        result=quantity_witness(mint,wallet,2**64-1,mint_source=source,
                            simulation=sim,keys=[wallet,token],transaction_hash='a'*64)
                        outputs.append(result)
                self.assertTrue(all(x==outputs[0] for x in outputs))
                expected=Decimal((0,tuple(map(int,str(2**64-1))),-decimals))
                self.assertEqual(Decimal(outputs[0]['human_decimal']),expected)
                self.assertLess(len(outputs[0]['human_decimal']),277)

    def test_returned_mint_state_conflict_cannot_replace_original_units(self):
        account=self.sim['accounts'][self.keys.index(self.mint)]
        raw=bytearray(base64.b64decode(account['data'][0]));raw[44]=7
        account['data'][0]=base64.b64encode(raw).decode()
        with self.assertRaisesRegex(ValueError,'MINT_STATE_CONFLICT'): self.witness()

    def test_older_single_sell_capture_does_not_invent_missing_sources(self):
        record=json.loads((ROOT/'fixtures/mainnet-sell-simulation.json').read_text())
        before=copy.deepcopy(record)
        result=diagnostic_quantity(record['mint'],record['wallet'],record['amount_raw'],
            mint_source=None,simulation=record['simulation'],keys=record['keys'],transaction_hash=None)
        self.assertEqual(result['status'],'UNKNOWN')
        self.assertFalse(result['eligible_for_trading'])
        self.assertEqual(record,before)

    def test_missing_inputs_and_failed_simulation_are_unknown(self):
        self.sim['err'] = {'InstructionError':[0,'failure']}
        result = diagnostic_quantity(self.mint,self.wallet,self.amount,mint_source=self.source,
                                     simulation=self.sim,keys=self.keys,transaction_hash=self.tx)
        self.assertEqual(result['status'],'UNKNOWN')
        self.assertFalse(result['eligible_for_trading'])
        self.assertFalse(result['transaction_policy_ok'])


class QuantityDiagnosticIntegrationTests(unittest.TestCase):
    def sell(self, capture=None):
        fixture = sell_fixtures.SimulationTests(); fixture.setUp()
        def quote(*args):
            response=fixture.quote(*args)
            response['response']['swapInstruction']['accounts'] += [
                {'pubkey':fixture.holding,'isSigner':False,'isWritable':True},
                {'pubkey':fixture.mint,'isSigner':False,'isWritable':False}]
            return response
        def rpc(method,params):
            if method != 'simulateTransaction': return fixture.rpc(method,params)
            fixture.calls.append(method)
            keys=params[1]['accounts']['addresses']
            mint_raw=bytearray(82);mint_raw[36:44]=(10000).to_bytes(8,'little');mint_raw[45]=1
            mint=fixture.account(mint_raw)
            token=fixture.token(); raw=bytearray(base64.b64decode(token['data'][0]));raw[64:72]=(990).to_bytes(8,'little')
            token['data'][0]=base64.b64encode(raw).decode();token['lamports']=2039280
            wallet={'owner':'11111111111111111111111111111111','executable':False,'lamports':101000,'data':['','base64']}
            mint['lamports']=0
            accounts=[wallet if k==fixture.wallet else token if k==fixture.holding else mint if k==fixture.mint else None for k in keys]
            pre=[100000 if k==fixture.wallet else 2039280 if k==fixture.holding else 0 for k in keys]
            post=[a['lamports'] if a else 0 for a in accounts]
            row=lambda n:{'accountIndex':keys.index(fixture.holding),'owner':fixture.wallet,'mint':fixture.mint,
                          'programId':TOKEN_PROGRAM,'uiTokenAmount':{'amount':str(n),'decimals':0}}
            return {'context':{'slot':124},'value':{'err':None,'accounts':accounts,'preBalances':pre,'postBalances':post,
                      'preTokenBalances':[row(1000)],'postTokenBalances':[row(990)],'fee':1}}
        with patch('socket.socket',side_effect=AssertionError('No network')):
            result=simulate_sell(fixture.mint,fixture.wallet,fixture.holding,10,rpc,quote,capture=capture)
        self.assertEqual(fixture.calls,['getMultipleAccounts','getLatestBlockhash','simulateTransaction'])
        return result

    def test_actual_sell_output_capture_and_offline_replay_recompute_witness(self):
        with tempfile.TemporaryDirectory() as directory:
            store=EvidenceStore(Path(directory)/'evidence.sqlite')
            result=self.sell(store.save)
            witness=result['quantity_witness']
            self.assertEqual(witness['status'],'WITNESSED_RAW_QUANTITY')
            self.assertEqual(witness['human_decimal'],'10')
            record=store.load(result['evidence_hash'])
            self.assertEqual(record['quantity_witness'],witness)
            self.assertIn('mint_lookup',record)
            replay=replay_sell(EvidenceStore(store.path,read_only=True),result['evidence_hash'])
            self.assertEqual(replay['quantity_witness'],witness)
            for field in ('signed','submitted','eligible_for_trading','transaction_policy_ok'):
                self.assertFalse(result[field])
            changed=copy.deepcopy(record);changed['quantity_witness']['human_decimal']='.1'
            with self.assertRaisesRegex(ValueError,'quantity witness differs'):
                replay_sell(store,store.save(changed))
            self.assertEqual(store.load(result['evidence_hash']),record)
            for amount in ('10.5',10.5,True):
                fractional=copy.deepcopy(record);fractional['amount_raw']=amount
                with self.assertRaisesRegex(ValueError,'QUANTITY_RAW_INTEGER_REQUIRED'):
                    replay_sell(store,store.save(fractional))

    def test_roundtrip_output_capture_and_compiled_identity_mismatch(self):
        fixture=roundtrip_fixtures.RoundtripTests();fixture.setUp()
        raw=b'explicit-synthetic-quantity-transport'
        sequence=copy.deepcopy(fixture.sequence)
        sequence['transaction_hashes']=[hashlib.sha256(raw).hexdigest()]*2
        legs=[{**leg,'raw':raw} for leg in fixture.p['legs']]
        saved=[]
        with patch('desk.roundtrip.compile_unsigned',side_effect=legs), \
                patch('desk.roundtrip.simulate_sequence',return_value=sequence), \
                patch('socket.socket',side_effect=AssertionError('No network')):
            from desk.roundtrip import simulate_roundtrip
            result=simulate_roundtrip(fixture.r['mint'],fixture.r['wallet'],int(fixture.r['spend_lamports']),
                rpc=fixture.rpc,quote=fixture.quote,capture=saved.append)
        self.assertEqual(fixture.calls,['getMultipleAccounts','getLatestBlockhash'])
        self.assertEqual(result['sell_quantity_witness']['human_decimal'],'2.523452')
        self.assertEqual(result['sell_quantity_witness']['status'],'WITNESSED_RAW_QUANTITY')
        self.assertEqual(saved[0]['result']['sell_quantity_witness'],result['sell_quantity_witness'])
        self.assertIn('mint_lookup',saved[0]);self.assertIn('unsigned_transaction',saved[0]['legs'][1])
        for field in ('signed','submitted','eligible_for_trading','transaction_policy_ok'): self.assertFalse(result[field])
        fixture.setUp()
        with patch('desk.roundtrip.compile_unsigned',side_effect=legs),patch('desk.roundtrip.simulate_sequence',return_value=fixture.sequence):
            result=simulate_roundtrip(fixture.r['mint'],fixture.r['wallet'],int(fixture.r['spend_lamports']),rpc=fixture.rpc,quote=fixture.quote)
        self.assertEqual(result['sell_quantity_witness']['status'],'UNKNOWN')
        self.assertIn('QUANTITY_TRANSACTION_IDENTITY_MISMATCH',result['sell_quantity_witness']['reasons'])
