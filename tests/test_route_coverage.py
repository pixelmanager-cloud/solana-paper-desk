import unittest,copy
from desk.route_coverage import check_route_coverage,REQUIRED,instruction_receipt
from desk.instructions import JUPITER
from desk.providers import PUMPSWAP
from desk.dynamic_fees import fee_schema
class RouteCoverageTests(unittest.TestCase):
    def setUp(self):
        self.checks={k:{'passed':True} for k in REQUIRED};self.checks['amm']['instruction']='1.0'
        self.rows=[{'instruction':'1','program':JUPITER,'stack_height':1,'data_base64':'','reasons':[]},
            {'instruction':'1.0','program':PUMPSWAP,'stack_height':2,'data_base64':'','reasons':[]},
            {'instruction':'1.1','program':fee_schema()['address'],'stack_height':3,'data_base64':'','reasons':['UNSUPPORTED_ROUTE_PROGRAM']}]
        for row in self.rows:
            row['accounts']=[];row['parent_instruction']=None;row['parent_program']=None
        for name in ('envelope','amm','recipients','fee_query','event','setup'):self.checks[name]['checked_instructions']=[]
        for name,row in zip(('envelope','amm','fee_query'),self.rows):self.checks[name]['checked_instructions']=[instruction_receipt(row)]
        self.checks['router']['checked_instruction']={**instruction_receipt(self.rows[0]),'instruction':None}
    def check(self):return check_route_coverage({'stack_metadata_verified':True,'instructions':self.rows},self.checks)
    def test_component_coverage_never_implies_full_approval(self):
        r=self.check();self.assertTrue(r['coverage_passed']);self.assertFalse(r['transaction_policy_ok'])
    def test_unknown_inner_router_instruction_is_not_hidden_by_outer_approval(self):
        self.rows.append({'instruction':'1.2','program':JUPITER,'stack_height':2,'data_base64':'','reasons':[]})
        self.assertFalse(self.check()['coverage_passed']);self.assertEqual(self.check()['uncovered'][0]['instruction'],'1.2')
    def test_fee_program_requires_specific_query_check(self):
        self.checks['fee_query']['passed']=False;self.assertFalse(self.check()['coverage_passed'])
    def test_duplicate_paths_and_remaining_inventory_flags_fail(self):
        self.rows.append(copy.deepcopy(self.rows[1]));self.assertFalse(self.check()['coverage_passed']);self.rows.pop()
        self.rows[1]['reasons']=['UNSUPPORTED_PARSED_INSTRUCTION'];self.assertFalse(self.check()['coverage_passed'])
    def test_missing_account_effects_cannot_be_covered_by_instruction_checks(self):
        self.checks['controls']['passed']=False;r=self.check();self.assertFalse(r['coverage_passed']);self.assertIn('controls',r['incomplete_components'])
