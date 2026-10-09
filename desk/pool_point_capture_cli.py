"""Explicit coordinator-only composition of existing pool point contracts.

Requires an already provisioned fixed-source ledger and existing admission.
No source/policy/endpoint options, provisioning, admissions or entry approvals.
"""
from contextlib import ExitStack, closing
from dataclasses import asdict
import json
import os
import sqlite3
import time

from .coordinator_capture_cli import _Parser, _need, _path, _preflight_guard


def _parser():
    parser = _Parser(description='Existing-admission coordinator pool point diagnostic only.', allow_abbrev=False)
    parser.add_argument('--coordinator-diagnostic', action='store_true', required=True)
    parser.add_argument('--evidence-db', required=True)
    parser.add_argument('--ledger-db', required=True)
    parser.add_argument('--scan-id', required=True)
    parser.add_argument('--capture-id', required=True)
    return parser


def _run(args):
    from .coordinator_rpc import HeliusMainnetRPC
    from .history_progress import HistoryProgress
    from .model import digest
    from .pool_capture_bridge import PoolCaptureBridge, CoordinatorTransport, Investigation, canonical_accounts, identity, SCHEMA
    from .pool_receipt_ledger import CoordinatorBoundary, PoolReceiptLedger
    from .pool_vault_admission import PROFILE
    from .providers import SOL
    _need(identity(args.scan_id) and identity(args.capture_id))
    evidence, evidence_identity = _path(args.evidence_db)
    ledger_path, ledger_identity = _path(args.ledger_db)
    _need(evidence != ledger_path)
    transport = HeliusMainnetRPC()  # Constructor has no credential/network I/O.
    with ExitStack() as guards:
        guards.enter_context(_preflight_guard(evidence, evidence_identity))
        guards.enter_context(_preflight_guard(ledger_path, ledger_identity))
        with closing(sqlite3.connect(evidence.as_uri()+'?mode=ro', uri=True)) as c:
            _need(c.execute('PRAGMA application_id').fetchone()[0] == 0)
            sizes = c.execute('SELECT length(CAST(descriptor AS BLOB)),length(CAST(prepared_source AS BLOB)) FROM ownership_admissions WHERE id=?', (args.scan_id,)).fetchone()
            _need(sizes is not None and type(sizes[0]) is int and 0 < sizes[0] <= 8192
                  and (sizes[1] is None or type(sizes[1]) is int and sizes[1] <= 2*1024*1024))
            admission = HistoryProgress.inspect_admission(c, args.scan_id)
            _need(admission is not None and admission['request_ceiling'] == 18 and admission['state'] != 'PREPARED')
        with closing(sqlite3.connect(ledger_path.as_uri()+'?mode=ro', uri=True)) as c:
            sizes = c.execute('SELECT length(CAST(body AS BLOB)) FROM ledger_descriptor').fetchall()
            _need(len(sizes) == 1 and type(sizes[0][0]) is int and 0 < sizes[0][0] <= 8192)
            body, key = c.execute('SELECT body,hash FROM ledger_descriptor').fetchone()
            descriptor = json.loads(body)
            _need(digest(descriptor) == key and descriptor['profile'] == PROFILE
                  and descriptor['sources'] == [asdict(transport.source)]
                  and descriptor['allow_synthetic_fixtures'] is False
                  and type(descriptor['max_age_seconds']) is int and descriptor['max_age_seconds'] == 60)
        boundary = CoordinatorBoundary(descriptor['ledger_id'], ledger_path.parent, ledger_path,
                                       evidence, frozenset({transport.source}))
        reader = PoolReceiptLedger(boundary)  # Audits exact persisted configuration, chain and identities.
        binding = Investigation(args.scan_id, admission['descriptor_hash'], admission['descriptor']['mint'])
        _need(binding.mint != SOL)
        pool = canonical_accounts(binding.mint)[0]
    # Existing writer reopening does not initialize a ledger. Recheck identities
    # before opening; missing/replaced files cannot become fresh provisioning.
    _need(_path(args.evidence_db)[1] == evidence_identity and _path(args.ledger_db)[1] == ledger_identity)
    writer = PoolReceiptLedger.open_writer(boundary)
    _need(writer.descriptor == reader.descriptor)
    bridge = PoolCaptureBridge(writer, CoordinatorTransport(transport.source, transport), clock=time.time)
    # Read-only audit before bridge initialization or budget-consuming capture.
    with _preflight_guard(evidence, evidence_identity), closing(sqlite3.connect(evidence.as_uri()+'?mode=ro', uri=True)) as c:
        names = {row[0] for row in c.execute('SELECT name FROM sqlite_master')}
        runs = bridge._audit(c)[0] if names.intersection(SCHEMA) else {}
        expected = {'investigation': asdict(binding), 'pool': pool, 'mint': binding.mint,
                    'source': asdict(transport.source), 'profile': PROFILE}
        if args.capture_id in runs:
            run = runs[args.capture_id]
            _need(run['descriptor'] == expected and all(step['state'] == 'DONE' for step in run['steps']))
            required = 4-len(run['steps'])
        else:
            required = 4
        bridge._check_prior({key: value for key, value in runs.items() if key != args.capture_id}, pool, binding.mint)
        admission = HistoryProgress.inspect_admission(c, args.scan_id)
        _need(admission is not None and admission['descriptor_hash'] == binding.descriptor_hash
              and admission['state'] != 'PREPARED' and 18-admission['requests_used'] >= required)
    old_umask = os.umask(0o077)
    try:
        result = bridge.capture(capture_id=args.capture_id, investigation=binding, pool=pool)
        # Read existing counter without invoking initializing HistoryProgress.
        with closing(sqlite3.connect(evidence.as_uri()+'?mode=ro', uri=True)) as c:
            result['requests_used'] = HistoryProgress.inspect_admission(c, args.scan_id)['requests_used']
        return result
    finally:
        os.umask(old_umask)


def _summary(result):
    if type(result) is not dict: result = {}
    status = result.get('status')
    used, calls = result.get('requests_used'), result.get('provider_calls')
    out = {'status': status if status in ('CAPTURED_POINT', 'BLOCKED') else 'BLOCKED',
           'requests_used': used if type(used) is int and 0 <= used <= 18 else None,
           'request_ceiling': 18, 'provider_calls': calls if type(calls) is int and 0 <= calls <= 4 else 0}
    for flag in ('chain_authenticated', 'historical_interval_exclusion_allowed', 'continuity_verified',
                 'private_control_proven', 'ownership_approval', 'eligible_for_trading'):
        out[flag] = False
    return out


def main(argv=None):
    try:
        args = _parser().parse_args(argv)
    except Exception:
        print(json.dumps(_summary({}), sort_keys=True)); return 2
    try:
        result = _run(args)
    except Exception:
        result = {}
    summary = _summary(result)
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary['status'] == 'CAPTURED_POINT' else 1


if __name__ == '__main__':
    raise SystemExit(main())
