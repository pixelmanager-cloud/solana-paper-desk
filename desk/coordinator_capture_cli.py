"""Explicit local-coordinator diagnostic; never a candidate/entry endpoint.

Run only on a trusted owned stable local Linux path after independent review.
Operators must not rename/replace/link paths while any ownership worker runs.
Imports/help perform no I/O. No admission creation, budget reset or retry exists.
"""
import argparse
import json
import os
from pathlib import Path
import stat
import sys


class _Blocked(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes supplied arguments, which may contain secrets.
        raise _Blocked('Invalid coordinator arguments')


def _parser():
    parser = _Parser(description='Local coordinator only: one raw diagnostic on an existing admission; all approvals remain false.',
                     allow_abbrev=False)
    parser.add_argument('--coordinator-diagnostic', action='store_true', required=True,
                        help='Explicit coordinator invocation; not an authentication mechanism')
    parser.add_argument('--evidence-db', required=True, help='Existing canonical absolute DB in an owned 0700 local Linux directory; file mode 0600')
    parser.add_argument('--scan-id', required=True)
    parser.add_argument('--descriptor-hash', required=True)
    parser.add_argument('--mint', required=True)
    parser.add_argument('--signature', required=True)
    return parser


def _need(condition):
    if not condition: raise _Blocked('Coordinator capture blocked')


def _hash(value):
    return type(value) is str and len(value) == 64 and all(ch in '0123456789abcdef' for ch in value)


def _path(supplied):
    # Reuse the accepted positive local-filesystem allowlist; no guard fallback.
    from .pool_receipt_ledger import _local_fs
    _need(sys.platform == 'linux')
    path = Path(supplied)
    _need(path.is_absolute() and supplied == str(path) and str(path) == str(path.resolve(strict=True)))
    root = path.parent
    _local_fs(root)
    parent = root.lstat(); info = path.lstat()
    _need(stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.getuid() and stat.S_IMODE(parent.st_mode) == 0o700)
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
          and stat.S_IMODE(info.st_mode) == 0o600)
    for suffix in ('-wal', '-shm', '-journal', '.ownership.lock', '.ownership-invocation.lock'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            record = sidecar.lstat()
            _need(stat.S_ISREG(record.st_mode) and record.st_nlink == 1 and record.st_uid == os.getuid()
                  and stat.S_IMODE(record.st_mode) == 0o600)
    return path, (parent.st_dev, parent.st_ino, info.st_dev, info.st_ino)


def _run(args):
    # Strict supplied binding is checked before any database/credential I/O.
    from .coordinator_rpc import HeliusMainnetRPC, _params
    from .programs import address, unbase58
    from .security import base58
    _need(type(args.scan_id) is str and 1 <= len(args.scan_id) <= 128
          and all(ch.isascii() and (ch.isalnum() or ch in '_.:-') for ch in args.scan_id))
    _need(_hash(args.descriptor_hash) and type(args.mint) is str)
    address(args.mint); _need(base58(unbase58(args.mint)) == args.mint)
    options = {'encoding': 'json', 'commitment': 'finalized', 'maxSupportedTransactionVersion': 0}
    _params('getTransaction', [args.signature, options])
    path, identity = _path(args.evidence_db)

    from contextlib import closing
    import sqlite3
    from .evidence import EvidenceStore
    from .history_progress import HistoryProgress
    from .raw_transaction_capture import RawTransactionCapture, TransactionBinding, ReadOnlyRPC

    class ExistingEvidence(EvidenceStore):
        # Opening/guarding never initializes/recreates a DB, even if it vanishes.
        def connect(self):
            current_path, current_identity = _path(str(self.path))
            _need(current_path == path and current_identity == identity)
            mode = 'ro' if self.read_only else 'rw'
            return sqlite3.connect(self.path.as_uri() + '?mode=' + mode, uri=True,
                                   timeout=20, isolation_level=None)

    class AdmissionReader(HistoryProgress):
        def __init__(self, store):
            self.store = store  # Read-only preflight; no CREATE TABLE calls.

    store = ExistingEvidence(path, read_only=True)
    with closing(store.connect()) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        _need({'pages', 'ownership_admissions', 'ownership_budgets', 'ownership_history'} <= tables)
    admission = AdmissionReader(store).admission(args.scan_id)
    _need(admission is not None and admission['descriptor_hash'] == args.descriptor_hash
          and admission['descriptor']['mint'] == args.mint and admission['request_ceiling'] == 18)
    store.read_only = False
    transport = HeliusMainnetRPC()
    binding = TransactionBinding(args.scan_id, args.descriptor_hash, args.mint, args.signature)
    previous_umask = os.umask(0o077)
    try:
        return RawTransactionCapture(store, ReadOnlyRPC(transport.source.source_id, transport)).capture(binding)
    finally:
        os.umask(previous_umask)


def _summary(result):
    # Never emit raw records, diagnostic strings, dynamic reasons, paths, IDs,
    # exception text, provider bodies or any credential context.
    if type(result) is not dict: result = {}
    state = result.get('state')
    if type(state) is not str or state not in ('COMPLETE', 'REJECTED', 'FAILED', 'REFUSED', 'PENDING', 'BLOCKED'): state = 'BLOCKED'
    used = result.get('requests_used'); attempts = result.get('provider_calls')
    summary = {'state': state, 'requests_used': used if type(used) is int and 0 <= used <= 18 else None,
               'request_ceiling': 18, 'rpc_attempts': attempts if type(attempts) is int and attempts in (0, 1) else 0}
    for name in ('request_hash', 'response_hash', 'claim_hash'):
        summary[name] = result.get(name) if _hash(result.get(name)) else None
    for name in ('lifecycle_verified', 'finality_authenticated', 'signature_authenticated', 'source_authenticated',
                 'caller_privileges_verified', 'cpi_success_verified', 'account_state_verified',
                 'ownership_approved', 'eligible_for_trading'):
        summary[name] = False
    return summary


def main(argv=None):
    try:
        args = _parser().parse_args(argv)
    except Exception:
        print(json.dumps(_summary({'state': 'BLOCKED'}), sort_keys=True))
        return 2
    try:
        result = _run(args)
    except Exception:
        result = {'state': 'BLOCKED'}
    summary = _summary(result)
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary['state'] == 'COMPLETE' else 1


if __name__ == '__main__':
    raise SystemExit(main())
