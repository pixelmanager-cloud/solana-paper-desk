"""SYNTHETIC_TEST_ONLY: supported paper profile; no live token acceptance."""
import base64
import copy
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from solders.pubkey import Pubkey
from desk import paper_cycle as cycle, quote_execution as qe
from desk.security import TOKEN_2022, TOKEN_PROGRAM, mint_policy, holding_policy, entry_token_policy
from desk.token2022_paper import selected, NAME
from desk.model import digest, canonical, load_config
from desk.ownership_acquisition import acquire
from desk.job_persistence import JobPersistence
from tests.test_graduation_witness import fixture as migration_fixture
from desk.programs import unbase58, schemas
from desk.security import base58
from tests import test_token2022_state as layout
from tests import test_paper_cycle as cycle_fixtures, test_ownership_acquisition as acquisition_fixtures
from tests.test_paper_cycle import dump


def account(raw,program=TOKEN_2022):
    return {'owner':program,'executable':False,'data':[base64.b64encode(raw).decode(),'base64']}


def mint_bytes(mint,base=None):
    return layout.mint_bytes(base=base,pointer=mint,metadata_data=layout.metadata(mint=mint))


class PolicyTests(unittest.TestCase):
    def check(self,raw,mint=layout.MINT):
        return mint_policy(account(raw),mint=mint,token_profile_version=1)

    def test_supplied_public_capture_is_structural_only_and_stale_rejects(self):
        from desk.live_observation import ingest_mint, ProviderObservation, ObservationError
        fixture=json.loads((Path(__file__).parents[1]/'fixtures/token2022-metadata-only-public.json').read_text())
        self.assertEqual(fixture['provenance'],'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE')
        raw=base64.b64decode(fixture['account']['data'][0]);self.assertEqual(len(raw),391)
        original=copy.deepcopy(fixture)
        policy=mint_policy(fixture['account'],mint=fixture['mint'],token_profile_version=1)
        self.assertEqual(policy['decision'],'PASS_TOKEN_POLICY')
        with patch('socket.socket',side_effect=AssertionError('No URI or provider access')):
            with self.assertRaises(ObservationError):
                ingest_mint(lambda:ProviderObservation('stale-public-fixture',1,
                    {'mint':fixture['mint'],'account':fixture['account'],'slot':fixture['context']['slot']}),
                    mint=fixture['mint'],now=100,token_profile_version=1)
        self.assertEqual(fixture,original)

    def test_actual_identity_required_and_legacy_default_strict(self):
        raw=mint_bytes(layout.MINT)
        self.assertEqual(self.check(raw)['decision'],'PASS_TOKEN_POLICY')
        self.assertEqual(mint_policy(account(raw))['reasons'],['TOKEN_2022_NOT_ALLOWED'])
        for mint in (None,layout.OTHER):
            self.assertEqual(self.check(raw,mint)['decision'],'SKIP')
        self.assertEqual(self.check(mint_bytes(layout.MINT,layout.mint_base(supply=123,decimals=8)))['supply_raw'],'123')

    def test_all_unsupported_extension_types_and_malformed_tails_block(self):
        good=mint_bytes(layout.MINT)
        for kind in list(range(0,30))+[65535]:
            if kind in (18,19):continue
            with self.subTest(kind=kind):self.assertEqual(self.check(good+layout.tlv(kind))['decision'],'SKIP')
        for raw in (good+layout.tlv(18,bytes(64)),good+b'\0',good+bytes(4),good[:-1],good[:165],good+layout.tlv(19,b'x')):
            with self.subTest(length=len(raw)):self.assertEqual(self.check(raw)['decision'],'SKIP')

    def test_authorities_metadata_identity_utf8_and_payloads_block(self):
        variants=[layout.mint_bytes(pointer_authority=layout.OWNER),layout.mint_bytes(pointer=layout.OTHER),
            layout.mint_bytes(metadata_data=layout.metadata(authority=layout.OWNER)),
            layout.mint_bytes(metadata_data=layout.metadata(mint=layout.OTHER)),
            layout.mint_bytes(metadata_data=layout.metadata(pairs=[('x','y')])),
            layout.mint_bytes(metadata_data=layout.metadata(text={'name':b'\xff','symbol':'s','uri':'u'})),
            layout.mint_bytes(base=layout.mint_base(authority=layout.OWNER)),
            layout.mint_bytes(base=layout.mint_base(freeze=layout.OWNER)),
            layout.mint_bytes(base=layout.mint_base(initialized=2)),
            layout.mint_bytes(base=layout.mint_base(supply=0))]
        for raw in variants:
            with self.subTest(raw=raw[-8:].hex()):self.assertEqual(self.check(raw)['decision'],'SKIP')
        modified=account(mint_bytes(layout.MINT));modified['executable']=0
        self.assertEqual(mint_policy(modified,mint=layout.MINT,token_profile_version=1)['decision'],'SKIP')

    def test_immutable_owner_is_not_delegate_close_freeze_or_identity_waiver(self):
        def checked(raw,mint=layout.MINT,owner=layout.OWNER):
            return holding_policy(account(raw),mint,owner,token_profile_version=1)
        self.assertEqual(checked(layout.account_bytes())['decision'],'PASS_HOLDING_POLICY')
        for raw in (layout.account_bytes(delegate=layout.OTHER),layout.account_bytes(close=layout.OTHER),
                    layout.account_bytes(allowance=1),layout.account_bytes(state=2),
                    layout.account_bytes(native=1),layout.account_bytes(entries=b''),
                    layout.account_bytes(entries=layout.tlv(7,b'x')),layout.account_bytes(entries=layout.tlv(7)+layout.tlv(7)),
                    layout.account_bytes(entries=layout.tlv(7)+layout.tlv(2,bytes(8)))):
            self.assertEqual(checked(raw)['decision'],'SKIP')
        self.assertEqual(checked(layout.account_bytes(),owner=layout.OTHER)['decision'],'SKIP')
        self.assertEqual(checked(layout.account_bytes(),mint=layout.OTHER)['decision'],'SKIP')
        self.assertEqual(holding_policy(account(layout.account_bytes()),layout.MINT,layout.OWNER)['decision'],'SKIP')

    def test_config_requires_exact_explicit_paper_versions_no_event_waiver(self):
        cfg={'mode':'paper','paper_signal_policy_version':3,'paper_quote_execution_version':1,'paper_token_profile_version':1}
        self.assertEqual(selected(cfg),1);self.assertEqual(selected({**cfg,'paper_token_profile_version':2}),2);self.assertEqual(selected({}),0)
        for value in (True,None,0,3,'1',1.0):
            with self.assertRaises(ValueError):selected({**cfg,'paper_token_profile_version':value})
        for changed in ({'mode':'live'},{'paper_signal_policy_version':2},{'paper_quote_execution_version':None}):
            with self.assertRaises(ValueError):selected({**cfg,**changed})
        event={'mint':layout.MINT,'ts':100,'extensions_safe':True,
               'paper_token_profile_version':1,'token_evidence':{'mint':layout.MINT,'observed_at':100,'account':account(mint_bytes(layout.MINT))}}
        self.assertEqual(entry_token_policy(event),['TOKEN_2022_NOT_ALLOWED'])
        self.assertEqual(entry_token_policy(event,cfg),[])
        event['token_evidence']['observed_at']=89
        self.assertEqual(entry_token_policy(event,cfg),['TOKEN_EVIDENCE_STALE'])


class AcquisitionProfileTests(unittest.TestCase):
    def setUp(self):
        self.f=acquisition_fixtures.AcquisitionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.values[0]=account(mint_bytes(self.f.mint))

    def test_persisted_acquisition_profile_and_resume_no_reset(self):
        f=self.f
        result=acquire(f.db,f.evidence,f.rpc,mint=f.mint,paper_token_profile_version=1)
        self.assertNotEqual(result['status'],'UNSUPPORTED_TOKEN',result)
        uid=result['scan_id'];descriptor=f.jobs().descriptor(uid)
        self.assertEqual(descriptor['paper_token_profile_version'],1)
        self.assertEqual(result['report']['token_policy']['decision'],'PASS_TOKEN_POLICY')
        before=dump(f.db),dump(f.evidence)
        with self.assertRaises(ValueError):acquire(f.db,f.evidence,f.rpc,scan_id=uid)
        self.assertEqual((dump(f.db),dump(f.evidence)),before)
        retry=acquire(f.db,f.evidence,f.rpc,scan_id=uid,paper_token_profile_version=1)
        self.assertEqual(retry['status'],'ALREADY_COMPLETE');self.assertEqual(len(f.calls),3)
        self.assertEqual(f.progress().admission(uid)['requests_used'],3)

    def test_hazard_rejects_before_cutoff_and_history(self):
        self.f.values[0]=account(mint_bytes(self.f.mint)+layout.tlv(14,bytes(64)))
        result=acquire(self.f.db,self.f.evidence,self.f.rpc,mint=self.f.mint,paper_token_profile_version=1)
        self.assertEqual(result['status'],'UNSUPPORTED_TOKEN');self.assertEqual(result['provider_calls'],1)


class VerticalProfileTests(unittest.TestCase):
    def setUp(self):
        self.f=cycle_fixtures.PaperCycleTests()
        profile=getattr(self,'profile',1)
        admit=JobPersistence.admit
        def profiled(jobs,*args,**kwargs):
            return admit(jobs,*args,**kwargs,paper_token_profile_version=profile)
        with patch.object(JobPersistence,'admit',profiled):self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f=self.f;protocol=f.f.protocol
        migration,mint,pool=migration_fixture('migrate_v2')
        migration['blockTime']=f.f.at-600
        ix=migration['meta']['innerInstructions'][0]['instructions'][0]
        raw=bytearray(unbase58(ix['data']));raw[136:144]=migration['blockTime'].to_bytes(8,'little',signed=True);ix['data']=base58(raw)
        spec=next(x for x in schemas()[migration['transaction']['message']['instructions'][0]['programId']].values() if x['name']=='migrate_v2')
        for index,item in enumerate(spec['accounts']):
            if item['name'] in ('token_program','base_token_program'):
                migration['transaction']['message']['instructions'][0]['accounts'][index]=TOKEN_2022
        response_hash=f.f.progress.store.save({'data':[migration],'paginationToken':None})
        ref=f.f.progress.store.save({'kind':'history_request_v1','method':'getTransactionsForAddress',
            'params':[pool,{'transactionDetails':'full','commitment':'finalized','encoding':'jsonParsed'}],
            'response_hash':response_hash})
        f.item=replace(f.item,graduation_refs=(ref,))
        # Original source-built pool now owns the Token-2022 ATA; no event booleans.
        ata=Pubkey.find_program_address([bytes(protocol.pool),bytes(Pubkey.from_string(TOKEN_2022)),bytes(protocol.mint)],
                                      Pubkey.from_string('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'))[0]
        raw=bytearray(protocol.raw);raw[139:171]=bytes(ata);protocol.raw=bytes(raw)
        rpc=protocol.rpc
        def wrapped(method,params):
            result=copy.deepcopy(rpc(method,params))
            if method=='getMultipleAccounts':
                base=base64.b64decode(result['value'][6]['data'][0])
                result['value'][6]=account(mint_bytes(f.target.mint,base))
                vault=base64.b64decode(result['value'][0]['data'][0])
                result['value'][0]=account(vault+b'\x02'+layout.tlv(7))
            return result
        protocol.rpc=wrapped
        f.cfg={**f.cfg,'paper_token_profile_version':profile}
        f.path=Path(f.f.tmp.name)/'token2022-new-experiment.sqlite'
        cycle.initialize(f.path,f.cfg)
        f.http_calls=[];f.sell_output=10_000_000

    def test_actual_entry_mark_exit_restart_and_costs(self):
        # Reuse the existing actual vertical fixture assertions, not duplicate wrappers.
        self.f.test_actual_entry_mark_full_exit_restart_originals_and_costs()
        from desk.experiment_report import experiment_report
        self.assertIsNotNone(experiment_report(self.f.path,now=self.f.f.at))

    def test_actual_acquisition_entry_monitor_exit_restart_same_counter(self):
        f=self.f
        migration=f.f.progress.store.load(f.item.graduation_refs[0])
        rows=f.f.progress.store.load(migration['response_hash'])['data']
        def rpc(method,params):
            if method=='getAccountInfo':return {'context':{'slot':100},'value':f.f.protocol.rpc('getMultipleAccounts',[])['value'][6]}
            if method=='getSlot':return 110
            if method=='getTransactionsForAddress':return {'data':rows,'paginationToken':None}
            raise AssertionError('Unexpected acquisition read')
        acquired=acquire(f.f.jobs.path,f.f.progress.store.path,rpc,scan_id=f.target.scan_id,paper_token_profile_version=1)
        self.assertEqual(acquired['provider_calls'],3);self.assertEqual(acquired['report']['token_policy']['decision'],'PASS_TOKEN_POLICY')
        source=f.f.jobs.source(f.target.scan_id)
        entry=f.actual_cycle();self.assertEqual(entry['status'],'COMPLETE',entry)
        buy=next(x for x in entry['outcomes'] if x.get('side')=='buy')
        self.assertEqual(f.f.progress.admission(f.target.scan_id)['requests_used'],12)
        state=cycle._state(f.path,f.cfg);p=state['positions'][f.target.mint]
        item=replace(f.item,target=replace(f.target,amount_raw=qe.raw_quantity(p['qty'],6)),graduation_refs=())
        allowance=cycle.MonitoringBudget(f.f.progress.store,f.path,f.cfg,clock=lambda:f.f.at)
        allowance.provision()  # SYNTHETIC_TEST_ONLY explicit fixture provisioning.
        mark=f.actual_cycle(positions=(item,),candidates=(),monitoring=True)
        self.assertEqual(mark['status'],'COMPLETE',mark);self.assertFalse(any(x['type']=='fill' for x in mark['outcomes']))
        self.assertEqual(cycle._state(f.path,f.cfg)['positions'][f.target.mint]['qty'],p['qty'])
        f.sell_output=7_000_000
        exited=f.actual_cycle(positions=(item,),candidates=(),monitoring=True)
        self.assertEqual(exited['status'],'COMPLETE',exited)
        sell=next(x for x in exited['outcomes'] if x.get('side')=='sell')
        state=cycle._state(f.path,f.cfg);self.assertEqual(state['positions'],{})
        self.assertEqual(Decimal(state['cash']),Decimal(f.cfg['initial_equity_sol'])-Decimal(buy['amount_sol'])-Decimal(buy['fee_sol'])+Decimal(sell['proceeds_sol']))
        self.assertEqual(f.f.jobs.source(f.target.scan_id),source)
        self.assertEqual(f.f.progress.admission(f.target.scan_id)['requests_used'],12)
        self.assertEqual(allowance.snapshot()['total_used'],8)
        before=dump(f.path)
        restart=f.actual_cycle(candidates=(),monitoring=True)
        self.assertEqual(restart['status'],'COMPLETE');self.assertEqual(restart['attempted_requests'],0)
        self.assertEqual(allowance.snapshot()['total_used'],8)
        self.assertEqual(dump(f.path),before)

    def test_actual_partial_full_exit_exact_size_restart(self):
        self.f.test_actual_partial_then_full_exit_exact_sizes_within18()

    def test_saved_profile_cannot_adopt_old_ledger_or_be_removed_on_restart(self):
        f=self.f
        old=Path(f.f.tmp.name)/'cycle.sqlite';before=dump(old)
        with self.assertRaises(cycle.CycleBlocked):cycle._state(old,f.cfg)
        self.assertEqual(dump(old),before)
        entry=f.actual_cycle();self.assertEqual(entry['status'],'COMPLETE',entry)
        before=dump(f.path)
        with self.assertRaises(cycle.CycleBlocked):cycle._state(f.path,{k:v for k,v in f.cfg.items() if k!='paper_token_profile_version'})
        self.assertEqual(dump(f.path),before)
        state=cycle._state(f.path,f.cfg)
        position=state['positions'][f.target.mint]
        self.assertIn('UNRESOLVED_OWNERSHIP_HISTORY',position['entry_policy']['risk_flags'])
        self.assertEqual(position['quote_execution']['status'],'EXECUTION_UNVERIFIED')
        self.assertEqual(position['quote_execution']['token_profile'],{'version':1,'name':NAME})
        self.assertEqual(json.loads(position['quote_execution']['original_mint_json'])['account']['owner'],TOKEN_2022)

    def test_invalid_collector_profile_refuses_before_io_or_charge(self):
        with self.assertRaises(ValueError):self.f.f.collect(candidates=(self.f.target,),token_profile_version=True)
        self.assertEqual(self.f.f.calls,[])
        self.assertEqual(self.f.f.progress.admission(self.f.target.scan_id)['requests_used'],0)

    def test_actual_mint_hazard_stops_before_quote_or_fill(self):
        f=self.f;rpc=f.f.protocol.rpc
        def hazardous(method,params):
            response=rpc(method,params)
            if method=='getMultipleAccounts':
                value=response['value'][6]
                value['data'][0]=base64.b64encode(base64.b64decode(value['data'][0])+layout.tlv(14,bytes(64))).decode()
            return response
        f.f.protocol.rpc=hazardous
        source=f.f.jobs.source(f.target.scan_id)
        result=f.actual_cycle()
        self.assertFalse(any(x.get('type')=='fill' for x in result['outcomes']))
        self.assertEqual(result['attempted_requests'],1)
        self.assertEqual(cycle._state(f.path,f.cfg)['positions'],{})
        self.assertEqual(f.f.jobs.source(f.target.scan_id),source)

    def test_actual_vault_duplicate_extension_blocks_quote_and_entry(self):
        f=self.f;rpc=f.f.protocol.rpc
        def hazardous(method,params):
            response=rpc(method,params)
            if method=='getMultipleAccounts':
                value=response['value'][0]
                value['data'][0]=base64.b64encode(base64.b64decode(value['data'][0])+layout.tlv(7)).decode()
            return response
        f.f.protocol.rpc=hazardous
        result=f.actual_cycle()
        self.assertFalse(any(x.get('type')=='fill' for x in result['outcomes']))
        self.assertEqual(result['attempted_requests'],3)
        self.assertEqual(cycle._state(f.path,f.cfg)['positions'],{})
        self.assertTrue(all(request.method=='POST' for request in f.http_calls))

    def test_restart_hazardous_original_mint_rebind_is_rejected_readonly(self):
        f=self.f;entry=f.actual_cycle();self.assertEqual(entry['status'],'COMPLETE',entry)
        # Construct corrupt restore, not an authorized repair: changing both
        # local original hash and serialized mint bytes cannot approve a hook.
        with sqlite3.connect(f.path) as connection:
            state=json.loads(connection.execute("SELECT payload FROM state WHERE id=1").fetchone()[0])
            record=state['positions'][f.target.mint]['quote_execution']
            raw=json.loads(record['original_mint_json'])
            bad=base64.b64encode(base64.b64decode(raw['account']['data'][0])+layout.tlv(14,bytes(64))).decode()
            raw['account']['data'][0]=bad
            raw['original_rpc_observation']['result']['value']['data'][0]=bad
            record['original_mint_json']=canonical(raw);record['mint_hash']=digest(raw)
            connection.execute("UPDATE state SET payload=? WHERE id=1",(canonical(state),))
        before=dump(f.path)
        with self.assertRaises(ValueError):cycle._state(f.path,f.cfg)
        self.assertEqual(dump(f.path),before)

    def contradictory_programs(self,*,atomic_legacy,held,monitoring=False):
        case=VerticalProfileTests();case.setUp();f=case.f
        try:
            item=None
            if held:
                entry=f.actual_cycle();self.assertEqual(entry['status'],'COMPLETE',entry)
                state=cycle._state(f.path,f.cfg);position=copy.deepcopy(state['positions'][f.target.mint])
                item=replace(f.item,target=replace(f.target,amount_raw=qe.raw_quantity(position['qty'],6)),graduation_refs=())
                allowance=cycle.MonitoringBudget(f.f.progress.store,f.path,f.cfg,clock=lambda:f.f.at);allowance.provision()
                f.sell_output=7_000_000  # would trigger STOP if contradiction passed.
            if atomic_legacy:
                protocol=f.f.protocol
                ata=Pubkey.find_program_address([bytes(protocol.pool),bytes(Pubkey.from_string(TOKEN_PROGRAM)),bytes(protocol.mint)],
                    Pubkey.from_string('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'))[0]
                raw=bytearray(protocol.raw);raw[139:171]=bytes(ata);protocol.raw=bytes(raw)
                rpc=protocol.rpc
                def legacy(method,params):
                    result=rpc(method,params)
                    if method=='getMultipleAccounts':
                        for index,size in ((0,165),(6,82)):
                            result['value'][index]['owner']=TOKEN_PROGRAM
                            result['value'][index]['data'][0]=base64.b64encode(base64.b64decode(result['value'][index]['data'][0])[:size]).decode()
                    return result
                protocol.rpc=legacy
            response=cycle_fixtures.Response; originals=[]
            def wire(raw):
                value=json.loads(raw)
                account_value=value.get('result',{}).get('value') if type(value.get('result')) is dict else None
                if type(account_value) is dict and account_value.get('owner') in (TOKEN_PROGRAM,TOKEN_2022):
                    data=base64.b64decode(account_value['data'][0])[:82]
                    account_value.update(account(mint_bytes(f.target.mint,data) if atomic_legacy else data,
                                                  TOKEN_2022 if atomic_legacy else TOKEN_PROGRAM))
                    raw=canonical(value).encode()
                originals.append(raw);return response(raw)
            source=f.f.jobs.source(f.target.scan_id);before=f.f.progress.admission(f.target.scan_id)['requests_used']
            with patch.object(cycle_fixtures,'Response',side_effect=wire):
                result=f.actual_cycle(positions=(item,) if held else (),candidates=() if held else None,monitoring=held and monitoring)
            self.assertEqual(result['attempted_requests'],3,result)
            self.assertIn('MINT_POOL_TOKEN_PROGRAM_MISMATCH',result['blockers'])
            self.assertFalse(any(x.get('type')=='fill' for x in result['outcomes']))
            self.assertEqual(f.f.jobs.source(f.target.scan_id),source)
            state=cycle._state(f.path,f.cfg)
            if held:
                self.assertEqual(state['positions'][f.target.mint],position)
                self.assertEqual(allowance.snapshot()['total_used'],3 if monitoring else 0)
                self.assertEqual(f.f.progress.admission(f.target.scan_id)['requests_used'],before if monitoring else before+3)
            else:
                self.assertEqual(state['positions'],{})
                self.assertEqual(f.f.progress.admission(f.target.scan_id)['requests_used'],before+3)
            # Every synthetic received wire is genuinely retained, including
            # the contradictory pair; no normalization or replacement.
            with f.f.progress.store.connect() as connection:
                rows=connection.execute('SELECT hash FROM pages').fetchall()
            retained=[]
            for (key,) in rows:
                record=f.f.progress.store.load(key)
                if type(record) is dict and 'response_bytes_base64' in record:
                    retained.append(base64.b64decode(record['response_bytes_base64']))
            self.assertTrue(all(raw in retained for raw in originals))
        finally:case.doCleanups()

    def test_actual_both_program_substitutions_entry_reject_before_quote(self):
        for atomic_legacy in (False,True):
            with self.subTest(atomic_legacy=atomic_legacy):self.contradictory_programs(atomic_legacy=atomic_legacy,held=False)

    def test_actual_both_program_substitutions_held_stop_reject_before_quote(self):
        for atomic_legacy in (False,True):
            with self.subTest(atomic_legacy=atomic_legacy):self.contradictory_programs(atomic_legacy=atomic_legacy,held=True)

    def test_actual_monitoring_held_collector_rejects_original_owner_contradiction(self):
        self.contradictory_programs(atomic_legacy=False,held=True,monitoring=True)

    def test_replay_candidate_and_held_reject_consistently_hashed_owner_substitution(self):
        from desk.live_observation import ingest_mint,ProviderObservation
        from desk.paper_market_adapter import _replay_collected,MarketContext
        from desk.paper_exit_adapter import ExitContext
        f=self.f;collected=f.f.collect(candidates=(f.target,),token_profile_version=1).observations[0]
        self.assertIsNone(collected.failure)
        payload=json.loads(collected.mint.source.original_json)
        envelope=payload['original_rpc_observation'];old_hash=digest(envelope)
        legacy=account(base64.b64decode(payload['account']['data'][0])[:82],TOKEN_PROGRAM)
        envelope['result']['value']=legacy;payload['account']=legacy
        replacement=f.f.progress.store.save(envelope)
        mint=ingest_mint(lambda:ProviderObservation(collected.mint.source.source_id,collected.mint.source.observed_at,payload),
                         mint=f.target.mint,now=f.f.at,token_profile_version=1)
        refs=tuple(replacement if key==old_hash else key for key in collected.evidence_refs)
        forged=replace(collected,mint=mint,quote=replace(collected.quote,mint_source=mint.source),evidence_refs=refs)
        contexts=(MarketContext(f.f.at,f.target,mint.source.source_id,collected.quote.source.source_id,
                    'SYNTHETIC_TEST_ONLY',f.item.graduated_at,None,(),'25',token_profile_version=1),
                  ExitContext(f.f.at,f.target,mint.source.source_id,collected.quote.source.source_id,
                    'SYNTHETIC_TEST_ONLY',token_profile_version=1))
        before=dump(f.path)
        for context in contexts:
            with self.assertRaisesRegex(ValueError,'program owners disagree'):
                _replay_collected(forged,context,f.f.progress.store.load)
        self.assertEqual(dump(f.path),before)

    def test_profile_omission_and_hazard_reject_no_fill(self):
        f=self.f
        # Default collector remains strict, regardless of Token-2022 account bytes.
        f.cfg.pop('paper_token_profile_version');f.path=Path(f.f.tmp.name)/'strict-again.sqlite';cycle.initialize(f.path,f.cfg)
        strict=f.actual_cycle();self.assertFalse(any(x.get('type')=='fill' for x in strict['outcomes']))
        self.assertEqual(cycle._state(f.path,f.cfg)['positions'],{})
