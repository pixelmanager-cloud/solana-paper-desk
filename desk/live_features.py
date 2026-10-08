"""Read-only historical candidate diagnostics. This module cannot emit market events."""
import json
import re
import sqlite3
import zlib
from contextlib import closing, ExitStack
from pathlib import Path

from .decision_runner import assess
from .evidence import EvidenceStore
from .model import canonical, digest
from .control_obligations import ReplayView, inventory, read_guard, unknown_inventory

MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_LOADS = 128
MAX_REPLAY_BYTES = 32 * 1024 * 1024
MAX_HASHES = 128

# These units describe required inputs, not a conversion or an attestation.
REQUIRED = {
    'reserve_sol': ('SOL', 'RAW_RESERVES_NOT_CONVERTED_TO_STRATEGY_UNITS'),
    'reserve_tokens': ('tokens', 'RAW_RESERVES_NOT_CONVERTED_TO_STRATEGY_UNITS'),
    'sol_usd': ('USD/SOL', 'PRICE_SOURCE_UNAVAILABLE'),
    'market_cap_usd': ('USD', 'PRICE_AND_SUPPLY_VALUATION_UNVERIFIED'),
    'top10_pct': ('percent_private_supply', 'PRIVATE_OWNER_CLASSIFICATION_UNVERIFIED'),
    'dev_pct': ('percent_supply', 'DEVELOPER_ATTRIBUTION_UNVERIFIED'),
    'bundle_pct': ('percent_supply', 'CURRENT_HOLDER_BUNDLE_EXPOSURE_UNVERIFIED'),
    'cluster_pct': ('percent_supply', 'FUNDING_SERVICE_CLASSIFICATION_UNVERIFIED'),
    'flow': ('percent', 'LIVE_FLOW_MOMENTUM_AND_COST_INPUTS_UNVERIFIED'),
    'fresh_wallet_ratio': ('ratio', 'WALLET_AGE_UNVERIFIED'),
    'manip_safety': ('ratio', 'MANIPULATION_MODEL_UNVERIFIED'),
    'manip_flow': ('ratio', 'MANIPULATION_MODEL_UNVERIFIED'),
    'net_buy_ratio': ('ratio', 'FLOW_WINDOW_UNVERIFIED'),
    'drawdown_from_high': ('ratio', 'MOMENTUM_WINDOW_UNVERIFIED'),
    'wash_score': ('ratio', 'WASH_CLASSIFICATION_UNVERIFIED'),
    'unique_buyers_5m': ('owners/5min', 'FLOW_WINDOW_UNVERIFIED'),
    'volume_vs_liq': ('ratio', 'FLOW_AND_LIQUIDITY_UNITS_UNVERIFIED'),
    'dev_launches_7d': ('launches/7days', 'DEVELOPER_HISTORY_UNVERIFIED'),
    'pool_fee_bps': ('basis_points', 'EXACT_ROUTE_COST_UNVERIFIED'),
}
for _name in ('graduated', 'mint_revoked', 'freeze_revoked', 'lp_verified',
              'extensions_safe', 'data_healthy', 'flow_confirmed', 'danger', 'route_available'):
    REQUIRED[_name] = ('boolean_attestation', 'CURRENT_ENTRY_ATTESTATION_UNAVAILABLE')
for _name in ('price_at', 'holder_at', 'flow_at', 'momentum_at', 'graduated_at'):
    REQUIRED[_name] = ('UTC_epoch_seconds', 'CURRENT_COMPONENT_OBSERVATION_UNAVAILABLE')
REQUIRED['pool'] = ('public_key', 'EXACT_ENTRY_ROUTE_UNVERIFIED')
REQUIRED['provenance'] = ('source_binding', 'CURRENT_MARKET_EVENT_PROVENANCE_UNAVAILABLE')


class _BoundedStore(EvidenceStore):
    def __init__(self, path):
        super().__init__(path, read_only=True)
        self.loads = 0
        self.bytes = 0

    def load(self, key):
        self.loads += 1
        if self.loads > MAX_LOADS:
            raise ValueError('Diagnostic replay load ceiling exceeded')
        payload = super().load(key)
        self.bytes += len(canonical(payload).encode())
        if self.bytes > MAX_REPLAY_BYTES:
            raise ValueError('Diagnostic replay byte ceiling exceeded')
        return payload


def candidate_snapshot(research_db, evidence_db, scan_id, *, revision_hash, now):
    """Read persisted source and replay a caller-selected head, never caller features.

    Hash integrity establishes local binding, not provider authenticity or finality.
    The selected revision must still be the persisted head during existing replay.
    """
    if (not isinstance(scan_id, str) or not 0 < len(scan_id) <= 256
            or not isinstance(revision_hash, str)
            or re.fullmatch('[0-9a-f]{64}', revision_hash) is None
            or type(now) is not int or not 0 <= now <= 2**63 - 1):
        raise ValueError('Invalid diagnostic identity, revision or evaluation time')
    fields = {name: {'status': 'UNKNOWN', 'value': None, 'units': units,
                     'unknown_reasons': [reason], 'evidence_hashes': []}
              for name, (units, reason) in sorted(REQUIRED.items())}
    result = {'schema': 'candidate_snapshot_diagnostic_v1', 'decision': 'REJECT',
              'eligible_for_trading': False, 'evaluated_at': now, 'observed_at': None,
              'scan_id': scan_id, 'revision_hash': revision_hash, 'source_hash': None,
              'scope': 'Historical local evidence; no current entry attestation',
              'provider_authenticity': 'UNVERIFIED', 'fields': fields,
              'components': {}, 'evidence_hashes': [],
              'control_obligations': unknown_inventory(revision_hash=revision_hash, now=now),
              'reasons': ['LIVE_FEATURE_ADAPTER_NOT_READY']}
    guards = ExitStack()
    try:
        guards.enter_context(read_guard(research_db))
        guards.enter_context(read_guard(evidence_db))
        with closing(sqlite3.connect(Path(research_db).resolve().as_uri() + '?mode=ro',
                                    uri=True, timeout=2)) as c:
            c.row_factory = sqlite3.Row
            c.execute('BEGIN')
            row = c.execute('SELECT id,mint,created,status,result FROM scans WHERE id=?',
                            (scan_id,)).fetchone()
            if row is None or not isinstance(row['result'], str):
                raise ValueError('Missing source')
            if len(row['result'].encode()) > MAX_SOURCE_BYTES:
                raise ValueError('Oversized source')
            scan = dict(row)
        report = json.loads(scan['result'])
        if not isinstance(report, dict):
            raise ValueError('Malformed source')
        observed = report.get('observed_at')
        if type(observed) is int and 0 <= observed <= 2**63 - 1:
            result['observed_at'] = observed
        result['source_hash'] = digest(scan)
        store = ReplayView(_BoundedStore(evidence_db))
        decision = assess(scan, now, store,
                          progress={'evidence_hash': revision_hash})
        result['control_obligations'] = inventory(scan, store, revision_hash=revision_hash, now=now)
        gates = decision['entry_evidence']['gates']
        # A pinned raw historical reconciliation also validates source/seal/budget.
        bound = gates['history_snapshot']['status'] == 'VERIFIED_COMPONENT'
        hashes = set()
        for name, gate in sorted(gates.items()):
            keys = gate.get('evidence_hashes', [])
            if (not isinstance(keys, list) or len(keys) > MAX_HASHES
                    or any(not isinstance(k, str) or re.fullmatch('[0-9a-f]{64}', k) is None for k in keys)):
                raise ValueError('Unbounded or malformed evidence manifest')
            hashes.update(keys)
            result['components'][name] = {
                'status': gate['status'] if bound else 'BLOCKED',
                'scope': 'historical_raw_replay', 'evidence_hashes': sorted(set(keys)),
                'unknown_reasons': [] if bound and gate['status'] == 'VERIFIED_COMPONENT'
                else sorted(set(['COMPONENT_OR_SOURCE_REVISION_UNVERIFIED'] + [
                    r for r in gate.get('reasons', [])
                    if isinstance(r, str) and re.fullmatch('[A-Z0-9_]{1,128}', r)][:32]))}
        hashes.update(result['control_obligations']['evidence_hashes'])
        if len(hashes) > MAX_HASHES:
            raise ValueError('Unbounded evidence manifest')
        result['evidence_hashes'] = sorted(hashes)
        if bound:
            metrics = decision['entry_evidence']['metrics']
            for name, units in (('gross_top10_supply_pct', 'percent_gross_supply'),
                                ('holder_owner_count', 'owners'), ('holder_slot', 'slot')):
                if gates['holder_snapshot']['status'] == 'VERIFIED_COMPONENT' and name in metrics:
                    fields[name] = {'status': 'HISTORICAL_COMPONENT', 'value': metrics[name],
                                    'units': units, 'unknown_reasons': [],
                                    'evidence_hashes': result['components']['holder_snapshot']['evidence_hashes']}
        else:
            result['reasons'].append('SOURCE_REVISION_RAW_REPLAY_UNVERIFIED')
        if result['observed_at'] is None or not 0 <= now - result['observed_at'] <= 10:
            result['reasons'].append('INVESTIGATION_NOT_FRESH_FOR_ENTRY')
    except (ValueError, TypeError, KeyError, sqlite3.Error, OverflowError, RecursionError, zlib.error, UnicodeError, OSError):
        result['components'] = {}
        result['evidence_hashes'] = []
        result['control_obligations'] = unknown_inventory(revision_hash=revision_hash, now=now)
        result['reasons'].append('DIAGNOSTIC_SOURCE_OR_REPLAY_UNAVAILABLE')
    finally:
        guards.close()
    result['reasons'] = sorted(set(result['reasons']))
    result['manifest_hash'] = digest(result)
    return result
