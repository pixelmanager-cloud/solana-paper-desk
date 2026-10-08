"""Read-only legacy obligation inventory, never a token-control certificate.

Trusted local code supplies a persisted scan and read-only store; candidate JSON
cannot supply a loader, policy or approval. Source binding and canonical raw
replay come from the existing continuation consumer. Hashes bind local records,
not chain truth. Token-2022 and stronger semantic claims stay unresolved.
"""
from contextlib import contextmanager, closing
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import zlib

from .entry_evidence import continuation_snapshot
from .model import canonical, digest
from .security import account_bytes, base58, holding_policy, mint_policy

MAX_HASHES = 128
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_REPLAY_BYTES = 32 * 1024 * 1024
UNRESOLVED = {
    'historical_controls': 'HISTORICAL_CONTROL_SEMANTICS_UNVERIFIED',
    'initialization_completion': 'INITIALIZATION_FINAL_COMPLETION_UNVERIFIED',
    'parent_noninterference': 'PARENT_WRITE_NONINTERFERENCE_UNVERIFIED',
    'individual_cpi_outcomes': 'FINAL_CPI_OUTCOMES_AND_PRIVILEGES_UNVERIFIED',
    'historical_deployment': 'HISTORICAL_DEPLOYMENT_RUNTIME_IDENTITY_UNVERIFIED',
    'source_authentication': 'SOURCE_FINALITY_AND_HISTORY_COMPLETENESS_UNAUTHENTICATED',
    'transaction_bound_birth_state': 'TRANSACTION_BOUND_BIRTH_BYTES_UNAVAILABLE',
}


class ReadUnavailable(ValueError):
    """No approved read capability; never fall back to weaker locks."""


def read_platform_available():
    """Static Linux LP64 contract, not a proof of this path/kernel's locks.

    The actual guard must still succeed before any SQLite open. No filesystem
    probes or imports of platform-specific modules occur on other platforms.
    """
    if sys.platform != 'linux':
        return False
    import ctypes
    return ctypes.sizeof(ctypes.c_long) == 8 and ctypes.sizeof(ctypes.c_void_p) == 8


def _hash(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _codes(values):
    return sorted({value for value in values if type(value) is str
                   and re.fullmatch('[A-Z0-9_]{1,128}', value)})[:32]


@contextmanager
def read_guard(path):
    """Nonmutating, consistent read window on a stable Linux local file.

    Accepted rollback-only OFD guard blocks standard SQLite POSIX writers and
    journal transitions across all reader connection closes. WAL/hot or stale
    journals, non-LP64 kernels/VFSs and writer contention fail closed. No
    immutable=1, copying, checkpointing, repairs, lock files or retries.
    Operators must not rename/replace/link files while workers use them.
    """
    if not read_platform_available():
        raise ReadUnavailable('DIAGNOSTIC_READ_PLATFORM_UNAVAILABLE')
    from .coordinator_capture_cli import _preflight_guard
    resolved = Path(path).resolve(strict=True)
    # Read-only diagnostics also support local tmpfs (fixture/volatile evidence),
    # without changing the receipt writer's stricter durable-filesystem policy.
    selected = None
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split(); marker = fields.index('-')
        mount = Path(fields[4].replace('\\040',' ').replace('\\134','\\'))
        if mount == resolved or mount in resolved.parents:
            if selected is None or len(str(mount)) >= selected[0]:
                selected = (len(str(mount)), fields[marker+1])
    if selected is None or selected[1] not in {'ext2','ext3','ext4','xfs','btrfs','zfs','overlay','tmpfs'}:
        raise ValueError('Unsupported local read filesystem')
    info = resolved.lstat(); parent = resolved.parent.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise ValueError('Unsupported diagnostic read boundary')
    with _preflight_guard(resolved, (parent.st_dev, parent.st_ino, info.st_dev, info.st_ino)):
        yield


class ReplayView:
    """Local immutable-record cache; shared physical read/byte limits stay intact.

    The caller must hold read_guard while using this view. It wraps a trusted
    read-only bounded EvidenceStore, never a caller-selected remote reader.
    JSON round trips prevent consumers from modifying the cached projection.
    """
    def __init__(self, store):
        if store.read_only is not True:
            raise ValueError('Read-only diagnostic store required')
        self.store = store; self.path = store.path; self.read_only = True
        self.cache = {}; self.requested = set(); self.bytes = 0

    def connect(self): return self.store.connect()

    def load(self, key):
        if not _hash(key): raise ValueError('Invalid raw reference')
        self.requested.add(key)
        if len(self.requested) > MAX_HASHES:
            raise ValueError('Diagnostic evidence reference ceiling')
        if key not in self.cache:
            payload = self.store.load(key)
            if digest(payload) != key: raise ValueError('Raw hash mismatch')
            encoded = canonical(payload)
            self.bytes += len(encoded.encode())
            if self.bytes > MAX_REPLAY_BYTES:
                raise ValueError('Diagnostic replay byte ceiling')
            self.cache[key] = encoded
        return json.loads(self.cache[key])


def unknown_inventory(*, revision_hash=None, now=None, unavailable_reason=None):
    obligations = {name: {'status':'UNRESOLVED', 'unknown_reasons':[reason],
                          'evidence_hashes':[]} for name, reason in UNRESOLVED.items()}
    for name in ('endpoint_controls', 'historical_accounting'):
        obligations[name] = {'status':'UNRESOLVED', 'unknown_reasons':['PERSISTED_RAW_REPLAY_UNAVAILABLE'],
                             'evidence_hashes':[]}
    if unavailable_reason is not None:
        for name in ('endpoint_controls', 'historical_accounting'):
            obligations[name]['unknown_reasons'] = [unavailable_reason]
    return _finish({'schema':'legacy_control_obligations_v1', 'decision':'REJECT',
            'read_status':'UNAVAILABLE',
            'revision_hash':revision_hash, 'evaluated_at':now, 'observed_at':None,
            'snapshot_slot':None, 'snapshot_time':None, 'source_hash':None,
            'requests_used':None, 'request_ceiling':18, 'provider_calls':0,
            'source_binding':None, 'evidence_hashes':[], 'obligations':obligations,
            'historical_control_verified':False, 'noninterference_verified':False,
            'lifecycle_verified':False, 'chain_authenticated':False,
            'common_control_verified':False, 'ownership_approved':False,
            'eligible_for_trading':False,
            'scope':'Historical legacy components at actual bank T; no birth-state, current-entry or control certificate'})


def _finish(result):
    result['blockers'] = sorted({reason for obligation in result['obligations'].values()
                                for reason in obligation['unknown_reasons']} |
                               set(result.get('freshness_reasons', [])))
    result.pop('manifest_hash', None)
    result['manifest_hash'] = digest(result)
    return result


def _endpoint(mint, snapshot, history):
    """Named legacy endpoint policy over the persisted discovered frontier.

    Frontier/history completeness is a SEPARATE accounting obligation. A clean
    endpoint cannot erase transient controls or prove absence throughout time.
    """
    values = snapshot['result']['value']; keys = snapshot['params'][0]
    reasons = list(mint_policy(values[0])['reasons'])
    endings = {row['account']:row for row in history['account_continuity']['end_states']}
    accounts = []
    for key, value in zip(keys[1:], values[1:]):
        if value is None:
            end = endings.get(key)
            if not end or end['closed'] is not True or end['amount_raw'] is not None:
                reasons.append('ENDPOINT_CLOSED_ACCOUNT_UNVERIFIED')
            accounts.append({'account':key, 'state':'ABSENT'})
            continue
        raw = account_bytes(value)
        if len(raw) != 165:
            reasons.append('INVALID_HOLDING_LAYOUT'); continue
        owner = base58(raw[32:64])
        checked = holding_policy(value, mint, owner)
        reasons.extend(checked['reasons'])
        if checked['decision'] != 'PASS_HOLDING_POLICY':
            reasons.append('ENDPOINT_HOLDING_POLICY_FAILED')
        accounts.append({'account':key, 'token_authority':owner,
                         'amount_raw':checked.get('amount_raw'), 'state':'OBSERVED'})
    return _codes(reasons), accounts


def inventory(scan, store, *, revision_hash, now):
    """Replay bound persisted evidence; no caller summary can promote a claim.

    scan is the exact local scans-row projection (also independently bound by
    continuation_snapshot). store is a locally constructed read-only dependency.
    No summary/progress/policy/approved-source parameters are accepted.
    """
    result = unknown_inventory(revision_hash=revision_hash, now=now)
    try:
        if (type(scan) is not dict or set(scan) != {'id','mint','created','status','result'}
                or not _hash(revision_hash) or type(now) is not int or not 0 <= now < 2**63
                or type(scan['result']) is not str or len(scan['result'].encode()) > MAX_SOURCE_BYTES):
            raise ValueError('Invalid source projection')
        report = json.loads(scan['result'])
        if type(report) is not dict: raise ValueError('Invalid source report')
        observed = report.get('observed_at')
        result['source_hash'] = digest(scan)
        view = ReplayView(store)
        with read_guard(store.path):
            replay = continuation_snapshot(scan, report, view, progress={'evidence_hash':revision_hash})
            if 'history' not in replay or 'replay' not in replay:
                reasons = _codes(replay['reasons'] + ['PERSISTED_RAW_REPLAY_UNAVAILABLE'])
                for name in ('endpoint_controls','historical_accounting'):
                    result['obligations'][name]['unknown_reasons'] = reasons
            else:
                if type(observed) is int and 0 <= observed < 2**63: result['observed_at'] = observed
                # All mutable bindings are stable under the OFD guard. Read the
                # exact canonical bank, not the progress summary's asserted slot.
                with closing(view.connect()) as c:
                    bank = c.execute('SELECT snapshot_hash,clock_hash FROM ownership_banks WHERE budget=?',(scan['id'],)).fetchone()
                    budget = c.execute('SELECT source_hash,used,ceiling FROM ownership_budgets WHERE id=?',(scan['id'],)).fetchone()
                    tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    admission = c.execute('SELECT descriptor,state,prepared_source,prepared_used,completed_source_hash FROM ownership_admissions WHERE id=?',(scan['id'],)).fetchone() if 'ownership_admissions' in tables else None
                result['source_binding'] = {
                    'mode':'SEALED_ADMISSION' if admission else 'LEGACY_COMPLETED_SOURCE',
                    'budget_source_hash':budget[0], 'admission_hash':digest(list(admission)) if admission else None,
                    'completed_source_hash':admission[4] if admission else digest(scan),
                    'snapshot_hash':bank[0], 'clock_hash':bank[1]}
                snapshot = view.load(bank[0])
                reasons, accounts = _endpoint(scan['mint'], snapshot, replay['history'])
                result.update(snapshot_slot=replay['replay']['slot'], snapshot_time=replay['replay']['block_time'],
                              requests_used=budget[1])
                result['obligations']['endpoint_controls'].update(
                    status='UNRESOLVED' if reasons else 'OBSERVED_COMPONENT', unknown_reasons=reasons,
                    evidence_hashes=[bank[0]], policy='legacy-endpoint-controls-v1',
                    scope='Persisted discovered frontier at T; completeness and historical controls are separate',
                    accounts=accounts)
                result['obligations']['historical_accounting'].update(
                    status='RECONCILED_COMPONENT' if replay['replay']['reconciled'] else 'UNRESOLVED',
                    unknown_reasons=_codes(replay['reasons']), evidence_hashes=sorted(view.requested),
                    scope='Provider-declared complete history replay to actual T; no authenticated CPI/control theorem')
                result['evidence_hashes'] = sorted(view.requested)
                result['read_status'] = 'AVAILABLE'
        if result['observed_at'] is None or not 0 <= now-result['observed_at'] <= 10:
            result['freshness_reasons'] = ['INVESTIGATION_NOT_FRESH_FOR_ENTRY']
        else:
            result['freshness_reasons'] = []
    except ReadUnavailable:
        result = unknown_inventory(revision_hash=revision_hash, now=now,
                                   unavailable_reason='DIAGNOSTIC_READ_PLATFORM_UNAVAILABLE')
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, sqlite3.Error,
            OverflowError, RecursionError, zlib.error, UnicodeError, OSError):
        result = unknown_inventory(revision_hash=revision_hash, now=now)
    return _finish(result)
