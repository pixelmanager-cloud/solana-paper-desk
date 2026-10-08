"""Exact component receipts are diagnostic, never full route authorization.

Real offline checker fixtures produce receipts; other component flags in these
unit contracts are synthetic placeholders, not fresh full-transaction evidence.
"""
import base64
import copy
import unittest
from desk.route_coverage import check_route_coverage, instruction_receipt, outer_receipt, REQUIRED, ROW_CHECKS
from desk.providers import PUMPSWAP
from desk.instructions import JUPITER,COMPUTE
from tests import test_fee_call, test_sell_event, test_recipients, test_setup_policy, test_router
from desk.envelope import check_sell_envelope


class ExactRouteCoverageTests(unittest.TestCase):
    def checks(self):
        checks={k:{'passed':True} for k in REQUIRED}
        for k in ROW_CHECKS:checks[k]['checked_instructions']=[]
        checks['router']['checked_instruction']={**dict.fromkeys(('instruction','program','data_base64','accounts','stack_height','parent_instruction','parent_program')), 'instruction':None}
        return checks
    def coverage(self,rows,checks):
        result=check_route_coverage({'stack_metadata_verified':True,'instructions':rows},checks)
        self.assertIs(result['full_route_policy_passed'],False)
        self.assertIs(result['transaction_policy_ok'],False)
        return result
    def event(self):
        fixture=test_sell_event.SellEventTests();fixture.setUp()
        row=fixture.row();row['reasons']=[]
        checks=self.checks();checks['event']=fixture.check(row);checks['amm']['instruction']=fixture.bind['instruction']
        self.assertTrue(checks['event']['passed']);return row,checks
    def test_exact_event_receipt_does_not_label_another_pumpswap_row(self):
        row,checks=self.event();self.assertTrue(self.coverage([row],checks)['coverage_passed'])
        for data in (row['data_base64'],base64.b64encode(b'unknown').decode()):
            extra={**row,'instruction':'2.10','data_base64':data}
            result=self.coverage([row,extra],checks)
            self.assertEqual(result['roles'],[{'instruction':row['instruction'],'role':'bound_sell_event'}])
            self.assertEqual(result['uncovered'],[{'instruction':'2.10','program':PUMPSWAP}])
    def test_stale_event_receipt_rejects_each_changed_identity_field(self):
        row,checks=self.event();before=copy.deepcopy(checks)
        changes={'instruction':'2.10','program':JUPITER,'data_base64':'AA==','accounts':['unmatched'],
                 'stack_height':4,'parent_instruction':'2.8','parent_program':JUPITER}
        for name,value in changes.items():
            with self.subTest(field=name):
                result=self.coverage([{**row,name:value}],checks)
                self.assertFalse(result['coverage_passed']);self.assertEqual(len(result['uncovered']),1)
                self.assertEqual(checks,before)
    def test_fee_receipt_only_exempts_the_exact_checked_unknown_program_flag(self):
        fixture=test_fee_call.FeeCallTests();fixture.setUp()
        row=copy.deepcopy(fixture.row);row['reasons']=['UNSUPPORTED_ROUTE_PROGRAM']
        checks=self.checks();checks['fee_query']=fixture.check()
        self.assertTrue(self.coverage([row],checks)['coverage_passed'])
        extra={**row,'instruction':'2.2'}
        result=self.coverage([row,extra],checks)
        self.assertEqual(result['uncovered'],[{'instruction':'2.2','program':row['program']}])
        self.assertIn('ROUTE_INSTRUCTION_FLAGS_UNRESOLVED',result['reasons'])
        row['accounts']=list(reversed(row['accounts']))
        self.assertFalse(self.coverage([row],checks)['coverage_passed'])
    def test_recipient_receipts_bind_raw_amount_decimals_accounts_and_path(self):
        fixture=test_recipients.RecipientTests();fixture.setUp();fixture.rows[1]['instruction']='2.2'
        for row in fixture.rows:row['parent_program']=PUMPSWAP;row['reasons']=[]
        checks=self.checks();checks['recipients']=fixture.check()
        self.assertTrue(self.coverage(fixture.rows,checks)['coverage_passed'])
        for change in ({'instruction':'2.3'},{'data_base64':base64.b64encode(b'\x0c'+bytes(8)+b'\x09').decode()},
                       {'accounts':list(reversed(fixture.rows[0]['accounts']))}):
            extra={**fixture.rows[0],**change}
            rows=copy.deepcopy(fixture.rows)
            if extra['instruction']==rows[0]['instruction']:rows[0]=extra
            else:rows.append(extra)
            result=self.coverage(rows,checks);self.assertFalse(result['coverage_passed'])
            self.assertGreaterEqual(len(result['uncovered']),1)
    def test_setup_receipts_do_not_cover_extra_system_or_token_operations(self):
        fixture=test_setup_policy.SetupTests();fixture.setUp();fixture.rows[2]['instruction']='1.1'
        checks=self.checks();checks['setup']=fixture.check();rows=fixture.rows[1:]
        self.assertTrue(self.coverage(rows,checks)['coverage_passed'])
        for row in rows:
            extra={**row,'instruction':'1.2'}
            result=self.coverage(rows+[extra],checks)
            self.assertEqual(result['uncovered'],[{'instruction':'1.2','program':row['program']}])
    def test_outer_envelope_and_router_require_exact_rows_and_unique_swap_identity(self):
        fixture=test_router.RouterTests();fixture.setUp();outer=fixture.p['outer']
        checks=self.checks();checks['router']=fixture.check()
        checks['envelope']=check_sell_envelope(outer,fixture.p['wallet'])
        rows=[outer_receipt(ix,i) for i,ix in enumerate(outer)]
        self.assertTrue(self.coverage(rows,checks)['coverage_passed'])
        swap=next(r for r in rows if r['program']==JUPITER)
        result=self.coverage(rows+[{**swap,'instruction':'99'}],checks)
        self.assertFalse(result['coverage_passed'])
        self.assertIn({'instruction':swap['instruction'],'program':JUPITER},result['uncovered'])
        compute=next(r for r in rows if r['program']==COMPUTE)
        result=self.coverage(rows+[{**compute,'instruction':'99'}],checks)
        self.assertEqual(result['uncovered'],[{'instruction':'99','program':COMPUTE}])
        changed=copy.deepcopy(rows);next(r for r in changed if r['program']==JUPITER)['data_base64']='AA=='
        self.assertFalse(self.coverage(changed,checks)['coverage_passed'])
    def test_blanket_passed_flags_or_partial_and_duplicate_receipts_cannot_cover(self):
        row,checks=self.event()
        blanket={k:{'passed':True} for k in REQUIRED}
        result=self.coverage([row],blanket);self.assertFalse(result['coverage_passed'])
        self.assertIn('event',result['unavailable_bindings'])
        for receipts in ([{'instruction':row['instruction']}],
                         [instruction_receipt(row),instruction_receipt(row)]):
            checks['event']['checked_instructions']=receipts
            self.assertFalse(self.coverage([row],checks)['coverage_passed'])
    def test_duplicate_paths_are_all_uncovered_and_inventory_flags_remain_blocking(self):
        row,checks=self.event()
        result=self.coverage([row,copy.deepcopy(row)],checks)
        self.assertEqual(result['roles'],[]);self.assertEqual(len(result['uncovered']),2)
        self.assertIn('ROUTE_DUPLICATE_INSTRUCTION_PATH',result['reasons'])
        row['reasons']=['UNSUPPORTED_PARSED_INSTRUCTION']
        self.assertFalse(self.coverage([row],checks)['coverage_passed'])
    def test_receipts_are_detached_and_failed_checkers_publish_none(self):
        fixture=test_fee_call.FeeCallTests();fixture.setUp();receipt=fixture.check()['checked_instructions'][0]
        fixture.row['accounts'][0]='unmatched'
        self.assertNotEqual(receipt['accounts'],fixture.row['accounts'])
        self.assertFalse(fixture.check()['passed']);self.assertEqual(fixture.check()['checked_instructions'],[])
        row,checks=self.event();checks['event']['passed']=False
        self.assertEqual(self.coverage([row],checks)['roles'],[])
    def test_malformed_row_identity_and_boolean_stack_are_uncovered(self):
        fixture=test_router.RouterTests();fixture.setUp();outer=fixture.p['outer']
        checks=self.checks();checks['router']=fixture.check()
        checks['envelope']=check_sell_envelope(outer,fixture.p['wallet'])
        rows=[outer_receipt(ix,i) for i,ix in enumerate(outer)]
        for change in ({'stack_height':True},{'accounts':'not-a-list'},{'data_base64':'!'},{'reasons':None}):
            changed=copy.deepcopy(rows);changed[0].update(change)
            result=self.coverage(changed,checks)
            self.assertFalse(result['coverage_passed']);self.assertGreaterEqual(len(result['uncovered']),1)
    def test_boolean_or_partial_checker_receipts_do_not_equal_typed_identity(self):
        fixture=test_router.RouterTests();fixture.setUp();outer=fixture.p['outer']
        checks=self.checks();checks['router']=fixture.check()
        checks['envelope']=check_sell_envelope(outer,fixture.p['wallet'])
        rows=[outer_receipt(ix,i) for i,ix in enumerate(outer)]
        checks['envelope']['checked_instructions'][0]['stack_height']=True
        self.assertFalse(self.coverage(rows,checks)['coverage_passed'])
        checks['envelope']=check_sell_envelope(outer,fixture.p['wallet'])
        checks['router']['checked_instruction']['stack_height']=True
        self.assertFalse(self.coverage(rows,checks)['coverage_passed'])
