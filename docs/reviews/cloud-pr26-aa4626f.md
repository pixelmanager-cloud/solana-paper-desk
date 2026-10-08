# PR26 repair independent review

Independent re-review (cloud-independent-review-v1) at aa4626fec148c244f841f0614abc4d18c52274dd; exact head rechecked immediately before submission. Prior P2 conflicting-capture finding is resolved within the protected-policy contract. No new blocking defect found in this repair.

The selected reference tuple now establishes endorsement only. All approved receipts for the same network/genesis/pool/base-mint/finalized slot are checked, across source IDs and independent of block-time or freshness grouping. Contradictory block times/raw bytes/owner/executable/lamports block admission; missing, unreadable, malformed or future sibling captures fail closed. Stale competing observations cannot erase an immutable-slot contradiction. Both selection directions and both vaults remain blocked for a conflicting scope.

Independent adversarial fixture script passed 302 cases: six-account lamports/owner/executable/data/space corruption, incomplete account sets, nested bool slots, confirmed request, RPC errors, oversized evidence, missing/hash-mismatched/non-dict/raising offline reads, cross-source block-time contradictions, stale competitors with either ref selected, and transport-equivalent positive captures with cached reads. All rejection cases kept production exclusion disabled. Metadata-equivalent captures still admit only the point label and keep interval/entry/chain-authentication flags false. Original 6000-versus-6001 reproduction is rejected in either selection direction, including cross-source fixtures.

Validation: compare from previously reviewed f873c52 contains only the two repaired module/test files; both fetched at the requested SHA and Git blob hashes matched the isolated review tree. Python 3.12.14: targeted 43/43; exact-head full suite 713/713, zero skips; independent 302-case probe passed. Exact-head GitHub Tests run 37714109422 completed success. Existing localhost HTTP fixtures used sandbox network permission; no live/provider calls.

Remaining trust boundary: the protected acquisition ledger must supply EVERY known approved capture for the immutable scope and candidate-independent current time/source/cluster/receipt bindings. Omitting a competing receipt cannot be detected by this offline API. Typed receipts, source names/genesis strings and hashes still do not authenticate their issuer; the simulated coordinator_capture positives are synthetic harness attestations. No production receipt issuer, positive live acceptance, historical continuity, exposure/entry integration or ownership completion is certified by this result. Desktop retains integration/dispatch; no merge or live calls.

Review: https://github.com/pixelmanager-cloud/solana-paper-desk/pull/26#pullrequestreview-5450436592

## Independent reproduction script

Run with declared dependencies from the exact-head tree. Synthetic fixture harness only.

```python
from copy import deepcopy
from dataclasses import replace
from tests.test_pool_vault_admission import PoolVaultAdmissionTests
from desk.model import digest

count=0
def check_pair(mutate):
    global count
    for cross_source in (False,True):
        f=PoolVaultAdmissionTests();f.setUp()
        a,b,first,second=f.paired_captures(mutate=mutate,other_source='source-two' if cross_source else None)
        for refs in (a,b):
            for account in (f.q['base_vault'],f.q['quote_vault']):
                r=f.admit(refs=refs,account=account)
                assert not r['snapshot_label_admitted'] and not r['production_snapshot_exclusion_allowed'],r
                assert r['reasons'],r
                for flag in ('historical_interval_exclusion_allowed','chain_authenticated','eligible_for_trading','ownership_approval'):
                    assert not r[flag],r
                count+=1

for index in range(6):
    for field,value in (('lamports',99999999),('owner','bad-owner'),('executable',True),('data',['!!!','base64']),('space',True)):
        check_pair(lambda s,i=index,k=field,v=value:s['result']['value'][i].update({k:v}))
for mutate in (
    lambda s:s['result'].update(value=None),
    lambda s:s['result'].update(value=s['result']['value'][:5]),
    lambda s:s['result']['context'].update(slot=True),
    lambda s:s['params'][1].update(commitment='confirmed'),
    lambda s:s.update(error={'code':-1}),
    lambda s:s.update(padding='x'*65536),
):
    check_pair(mutate)

for broken in ('missing','wrong-hash','not-dict','raising'):
    for selection in (0,1):
        f=PoolVaultAdmissionTests();f.setUp()
        a,b,_,_=f.paired_captures(mutate=lambda s:s.update(annotation='metadata'),other_source='source-two')
        if broken=='missing':del f.evidence[b.snapshot]
        if broken=='wrong-hash':f.evidence[b.snapshot]['extra']='changed after attestation'
        if broken=='not-dict':f.evidence[b.snapshot]=[]
        def load(key):
            if broken=='raising' and key==b.snapshot:raise RuntimeError('private offline reader failure')
            return deepcopy(f.evidence[key])
        r=f.admit(refs=(a,b)[selection],load=load)
        assert not r['production_snapshot_exclusion_allowed'] and r['reasons'],r
        assert 'private offline reader failure' not in str(r)
        count+=1

for direction in (0,1):
    f=PoolVaultAdmissionTests();f.setUp()
    a,b,first,second=f.paired_captures(other_time=f.q['snapshot_time']+1,other_source='source-two')
    r=f.admit(refs=(a,b)[direction],snapshot_time=f.q['snapshot_time']+direction)
    assert r['reasons']==['ACQUISITION_CAPTURE_CONFLICT'],r
    count+=1
    f.setUp()
    a,b,first,second=f.paired_captures(mutate=f.change_base_amount,other_source='source-two')
    # Whichever reference is selected stays fresh; the opposing immutable
    # observation expires. Its contradiction must still block selection.
    fresh,stale=(first,replace(second,captured_at=f.q['snapshot_time'])) if direction==0 else (second,replace(first,captured_at=f.q['snapshot_time']))
    p=replace(f.policy,receipts=frozenset((fresh,stale)),max_age_seconds=1)
    r=f.admit(refs=(a,b)[direction],policy=p)
    assert r['reasons']==['ACQUISITION_CAPTURE_CONFLICT'],r
    count+=1

f=PoolVaultAdmissionTests();f.setUp()
a,b,_,_=f.paired_captures(mutate=lambda s:s['result']['context'].update(apiVersion='other'),other_source='source-two')
for refs in (a,b):
    loaded=[]
    def load(key):
        loaded.append(key)
        return deepcopy(f.evidence[key])
    r=f.admit(refs=refs,load=load)
    assert r['production_snapshot_exclusion_allowed'] and r['supporting_capture_count']==2,r
    assert len(loaded)==len(set(loaded))==6,loaded
    assert not r['historical_interval_exclusion_allowed'] and not r['eligible_for_trading'] and not r['chain_authenticated'],r
    count+=1
print(f'PASS: {count} independent conflict/malformed/direction/stale/time/metadata cases; fixture harness only, no acquisition authenticity claimed.')
```
