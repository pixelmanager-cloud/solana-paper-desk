# PR32 admission/source-seal independent review

Independent exact-head review (cloud-independent-review-v1) at 64765af62379fe4fbb185271c1d54405a527a0af. Head rechecked immediately before submission. No blocking defect found within this admission/budget/source-seal foundation contract.

Verified:
- One existing ownership_budgets counter remains authoritative. New admissions retain immutable descriptor hash in source_hash; separate lifecycle metadata never replaces it with completed-source hash. Retry cannot reset usage or change descriptor/ceiling. Legacy schema/rows/compressed evidence are preserved by the additive migration; legacy IDs cannot become admissions.
- BEGIN IMMEDIATE serializes admit/reserve/prepare/seal. Setup reservation commits before caller I/O; history/bank APIs reserve their own attempts. An independent separate-connection probe observed used=1,2,3,4 before four failing history calls and used=5 before a failing bank call. Failure remains charged.
- Preparation binds exact five-field source projection, original result serialization, report hash/mint/research-only eligibility and every charged attempt. PREPARED blocks setup/history/bank reservations; cross-ID descriptor use, unsealed budget binding, source/ceiling rebinding and changed result whitespace fail closed.
- Seal requires exact prepared descriptor/source hashes, is idempotent and preserves bytes/counts. Independent subprocess death immediately after committed seal recovered SEALED exactly; thirteen subsequent reservations reached the same18 ceiling. Reusing original calls=5 through budget/prepare/seal did not refund usage, and exhausted continuation made no further I/O.
- Existing worker validates completed-source binding before mint policy or provider I/O. Unsealed/changed projections fail, sealed exact projections continue with the single counter, unsupported-token and eligibility gates remain intact.

Successor requirements remain material: reserve() cannot itself wrap or authenticate a caller's provider operation. Acquisition must hold the canonical whole-invocation lock and queue claim/generation fence, finish in-flight calls before prepare, and refuse I/O when reserve returns false. PREPARED freezes NEW reservations; it does not drain a request already reserved by another caller. The two SQLite files are not atomic together. The documented prepared-source seal/publish recovery protocol, exact-byte research-row conflict handling, queue/daily limits and acquisition implementation are external dependencies, not delivered or certified here. Descriptor/report hashes bind identity, not raw provider authenticity. No entry/ownership/live readiness acceptance.

Validation: actual four-file commit diff read relative to parent f2009acd09f846a305eca94b4179574920a845fa (PR base has subsequently advanced). Isolated exact-head tree reconstructed from clean local ancestor e95a8df; all51 changed Git blob hashes matched. Python3.12.14 focused53/53; full737/737, zero skips. Author adversarial crash-after-reserve/prepare, rollback, backup, competing prepare and prepare-vs-reserve tests read and run; additional independent rebinding/reserve-before-I/O/process-death-after-seal/shared-ceiling probes passed. Exact-head GitHub Tests run37715023000 completed success. Permission used solely for existing temporary localhost HTTP fixture. No provider/live calls, merge, dispatch or production edits.

Review: https://github.com/pixelmanager-cloud/solana-paper-desk/pull/32#pullrequestreview-5450508179

## Independent fixture probe

Run from the exact-head tree. Blob manifest entries are GitHub file path/blob SHA pairs fetched at this head; the manifest is a review scratch artifact.

```python
import copy,hashlib,json,os,sqlite3,subprocess,sys,tempfile
from pathlib import Path
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.model import canonical,digest
from tests.test_ownership_budget_admission import AdmissionBudgetTests,MINT

manifest=json.loads(Path('review_blob_manifest.json').read_text())
for item in manifest:
    raw=Path(item['path']).read_bytes()
    actual=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    assert actual==item['sha'],(item['path'],actual,item['sha'])
print('PASS: all',len(manifest),'exact-head changed blobs verified')

f=AdmissionBudgetTests();f.setUp()
try:
    p=f.progress
    job=p.create('scan',MINT,0,21)
    seen=[]
    def failed_io(*args):
        # Separate SQLite connection observes committed reservation at I/O entry.
        with sqlite3.connect(f.path) as c:
            used=c.execute("SELECT used FROM ownership_budgets WHERE id='scan'").fetchone()[0]
        seen.append(used)
        raise OSError('offline failure')
    for _ in range(4):
        p.advance(job,failed_io)
    assert seen==[1,2,3,4],seen
    p.capture_bank('scan',MINT,[MINT],failed_io)
    assert seen[-1]==5,seen
    with f.store.connect() as c:
        original=c.execute('SELECT * FROM ownership_budgets').fetchall()
    for ceiling in (0,True,17,19):
        try:p.admit('scan',f.descriptor,ceiling)
        except ValueError:pass
        else:raise AssertionError('ceiling rebound')
    other={**f.descriptor,'scan_id':'other'}
    b=p.admit('other',other)['descriptor_hash']
    for method,args in ((p.prepare_source,('other',f.binding,f.source(5))),
                        (p.prepare_source,('scan',b,f.source(5))),
                        (p.budget,('scan',digest(f.source(5)),5))):
        try:method(*args)
        except ValueError:pass
        else:raise AssertionError('descriptor/identity bypass')
    source=f.source(5);prepared=p.prepare_source('scan',f.binding,source)
    assert p.advance(job,failed_io)['blocked']=='INVESTIGATION_SOURCE_PREPARED'
    assert not p.reserve('scan')
    assert len(seen)==5
    # Real process dies immediately after seal commit; reopen must preserve it.
    code="""import json,os,sys
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
p=HistoryProgress(EvidenceStore(sys.argv[1]))
a=p.admission('scan')
p.seal_source('scan',a['descriptor_hash'],a['completed_source_hash'])
os._exit(9)
"""
    r=subprocess.run([sys.executable,'-c',code,str(f.path)],timeout=10)
    assert r.returncode==9
    p=HistoryProgress(EvidenceStore(f.path))
    assert p.admission('scan')['state']=='SEALED'
    for _ in range(13):assert p.reserve('scan')
    assert not p.reserve('scan')
    p.budget('scan',digest(source),5)
    assert p.seal_source('scan',f.binding,digest(source))['requests_used']==18
    assert p.prepare_source('scan',f.binding,source)['requests_used']==18
    assert p.advance(job,failed_io)['blocked']=='INVESTIGATION_REQUEST_BUDGET_EXHAUSTED'
    assert len(seen)==5
    for field,value in (('id','other'),('created',True),('mint','bad'),('result',canonical(json.loads(source['result'])))):
        changed={**source,field:value}
        try:p.prepare_source('scan',f.binding,changed)
        except ValueError:pass
        else:raise AssertionError('source rebound '+field)
    with f.store.connect() as c:
        row=c.execute("SELECT source_hash,used,ceiling FROM ownership_budgets WHERE id='scan'").fetchone()
    assert row==(f.binding,18,18),row
    assert p.admission('scan')['prepared_source']==source
    print('PASS: history+bank reservations committed before failed offline I/O; prepared freeze; cross-ID/descriptor/source/ceiling rejection; process death after seal; shared18 ceiling and immutable source after13 continuation reservations.')
finally:f.doCleanups()
```

## Exact-head blob manifest

```json
[
  {
    "path": "desk/account_classification.py",
    "sha": "ed1470d136d746890fb6ca0c0f86a65979f1ed3a"
  },
  {
    "path": "desk/classification_exposure_adapter.py",
    "sha": "3ceea4cb9567d374ee95dbb7e6a9c052a447010a"
  },
  {
    "path": "desk/decision_runner.py",
    "sha": "0bde6a64493521a470a6cc8133d195dec0194bdf"
  },
  {
    "path": "desk/distribution_exposure.py",
    "sha": "391fe3f6866019a466d5f2b97b8ab4c3be8c31f0"
  },
  {
    "path": "desk/entry_evidence.py",
    "sha": "36256e68c51fa2bf05026969706e9ea731df9d18"
  },
  {
    "path": "desk/history_progress.py",
    "sha": "3c6e79d79280330a2c1a1e34304f583566684530"
  },
  {
    "path": "desk/holders.py",
    "sha": "c59df1b30f9b05a06d7bb2c226be4fc483d3691b"
  },
  {
    "path": "desk/ownership_snapshot.py",
    "sha": "beb9d0368f2126368cb7213c149018abae749358"
  },
  {
    "path": "desk/ownership_worker.py",
    "sha": "78c56bdd1bf0935c4ebb39529fe5e499b7e8caf5"
  },
  {
    "path": "desk/providers.py",
    "sha": "57d63ef067975be9aa3aa243f130c4b18f3b4d2e"
  },
  {
    "path": "desk/security.py",
    "sha": "a94a730308e866a4c94c57cd1e8cba3d14166e3a"
  },
  {
    "path": "docs/agent-task-queue.json",
    "sha": "90b336c522d27fb56989b55c8be223d97f8f707b"
  },
  {
    "path": "docs/cloud-wave1.json",
    "sha": "e28316db25ad5584bb915e7c868ede22e28f410e"
  },
  {
    "path": "docs/coordination-operations.json",
    "sha": "17e59977fd81c9a070270e16e8d62ec1f74638bf"
  },
  {
    "path": "docs/deployment.md",
    "sha": "8f8bf51a66e138c1f3a9ee1a41da7a883618511e"
  },
  {
    "path": "docs/design/cloud-evidence-dashboard.md",
    "sha": "3f482bfac22d5ec251d9e8cfcdfdf7566ea19d61"
  },
  {
    "path": "docs/design/cloud-paper-readiness-gates.md",
    "sha": "31b6aa0c4948ec3c417c2c11d9136c35ae52f970"
  },
  {
    "path": "docs/readiness.md",
    "sha": "09e5151bcd4b6040605778c47c963210f2b96315"
  },
  {
    "path": "docs/reviews/cloud-entry-gates.md",
    "sha": "baa174efecbcdd2abaa18156b0d098dd306a80d4"
  },
  {
    "path": "docs/reviews/cloud-history.md",
    "sha": "645564095ac9edb2048831f1f92135411007eac2"
  },
  {
    "path": "docs/reviews/cloud-ledger-restart.md",
    "sha": "289b764e0f3688874106619c6f8081bde6f9b367"
  },
  {
    "path": "docs/reviews/cloud-sellability.md",
    "sha": "e9241e60e33a2c548a63fadfe2ab818198cae2fe"
  },
  {
    "path": "docs/reviews/cloud-token-controls.md",
    "sha": "083014a0cd1e80314c279aa562a34f94776ed6c3"
  },
  {
    "path": "docs/reviews/continuation-entry-adapter.md",
    "sha": "9acc31cf4884ca318e47949c958fab908a9693d9"
  },
  {
    "path": "docs/reviews/legacy-live-capture-feasibility.md",
    "sha": "a1f9dfaadfccbf8e79dc96b87f6a325778628e1f"
  },
  {
    "path": "docs/reviews/ownership-budget-foundation.md",
    "sha": "a0d8fc273c2176fe379d0833db26d3ee4e924304"
  },
  {
    "path": "fixtures/account-classification/README.md",
    "sha": "c1b5fd60bec972c2aaff16a8ee2170d86754d4b8"
  },
  {
    "path": "fixtures/account-classification/pool-vault.json",
    "sha": "9752237aa65097070aae36702eba772f2cdfc34a"
  },
  {
    "path": "fixtures/account-classification/service.json",
    "sha": "cdd652520d3f5fc935aec0a9f4de4366be5da0c3"
  },
  {
    "path": "fixtures/cloud_history/synthetic.json",
    "sha": "78b3490d4eb4999c17bfb4f09a96ef41830bb661"
  },
  {
    "path": "fixtures/cloud_history_integration/README.md",
    "sha": "753ab65be08bf524f82e75b9c5e5d94bb4b52dd1"
  },
  {
    "path": "fixtures/cloud_history_integration/multi_account.json",
    "sha": "2e4b523397a61bbfae5d4a1de74808c4bdeede27"
  },
  {
    "path": "tests/test_account_classification.py",
    "sha": "c2bce21d662ef7a9a911c535e8c5e15a76f8ff21"
  },
  {
    "path": "tests/test_classification_exposure_adapter.py",
    "sha": "a430aefe8120f42810a4b9682d7aaeba4e503741"
  },
  {
    "path": "tests/test_cloud_dashboard_evidence.py",
    "sha": "9bc70ff31bc575caad6a62d974fcf3d14a547c3b"
  },
  {
    "path": "tests/test_cloud_entry_gates.py",
    "sha": "9c899d88543907bd63d14fbc5be632f18737129d"
  },
  {
    "path": "tests/test_cloud_history_adversarial.py",
    "sha": "b0c56915ea3300467704ee3fccbad1b1d2520681"
  },
  {
    "path": "tests/test_cloud_ledger_restart.py",
    "sha": "7faffed0d11c961b06b7cea3d72aab7fd11ef6ea"
  },
  {
    "path": "tests/test_cloud_readiness_invariants.py",
    "sha": "a1dde3bf3acd56b8a8be238a9fbb87e17d8e251a"
  },
  {
    "path": "tests/test_cloud_sellability.py",
    "sha": "3340ed66e7eff8bd8ba1d75a1f928a9fb0700e0f"
  },
  {
    "path": "tests/test_cloud_token_controls.py",
    "sha": "b19cc63469faf9d9387f9941f9892bc236affe2b"
  },
  {
    "path": "tests/test_continuation_entry_evidence.py",
    "sha": "6b35958484568e6d9fa799ee6b2fed6737815814"
  },
  {
    "path": "tests/test_distribution_exposure.py",
    "sha": "8ba5cc24cabb14c8d328508786101de26010ba66"
  },
  {
    "path": "tests/test_entry_evidence.py",
    "sha": "7f8e33f3866a2af4503f0df1f7bbc118a9115804"
  },
  {
    "path": "tests/test_holder_snapshot.py",
    "sha": "471b8cb5acb9b48b9397dddef52820eb23242834"
  },
  {
    "path": "tests/test_ownership_budget_admission.py",
    "sha": "d5a8f1bfeb55ae7cde87b9885efb6c95b3728b8f"
  },
  {
    "path": "tests/test_ownership_integration.py",
    "sha": "052f4ee4e6902fb7837f3d846fef70bd35da1a71"
  },
  {
    "path": "tests/test_ownership_multihistory_integration.py",
    "sha": "266e6fa1184175f989f71646652f426e5596b251"
  },
  {
    "path": "tests/test_research.py",
    "sha": "c9cdbc2236bf9874c45a15cfa5adf752334d0837"
  },
  {
    "path": "tests/test_snapshot_collection.py",
    "sha": "a0bb0627fac744efe68107f7364e967a1ecc56dd"
  },
  {
    "path": "tests/test_snapshot_provider_boundary.py",
    "sha": "6257a2dcdc87b6b73d2f06755eddef71a924fc47"
  }
]
```
