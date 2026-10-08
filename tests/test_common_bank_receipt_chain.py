"""Explicit synthetic format-2 snapshots; no migration, publication or providers."""
import copy
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from desk import common_bank_receipt_chain as chain
from desk.model import canonical, digest
from desk.pool_vault_admission import PROFILE, GENESIS, NETWORK
from tests import test_pool_receipt_ledger as legacy_fixture

ROOT=Path(__file__).resolve().parents[1]


def row(number, publication, columns, payload, previous):
    body=canonical(payload);key=digest(payload)
    final=digest({'seq':number,'publication_id':publication,'payload_hash':key,'previous_hash':previous})
    return (number,publication,*columns,body,key,previous,final)


class ReceiptChainTests(unittest.TestCase):
    def setUp(self):
        fixture=json.loads((ROOT/'fixtures/pool-vault-admission-legacy.json').read_text())
        q=fixture['query'];self.pool=q['pool'];self.mint=q['mint'];self.slot=q['snapshot_slot']
        self.source={'source_id':'fixture-source','source_kind':'synthetic_fixture',
                     'network':NETWORK,'genesis_hash':GENESIS}
        self.receipt={**self.source,'pool':self.pool,'mint':self.mint,'slot':self.slot,
                      'snapshot_time':q['snapshot_time'],'captured_at':q['snapshot_time']+2,
                      'refs':fixture['refs']}
        self.descriptor={'version':1,'ledger_id':'synthetic-ledger','profile':PROFILE,
            'root':'/synthetic-only','root_identity':[1,2],
            'ledger_path':'/synthetic-only/receipts.sqlite','ledger_identity':[1,3],
            'lock_path':'/synthetic-only/receipts.sqlite.coordinator.lock','lock_identity':[1,4,5],
            'evidence_path':'/synthetic-only/evidence.sqlite','evidence_identity':[1,6],
            'sources':[{**self.source,'profile':PROFILE}],
            'allow_synthetic_fixtures':True,'max_age_seconds':60}
        self.prefix=(row(1,'legacy-original',(self.pool,self.mint,str(self.slot),'fixture-source'),
                         {'version':1,'profile':PROFILE,'receipt':self.receipt},digest(self.descriptor)),)
        self.capabilities=[{**self.source,'version':version,'profile':profile,'issuance_enabled':enabled}
            for version,profile,enabled in ((1,PROFILE,False),(2,PROFILE,True),(2,chain.PARENT_PROFILE,False))]
        self.schema_hash=digest('synthetic reviewed future schema, not installed')
        self.certificate={'kind':'pool_receipt_transition_certificate_v2','version':2,
            'legacy_descriptor_hash':digest(self.descriptor),'legacy_count':len(self.prefix),
            'legacy_head_hash':self.prefix[-1][-1],'dispatch_hash':chain.DISPATCH_HASH,
            'schema_hash':self.schema_hash,'capabilities':self.capabilities}
        self.binding={'capture_id':'synthetic-capture','scan_id':'synthetic-scan',
            **{k:digest('synthetic-only:'+k) for k in chain.BINDING if k.endswith('_hash')}}
        self.raw={**self.source,**self.binding,'pool':self.pool,'mint':self.mint,'slot':self.slot,
                  'snapshot_time':self.receipt['snapshot_time'],'captured_at':self.receipt['captured_at']-1000,
                  'manifest_hash':digest('synthetic manifest reference, unavailable raw witnesses')}
        self.extra=[];self.add_parent();self.build()

    def add_parent(self, **changes):
        self.extra.append(('common_bank_observation',chain.PARENT_PROFILE,{**self.raw,**changes}))

    def add_marker(self, **changes):
        marker={**self.source,**self.binding,'pool':self.pool,'mint':self.mint,'slot':self.slot,
                'status':'PENDING','evidence_hashes':[]}
        self.extra.append(('unresolved_marker',chain.PARENT_PROFILE,{**marker,**changes}))

    def build(self):
        key=digest(self.certificate);rows=list(self.prefix)
        previous=rows[-1][-1] if rows else digest(self.descriptor)
        rows.append(row(len(rows)+1,'transition:'+key,(chain.TRANSITION_SENTINEL,)*4,
            {'version':2,'type':'transition','profile':chain.FORMAT_PROFILE,'certificate_hash':key},previous))
        for i,(kind,profile,raw) in enumerate(self.extra):
            columns=(raw['pool'] if raw['pool'] is not None else chain.UNKNOWN_POOL,
                     raw['mint'] if raw['mint'] is not None else chain.UNKNOWN_MINT,
                     str(raw['slot']) if raw['slot'] is not None else chain.UNKNOWN_SLOT,raw['source_id'])
            payload={'version':2,'type':kind,'profile':profile,'certificate_hash':key,
                     'marker' if kind=='unresolved_marker' else 'observation':raw}
            rows.append(row(len(rows)+1,'synthetic-publication:'+str(i),columns,payload,rows[-1][-1]))
        self.snapshot=chain.ChainSnapshot(canonical(self.descriptor),digest(self.descriptor),
             canonical(self.certificate),key,(len(rows),rows[-1][-1]),tuple(rows))
        self.anchor=chain.ChainAnchor(canonical(self.descriptor),self.prefix,
            (len(self.prefix),self.prefix[-1][-1] if self.prefix else digest(self.descriptor)),key,self.schema_hash,self.snapshot.head)

    def validate(self):return chain.validate_receipt_chain(self.snapshot,self.anchor)
    def scope(self, **changes):
        return self.validate().enumerate_scope(**{'pool':self.pool,'mint':self.mint,'slot':self.slot,**changes})
    def refused(self):
        with self.assertRaises(chain.ChainUnavailable):self.validate()
    def rechain(self, rows):
        previous=digest(self.descriptor);out=[]
        for seq,old in enumerate(rows,1):
            old=list(old);old[0]=seq;old[8]=previous
            old[9]=digest({'seq':seq,'publication_id':old[1],'payload_hash':old[7],'previous_hash':previous})
            previous=old[9];out.append(tuple(old))
        self.snapshot=replace(self.snapshot,rows=tuple(out),head=(len(out),previous))
        # Adversarial helper explicitly repins the synthetic current head so
        # malformed dispatch cases exercise checks deeper than the outer pin.
        self.anchor=replace(self.anchor,head=self.snapshot.head)

    def test_mixed_chain_keeps_exact_prefix_expired_and_disabled_sources_all_false(self):
        before=self.snapshot;result=self.validate();scope=result.enumerate_scope(pool=self.pool,mint=self.mint,slot=self.slot)
        self.assertEqual(result.records[0].row,self.prefix[0])
        self.assertEqual([r.version for r in scope.observations],[1,2])
        self.assertTrue(all(not r.issuance_enabled for r in scope.observations))
        self.assertEqual(scope.observations[1].row[6],self.snapshot.rows[-1][6])
        self.assertEqual(self.snapshot,before)
        out=scope.diagnostic();self.assertEqual(out['decision'],'REJECT')
        for k,v in out.items():
            if type(v) is bool:self.assertFalse(v,k)
        with self.assertRaises(FrozenInstanceError):result.head=(0,'f'*64)

    def test_wrapped_new_v1_and_union_are_in_one_scope(self):
        self.extra.append(('legacy_observation',PROFILE,{**self.receipt,'captured_at':0}));self.build()
        self.assertEqual([r.profile for r in self.scope().observations],[PROFILE,chain.PARENT_PROFILE,PROFILE])

    def test_certificate_capability_is_exact_version_profile_and_source(self):
        for key,value in [('version',1),('profile',PROFILE),('source_id','other'),('network','devnet'),('source_kind','coordinator_capture')]:
            with self.subTest(key=key):
                self.setUp();self.capabilities[-1][key]=value;self.build();self.refused()

    def test_legacy_capability_cannot_disappear_or_rebind(self):
        self.certificate['capabilities']=self.capabilities[1:];self.build();self.refused()
        self.setUp();self.capabilities[0]['source_kind']='coordinator_capture';self.build();self.refused()

    def test_duplicate_capabilities_and_invalid_enabled_flag_reject(self):
        self.capabilities.append(copy.deepcopy(self.capabilities[-1]));self.build();self.refused()
        self.setUp();self.capabilities[-1]['issuance_enabled']=1;self.build();self.refused()

    def test_all_scope_markers_enumerated_without_status_filter(self):
        for status in ('PENDING','FAILED','INCOMPLETE','UNAVAILABLE'):self.add_marker(status=status)
        self.build();scope=self.scope();self.assertEqual(len(scope.markers),4)
        self.assertEqual(set(scope.reasons),{'CHAIN_UNRESOLVED_'+s for s in ('PENDING','FAILED','INCOMPLETE','UNAVAILABLE')})

    def test_unknown_slot_blocks_all_slots_only_for_known_pool_mint(self):
        self.add_marker(slot=None);self.build()
        self.assertIn('CHAIN_UNKNOWN_SLOT_MARKER',self.scope(slot=self.slot+1).reasons)
        self.assertEqual(self.scope(pool=self.mint,mint=self.pool).markers,())

    def test_unknown_identity_blocks_other_scopes_not_hidden_by_known_slot(self):
        for changes in ({'pool':None},{'mint':None},{'pool':None,'mint':None,'slot':None}):
            with self.subTest(changes=changes):
                self.setUp();self.add_marker(**changes);self.build()
                self.assertIn('CHAIN_UNKNOWN_IDENTITY_MARKER',self.scope(pool=self.mint,mint=self.pool,slot=self.slot+1).reasons)

    def test_new_observation_does_not_resolve_marker_or_drop_failed_attempt(self):
        self.add_marker(status='FAILED');self.add_parent(captured_at=99999999);self.build()
        scope=self.scope();self.assertEqual(len(scope.observations),3);self.assertEqual(len(scope.markers),1)
        self.assertIn('CHAIN_UNRESOLVED_FAILED',scope.reasons)

    def test_unknown_type_version_profile_and_resolution_refuse_entire_chain(self):
        for field,value in [('type','resolve_marker'),('version',3),('version',True),('profile','unknown')]:
            with self.subTest(field=field,value=value):
                self.setUp();rows=list(self.snapshot.rows);payload=json.loads(rows[-1][6]);payload[field]=value
                rows[-1]=row(rows[-1][0],rows[-1][1],rows[-1][2:6],payload,rows[-1][8]);self.rechain(rows);self.refused()

    def test_original_prefix_cannot_be_reserialized_even_with_recomputed_head(self):
        rows=list(self.snapshot.rows);old=list(rows[0]);old[6]=json.dumps(json.loads(old[6]),indent=2);rows[0]=tuple(old)
        self.rechain(rows);self.refused()

    def test_descriptor_and_prefix_rewrite_cannot_be_hidden_by_valid_new_chain(self):
        rows=list(self.snapshot.rows);payload=json.loads(rows[0][6]);payload['receipt']['captured_at']+=1
        rows[0]=row(1,rows[0][1],rows[0][2:6],payload,digest(self.descriptor));self.rechain(rows);self.refused()
        self.setUp();self.descriptor['ledger_id']='rewritten';self.build()
        self.anchor=replace(self.anchor,descriptor_body=self.anchor.descriptor_body.replace('rewritten','synthetic-ledger'))
        self.refused()

    def test_old_head_count_schema_dispatch_and_descriptor_hash_transition_pins(self):
        for key,value in [('legacy_count',True),('legacy_count',0),('legacy_head_hash','e'*64),
                          ('legacy_descriptor_hash','e'*64),('schema_hash','e'*64),('dispatch_hash','e'*64)]:
            with self.subTest(key=key):
                self.setUp();self.certificate[key]=value;self.build();self.refused()

    def test_unpinned_or_mismatched_certificate_and_payload_hash_refuse(self):
        self.snapshot=replace(self.snapshot,certificate_hash='e'*64);self.refused()
        self.setUp();self.anchor=replace(self.anchor,certificate_hash='e'*64);self.refused()
        self.setUp();rows=list(self.snapshot.rows);payload=json.loads(rows[-1][6]);payload['certificate_hash']='e'*64
        rows[-1]=row(rows[-1][0],rows[-1][1],rows[-1][2:6],payload,rows[-1][8]);self.rechain(rows);self.refused()

    def test_transition_sentinels_and_position_must_be_exact(self):
        for index in (2,3,4,5):
            self.setUp();rows=list(self.snapshot.rows);old=list(rows[1]);old[index]='fixture-source';rows[1]=tuple(old)
            self.rechain(rows);self.refused()
        self.setUp();rows=list(self.snapshot.rows);rows[1],rows[2]=rows[2],rows[1];self.rechain(rows);self.refused()

    def test_marker_sentinels_cannot_be_valid_scope_or_fake_slot(self):
        self.add_marker(slot=None,pool=None);self.build();rows=list(self.snapshot.rows)
        for index,value in ((2,self.pool),(4,str(self.slot))):
            with self.subTest(index=index):
                changed=list(rows[-1]);changed[index]=value;self.rechain(rows[:-1]+[tuple(changed)]);self.refused()

    def test_boolean_slots_and_malformed_identity_refuse(self):
        for changes in ({'slot':True},{'pool':'not-an-address'},{'manifest_hash':'short'},{'captured_at':True}):
            self.setUp();self.extra=[];self.add_parent(**changes);self.build();self.refused()

    def test_signed_invalid_times_retained_for_later_semantic_rejection(self):
        self.extra=[];self.add_parent(snapshot_time=-1,captured_at=-10);self.build()
        self.assertEqual(len(self.scope().observations),2)
        self.assertFalse(self.scope().diagnostic()['raw_conflicts_checked'])

    def test_head_count_hash_gap_duplicate_publication_and_payload_refuse(self):
        for change in ('count','hash','gap','publication','payload'):
            self.setUp();rows=list(self.snapshot.rows)
            if change=='count':self.snapshot=replace(self.snapshot,head=(2,self.snapshot.head[1]))
            elif change=='hash':self.snapshot=replace(self.snapshot,head=(3,'e'*64))
            elif change=='gap':old=list(rows[-1]);old[0]=4;rows[-1]=tuple(old);self.snapshot=replace(self.snapshot,rows=tuple(rows))
            elif change=='publication':old=list(rows[-1]);old[1]=rows[0][1];rows[-1]=tuple(old);self.rechain(rows)
            else:rows.append(rows[-1]);self.rechain(rows)
            self.refused()

    def test_no_partial_prefix_or_prior_success_cache_after_tail_damage(self):
        self.validate();rows=list(self.snapshot.rows);old=list(rows[-1]);old[9]='e'*64;rows[-1]=tuple(old)
        self.snapshot=replace(self.snapshot,rows=tuple(rows));self.refused()
        self.snapshot=replace(self.snapshot,rows=self.prefix,head=self.anchor.legacy_head);self.refused()

    def test_bounded_rows_and_payload_preflight_precedes_json_hashing(self):
        self.snapshot=replace(self.snapshot,rows=self.snapshot.rows*(chain.MAX_RECORDS//3+1))
        with patch.object(chain,'_json',side_effect=AssertionError('must preflight')):self.refused()
        self.setUp();rows=list(self.snapshot.rows);old=list(rows[-1]);old[6]='x'*(chain.MAX_BYTES+1);rows[-1]=tuple(old)
        self.snapshot=replace(self.snapshot,rows=tuple(rows))
        with patch.object(chain,'_json',side_effect=AssertionError('must preflight')):self.refused()

    def test_multibyte_payload_and_total_shared_byte_bound_precede_parse(self):
        rows=list(self.snapshot.rows);old=list(rows[-1]);old[6]='é'*(chain.MAX_BYTES//2+1);rows[-1]=tuple(old)
        self.snapshot=replace(self.snapshot,rows=tuple(rows))
        with patch.object(chain,'_json',side_effect=AssertionError('must preflight')):self.refused()
        self.setUp()
        with patch.object(chain,'MAX_TOTAL_BYTES',100),patch.object(chain,'_json',side_effect=AssertionError('must preflight')):self.refused()

    def test_scoped_bound_counts_all_profiles_and_markers_together(self):
        self.add_marker();self.build()
        with patch.object(chain,'MAX_SCOPED_RECORDS',2):
            with self.assertRaises(chain.ChainUnavailable):self.scope()
        self.assertEqual(len(self.scope().observations)+len(self.scope().markers),3)

    def test_shared_reference_bound_deduplicates_across_profiles_not_per_record(self):
        self.add_marker(evidence_hashes=[digest('extra witness')]);self.build()
        scope=self.scope();bound=len(scope.evidence_hashes)
        with patch.object(chain,'MAX_REFERENCES',bound):self.assertEqual(self.scope(),scope)
        with patch.object(chain,'MAX_REFERENCES',bound-1):
            with self.assertRaises(chain.ChainUnavailable):self.scope()

    def test_certificate_and_capability_bounds_do_not_allow_per_profile_quota(self):
        with patch.object(chain,'MAX_CAPABILITIES',2):self.refused()
        self.snapshot=replace(self.snapshot,certificate_body='x'*(chain.MAX_BYTES+1));self.refused()

    def test_duplicate_keys_deep_json_nonfinite_unknown_fields_refuse(self):
        for body in ('{"version":2,"version":2}', '{"x":'+('['*20)+'0'+(']'*20)+'}', '{"x":NaN}'):
            self.setUp();self.snapshot=replace(self.snapshot,certificate_body=body);self.refused()
        self.setUp();self.raw['source_authenticated']=True;self.extra=[];self.add_parent();self.build();self.refused()

    def test_empty_legacy_prefix_uses_descriptor_seed_and_requires_transition(self):
        self.prefix=();self.certificate.update(legacy_count=0,legacy_head_hash=digest(self.descriptor));self.build()
        self.assertEqual(len(self.scope().observations),1)
        self.assertEqual(self.validate().transition_row[0],1)

    def test_scope_bad_types_refuse_and_other_slot_does_not_relabel_actual_bank(self):
        self.assertEqual(self.scope(slot=self.slot+1).observations,())
        for changes in ({'slot':True},{'pool':'invalid'},{'mint':None}):
            with self.assertRaises(chain.ChainUnavailable):self.scope(**changes)

    @unittest.skipUnless(Path('/proc/self/mountinfo').is_file(),'Legacy protected ledger fixture requires Linux')
    def test_actual_persisted_v1_rows_and_file_bytes_unchanged_by_pure_dispatch(self):
        f=legacy_fixture.PoolReceiptLedgerTests();f.setUp();self.addCleanup(f.doCleanups)
        f.writer.publish('legacy-original',f.receipt)
        with sqlite3.connect(f.writer.path) as c:
            self.descriptor=json.loads(c.execute('SELECT body FROM ledger_descriptor').fetchone()[0])
            self.prefix=tuple(c.execute('SELECT * FROM coordinator_receipts ORDER BY seq'))
        self.certificate.update(legacy_descriptor_hash=digest(self.descriptor),legacy_head_hash=self.prefix[-1][-1])
        self.build();before={p.name:p.read_bytes() for p in f.root.iterdir() if p.is_file()}
        result=self.validate();self.assertEqual(result.records[0].row,self.prefix[0])
        self.assertEqual(before,{p.name:p.read_bytes() for p in f.root.iterdir() if p.is_file()})
        self.assertEqual(len(f.writer.policy(**f.scope()).receipts),1)

    def test_recomputed_truncated_tail_cannot_replace_independent_current_head_pin(self):
        self.add_marker(status='FAILED');self.build();self.validate()
        shortened=self.snapshot.rows[:-1]
        self.snapshot=replace(self.snapshot,rows=shortened,head=(len(shortened),shortened[-1][-1]))
        with self.assertRaisesRegex(chain.ChainUnavailable,'CHAIN_CURRENT_HEAD_PIN_MISMATCH'):self.validate()

    def test_real_256_scope_limit_includes_both_profiles_and_254_markers(self):
        for i in range(254):self.add_marker(capture_id='marker:'+str(i))
        self.build();scope=self.scope()
        self.assertEqual((len(scope.observations),len(scope.markers)),(2,254))
        self.add_marker(capture_id='marker:overflow');self.build()
        with self.assertRaisesRegex(chain.ChainUnavailable,'CHAIN_SCOPE_RECORD_BOUND'):self.scope()

    def test_empty_unknown_list_rows_or_unbounded_legacy_anchor_refuse_without_copy(self):
        self.snapshot=replace(self.snapshot,rows=list(self.snapshot.rows));self.refused()
        self.setUp();self.anchor=replace(self.anchor,descriptor_body='x'*(chain.MAX_BYTES+1));self.refused()

    def test_post_transition_v1_cannot_reuse_unwrapped_legacy_payload(self):
        rows=list(self.snapshot.rows);rows.append(row(4,'unwrapped-v1',self.prefix[0][2:6],
            {'version':1,'profile':PROFILE,'receipt':{**self.receipt,'captured_at':0}},rows[-1][-1]))
        self.rechain(rows);self.refused()

    def test_additional_transition_not_accepted_as_observation(self):
        rows=list(self.snapshot.rows);data=json.loads(rows[1][6]);rows.append(row(4,'transition-again',
            (chain.TRANSITION_SENTINEL,)*4,data,rows[-1][-1]));self.rechain(rows);self.refused()
