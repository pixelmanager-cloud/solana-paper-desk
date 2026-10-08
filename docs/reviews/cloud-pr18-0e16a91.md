# PR18 revision independent review — 8 October 2026

Independent re-review (cloud-independent-review-v1) at exact head 0e16a91b994191ea95e98bf7803bdc1284a3c922.

Resolved for callers using the same database pathname:
- The distinct .ownership-invocation.lock spans _advance_locked source validation, collection, aggregation, replay, evidence saving and ownership_heads publication. Nested per-request locks use a different file, so the positive path does not deadlock.
- The original same-path publication race regression now returns BUSY without provider I/O or publication.
- Independent process check killed a worker with SIGTERM after progress evidence committed but before head publication. Competing invocation was BUSY; death released the invocation lock; restart preserved bank/clock and 11 charged requests, replayed reconciled evidence and published a head with zero further RPCs.
- New positive/missing-query/new-frontier/balance-mismatch fixtures really run production decode, launch-anchor, inventory, request/page replay, continuity and snapshot reconciliation. Only fixture provider I/O is substituted. Positive synthetic replay is now demonstrated; it remains neither source authenticity nor live acceptance.

Remaining P2 — canonicalize the database identity used for sidecar locks (desk/ownership_worker.py:16–18; the request/bank locks in desk/history_progress.py use the same unnormalized store.path pattern). EvidenceStore retains Path(path) without resolve(). Calls using /tmp/evidence.sqlite and a symlink /tmp/alias.sqlite to that SAME database lock different sibling files. I reproduced the stale-head race on this revision using the new unmocked synthetic launch fixture:
1. A uses the real path, captures bank with max_calls=1, and pauses immediately before saving its built progress record (used=8, block_time_hash=None).
2. B uses the symlink path, acquires its distinct invocation lock, completes clock + both bounded histories, and publishes snapshot.reconciled=True with used=11.
3. Resume A: unconditional ownership_heads upsert overwrites B; published used=8 and block_time_hash=None, while the same SQLite database still has used=11 and a complete immutable bank/clock.
No budget refund or entry bypass occurs, but monotonic publication is still violated for a valid alternate filesystem path. No history/inventory/reconciliation mock was used in this reproduction; only the I/O fixture and Event pause at EvidenceStore.save.

Please normalize the evidence path consistently before deriving ALL ownership lock filenames (e.g. resolve the database path; both invocation and per-request/bank paths must agree), add a deterministic real-path/symlink concurrency regression that returns BUSY and preserves the newer head, and document/reject unsupported aliases such as hard links if pathname locking cannot safely support them. Do not simply change the invocation lock while leaving alternate per-request lock identities.

Validation: all five retrieved changed-file Git blob hashes matched GitHub exactly. Revision delta from 87f7bc7 changes only ownership_worker and the new integration tests. Python 3.12.14: 16/16 bank+integration tests; full suite 529/529 with the existing local loopback HTTP fixture permission. Independent process-death/restart check passed; independent symlink-path check reproduced the remaining race. GitHub Tests run 37711606250 completed success at this head. No merge, live/provider access, task acceptance or integration-branch approval. Re-review the fix SHA and test exact proposed combined commit before integrating #21.

Review ID: 5450263660.

## review_process_death.py

Run from an isolated checkout of 0e16a91b994191ea95e98bf7803bdc1284a3c922 with declared fixture dependencies. No provider requests.

```python
import multiprocessing,sqlite3,copy
from unittest.mock import patch
from tests.test_ownership_integration import OwnershipIntegrationTests
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.ownership_worker import advance,saved_progress

f=OwnershipIntegrationTests();f.setUp()
def rpc(method,params):
    if method=='getMultipleAccounts':return {'context':{'slot':20},'value':copy.deepcopy(f.values)}
    if method=='getBlockTime':return 105
    assert method=='getTransactionsForAddress'
    assert params[1]['filters']['slot']=={'gte':0,'lt':21}
    return {'data':[copy.deepcopy(f.raw)]}
ctx=multiprocessing.get_context('fork');reader,writer=ctx.Pipe(duplex=False)
original=EvidenceStore.save
def child():
    def save(store,payload):
        key=original(store,payload)
        if payload.get('kind')=='ownership_progress_v1':
            writer.send(key)
            # Block at a process boundary after evidence commit, before head publish.
            import signal
            signal.pause()
        return key
    with patch.object(EvidenceStore,'save',save):advance(f.db,f.evidence,'scan',rpc,max_calls=4)
p=ctx.Process(target=child);p.start()
try:
    assert reader.poll(10),'child did not reach publication boundary'
    orphan=reader.recv()
    busy=advance(f.db,f.evidence,'scan',lambda *a:(_ for _ in ()).throw(AssertionError('busy RPC')))
    assert busy['status']=='BUSY' and busy['provider_calls']==0
    assert saved_progress(f.store,f.scan) is None
    bank=HistoryProgress(f.store).bank('scan')
    with f.store.connect() as c:assert c.execute('SELECT used FROM ownership_budgets').fetchone()[0]==11
finally:
    p.terminate();p.join(10)
    if p.is_alive():p.kill();p.join(5)
assert p.exitcode is not None
def no_rpc(*a):raise AssertionError('completed persisted evidence must replay without RPC')
recovered=advance(f.db,f.evidence,'scan',no_rpc,max_calls=4)
assert recovered['snapshot']['reconciled'] and recovered['provider_calls']==0
assert recovered['requests_used']==11 and recovered['snapshot_evidence']==bank
assert saved_progress(f.store,f.scan)['evidence_hash']==recovered['evidence_hash']
assert f.store.load(orphan)['snapshot']['reconciled']
print('PASS: competing process BUSY with zero I/O; SIGTERM before head publication releases invocation lock; immutable bank/clock and 11 charged requests survive; restart publishes reconciled raw replay with zero RPC and unchanged bank.')
f.doCleanups()

```

## review_database_alias.py

Run from an isolated checkout of 0e16a91b994191ea95e98bf7803bdc1284a3c922 with declared fixture dependencies. No provider requests.

```python
import threading,copy
from unittest.mock import patch
from tests.test_ownership_integration import OwnershipIntegrationTests
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.ownership_worker import advance,saved_progress

f=OwnershipIntegrationTests();f.setUp()
alias=f.evidence.with_name('same-evidence-via-symlink.sqlite');alias.symlink_to(f.evidence)
ready=threading.Event();release=threading.Event();results={};errors=[]
original=EvidenceStore.save
def rpc(method,params):
    if method=='getMultipleAccounts':return {'context':{'slot':20},'value':copy.deepcopy(f.values)}
    if method=='getBlockTime':return 105
    return {'data':[copy.deepcopy(f.raw)]}
def save(store,payload):
    if threading.current_thread().name=='older' and payload.get('kind')=='ownership_progress_v1':
        ready.set()
        if not release.wait(10):raise RuntimeError('review synchronization timeout')
    return original(store,payload)
def older():
    try:results['older']=advance(f.db,f.evidence,'scan',rpc,max_calls=1)
    except BaseException as e:errors.append(repr(e));ready.set()
with patch.object(EvidenceStore,'save',save):
    t=threading.Thread(target=older,name='older');t.start()
    assert ready.wait(10)
    try:results['newer']=advance(f.db,alias,'scan',rpc,max_calls=4)
    finally:release.set();t.join(10)
assert not errors,errors
assert results['newer']['snapshot']['reconciled']
head=saved_progress(f.store,f.scan)
assert head['evidence_hash']==results['older']['evidence_hash']
assert head['snapshot_evidence']['block_time_hash'] is None
assert HistoryProgress(f.store).bank('scan')['block_time_hash'] is not None
assert head['requests_used']==8 and results['newer']['requests_used']==11
with f.store.connect() as c:assert c.execute('SELECT used FROM ownership_budgets').fetchone()[0]==11
print('REPRODUCED: same SQLite DB via real path and symlink acquires distinct invocation locks; older publication replaces reconciled newer head, clears published clock reference and regresses published requests_used 11->8; durable budget/bank retained, eligibility false.')
f.doCleanups()

```
