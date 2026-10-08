import base64,copy,unittest
from desk.setup_policy import check_sell_setup
from desk.instructions import ATA,SYSTEM
from desk.security import TOKEN_PROGRAM,base58
from desk.providers import SOL
from desk.programs import unbase58
class SetupTests(unittest.TestCase):
    def setUp(self):
        self.wallet=base58(bytes([1])*32);self.target=check_sell_setup({'stack_metadata_verified':True,'instructions':[]},self.wallet)['wallet_wsol_account']
        self.rows=[{'program':ATA,'instruction':'1','stack_height':1,'accounts':[],'data_base64':'AQ=='}]
        self.rows += [self.row(SYSTEM,bytes(4)+(100).to_bytes(8,'little')+(165).to_bytes(8,'little')+unbase58(TOKEN_PROGRAM),[self.wallet,self.target]),self.row(TOKEN_PROGRAM,b'\x12'+unbase58(self.wallet),[self.target,SOL])]
    def row(self,program,raw,accounts):return {'program':program,'instruction':'1.0','stack_height':2,'parent_instruction':'1','parent_program':ATA,'accounts':accounts,'data_base64':base64.b64encode(raw).decode()}
    def check(self):return check_sell_setup({'stack_metadata_verified':True,'instructions':self.rows},self.wallet)
    def test_bound_setup_is_only_partial_policy(self):
        r=self.check();self.assertTrue(r['passed']);self.assertFalse(r['full_route_policy_passed'])
    def test_unrelated_created_account_rejected(self):
        self.rows[1]['accounts'][1]=self.wallet;self.assertFalse(self.check()['passed'])
    def test_changed_initialization_owner_rejected(self):
        self.rows[2]['data_base64']=base64.b64encode(b'\x12'+unbase58(SOL)).decode();self.assertFalse(self.check()['passed'])
    def test_inner_close_cannot_hide_under_setup(self):
        self.rows.append(self.row(TOKEN_PROGRAM,b'\x09',[self.target,self.wallet,self.wallet]));self.assertFalse(self.check()['passed'])
    def test_duplicate_create_and_incomplete_init_rejected(self):
        self.rows.append(copy.deepcopy(self.rows[1]));self.assertFalse(self.check()['passed']);self.rows=self.rows[:2];self.assertFalse(self.check()['passed'])
    def test_wrong_caller_or_outer_native_transfer_rejected(self):
        self.rows[1]['parent_program']=TOKEN_PROGRAM;self.assertFalse(self.check()['passed'])
    def test_existing_ata_needs_no_initialization(self):
        self.rows=self.rows[:1];self.assertTrue(self.check()['passed'])
