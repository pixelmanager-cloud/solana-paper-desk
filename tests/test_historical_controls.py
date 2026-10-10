"""Bound persisted control-syntax projection; local fixture transport only."""
import copy
import sqlite3
import unittest
from unittest.mock import patch

from desk.history import collect_history
from desk.model import canonical, digest
from desk.security import TOKEN_PROGRAM, base58
from tests import test_control_obligations as fixtures


class HistoricalControlInventoryTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.ControlObligationsTests()
        self.addCleanup(self.case.doCleanups); self.case.setUp()
        self.h = self.case.h
        self.account = self.h.f['accounts'][0]
        self.owner = self.h.f['owners'][self.account]
        self.mint = self.h.f['mint']

    def instruction(self, kind, dual=True):
        target = self.account; owner = self.owner; mint = self.mint
        data,accounts,info = {
            'approve':(b'\x04'+bytes(8),[target,owner,owner],
                       {'source':target,'delegate':owner,'owner':owner,'amount':'0'}),
            'approveChecked':(b'\x0d'+bytes(9),[target,mint,owner,owner],
                              {'source':target,'mint':mint,'delegate':owner,'owner':owner,
                               'tokenAmount':{'amount':'0','decimals':0}}),
            'revoke':(b'\x05',[target,owner],{'source':target,'owner':owner}),
            'setAuthority':(b'\x06\x00\x00',[mint,owner],
                            {'mint':mint,'authority':owner,'authorityType':'mintTokens','newAuthority':None}),
            'freezeAccount':(b'\x0a',[target,mint,owner],{'account':target,'mint':mint,'freezeAuthority':owner}),
            'thawAccount':(b'\x0b',[target,mint,owner],{'account':target,'mint':mint,'freezeAuthority':owner}),
            'closeAccount':(b'\x09',[target,owner,owner],{'account':target,'destination':owner,'owner':owner}),
        }[kind]
        result = {'programId':TOKEN_PROGRAM,'accounts':accounts,'data':base58(data)}
        if dual: result['parsed'] = {'type':kind,'info':info}
        return result

    def capture(self, address_records=None):
        record = self.h.store.load(self.case.head['evidence_hash']); queries = []
        for q in record['history_queries']:
            def transport(method,params):
                if address_records is None: return self.h.transport(method,params)
                self.assertEqual(method,'getTransactionsForAddress')
                rows = address_records[params[0]]; split = max(1,len(rows)//2)
                if 'paginationToken' not in params[1]:
                    return {'data':copy.deepcopy(rows[:split]),'paginationToken':'second:'+params[0]}
                return {'data':copy.deepcopy(rows[split:])}
            _, replacement = collect_history(q['address'],q['start'],q['end'],transport,
                max_pages=2,capture=self.h.store.save,token_accounts='none',slot_range=q['slot_range'])
            binding = {k:q[k] for k in ('address','start','end','token_accounts_filter','slot_range')}
            with self.h.store.connect() as c:
                c.execute('UPDATE ownership_history SET coverage=? WHERE budget=? AND query=?',
                          (canonical(replacement),'multi',canonical(binding)))
            queries.append(replacement)
        record['history_queries'] = queries
        record['snapshot']['reconciled'] = True  # Deliberately forged summary.
        self.case.publish(record)

    def controls(self):
        result = self.case.project()['control_obligations']
        self.case.assert_reject(result)
        self.assertEqual(result['requests_used'],13)
        self.assertEqual(result['obligations']['historical_controls']['status'],'UNRESOLVED')
        controls = result['obligations']['historical_controls']['observed_inventory']
        for field in ('effect_order_verified','individual_cpi_success_verified','historical_control_verified',
                      'source_authenticated','ownership_approved','eligible_for_trading'):
            self.assertIs(controls[field],False)
        return result,controls

    @fixtures.requires_linux_reads
    def test_existing_thirteen_request_reference_gains_exact_initialization_witnesses(self):
        before = {p.name:p.read_bytes() for p in self.h.root.iterdir() if p.is_file()}
        result,controls = self.controls()
        self.assertEqual(controls['status'],'OBSERVED_INVENTORY')
        self.assertEqual([r['kind'] for r in controls['operations']],
                         ['initializeMint2','initializeAccount3','initializeAccount3','initializeAccount3'])
        self.assertEqual([r['instruction_path'] for r in controls['operations']],['0.0','0.1','0.2','0.3'])
        self.assertTrue(all(r['parsed_operation'] for r in controls['operations']))
        self.assertTrue(all(not r['raw_syntax_complete'] for r in controls['operations']))
        self.assertTrue(any(len(r['capture_locations'])>1 for r in controls['operations']))
        self.assertFalse(controls['observed_rows_enumerated'])
        for row in controls['operations']:
            for location in row['capture_locations']:
                raw = self.h.store.load(location['page_hash'])['data'][location['record_index']]
                self.assertEqual(digest(raw),row['transaction_hash'])
                self.assertIn(location['request_hash'],result['evidence_hashes'])
        self.assertEqual(result,self.controls()[0])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.h.root.iterdir() if p.is_file()})

    @fixtures.requires_linux_reads
    def test_raw_normalized_controls_preserve_paths_and_both_representations(self):
        kinds = ['approve','approveChecked','revoke','setAuthority','freezeAccount','thawAccount']
        self.h.records[-1]['transaction']['message']['instructions'] += [self.instruction(k) for k in kinds]
        self.capture(); result,controls = self.controls()
        selected = [r for r in controls['operations'] if r['kind'] in kinds]
        self.assertEqual([r['kind'] for r in selected],kinds)
        self.assertEqual([r['instruction_path'] for r in selected],[str(i) for i in range(1,7)])
        self.assertTrue(all(r['raw_syntax_complete'] and r['representations_match'] for r in selected))
        self.assertEqual(result['obligations']['endpoint_controls']['status'],'OBSERVED_COMPONENT')
        self.assertEqual(result['obligations']['historical_accounting']['status'],'UNRESOLVED')
        self.assertEqual(selected[0]['raw_operation']['amount_raw'],'0')
        self.assertIsNone(selected[3]['raw_operation']['new_authority'])

    @fixtures.requires_linux_reads
    def test_raw_parsed_contradiction_retains_original_targets_and_blocks(self):
        ix = self.instruction('approve')
        ix['parsed']['info']['source'] = self.h.f['accounts'][1]
        self.h.records[-1]['transaction']['message']['instructions'].append(ix)
        self.capture(); _,controls = self.controls()
        row = next(r for r in controls['operations'] if r['kind']=='approve')
        self.assertEqual(row['raw_operation']['target_account'],self.account)
        self.assertEqual(row['parsed_operation']['target_account'],self.h.f['accounts'][1])
        self.assertFalse(row['raw_syntax_complete'])
        self.assertIn('CONTROL_RAW_PARSED_MISMATCH',controls['unknown_reasons'])
        self.assertIn('CONTROL_TARGET_CONTRADICTION',controls['unknown_reasons'])

    @fixtures.requires_linux_reads
    def test_account_only_failed_attempt_and_duplicate_inner_paths_remain_observed(self):
        ix = self.instruction('approve',dual=False); ix['stackHeight'] = 3
        self.h.records[-1]['meta']['err'] = {'InstructionError':[0,'Custom']}
        self.h.records[-1]['meta']['innerInstructions'] = [
            {'index':0,'instructions':[ix]}, {'index':0,'instructions':[self.instruction('revoke',dual=False)]}]
        self.capture(); _,controls = self.controls()
        rows = [r for r in controls['operations'] if r['kind'] in ('approve','revoke')]
        self.assertEqual([r['instruction_path'] for r in rows],['0.0','0.0'])
        self.assertEqual([r['inner_group_ordinal'] for r in rows],[0,1])
        self.assertEqual(rows[0]['stack_height_witness'],3)
        self.assertTrue(all(r['transaction_status']=='PROVIDER_FAILED' for r in rows))
        self.assertIn('CONTROL_INNER_PARENT_DUPLICATE',controls['unknown_reasons'])
        self.assertIn('CONTROL_FAILED_OR_UNKNOWN_TRANSACTION_OUTCOME',controls['unknown_reasons'])

    @fixtures.requires_linux_reads
    def test_close_and_reinitialization_syntax_never_infers_lifetime_success(self):
        instructions = [self.instruction('closeAccount',dual=False),
            {'programId':TOKEN_PROGRAM,'accounts':[self.account,self.mint],
             'data':base58(b'\x12'+bytes([9])*32)}]
        self.h.records[-1]['transaction']['message']['instructions'] += instructions
        self.capture(); _,controls = self.controls()
        rows = [r for r in controls['operations'] if r['signature']==self.h.records[-1]['signature']]
        self.assertEqual([r['kind'] for r in rows],['closeAccount','initializeAccount3'])
        self.assertTrue(all(r['raw_syntax_complete'] for r in rows))
        self.assertEqual(rows[0]['raw_operation']['authority_account'],self.owner)
        self.assertEqual(rows[1]['lifetime_role'],'UNVERIFIED_INITIALIZATION_OR_REINITIALIZATION')

    @fixtures.requires_linux_reads
    def test_mint_and_frontier_mismatch_and_unknown_tags_remain_blockers(self):
        ix = self.instruction('approveChecked'); other = base58(bytes([29])*32)
        ix['accounts'][0] = other; ix['parsed']['info']['source'] = other
        ix['accounts'][1] = other; ix['parsed']['info']['mint'] = other
        self.h.records[-1]['transaction']['message']['instructions'] += [ix,
            {'programId':TOKEN_PROGRAM,'accounts':[self.account],'data':base58(b'\xff')}]
        self.capture(); _,controls = self.controls()
        for reason in ('CONTROL_TARGET_OUTSIDE_BOUND_FRONTIER','CONTROL_TARGET_MINT_MISMATCH',
                       'CONTROL_OPERATION_UNKNOWN','CONTROL_RAW_OPERATION_UNSUPPORTED_OR_MALFORMED'):
            self.assertIn(reason,controls['unknown_reasons'])

    @fixtures.requires_linux_reads
    def test_malformed_parsed_representation_cannot_hide_valid_raw_control(self):
        ix = self.instruction('approve'); ix['parsed']['type'] = []
        self.h.records[-1]['transaction']['message']['instructions'].append(ix)
        self.capture(); _,controls = self.controls()
        row = next(r for r in controls['operations'] if r['raw_kind']=='approve')
        self.assertIsNotNone(row['raw_operation'])
        self.assertFalse(row['raw_syntax_complete'])
        self.assertIn('PARSED_CONTROL_TYPE_UNSUPPORTED',row['unknown_reasons'])

    @fixtures.requires_linux_reads
    def test_conflicting_account_query_version_retained_without_lexical_resolution(self):
        self.h.records[-1]['transaction']['message']['instructions'].append(self.instruction('approve'))
        mapping = {q['address']:copy.deepcopy(self.h.rows_for(q['address'])) for q in self.case.head['history_queries']}
        last = mapping[self.account][-1]
        last['transaction']['message']['instructions'][-1] = self.instruction('revoke')
        self.capture(mapping); _,controls = self.controls()
        rows = [r for r in controls['operations'] if r['signature']==self.h.records[-1]['signature']]
        self.assertEqual({r['kind'] for r in rows},{'approve','revoke'})
        self.assertEqual(len({r['transaction_hash'] for r in rows}),2)
        self.assertIn('CONTROL_CONFLICTING_TRANSACTION_WITNESSES',controls['unknown_reasons'])

    @fixtures.requires_linux_reads
    def test_compiled_control_uses_existing_resolver_and_original_instruction_hash(self):
        mapping = {q['address']:copy.deepcopy(self.h.rows_for(q['address'])) for q in self.case.head['history_queries']}
        raw = copy.deepcopy(self.h.records[-1]); message = raw['transaction']['message']
        keys = [k['pubkey'] for k in message['accountKeys']]
        for key in (self.owner,TOKEN_PROGRAM):
            if key not in keys: keys.append(key)
        message['accountKeys'] = keys
        message['header'] = {'numRequiredSignatures':1,'numReadonlySignedAccounts':0,'numReadonlyUnsignedAccounts':1}
        raw['version'] = 'legacy'
        message['instructions'] = [{'programIdIndex':keys.index(TOKEN_PROGRAM),
            'accounts':[keys.index(self.account),keys.index(self.owner)],'data':base58(b'\x05')}]
        raw['meta']['innerInstructions'] = []
        for rows in mapping.values():
            for i,row in enumerate(rows):
                if row['signature']==raw['signature']: rows[i] = copy.deepcopy(raw)
        self.capture(mapping); _,controls = self.controls()
        row = next(r for r in controls['operations'] if r['kind']=='revoke')
        self.assertEqual(row['instruction_hash'],digest(message['instructions'][0]))
        self.assertNotEqual(row['instruction_hash'],row['resolved_instruction_hash'])
        self.assertEqual(row['raw_operation']['target_account'],self.account)
        self.assertTrue(row['raw_syntax_complete'])

    @fixtures.requires_linux_reads
    def test_missing_frontier_query_and_unbound_revision_never_supply_control_completeness(self):
        record = self.h.store.load(self.case.head['evidence_hash'])
        record['history_queries'].pop()
        self.case.publish(record)
        _,controls = self.controls()
        self.assertIn('CONTROL_FRONTIER_QUERY_MISSING_OR_AMBIGUOUS',controls['unknown_reasons'])
        self.assertFalse(controls['observed_rows_enumerated'])
        from desk.live_features import candidate_snapshot
        result = candidate_snapshot(self.case.db,self.h.path,'multi',revision_hash='f'*64,now=120)
        controls = result['control_obligations']['obligations']['historical_controls']['observed_inventory']
        self.assertEqual(controls['status'],'UNAVAILABLE')
        self.assertEqual(controls['operations'],[])

    @fixtures.requires_linux_reads
    def test_omission_forged_summary_and_resource_exhaustion_never_approve(self):
        record = self.h.store.load(self.case.head['evidence_hash'])
        record['historical_controls'] = {'verified':True,'operations':[]}
        self.case.publish(record)
        self.assertEqual(len(self.controls()[1]['operations']),4)
        with patch('desk.historical_controls.MAX_OPERATIONS',0):
            result = self.case.project()['control_obligations']
        self.case.assert_reject(result)
        self.assertIn('CONTROL_PROJECTION_RESOURCE_LIMIT',result['blockers'])
        self.assertEqual(result['obligations']['historical_controls']['observed_inventory']['status'],'UNAVAILABLE')
        key = self.case.head['history_queries'][0]['pages'][0]['payload_hash']
        with self.h.store.connect() as c: c.execute('DELETE FROM pages WHERE hash=?',(key,))
        result = self.case.project()['control_obligations']
        self.assertIn('CONTROL_RAW_HISTORY_UNAVAILABLE',result['blockers'])
        self.assertEqual(result['obligations']['historical_controls']['observed_inventory']['operations'],[])

    @fixtures.requires_linux_reads
    def test_observed_control_field_cannot_bypass_sealed_source_consumer(self):
        from tests import test_sealed_continuation_entry_evidence as sealed
        from desk.control_obligations import inventory
        from desk.evidence import EvidenceStore
        case = sealed.SealedContinuationEntryEvidenceTests()
        self.addCleanup(case.doCleanups); case.setUp()
        def project():
            return inventory(case.scan,EvidenceStore(case.evidence,read_only=True),
                             revision_hash=case.head['evidence_hash'],now=110)
        self.assertEqual(project()['obligations']['historical_controls']['observed_inventory']['status'],'OBSERVED_INVENTORY')
        with case.store.connect() as c:
            c.execute("UPDATE ownership_admissions SET state='PREPARED'")
        result = project()
        self.assertEqual(result['obligations']['historical_controls']['observed_inventory']['status'],'UNAVAILABLE')
        self.assertFalse(result['historical_control_verified'])


class PortableHistoricalSyntaxBoundaryTests(unittest.TestCase):
    def test_compiled_identity_and_missing_groups_do_not_guess_effect_order(self):
        from desk.historical_controls import _instruction_rows, _operation
        account,authority = [base58(bytes([n])*32) for n in (1,2)]
        raw = {'version':'legacy','transaction':{'signatures':['synthetic'], 'message':{
            'accountKeys':[authority,account,TOKEN_PROGRAM],
            'header':{'numRequiredSignatures':1,'numReadonlySignedAccounts':0,'numReadonlyUnsignedAccounts':1},
            'instructions':[{'programIdIndex':2,'accounts':[1,0],'data':base58(b'\x05')}]}},
            'meta':{'err':None}}
        rows,keys,reasons = _instruction_rows(raw)
        self.assertIn('CONTROL_INNER_INSTRUCTIONS_UNAVAILABLE',reasons)
        from desk.account_keys import compiled_instruction
        row = _operation(compiled_instruction(rows[0][3],keys),rows[0][0],None)
        self.assertEqual(row['raw_operation']['target_account'],account)
        self.assertTrue(row['raw_syntax_complete'])
        self.assertEqual(rows[0][:3],('0',None,None))

    def test_parsed_movement_cannot_hide_raw_control_or_missing_bytes(self):
        from desk.historical_controls import _operation
        account,authority = [base58(bytes([n])*32) for n in (1,2)]
        ix = {'programId':TOKEN_PROGRAM,'accounts':[account,authority],'data':base58(b'\x05'),
              'parsed':{'type':'transfer','info':{}}}
        row = _operation(ix,'0',None)
        self.assertEqual(row['kind'],'revoke')
        self.assertIsNotNone(row['raw_operation'])
        self.assertFalse(row['raw_syntax_complete'])
        self.assertTrue(row['unknown_reasons'])
