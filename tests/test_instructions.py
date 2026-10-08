import base64,unittest
from desk.instructions import inventory,TOKEN_PROGRAM,SYSTEM,COMPUTE,JUPITER
from desk.security import base58
WALLET=base58(bytes([2])*32);HOLDING=base58(bytes([3])*32);OTHER=base58(bytes([4])*32)
class InstructionTests(unittest.TestCase):
    def ix(self,program,data,accounts=()):
        return {'programId':program,'data':base64.b64encode(data).decode(),
                'accounts':[{'pubkey':a,'isSigner':a==WALLET,'isWritable':True} for a in accounts]}
    def run_inventory(self,outer,inner=()):
        return inventory(outer,{'innerInstructions':list(inner)},[WALLET,HOLDING,OTHER,TOKEN_PROGRAM,SYSTEM,COMPUTE,JUPITER],WALLET)
    def test_transfer_is_inventory_only_not_route_approval(self):
        r=self.run_inventory([self.ix(TOKEN_PROGRAM,b'\x03'+(10).to_bytes(8,'little'),[HOLDING,OTHER,WALLET])])
        self.assertTrue(r['inventory_checks_passed']);self.assertFalse(r['full_route_policy_passed'])
    def test_outer_delegate_approval_rejected(self):
        r=self.run_inventory([self.ix(TOKEN_PROGRAM,b'\x04'+bytes(8),[HOLDING,OTHER,WALLET])])
        self.assertIn('TOKEN_APPROVE_NOT_ALLOWED',r['reasons'])
    def test_hidden_cpi_authority_change_rejected(self):
        outer=[self.ix(JUPITER,bytes(8),[WALLET])]
        inner=[{'index':0,'instructions':[{'programIdIndex':3,'accounts':[1,0], 'data':base58(b'\x06'+bytes(34))}]}]
        self.assertIn('TOKEN_SET_AUTHORITY_NOT_ALLOWED',self.run_inventory(outer,inner)['reasons'])
    def test_missing_inner_metadata_is_not_clean(self):
        r=inventory([self.ix(JUPITER,bytes(8),[WALLET])],{},[WALLET,JUPITER],WALLET)
        self.assertFalse(r['inventory_checks_passed'])
    def test_unexpected_program(self):
        r=self.run_inventory([self.ix(OTHER,b'anything')]);self.assertIn('UNSUPPORTED_ROUTE_PROGRAM',r['reasons'])
    def test_bad_account_index(self):
        with self.assertRaises(ValueError):self.run_inventory([self.ix(JUPITER,bytes(8))],[{'index':0,'instructions':[{'programIdIndex':999,'accounts':[],'data':'1'}]}])
    def test_close_to_attacker(self):
        r=self.run_inventory([self.ix(TOKEN_PROGRAM,b'\x09',[HOLDING,OTHER,WALLET])])
        self.assertIn('TOKEN_CLOSE_TO_EXTERNAL_RECIPIENT',r['reasons'])
    def test_system_assign_rejected(self):
        r=self.run_inventory([self.ix(SYSTEM,(1).to_bytes(4,'little')+bytes(32),[WALLET])])
        self.assertIn('UNSUPPORTED_SYSTEM_INSTRUCTION',r['reasons'])
    def test_truncated_transfer_rejected(self):
        r=self.run_inventory([self.ix(TOKEN_PROGRAM,b'\x03',[HOLDING,OTHER,WALLET])]);self.assertFalse(r['inventory_checks_passed'])
    def test_duplicate_inner_group_rejected(self):
        with self.assertRaises(ValueError):self.run_inventory([self.ix(JUPITER,bytes(8))],[{'index':0,'instructions':[]}]*2)
    def test_parsed_cpi_approval_is_rejected(self):
        inner=[{'index':0,'instructions':[{'programId':TOKEN_PROGRAM,'parsed':{'type':'approve','info':{'source':HOLDING,'delegate':OTHER,'owner':WALLET,'amount':'1'}}}]}]
        r=self.run_inventory([self.ix(JUPITER,bytes(8))],inner)
        self.assertIn('TOKEN_APPROVE_NOT_ALLOWED',r['reasons'])
    def test_unknown_parsed_operation_never_disappears(self):
        inner=[{'index':0,'instructions':[{'programId':TOKEN_PROGRAM,'parsed':{'type':'futureOperation','info':{}}}]}]
        r=self.run_inventory([self.ix(JUPITER,bytes(8))],inner)
        self.assertFalse(r['inventory_checks_passed']);self.assertIn(TOKEN_PROGRAM,r['programs'])
    def test_resolved_inner_account_must_exist_in_message(self):
        inner=[{'index':0,'instructions':[{'programId':TOKEN_PROGRAM,'accounts':[base58(bytes([8])*32)],'data':base58(b'\x11')}]}]
        with self.assertRaises(ValueError):self.run_inventory([self.ix(JUPITER,bytes(8))],inner)
    def test_real_unsigned_simulation_replays_all_instruction_formats(self):
        import json
        from pathlib import Path
        p=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-sell-simulation.json').read_text())
        r=inventory(p['outer'],p['simulation'],p['keys'],p['wallet'])
        self.assertEqual(r['reasons'],['UNSUPPORTED_ROUTE_PROGRAM'])
        self.assertGreater(len(r['instructions']),len(p['outer']))
        self.assertFalse(r['full_route_policy_passed'])
        self.assertIn('LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo',r['programs'])
    def test_cpi_parent_context_tracks_depth_and_sibling_return(self):
        outer=[self.ix(JUPITER,bytes(8))]
        inner=[{'index':0,'instructions':[
            {'programId':JUPITER,'accounts':[],'data':base58(bytes(8)),'stackHeight':2},
            {'programId':TOKEN_PROGRAM,'accounts':[HOLDING],'data':base58(b'\x11'),'stackHeight':3},
            {'programId':SYSTEM,'accounts':[WALLET,OTHER],'data':base58((2).to_bytes(4,'little')+bytes(8)),'stackHeight':2}]}]
        r=self.run_inventory(outer,inner);rows={x['instruction']:x for x in r['instructions']}
        self.assertTrue(r['stack_metadata_verified']);self.assertEqual(rows['0.1']['parent_instruction'],'0.0')
        self.assertEqual(rows['0.2']['parent_instruction'],'0');self.assertEqual(rows['0.2']['parent_program'],JUPITER)
    def test_missing_stack_height_cannot_establish_caller(self):
        r=self.run_inventory([self.ix(JUPITER,bytes(8))],[{'index':0,'instructions':[
            {'programId':TOKEN_PROGRAM,'accounts':[HOLDING],'data':base58(b'\x11')}]}])
        self.assertFalse(r['stack_metadata_verified']);self.assertFalse(r['inventory_checks_passed'])
    def test_stack_jump_does_not_invent_intermediate_caller(self):
        for heights in ([3],[2,4]):
            r=self.run_inventory([self.ix(JUPITER,bytes(8))],[{'index':0,'instructions':[
                {'programId':TOKEN_PROGRAM,'accounts':[HOLDING],'data':base58(b'\x11'),'stackHeight':h} for h in heights]}])
            self.assertIn('INSTRUCTION_STACK_TRANSITION_UNVERIFIED',r['reasons'])
