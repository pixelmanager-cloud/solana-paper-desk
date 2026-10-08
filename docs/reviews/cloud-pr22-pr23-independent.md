# PR22 and PR23 independent review — 8 October 2026

No merge/acceptance/live claims. Policy cloud-independent-review-v1.

## PR22

Independent exact-head review (cloud-independent-review-v1) at c50b7aa5e690fb2a3517c7f9afd7a75f3908384b. Full actual diff and predecessor code read; every retrieved Git blob matched the isolated local review files. Head checked before review and again before submission. No blocking defect found within the deliberately reject-only contract; no positive classification, ownership acceptance or entry approval follows.

Verified behavior:
- Raw chain/request/slot/mint/token-authority bindings are inspected separately from assertions; program is not copied into owner. Exclusive expires_slot maps to inclusive through=expiry-1, and time validity is checked at both replay boundaries and actual evaluated_at.
- Positive-looking candidate flags, a self-hash, invented source_hash and RPC-shaped bytes never create trusted policy membership. The two constant blockers remain present, verified/private_control_proven/ownership_approval remain false, and to_core always uses UNRESOLVED with verified=False.
- Independent 19-variant probe (well-shaped, absent, forged summary, wrong/null/string/bool slots, malformed encodings, delegate/freeze/native/close-control mutations) passed through the exact PR12 core at 0b4fbdb108cb00ac2927071812ed725c6469f1ea. Even an asserted validated/complete 100-unit snapshot produced excluded=0, lower_bound=0, unresolved=100 for every variant.

Required positive-source path remains NOT IMPLEMENTED: establish a candidate-independent, versioned admission authority/policy; independently verify raw source identity and exact chain/program/address/PDA/service or pool role; bind admitted attestations to persisted source evidence and reviewer/verifier provenance; reconstruct whole-interval account lifecycle/control continuity, mint/holder/history coverage and finalized ordering; enforce actual time and scoped expiry and retain contradictions/gaps. Only that trusted reconstruction may construct positive classifications/validated snapshots/seeds. Neither this adapter's input genesis_hash nor evidence digests authenticate source capture. Boundary identity equality alone cannot rule out authority changes and restoration between boundaries. UNKNOWN must not become PRIVATE. Keep entry wiring disabled until predecessor acceptance and independent positive-path review.

Actionable test follow-up: exact PR22 branch lacks distribution_exposure, so its full suite reports 535 tests with ONE SKIP (534 passed). I separately loaded the GitHub blob of the exact PR12 core in a composed fixture harness and ran all 10 adapter tests with zero skips, including test_exact_pr12_core_forged_exclusion_bypass_is_blocked. Preserve this dependency test in the actual combined integration suite and report skips explicitly; successful characterization or rejection does not satisfy OWN-2/OWN-3/OWN-4 acceptance.

Validation: Python 3.12.14, full exact-head suite 535 run / 1 skipped; 10/10 composed adapter tests; independent 19-case core rejection probe passed. Local loopback permission used only for the existing HTTP fixture; no provider calls. Exact-head GitHub Tests run 37711837319 succeeded. No merge, issue closure, source-authenticity approval or live readiness claim.

Review ID: 5450300745.

```python
import copy,base64
from tests.test_classification_exposure_adapter import ClassificationExposureAdapterTests
from desk.distribution_exposure import Balance,ValidatedSnapshot,Seed,trace_exposure

f=ClassificationExposureAdapterTests();f.setUp()
snapshot=ValidatedSnapshot(f.mint,10,19,100,(Balance(f.account,f.authority,100),),
    (Balance(f.account,f.authority,100),),'asserted-snapshot','asserted-history',True,True)
variants=[None,{}, {'verified':True}, f.current]
for slot in (True,-1,18,20,None,'19'):
    raw=copy.deepcopy(f.current);raw['result']['context']['slot']=slot;variants.append(raw)
for data in (['!!!!','base64'],[123,'base64'],['AA==','base64'],None):
    raw=copy.deepcopy(f.current);raw['result']['value']['data']=data;variants.append(raw)
for offset,value in ((72,1),(108,2),(121,1),(129,2),(109,1)):
    raw=copy.deepcopy(f.current);data=bytearray(base64.b64decode(raw['result']['value']['data'][0]));data[offset]=value
    raw['result']['value']['data'][0]=base64.b64encode(data).decode();variants.append(raw)
for raw in variants:
    result=f.adapt(current_raw=raw)
    assert not result.verified and not result.private_control_proven and not result.ownership_approval
    outcome=trace_exposure(snapshot,(),(Seed(f.account,100),),(result.to_core(),))
    assert (outcome.excluded,outcome.lower_bound,outcome.unresolved)==(0,0,100)
print(f'PASS: {len(variants)} valid/forged/malformed raw boundary variants remain non-admitted and produce zero exclusion/lower bound in exact PR12 core; every 100-unit holding stays unresolved.')

```

## PR23

Independent exact-head review (cloud-independent-review-v1) at 11bf7d168200f7945a36493f763378b4a8a5ca39. Actual security/holder code and all changed adversarial expectations reviewed; downloaded Git blobs matched local review files. No blocking defect found within this scoped conservative legacy-token correction. No merge approval or ownership/live acceptance.

Verified:
- Both holding_policy and atomic holder verification now reject delegate-option=0 with nonzero delegated amount, even when indexed delegated_raw is changed to match. Matching asserted index data cannot hide that contradiction.
- Close option must be 0 or 1; when present, close key must match the bound token-authority wallet. Identity/layout/program checks remain separate and unchanged. Inactive option payload bytes are not treated as live authorities.
- Actual delegates, including zero allowance, remain rejected by holding_policy. Snapshot coverage may reconcile such balances but reports delegated_accounts; I inspected desk/entry_evidence.py and desk/screen.py, which retain their delegated-holder blockers. A verified coverage result alone must never be promoted to token-control approval.
- Independent 150-case matrix of raw delegate tags (0,1,2,256,UINT32_MAX), allowances (0,1,UINT64_MAX), close tags and wallet/external keys with MATCHING indexed allowances passed. Snapshot versus holding-policy results matched those stricter contracts for every case.
- The original mainnet-holder-snapshot fixture is unchanged. Its external close authorities now correctly block verification while preserving the observed exact supply sum and raw evidence. Positive mutation tests explicitly use a synthetic copy; changed persisted-entry/scanner tests require rejection and suppress verified holder metrics, rather than relaxing production gates to preserve a fixture pass.

Remaining limitations: this is policy validation of persisted bytes, not independent source authentication or program-internals verification. Token-2022 remains excluded; structural extension inventory is not semantic support. These checks do not establish historical transfer/lifecycle completeness, classification, distribution exposure or liquidity/entry permission. Fresh live verification and ownership acceptance remain with the original desktop coordinator.

Validation: Python 3.12.14 full exact-head suite 531/531, zero skips; independent 150-case authority matrix passed. Existing private-loopback fixture permission only; no provider calls. GitHub Tests run 37712033090 completed success at this exact head. When incorporated into #21, rerun the exact combined commit (especially ownership snapshot/replay and reject-only adapter paths) and require current CI; these separate head results do not certify an untested combined revision.

Review ID: 5450301585.

```python
from tests.test_cloud_token_controls import CloudTokenControlsTests
from desk.security import holding_policy

f=CloudTokenControlsTests();f.setUp();baseline=f.raw(f.holder);count=0
for delegate in (0,1,2,256,2**32-1):
 for amount in (0,1,2**64-1):
  for close in (0,1,2,256,2**32-1):
   for close_key in (baseline[32:64],bytes([9])*32):
    raw=baseline.copy();raw[72:76]=delegate.to_bytes(4,'little');raw[121:129]=amount.to_bytes(8,'little')
    raw[129:133]=close.to_bytes(4,'little');raw[133:165]=close_key;f.put(f.holder,raw)
    f.fixture['enumeration']['accounts'][0]['delegated_raw']=str(amount)
    snapshot=f.snapshot();holding=f.holding()
    allowed_close=close==0 or (close==1 and close_key==baseline[32:64])
    allowed_consistency=delegate!=0 or amount==0
    assert snapshot['verified']==(delegate in (0,1) and allowed_consistency and allowed_close)
    assert (holding['decision']=='PASS_HOLDING_POLICY')==(delegate==0 and amount==0 and allowed_close)
    if delegate==1:assert f.fixture['enumeration']['accounts'][0]['address'] in snapshot['delegated_accounts']
    count+=1
print(f'PASS: {count} independent raw delegate-option/amount/close-option/key combinations with matching indexed amounts reject orphaned delegation, malformed options and external close keys; real delegates never pass holding policy.')

```
