"""Source/revision-bound pool point prerequisite, never interval ownership.

Trusted application initialization supplies a read-only PoolReceiptLedger from
the protected coordinator boundary. Never construct that boundary from candidate
JSON. No caller policies, trusted hashes or admission flags are accepted here.
Local source binding and explicit coordinator RPC-source trust are distinct from
chain authentication. Existing history/entry guards remain unresolved.
"""
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3

from .control_obligations import ReplayView, read_guard, _hash
from .entry_evidence import continuation_snapshot
from .live_features import _BoundedStore, MAX_SOURCE_BYTES
from .model import digest
from .pool_receipt_ledger import PoolReceiptLedger
from .pool_vault_admission import admit_pool_vault, _raw_account
from .programs import address
from .security import TOKEN_PROGRAM


class _Blocked(ValueError):
    pass


def _state(value, length):
    # Reuse admission's raw metadata/layout checks. Ignore transport annotations,
    # compare bytes, chain owner, executable status and lamports without rewriting.
    raw = _raw_account(value, TOKEN_PROGRAM, (length,))
    return value['owner'], value['executable'], value['lamports'], raw


def project_pool_vault(research_db, ledger, scan_id, *, revision_hash, account,
                       pool, now):
    """Inspect an exact current persisted revision under guarded local reads.

    ledger is a trusted configured dependency, not a candidate-supplied object.
    Selection of the newest scoped receipt does not prune observations: the
    existing admission replays ALL approved observations of the same bank.
    Shared replay limits are 128 references/loads and 32 MiB, including admission.
    No generic classification record or exposure-core input is issued.
    """
    out = {'schema': 'source_bound_pool_vault_point_v1', 'scan_id': scan_id,
           'revision_hash': revision_hash, 'account': account, 'pool': pool,
           'evaluated_at': now, 'source_hash': None, 'snapshot_slot': None,
           'snapshot_time': None, 'snapshot_hash': None, 'clock_hash': None,
           'mint': None, 'purpose': 'holder_exclusion_point_prerequisite',
           'expires_at': None,
           'label': 'UNKNOWN', 'point_binding_verified': False,
           'production_point_prerequisite': False, 'admission': None,
           'historical_interval_exclusion_allowed': False,
           'classification_resolved': False, 'exclusion_allowed': False,
           'private_control_proven': False, 'ownership_approval': False,
           'chain_authenticated': False, 'eligible_for_trading': False,
           'provider_calls': 0, 'request_ceiling': 18, 'requests_used': None,
           'history_reasons': [], 'evidence_hashes': [], 'reasons': []}
    def reject(reason):
        raise _Blocked(reason)
    try:
        if type(ledger) is not PoolReceiptLedger or ledger.writer:
            reject('PROTECTED_READ_ONLY_RECEIPT_LEDGER_REQUIRED')
        if (type(scan_id) is not str or not 1 <= len(scan_id) <= 128
                or not _hash(revision_hash) or type(now) is not int or not 0 <= now < 2**63):
            reject('CLASSIFICATION_QUERY_INVALID')
        address(account); address(pool)
        research = Path(research_db).resolve(strict=True)
        evidence = ledger.evidence_path
        if research.samefile(evidence) or research.samefile(ledger.path):
            reject('CLASSIFICATION_DATABASE_IDENTITY_OVERLAP')
        # Fixed order: source read guard, evidence read guard, receipt ledger.
        # No writer acquisition or provider call occurs in this projection.
        with ExitStack() as guards:
            guards.enter_context(read_guard(research))
            guards.enter_context(read_guard(evidence))
            with closing(sqlite3.connect(research.as_uri() + '?mode=ro', uri=True)) as c:
                c.row_factory = sqlite3.Row
                c.execute('PRAGMA query_only=ON'); c.execute('BEGIN')
                row = c.execute('SELECT id,mint,created,status,result FROM scans WHERE id=?',
                                (scan_id,)).fetchone()
            if row is None or type(row['result']) is not str or len(row['result'].encode()) > MAX_SOURCE_BYTES:
                reject('PERSISTED_CLASSIFICATION_SOURCE_UNAVAILABLE')
            scan = dict(row); report = json.loads(scan['result'])
            view = ReplayView(_BoundedStore(evidence))
            replay = continuation_snapshot(scan, report, view,
                                           progress={'evidence_hash': revision_hash})
            if 'history' not in replay or 'replay' not in replay:
                reject('SOURCE_REVISION_RAW_BANK_UNVERIFIED')
            revision = view.load(revision_hash)
            refs = revision['snapshot_evidence']
            bank = view.load(refs['snapshot_hash'])
            view.load(refs['block_time_hash'])
            keys = bank['params'][0]; values = bank['result']['value']
            slot = replay['replay']['slot']; at = replay['replay']['block_time']
            if account not in keys[1:] or values[keys.index(account)] is None:
                reject('VAULT_NOT_IN_REVISION_BANK')
            with ledger.policy_view(pool=pool, mint=scan['mint'], slot=slot) as policy_view:
                receipts = policy_view.policy.receipts
                if not receipts:
                    reject('SOURCE_APPROVED_POOL_POINT_UNAVAILABLE')
                receipt = max(receipts, key=lambda r: (r.captured_at, r.refs.hashes(), r.source_id))
                # Same immutable evidence store and guarded policy window; sharing
                # the bounded cache keeps aggregate history+point replay finite.
                admitted = admit_pool_vault(account=account, pool=pool, mint=scan['mint'],
                    snapshot_slot=slot, snapshot_time=at, now=now, refs=receipt.refs,
                    policy=policy_view.policy, load=view.load)
                if not admitted['snapshot_label_admitted']:
                    out['reasons'] = list(admitted['reasons'])
                    reject('SOURCE_APPROVED_POOL_POINT_REJECTED')
                if admitted['vault_mint'] != scan['mint']:
                    reject('CLASSIFICATION_VAULT_MINT_SCOPE_MISMATCH')
                point = view.load(receipt.refs.snapshot)
                point_keys = point['params'][0]; point_values = point['result']['value']
                if (_state(values[0], 82) != _state(point_values[point_keys.index(scan['mint'])], 82)
                        or _state(values[keys.index(account)], 165) !=
                           _state(point_values[point_keys.index(account)], 165)):
                    reject('REVISION_POOL_POINT_RAW_STATE_CONFLICT')
                matched = dict(label='POOL_VAULT', point_binding_verified=True,
                    production_point_prerequisite=admitted['production_snapshot_exclusion_allowed'],
                    admission=admitted, source_hash=digest(scan), snapshot_slot=slot,
                    snapshot_time=at, snapshot_hash=refs['snapshot_hash'],
                    mint=scan['mint'], expires_at=admitted['expires_at'],
                    clock_hash=refs['block_time_hash'], requests_used=replay['requests_used'],
                    history_reasons=list(replay['reasons']), evidence_hashes=sorted(view.requested))
        # A guard teardown failure also rejects; publish no positive prefix while
        # the protected read windows are still open.
        out.update(matched)
    except Exception as exc:
        # A failed local read/receipt/raw binding cannot promote a partial label.
        out['reasons'] = sorted(set(out['reasons'] + [str(exc) if type(exc) is _Blocked
                                  else 'CLASSIFICATION_POINT_READ_OR_BINDING_UNAVAILABLE']))
    return out
