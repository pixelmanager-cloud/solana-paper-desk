"""Persisted synthetic acquisition -> funding groups -> fanout -> raw snapshot.

The connection is a TEST HARNESS, not a live adapter. BUY_INTENT is never a
fill: seeds use the acquisition's witnessed token balances. Interval coverage
and PRIVATE/SERVICE/POOL labels are separately provisioned synthetic assumptions;
address-query exhaustion never creates whole-token completeness or ownership.
The independent oracle chooses concrete unique token IDs, not interval formulae.
"""
import base64
from copy import deepcopy
from dataclasses import replace
from itertools import combinations
from pathlib import Path
import tempfile
import unittest

from desk.decode import SYSTEM
from desk.distribution_exposure import Balance, Classification, Kind, Position, Seed, Transfer, ValidatedSnapshot, trace_exposure
from desk.evidence import EvidenceStore
from desk.history import collect_history
from desk.model import digest
from desk.ownership_signals import funding_groups
from desk.programs import schemas
from desk.providers import PUMP
from desk.replay_history import replay_history
from desk.security import TOKEN_PROGRAM, base58, account_bytes, holding_policy
from desk.programs import unbase58
from desk.ownership_snapshot import validate_bank


class HolderExposureEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = EvidenceStore(Path(self.tmp.name)/'evidence.sqlite')
        self.mint = base58(bytes([7])*32)
        self.accounts = {a: base58(bytes([n])*32) for n,a in enumerate(('pool','a','b','c','d','hub'),20)}
        self.owners = {a: base58(bytes([n])*32) for n,a in enumerate(self.accounts,40)}
        self.funder = base58(bytes([60])*32)
        self.spec = next(s for s in schemas()[PUMP].values() if s['name']=='buy')
        self.initial = dict(pool=4,a=6,b=2,c=0,d=0,hub=0)
        self.flows = [(103,'a','c',2),(104,'a','c',2),(105,'b','c',2),
                      (106,'c','d',3),(107,'c','a',2),(108,'a','hub',4),(109,'hub','d',4)]

    def raw(self, signature, slot, at, instructions, pre=None, post=None):
        pre = pre or {}; post = post or {}
        names = list(dict.fromkeys(list(pre)+list(post)))
        rows = lambda amounts: [{'accountIndex':names.index(a),'mint':self.mint,'owner':self.owners[a],
                        'programId':TOKEN_PROGRAM,'uiTokenAmount':{'amount':str(n),'decimals':0}}
                        for a,n in amounts.items()]
        return {'signature':signature,'slot':slot,'blockTime':at,
                'transaction':{'signatures':[signature],'message':{
                    'accountKeys':[{'pubkey':self.accounts[a]} for a in names],'instructions':instructions}},
                'meta':{'err':None,'preTokenBalances':rows(pre),'postTokenBalances':rows(post),'innerInstructions':[]}}

    def token(self, source, destination, amount):
        return {'programId':TOKEN_PROGRAM,'parsed':{'type':'transferChecked','info':{
            'source':self.accounts[source],'destination':self.accounts[destination],'mint':self.mint,
            'tokenAmount':{'amount':str(amount),'decimals':0}}}}

    def acquisition(self, name, slot, amount, pool_balance):
        entries = {e['name']: e.get('address', self.mint) for e in self.spec['accounts']}
        entries.update(user=self.owners[name],mint=self.mint)
        buy = {'programId':PUMP,'accounts':[entries[e['name']] for e in self.spec['accounts']],
               'data':base58(bytes(self.spec['discriminator'])+amount.to_bytes(8,'little')+bytes(8))}
        return self.raw('buy-'+name,slot,100,[buy,self.token('pool',name,amount)],
                        {'pool':pool_balance,name:0},{'pool':pool_balance-amount,name:amount})

    def capture_rows(self, address, rows, *, start=100, end=110, partial=False):
        page = {'data':rows}
        if partial:page['paginationToken']='unqueried-continuation'
        _, coverage = collect_history(address,start,end,lambda *_:deepcopy(page),max_pages=1,
                                      capture=self.store.save,token_accounts='none' if address==self.mint else 'balanceChanged')
        return replay_history(coverage,self.store)

    def funding(self, wallet, fragments=(600000,500000), *, same_slot=False, partial=False):
        rows = [self.raw('fund-'+wallet+'-'+str(i),101 if same_slot else 100,100 if same_slot else 99,
                 [{'programId':SYSTEM,'parsed':{'type':'transfer','info':{
                     'source':self.funder,'destination':self.owners[wallet],'lamports':amount}}}])
                 for i,amount in enumerate(fragments)]
        return self.capture_rows(self.owners[wallet],rows,start=0,end=101,partial=partial)[1]

    def fixture(self, *, partial=False, hub_kind=Kind.PRIVATE):
        raws=[self.acquisition('a',101,6,self.initial['pool']+8),
              self.acquisition('b',102,2,self.initial['pool']+2)]
        amounts=dict(self.initial)
        for i,(slot,source,dest,n) in enumerate(self.flows):
            pre={source:amounts[source],dest:amounts[dest]}
            amounts[source]-=n;amounts[dest]+=n
            raws.append(self.raw('flow-'+str(i),slot,slot-3,[self.token(source,dest,n)],pre,
                                 {source:amounts[source],dest:amounts[dest]}))
        observations,coverage=self.capture_rows(self.mint,raws,partial=partial)
        queries=[self.funding('a'),self.funding('b',(400000,600000))]
        groups=funding_groups(self.mint,observations,queries,self.store,{},100)
        # Independently witnessed initial boundary: never use BUY_INTENT args.
        seeds=[]
        for obs in observations:
            if not obs['signature'].startswith('buy-'):continue
            name=obs['signature'][4:]
            witness=next(r for r in obs['token_deltas'] if r['account']==self.accounts[name])
            seeds.append(Seed(self.accounts[name],int(witness['post_raw'])-int(witness['pre_raw'])))
        rows=lambda values:tuple(Balance(self.accounts[a],self.owners[a],n) for a,n in values.items())
        # A persisted finalized fixture bank contains actual SPL account bytes;
        # amounts are reconstructed from raw offsets/control checks, not copied
        # from an asserted percentage or normalized holder summary.
        mint_bytes=bytearray(82);mint_bytes[36:44]=(12).to_bytes(8,'little');mint_bytes[45]=1
        account=lambda raw:{'owner':TOKEN_PROGRAM,'executable':False,'lamports':2039280,
                            'data':[base64.b64encode(raw).decode(),'base64']}
        values=[account(mint_bytes)]
        for a,n in amounts.items():
            raw=bytearray(165);raw[:32]=unbase58(self.mint);raw[32:64]=unbase58(self.owners[a])
            raw[64:72]=n.to_bytes(8,'little');raw[108]=1
            values.append(account(raw))
        bank={'method':'getMultipleAccounts','params':[[self.mint]+list(self.accounts.values()),
                   {'encoding':'base64','commitment':'finalized'}],
              'result':{'context':{'slot':110},'value':values}}
        current_ref=self.store.save(bank)
        replayed=self.store.load(current_ref)
        self.assertEqual(validate_bank(self.mint,set(self.accounts.values()),replayed),110)
        raw_amounts={}
        for a,value in zip(amounts,replayed['result']['value'][1:]):
            checked=holding_policy(value,self.mint,self.owners[a])
            self.assertEqual(checked['decision'],'PASS_HOLDING_POLICY',checked)
            raw_amounts[a]=int.from_bytes(account_bytes(value)[64:72],'little')
        self.assertEqual(raw_amounts,amounts)
        snapshot=ValidatedSnapshot(self.mint,102,110,12,rows(self.initial),rows(raw_amounts),current_ref,
                                   coverage['evidence_hash'],True,not partial)
        transfers=tuple(Transfer(o['signature'],Position(o['slot'],0,0),t['source'],t['destination'],
                                int(t['amount_raw']),o['payload_hash'])
                        for o in observations if o['slot']>102 for t in o['transfers'] if t['asset']=='TOKEN')
        labels=tuple(Classification(self.accounts[a],self.owners[a],self.mint,
                   Kind.POOL if a=='pool' else hub_kind if a=='hub' else Kind.PRIVATE,102,110,
                   ('synthetic-label-'+a,),True) for a in amounts)
        return snapshot,transfers,tuple(seeds),labels,groups,amounts,raws,observations,coverage

    def oracle(self):
        # Unique token identities distinguish physical units. Enumerate every
        # feasible selected subset at every send; no FIFO or interval shortcut.
        ids={}; cohort=set(); serial=0
        for a,n in self.initial.items():
            ids[a]=tuple(range(serial,serial+n))
            if a in ('a','b'):cohort.update(ids[a])
            serial+=n
        states={tuple(ids[a] for a in self.initial)}; names=list(self.initial)
        for _,source,dest,n in self.flows:
            si,di=names.index(source),names.index(dest); following=set()
            for state in states:
                for chosen in combinations(state[si],n):
                    moved=set(chosen); after=list(state)
                    after[si]=tuple(x for x in state[si] if x not in moved)
                    after[di]=tuple(sorted(state[di]+chosen));following.add(tuple(after))
            states=following
        return {a:(len(next(iter(states))[i]), min(len(cohort.intersection(s[i])) for s in states),
                   max(len(cohort.intersection(s[i])) for s in states)) for i,a in enumerate(names)}

    def test_persisted_early_acquisition_ordered_split_cycle_to_current_oracle(self):
        snapshot,transfers,seeds,labels,groups,amounts,raws,_,_=self.fixture()
        original=deepcopy(raws);result=trace_exposure(snapshot,tuple(reversed(transfers)),seeds,labels)
        oracle=self.oracle();by_account={h.account:h for h in result.holders}
        for a,(balance,minimum,maximum) in oracle.items():
            holder=by_account[self.accounts[a]]
            self.assertEqual(holder.balance,balance)
            if a!='pool':
                self.assertLessEqual(holder.lower_bound,minimum)
                self.assertGreaterEqual(holder.possible,maximum)
        self.assertEqual(amounts,dict(pool=4,a=0,b=0,c=1,d=7,hub=0))
        self.assertEqual((result.lower_bound,result.unresolved,result.excluded),(4,4,4))
        self.assertEqual(by_account[self.accounts['c']].lower_bound,1)
        self.assertEqual(by_account[self.accounts['d']].lower_bound,3)
        self.assertEqual(result.processed_transfers,7)
        self.assertEqual([e['amount_raw'] for e in groups['edges']],['1100000','1000000'])
        self.assertTrue(all(e['fragmented'] for e in groups['edges']))
        self.assertEqual(groups['shared_sources'][0]['classification'],'UNKNOWN_SERVICE_OR_PRIVATE_SOURCE')
        self.assertFalse(groups['common_control_verified']);self.assertFalse(groups['funding_history_complete'])
        self.assertEqual(raws,original)

    def test_commingled_unseeded_holdings_against_unique_token_subset_oracle(self):
        # Two pre-existing noncohort units share the relay with witnessed early
        # acquisitions. The oracle explores physical subsets, preserving global
        # conservation/correlation rather than choosing FIFO or copying bounds.
        self.initial.update(pool=2,c=2)
        snapshot,transfers,seeds,labels,*_=self.fixture()
        result=trace_exposure(snapshot,transfers,seeds,labels)
        oracle=self.oracle();by_account={h.account:h for h in result.holders}
        self.assertEqual(oracle['c'],(3,1,3))
        self.assertEqual(oracle['d'],(7,5,7))
        for a,(balance,minimum,maximum) in oracle.items():
            holder=by_account[self.accounts[a]]
            self.assertEqual(holder.balance,balance)
            if a!='pool':
                self.assertLessEqual(holder.lower_bound,minimum)
                self.assertGreaterEqual(holder.possible,maximum)
        self.assertEqual((result.lower_bound,result.unresolved,result.excluded),(2,8,2))

    def test_service_hub_retains_downstream_unknown_and_oracle_raw_balances(self):
        s,t,seeds,labels,groups,amounts,*_=self.fixture(hub_kind=Kind.SERVICE)
        r=trace_exposure(s,t,seeds,labels)
        self.assertEqual((r.lower_bound,r.unresolved,r.excluded),(4,4,4))
        self.assertIn('SERVICE_OR_UNCLASSIFIED_PATH_UNRESOLVED',r.reasons)
        self.assertEqual({h.account:h.balance for h in r.holders},{self.accounts[a]:n for a,n in amounts.items()})
        self.assertFalse(groups['common_control_verified'])

    def test_partial_saved_history_cannot_be_promoted_by_matching_ending_balances(self):
        s,t,seeds,labels,_,amounts,_,_,coverage=self.fixture(partial=True)
        self.assertFalse(coverage['query_coverage_verified']);self.assertFalse(coverage['launch_history_complete'])
        r=trace_exposure(s,t,seeds,labels)
        self.assertEqual((r.lower_bound,r.unresolved,r.excluded),(0,12,0))
        self.assertIn('SNAPSHOT_OR_HISTORY_UNVERIFIED',r.reasons)
        self.assertEqual(sum(h.balance for h in r.holders),sum(amounts.values()))

    def test_partial_funding_pages_preserve_observed_fragments_without_full_control_claim(self):
        *_, observations,coverage=self.fixture()
        query=self.funding('a',partial=True)
        r=funding_groups(self.mint,observations,[query],self.store,{},100)
        self.assertEqual(r['edges'][0]['amount_raw'],'1100000')
        self.assertEqual(r['verified_window_wallets'],[])
        self.assertIn('EARLY_BUYER_FUNDING_QUERY_INCOMPLETE',r['reasons'])
        self.assertFalse(r['funding_history_complete']);self.assertFalse(r['common_control_verified'])

    def test_same_slot_funding_unknown_order_cannot_manufacture_material_group(self):
        *_, observations,coverage=self.fixture()
        q=self.funding('a',same_slot=True)
        r=funding_groups(self.mint,observations,[q],self.store,{},100)
        self.assertEqual(r['edges'],[])
        self.assertEqual(len(r['ambiguous_transfers']),2)
        self.assertIn('FUNDING_SAME_SLOT_ORDER_UNVERIFIED',r['reasons'])
        self.assertFalse(r['common_control_verified'])

    def test_same_slot_transfer_positions_without_unique_order_are_rejected(self):
        s,t,seeds,labels,*_=self.fixture()
        ambiguous=tuple(replace(row,position=Position(103,0,0)) for row in t[:2])+t[2:]
        with self.assertRaisesRegex(ValueError,'order position'):trace_exposure(s,ambiguous,seeds,labels)

    def test_zero_acquisition_seed_cannot_bypass_missing_cohort_gate(self):
        s,t,_,labels,*_=self.fixture()
        with self.assertRaises(ValueError):trace_exposure(s,t,(Seed(self.accounts['a'],0),),labels)

    def test_conflicting_same_slot_buy_points_never_choose_convenient_query(self):
        *_, observations,coverage=self.fixture()
        raw=self.acquisition('a',101,6,12)
        changed=deepcopy(raw);changed.update(signature='second-buy-a',blockTime=101)
        changed['transaction']['signatures']=['second-buy-a']
        conflicting,_=self.capture_rows(self.mint,[raw,changed])
        buy,changed=conflicting
        query=self.funding('a')
        for order in ([buy,changed],[changed,buy]):
            r=funding_groups(self.mint,order,[query],self.store,{},100)
            self.assertEqual(r['edges'],[])
            self.assertEqual(r['verified_window_wallets'],[])
            self.assertIn('EARLY_BUY_POINT_CONFLICT',r['reasons'])
            self.assertFalse(r['common_control_verified'])

    def test_conflicting_first_slot_buy_finality_remains_unresolved_in_both_orders(self):
        *_, observations,coverage=self.fixture()
        buy=deepcopy(next(o for o in observations if o['signature']=='buy-a'))
        changed=deepcopy(buy);changed.update(signature='unfinalized-buy-a',commitment='confirmed')
        query=self.funding('a')
        for order in ([buy,changed],[changed,buy]):
            r=funding_groups(self.mint,order,[query],self.store,{},100)
            self.assertEqual(r['edges'],[])
            self.assertIn('EARLY_BUY_POINT_CONFLICT',r['reasons'])
