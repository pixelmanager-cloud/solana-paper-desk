"""Offline, scoped account labels; never proof of private/common control.

The coordinator must independently verify provenance and pin evidence hashes in
an immutable policy before calling this API. A hash proves integrity, not truth.
Do not populate trusted_hashes from an investigation's asserted labels. load is
an offline content-addressed reader (for example EvidenceStore.load), never RPC.
No live service registry or private-controller inference is implemented here.
"""
from collections.abc import Callable, Mapping
from copy import deepcopy

from .model import digest
from .programs import address

LABELS = frozenset({'POOL', 'POOL_VAULT', 'SERVICE'})
PURPOSES = frozenset({'holder_exclusion', 'funding_source', 'distribution_endpoint'})


def _integer(value):
    return type(value) is int and value >= 0


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 2048


def _hash(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in '0123456789abcdef' for c in value))


def _validate(record, account, program, mint, purpose, now, slot):
    if not isinstance(record, dict) or record.get('version') != 1 or type(record.get('version')) is not int:
        return 'MALFORMED_CLASSIFICATION_EVIDENCE'
    if record.get('label') not in LABELS:
        return 'UNKNOWN_OR_UNSUPPORTED_LABEL'
    if record.get('account') != account or record.get('program') != program:
        return 'ACCOUNT_PROGRAM_BINDING_MISMATCH'
    scope = record.get('scope')
    if not isinstance(scope, dict) or scope != {'mint': mint, 'purpose': purpose}:
        return 'CLASSIFICATION_SCOPE_MISMATCH'
    provenance = record.get('provenance')
    if (not isinstance(provenance, dict)
            or not all(_text(provenance.get(k)) for k in ('source', 'method', 'verifier'))
            or not _hash(provenance.get('source_hash'))):
        return 'CLASSIFICATION_PROVENANCE_MISSING'
    bounds = [record.get(k) for k in ('observed_at', 'expires_at', 'observed_slot', 'expires_slot')]
    if not all(_integer(v) for v in bounds):
        return 'CLASSIFICATION_VALIDITY_MALFORMED'
    start, end, first, last = bounds
    if end <= start or last <= first:
        return 'CLASSIFICATION_VALIDITY_MALFORMED'
    # Both dimensions are mandatory, with exclusive expiration boundaries.
    if not start <= now < end or not first <= slot < last:
        return 'CLASSIFICATION_STALE_OR_FUTURE'
    return None


def classify_account(account: str, program: str, *, mint: str, purpose: str,
                     now: int, slot: int, evidence_hashes: list[str],
                     trusted_hashes: frozenset[str], load: Callable[[str], Mapping]) -> dict:
    """Resolve one exact account at a point in time from independently pinned evidence.

    `program` is the observed chain account owner, not the token authority wallet.
    Scope is exact: no wildcard mints, program-wide labels or funding propagation.
    Every supplied candidate must validate; even a stale contradictory candidate
    blocks resolution. Duplicate hashes are harmless. Empty evidence is unknown.
    Untrusted input/loader failures return unresolved; invalid query/policy raises.
    `exclusion_allowed` applies only to this query's scope and point in time. It
    is never token eligibility, history completeness or ownership approval.
    """
    for key in (account, program, mint):
        address(key)
    if purpose not in PURPOSES or not _integer(now) or not _integer(slot):
        raise ValueError('Exact supported scope, time and slot required')
    if not isinstance(trusted_hashes, frozenset) or not all(_hash(h) for h in trusted_hashes):
        raise ValueError('Explicit immutable trusted evidence hash policy required')
    result = {'account': account, 'program': program, 'scope': {'mint': mint, 'purpose': purpose},
              'evaluated_at': now, 'evaluated_slot': slot, 'label': 'UNKNOWN',
              'classification_resolved': False, 'exclusion_allowed': False,
              'private_control_proven': False, 'ownership_approval': False,
              'evidence': [], 'reasons': []}
    if not isinstance(evidence_hashes, list) or not evidence_hashes:
        result['reasons'] = ['CLASSIFICATION_EVIDENCE_MISSING']
        return result
    reasons = set()
    seen = set()
    for key in evidence_hashes:
        if not _hash(key):
            reasons.add('CLASSIFICATION_HASH_MALFORMED')
            continue
        if key in seen:
            continue
        seen.add(key)
        if key not in trusted_hashes:
            reasons.add('CLASSIFICATION_EVIDENCE_UNTRUSTED')
            continue
        try:
            record = load(key)
            if digest(record) != key:
                reasons.add('CLASSIFICATION_HASH_MISMATCH')
                continue
            reason = _validate(record, account, program, mint, purpose, now, slot)
        except Exception:
            # Reader/encoding failures cannot become a positive label.
            reasons.add('CLASSIFICATION_EVIDENCE_UNREADABLE')
            continue
        result['evidence'].append({'hash': key, 'record': deepcopy(record)})
        if reason:
            reasons.add(reason)
    labels = {e['record'].get('label') for e in result['evidence']
              if isinstance(e['record'].get('label'), str)}
    if len(labels) > 1:
        reasons.add('CLASSIFICATION_CONFLICT')
    if not reasons and len(labels) == 1:
        result['label'] = labels.pop()
        result['classification_resolved'] = True
        result['exclusion_allowed'] = purpose in {'holder_exclusion', 'distribution_endpoint'}
        # Earliest expiry wins when several independent attestations agree.
        result['expires_at'] = min(e['record']['expires_at'] for e in result['evidence'])
        result['expires_slot'] = min(e['record']['expires_slot'] for e in result['evidence'])
    result['reasons'] = sorted(reasons)
    return result
