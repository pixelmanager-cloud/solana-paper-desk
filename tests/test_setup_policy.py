import base64, copy, json, unittest
from pathlib import Path
from desk.setup_policy import check_sell_setup
from desk.instructions import ATA, SYSTEM, inventory
from desk.security import TOKEN_PROGRAM, base58
from desk.providers import SOL
from desk.programs import unbase58
from solders.pubkey import Pubkey


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.wallet = base58(bytes([1])*32)
        self.target = str(Pubkey.find_program_address([unbase58(self.wallet),unbase58(TOKEN_PROGRAM),unbase58(SOL)],Pubkey.from_string(ATA))[0])
        self.rows = [self.row(ATA,b'\x01',[self.wallet,self.target,self.wallet,SOL,SYSTEM,TOKEN_PROGRAM],'1',1,None,None)]
        self.rows += [self.row(TOKEN_PROGRAM,b'\x15',[SOL],'1.0'),
                      self.row(SYSTEM,bytes(4)+(100).to_bytes(8,'little')+(165).to_bytes(8,'little')+unbase58(TOKEN_PROGRAM),[self.wallet,self.target],'1.1'),
                      self.row(TOKEN_PROGRAM,b'\x16',[self.target],'1.2'),
                      self.row(TOKEN_PROGRAM,b'\x12'+unbase58(self.wallet),[self.target,SOL],'1.3')]
    def row(self,program,raw,accounts,path,height=2,parent='1',caller=ATA):
        return dict(program=program,instruction=path,stack_height=height,parent_instruction=parent,parent_program=caller,accounts=accounts,data_base64=base64.b64encode(raw).decode(),reasons=[])
    def evidence(self):return dict(stack_metadata_verified=True,inventory_checks_passed=True,reasons=[],instructions=self.rows)
    def check(self):return check_sell_setup(self.evidence(),self.wallet)
    def test_complete_creation_and_detached_receipts(self):
        r=self.check();self.assertTrue(r['passed'],r);self.assertFalse(r['full_route_policy_passed']);self.assertFalse(r['runtime_cpi_success_verified'])
        self.rows[1]['accounts'].append(self.wallet);self.assertEqual(r['checked_instructions'][0]['accounts'],[SOL])
    def test_original_initialize_before_create_reproducer(self):
        self.rows=[self.rows[0],self.rows[2],self.rows[4]]
        self.rows[1]['instruction']='1.1';self.rows[2]['instruction']='1.0';self.rows[1:]=reversed(self.rows[1:])
        self.assertFalse(self.check()['passed'])
    def test_complete_but_reordered_semantics_rejected(self):
        self.rows[2]['instruction'],self.rows[4]['instruction']='1.3','1.1'
        self.assertFalse(self.check()['passed'])
    def test_input_array_order_is_not_execution_order(self):
        self.rows=list(reversed(self.rows));self.assertTrue(self.check()['passed'])
    def test_missing_each_duplicate_extra_and_gapped_paths(self):
        original=copy.deepcopy(self.rows)
        for i in range(1,5):
            self.rows=copy.deepcopy(original);self.rows.pop(i);self.assertFalse(self.check()['passed'])
        self.rows=copy.deepcopy(original)+[copy.deepcopy(original[1])];self.assertFalse(self.check()['passed'])
        self.rows=copy.deepcopy(original);self.rows[-1]['instruction']='1.4';self.assertFalse(self.check()['passed'])
        self.rows=copy.deepcopy(original)+[self.row(TOKEN_PROGRAM,b'\x16',[self.target],'1.4')];self.assertFalse(self.check()['passed'])
    def test_wrong_binding_bytes_and_direct_parent(self):
        original=copy.deepcopy(self.rows)
        for index,field,value in [(0,'accounts',[]),(2,'accounts',[self.wallet,self.wallet]),(4,'data_base64',base64.b64encode(b'\x12'+unbase58(SOL)).decode()),(3,'parent_program',TOKEN_PROGRAM),(3,'parent_instruction','0'),(3,'stack_height',3),(1,'data_base64','FQAA'),(3,'accounts',[SOL])]:
            self.rows=copy.deepcopy(original);self.rows[index][field]=value;self.assertFalse(self.check()['passed'])
    def test_noop_and_funded_allocation_branches_unsupported(self):
        original=copy.deepcopy(self.rows);self.rows=self.rows[:1];self.assertIn('SETUP_EXISTING_ATA_NOOP_UNSUPPORTED',self.check()['reasons'])
        self.rows=copy.deepcopy(original);self.rows[2]=self.row(SYSTEM,(8).to_bytes(4,'little')+(165).to_bytes(8,'little'),[self.target],'1.1');self.assertFalse(self.check()['passed'])
    def test_unknown_child_and_incomplete_enumeration(self):
        self.rows.append(self.row(ATA,b'\x01',[self.target],'1.4'));self.assertFalse(self.check()['passed'])
        for reason in ('UNSUPPORTED_PARSED_INSTRUCTION','UNSUPPORTED_INNER_INSTRUCTION_ENCODING','INNER_INSTRUCTIONS_UNAVAILABLE'):
            e=self.evidence();e.update(reasons=[reason],inventory_checks_passed=False);self.assertFalse(check_sell_setup(e,self.wallet)['passed'])
    def test_close_binding_and_outside_operations(self):
        self.rows.append(self.row(TOKEN_PROGRAM,b'\x09',[self.target,self.wallet,self.wallet],'3',1,None,None));self.assertTrue(self.check()['passed'])
        self.rows[-1]['accounts'][1]=SOL;self.assertFalse(self.check()['passed'])
    def test_unchanged_public_capture_through_production_inventory(self):
        path=Path('fixtures/mainnet-sell-simulation.json');raw=path.read_bytes();r=json.loads(raw)
        observed=inventory(r['outer'],r['simulation'],r['keys'],r['wallet']);result=check_sell_setup(observed,r['wallet'])
        self.assertTrue(result['passed'],result);self.assertEqual([x['instruction'] for x in result['checked_instructions'][:4]],['1.0','1.1','1.2','1.3'])
        self.assertEqual(result['unresolved_inventory_reasons'],['UNSUPPORTED_ROUTE_PROGRAM']);self.assertFalse(result['full_route_policy_passed'])
        self.assertEqual(path.read_bytes(),raw)
        changed=copy.deepcopy(observed);a=next(x for x in changed['instructions'] if x['instruction']=='1.1');b=next(x for x in changed['instructions'] if x['instruction']=='1.3');a['instruction'],b['instruction']=b['instruction'],a['instruction']
        self.assertFalse(check_sell_setup(changed,r['wallet'])['passed'])
        for kind in ('approve','unknownOperation'):
            saved=copy.deepcopy(r)
            group=next(g for g in saved['simulation']['innerInstructions'] if g['index']==1)
            group['instructions'].append(dict(programId=TOKEN_PROGRAM,stackHeight=2,parsed=dict(type=kind,info=dict(source=result['wallet_wsol_account'],owner=r['wallet'],delegate=r['wallet'],amount='1'))))
            incomplete=inventory(saved['outer'],saved['simulation'],saved['keys'],saved['wallet'])
            self.assertFalse(check_sell_setup(incomplete,r['wallet'])['passed'])
