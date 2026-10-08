# PR18 canonical-lock repair review — 8 October 2026

Independent exact-head disposition (cloud-independent-review-v1) at 92e9c6104b411101e414c03774a1675cbbf1ac91: both previously reproduced P2 publication findings are RESOLVED under the documented local-filesystem, stable-path, regular-single-link database contract. No new blocking defect found within this scoped revision. This is a COMMENT disposition, not merge/ownership/live approval.

Checked actual revision delta and complete current ownership code:
- The invocation, bank-capture and history-request locks all derive filenames through ownership_lock_path -> canonical_ownership_path. Symlink resolution occurs before worker EvidenceStore construction; _advance_locked receives store.path. Direct HistoryProgress construction normalizes store.path, and both direct I/O lock paths recheck file type/link count.
- The whole-invocation lock stays alive across collection, replay and head publication; it remains distinct from nested request locking.
- Hardlinks (st_nlink != 1) and nonregular database paths are rejected. Path replacement/rename/link during running workers remains explicitly unsupported; this is local flock coordination, not distributed ownership locking.

Independent regression checks:
1. Five real/symlink-chain/directory-symlink/relative pathname variants resolve to identical invocation and request lock identities. Replayed the earlier paused-publication alias race: competing invocation now BUSY with zero provider calls/head writes. Retry via a directory alias publishes reconciled progress at 11 requests without regression.
2. Holding the canonical request lock blocks direct aliased capture_bank and HistoryProgress.advance without I/O or request spending. No alias-named sidecar lock is created.
3. Added a hardlink AFTER a live HistoryProgress object existed. Both worker path variants and that object's bank/history methods reject; existing head and 8-request budget are unchanged. Removing the alias permits completion to 11 requests.
4. Reran independent process-death check: SIGTERM after raw progress commit and before head publication releases the invocation lock; restart preserves bank/clock and charged budget and publishes reconciled raw replay with zero new RPC.
5. New unmocked positive launch/inventory/account history/cutoff/snapshot replay and missing-query/new-frontier/balance mismatch regressions continue to pass. Positive fixture reconciliation still does not authenticate a live source or establish common control.

Validation: five fetched Git blob hashes matched local exact-head inputs. Python 3.12.14: bank+integration suites 21/21; full suite 534/534, zero skips (existing private loopback fixture permission); both independent process and alias/hardlink checks passed. GitHub Tests run 37712377104 completed success at this head. No provider requests, production changes, merges, issue closure or acceptance. OWN-1 bounded live verification remains pending with the desktop coordinator. The prior #21 checkpoint does not test this revision: require full combined-suite/CI on the actual proposed integration head and recheck that head before merge.

Review ID: 5450316600.

## review_alias_fixed.py

Run at exact head 92e9c6104b411101e414c03774a1675cbbf1ac91, fixture-only.

```python
import threading,copy,os,fcntl
from unittest.mock import patch
from tests.test_ownership_integration import OwnershipIntegrationTests
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress,ownership_lock_path
from desk.ownership_worker import advance,saved_progress

f=OwnershipIntegrationTests();f.setUp()
root=f.evidence.parent
alias=root/'alias.sqlite';alias.symlink_to(f.evidence)
chain=root/'chain.sqlite';chain.symlink_to(alias)
directory=root/'directory';directory.symlink_to(root,target_is_directory=True)
paths=(f.evidence,alias,chain,directory/f.evidence.name,os.path.relpath(alias))
for path in paths:
    store=EvidenceStore(path)
    for invocation in (False,True):
        assert ownership_lock_path(store,invocation)==ownership_lock_path(f.store,invocation)
ready=threading.Event();release=threading.Event();results={};errors=[]
original=EvidenceStore.save
def rpc(method,params):
    if method=='getMultipleAccounts':return {'context':{'slot':20},'value':copy.deepcopy(f.values)}
    if method=='getBlockTime':return 105
    return {'data':[copy.deepcopy(f.raw)]}
def save(store,payload):
    if threading.current_thread().name=='older' and payload.get('kind')=='ownership_progress_v1':
        ready.set()
        if not release.wait(10):raise RuntimeError('review timeout')
    return original(store,payload)
def older():
    try:results['older']=advance(f.db,chain,'scan',rpc,max_calls=1)
    except BaseException as e:errors.append(repr(e));ready.set()
with patch.object(EvidenceStore,'save',save):
    t=threading.Thread(target=older,name='older');t.start();assert ready.wait(10)
    try:
        busy=advance(f.db,f.evidence,'scan',rpc,max_calls=4)
        assert busy['status']=='BUSY' and busy['provider_calls']==0
        assert saved_progress(f.store,f.scan) is None
    finally:release.set();t.join(10)
assert not errors,errors
progress=HistoryProgress(EvidenceStore(alias))
key=progress.create('scan',f.mint,90,110)
with open(ownership_lock_path(f.store),'a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    assert progress.capture_bank('scan',f.mint,[f.account],rpc)['blocked']=='BUSY'
    assert progress.advance(key,rpc)['busy']
hard=root/'hardlink.sqlite';os.link(f.evidence,hard)
for path in (f.evidence,hard):
    try:advance(f.db,path,'scan',rpc)
    except ValueError as e:assert 'one hard link' in str(e)
    else:raise AssertionError('hardlink accepted')
for operation in (lambda:progress.capture_bank('scan',f.mint,[f.account],rpc),lambda:progress.advance(key,rpc)):
    try:operation()
    except ValueError as e:assert 'one hard link' in str(e)
    else:raise AssertionError('existing progress did not recheck hardlink')
assert saved_progress(f.store,f.scan)['evidence_hash']==results['older']['evidence_hash']
with f.store.connect() as c:assert c.execute('SELECT used FROM ownership_budgets').fetchone()[0]==8
hard.unlink()
newer=advance(f.db,directory/f.evidence.name,'scan',rpc,max_calls=4)
assert newer['snapshot']['reconciled'] and newer['requests_used']==11
assert saved_progress(f.store,f.scan)['evidence_hash']==newer['evidence_hash']
assert not __import__('pathlib').Path(str(alias)+'.ownership.lock').exists()
assert not __import__('pathlib').Path(str(chain)+'.ownership-invocation.lock').exists()
print('PASS: five real/symlink-chain/directory/relative path variants share both lock identities; original alias race now BUSY; direct bank/history locks shared; new hardlinks reject without budget/head change; restart reconciles and publishes monotonic head at 11 requests.')
f.doCleanups()

```

## review_process_death.py

Run at exact head 92e9c6104b411101e414c03774a1675cbbf1ac91, fixture-only.

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
