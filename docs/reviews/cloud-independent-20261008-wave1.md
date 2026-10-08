# Independent cloud review evidence — 8 October 2026

Policy: cloud-independent-review-v1. Fixture-only independent coordinator execution; no approvals, acceptance, merges or live claims. Each exact head was checked before reading and again before posting. Local reconstructed changed-file Git blob hashes all matched GitHub. Connector compare proved e95a8df..e298e15 changes only two coordination docs, so unchanged code/test/fixture inputs match the PR base. Git transport was unavailable through the proxy; supported GitHub file reads supplied exact changed blobs.

## PR #18

Independent exact-head review (cloud-independent-review-v1) of 87f7bc7a2a01c89b33d870748a35f919cd0c4ff7; head rechecked immediately before submission. Different review execution from worker 01; no approval or live acceptance.

P2 — serialize the whole ownership invocation or fence publication of ownership_heads (desk/ownership_worker.py:73–98). The new bank capture is locked per I/O, but aggregation/replay and the unconditional head upsert are outside that lock. A deterministic two-invocation fixture reproduced:
1. Invocation A advances the original mint page with max_calls=1 (durable used=8), then is paused at EvidenceStore.save for its already-built ownership_progress_v1 record.
2. Invocation B runs max_calls=2, captures bank + clock, and publishes a result containing snapshot_evidence (used=10).
3. Resume A: its unconditional upsert replaces B's head with A's older record. saved progress now reports used=8 and has no snapshot_evidence, although the durable budget is 10 and the immutable bank remains stored.
This does not refund requests or grant entry eligibility. It does regress published evidence/dashboard/decision inputs and invalidates a monotonic review state assumption. The unconditional head writer predates this PR; the new snapshot workflow remains exposed to it. Please use an invocation-level lock distinct from the per-request lock, or a generation/CAS fence that rejects stale publication, and add a worker-level concurrency regression. The existing concurrent capture test only checks the inner lock.

Reproduction used existing OwnershipWorkerTests setup, real EvidenceStore/HistoryProgress/worker code, a mocked verified inventory solely to isolate orchestration, synthetic snapshot bytes, and two threads synchronized by Events. No provider calls. The snapshot hash itself remained unchanged.

Additional acceptance gap: the positive matching replay test mocks reconstruct_launch_history and orchestration mocks account_inventory. Add an unmocked persisted initialization/history/account-query -> captured bank -> cutoff-bounded replay positive fixture and its missing/new-frontier variants before claiming integration validated. Existing tests correctly keep eligibility false; bounded live acceptance remains desktop-only.

Validation: changed-file blobs retrieved at exact head; base code verified equal to local e95a8df (e95a8df..e298e15 changes only queue/manifest docs). Python 3.12.14: added suite 10/10; full suite 523/523; independent concurrency reproduction passes and demonstrates the race. First full run's sole error was sandbox denial of the existing localhost HTTP fixture; loopback-capable permission rerun passed. GitHub Tests workflow 37710656102 completed success at this head. No merge, issue completion, gate change or readiness claim.

Review ID: 5450225333.

Independent reproduction (run from an isolated checkout of the stated head with its declared dependencies):

```python
import copy,threading,sqlite3
from unittest.mock import patch
from tests.test_ownership_worker import OwnershipWorkerTests
from tests.test_ownership_snapshot import OwnershipSnapshotTests
from desk.ownership_worker import advance
from desk.evidence import EvidenceStore

f=OwnershipWorkerTests();f.setUp()
s=OwnershipSnapshotTests();s.setUp()
snapshot=copy.deepcopy(s.snapshot)
snapshot['result']['value'][0]=f.store.load(f.mintkey)['result']['value']
inventory={'initialization_inventory_verified':True,'accounts':[{'address':s.key}]}
ready=threading.Event();release=threading.Event();results={};errors=[]
original=EvidenceStore.save
def save(self,record):
    if threading.current_thread().name=='older' and record.get('kind')=='ownership_progress_v1':
        ready.set()
        if not release.wait(10):raise RuntimeError('review synchronization timeout')
    return original(self,record)
def rpc(method,params):
    if method=='getMultipleAccounts':return snapshot['result']
    if method=='getBlockTime':return 100
    return {'data':[]}
def older():
    try:results['older']=advance(f.db,f.evidence,'scan',rpc,max_calls=1)
    except BaseException as e:errors.append(repr(e));ready.set()
with patch('desk.account_history.account_inventory',return_value=inventory),patch.object(EvidenceStore,'save',save):
    t=threading.Thread(target=older,name='older');t.start()
    assert ready.wait(10)
    try:results['newer']=advance(f.db,f.evidence,'scan',rpc,max_calls=2)
    finally:release.set();t.join(10)
assert not errors,errors
with f.store.connect() as c:head=c.execute('SELECT evidence_hash FROM ownership_heads WHERE scan_id=?',('scan',)).fetchone()[0]
record=f.store.load(head)
assert head==results['older']['evidence_hash']
assert 'snapshot_evidence' in results['newer'] and 'snapshot_evidence' not in record
assert results['newer']['requests_used']==10 and record['requests_used']==8
print('REPRODUCED: older publisher overwrites newer bank/clock progress; published requests_used=8 while durable budget=10; snapshot_evidence disappears from published head. Bank remains persisted; no entry approval.')
f.doCleanups()

```

## PR #19

Independent exact-head review (cloud-independent-review-v1) of 0a2cea9a1e255915f49831a3c7d467e4afb77c05; head rechecked before submission. No blocking code defect found within the documented offline/pinned-policy contract. This is not source authentication, approval of a real label, or OWN-2/live acceptance.

Actionable integration requirement — keep trusted policy admission outside candidate evidence (desk/account_classification.py:43–46, 95–103). Independent adversarial probe: a fabricated POOL_VAULT record with invented source/method/verifier and a syntactically valid but nonexistent source_hash is UNKNOWN with an empty trusted_hashes policy; inserting that record's digest into trusted_hashes makes exclusion_allowed true. This is expected for this contract, but confirms a hash/source string is not authenticity. The test helper currently auto-pins every supplied candidate as a fixture convenience. A real adapter must independently authenticate/replay the underlying exact account/program binding and persist policy version/admission provenance; never derive trusted_hashes from an investigation's candidate list. Add a separate adapter test showing self-hashed forged evidence and self-declared verified flags cannot populate this policy. No automatic production trust-policy builder exists in this PR.

Cross-PR adapter requirement: program here is the chain account owner (token program for a vault), while OWN-3 Classification.owner is the token authority/holder wallet. These are different identities and must both be reconstructed from raw state, not copied interchangeably. Purpose and actual evaluation time/slot must be retained. expires_slot is exclusive; OWN-3 valid_through_slot is inclusive. Test the exact expiry boundary when integrating; funding_source labels never authorize holder exclusions, and UNKNOWN is not PRIVATE.

Validation: read actual complete diff plus all changed blobs at this head. Python 3.12.14: classification suite 12/12; full suite 525/525; additional forged-policy and exact time/slot expiry probes passed. First full run's sole error was sandbox denial of the existing localhost HTTP fixture; loopback-capable permission rerun passed. GitHub Tests workflow 37710675720 completed success. No provider calls, live attribution, merge, issue closure or paper-readiness claim.

Review ID: 5450226120.

Independent reproduction (run from an isolated checkout of the stated head with its declared dependencies):

```python
import copy,json
from pathlib import Path
from desk.account_classification import classify_account
from desk.model import digest

r=json.loads(Path('fixtures/account-classification/pool-vault.json').read_text())
def check(records,policy,now=150,slot=15):
    pages={digest(x):x for x in records}
    return classify_account(r['account'],r['program'],mint=r['scope']['mint'],purpose='holder_exclusion',now=now,slot=slot,evidence_hashes=list(pages),trusted_hashes=policy,load=pages.__getitem__)
forged=copy.deepcopy(r)
forged['provenance']={'source':'invented','method':'invented','verifier':'invented','source_hash':'b'*64}
h=digest(forged)
assert not check([forged],frozenset())['exclusion_allowed']
assert check([forged],frozenset([h]))['exclusion_allowed']
assert not check([r],frozenset([digest(r)]),slot=r['expires_slot'])['exclusion_allowed']
assert not check([r],frozenset([digest(r)]),now=r['expires_at'])['exclusion_allowed']
print('BOUNDARY VERIFIED: self-hashed forged evidence cannot pass without policy membership; inserting its hash into trusted policy grants exclusion without authenticating source_hash. Exact time/slot expiry rejects. Policy admission must remain external and independently verified.')

```

## PR #12

Independent exact-head review (cloud-independent-review-v1) of 0b4fbdb108cb00ac2927071812ed725c6469f1ea; head rechecked before submission. No blocking arithmetic defect found in the bounded fixed-supply/offline-core contract. This does not accept OWN-3: OWN-1/OWN-2 predecessors and a trusted adapter remain required.

Actionable adapter requirements before any evidence/entry wiring:
- Typed validated/history_complete/verified flags and arbitrary nonempty refs are intentionally trusted input contracts, not authenticated evidence. An independent probe constructed an all-pool classification with arbitrary refs + True flags and obtained excluded=100, unresolved=0. Changing history_complete=False or expiring the classification produced excluded=0, unresolved=100. Only a non-candidate-controlled adapter that reconstructs raw snapshot/history/classification evidence may construct positive inputs. Add an integration bypass test; never deserialize candidate normalized JSON directly into these dataclasses.
- OWN-2 uses exclusive expires_slot; this core uses inclusive valid_through_slot (kind() checks through < cutoff). Directly copying expires_slot=20 to valid_through_slot=20 grants an exclusion at cutoff 20 that OWN-2 rejects. Map exclusive expiry deliberately (e.g. through=expires_slot-1), check actual time expiry independently, and verify validity covers the whole replay interval. There is no time/purpose field in this core's Classification.
- OWN-2 program means chain account owner; this core owner means holder/token authority. Preserve both bindings in the adapter. UNKNOWN classifications must stay UNRESOLVED; absence of a service label or shared funding cannot be promoted to Kind.PRIVATE. OWN-2 intentionally provides no PRIVATE proof.
- Seeds are initial-boundary amounts. Do not translate arbitrary later cohort buys, mint/burn, recreated accounts, ambiguous same-slot order or control changes into this fixed-supply interval without validated normalization. Retain per-holder possible/unresolved bounds; correlated upper bounds are not a common-control percentage.

Validation: actual full diff and changed blobs read at exact head. Python 3.12.14: exposure suite 25/25 including discrete allocation oracle; full suite 538/538; independent forged-contract, inclusive-expiry and incomplete-history probes passed. First full run's sole error was sandbox denial of the existing localhost HTTP fixture; loopback-capable permission rerun passed. GitHub Tests workflow 37710576311 completed success. No live verification, entry permission, merge or issue completion.

Review ID: 5450226914.

Independent reproduction (run from an isolated checkout of the stated head with its declared dependencies):

```python
from dataclasses import replace
from desk.distribution_exposure import *
s=ValidatedSnapshot('mint',10,20,100,(Balance('a','owner',100),),(Balance('a','owner',100),),'arbitrary-snapshot','arbitrary-history',True,True)
c=Classification('a','owner','mint',Kind.POOL,10,20,('arbitrary-classification',),True)
r=trace_exposure(s,(),(Seed('a',100),),(c,))
assert r.excluded==100 and r.unresolved==0
expired=trace_exposure(s,(),(Seed('a',100),),(replace(c,valid_through_slot=19),))
assert expired.excluded==0 and expired.unresolved==100
missing=trace_exposure(replace(s,history_complete=False),(),(Seed('a',100),),(c,))
assert missing.excluded==0 and missing.unresolved==100
print('BOUNDARY VERIFIED: core trusts constructed typed flags/refs, accepts inclusive valid_through_slot=20 at cutoff=20; through=19 and incomplete history fail closed. OWN-2 expires_slot is exclusive: adapters must validate scope, actual time and raw proofs, map expiry deliberately, and never infer PRIVATE from UNKNOWN.')

```

