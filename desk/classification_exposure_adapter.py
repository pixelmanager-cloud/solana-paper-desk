"""Offline classification-to-exposure contract, intentionally reject-only.

PR #19 labels require independent policy admission; neither a self-hash nor raw
RPC-shaped bytes authenticate a source. No such admission implementation exists
here. Candidate flags/hashes are never used to create a trusted policy. This
adapter reconstructs identities and checks candidate scope/validity, but emits
only UNRESOLVED, verified=False inputs for PR #12's exposure core. It does not
construct validated snapshots, seeds or histories, or wire any entry path.
"""
from dataclasses import dataclass
from copy import deepcopy

from .account_classification import _validate, _hash, _integer
from .model import digest
from .programs import address
from .security import TOKEN_PROGRAM, account_bytes, base58, holding_policy


@dataclass(frozen=True)
class ReplayScope:
    # Genesis hash is an exact chain identity, not a mutable cluster nickname.
    genesis_hash: str
    mint: str
    purpose: str
    start_slot: int
    cutoff_slot: int
    start_time: int
    end_time: int
    evaluated_at: int

    def validate(self):
        address(self.genesis_hash)
        address(self.mint)
        if self.purpose != 'holder_exclusion':
            raise ValueError('Exposure adapter requires holder_exclusion purpose')
        values = (self.start_slot, self.cutoff_slot, self.start_time,
                  self.end_time, self.evaluated_at)
        if (not all(_integer(v) for v in values)
                or self.start_slot > self.cutoff_slot
                or not self.start_time <= self.end_time <= self.evaluated_at):
            raise ValueError('Ordered replay slot/time interval and actual evaluation time required')


@dataclass(frozen=True)
class RawBinding:
    account: str
    chain_program: str
    token_authority: str
    mint: str
    amount: int
    slot: int


def _binding(envelope, account, scope, expected_slot):
    """Parse a request-bound account response; binding is not authentication."""
    if not isinstance(envelope, dict):
        raise ValueError('Raw envelope missing')
    if (envelope.get('genesis_hash') != scope.genesis_hash
            or envelope.get('method') != 'getAccountInfo'
            or envelope.get('params') != [account, {'encoding': 'base64', 'commitment': 'finalized'}]):
        raise ValueError('Raw chain/request binding mismatch')
    result = envelope['result']
    slot = result['context']['slot']
    if type(slot) is not int or slot != expected_slot:
        raise ValueError('Exact boundary slot required')
    value = result['value']
    if not isinstance(value, dict) or value.get('owner') != TOKEN_PROGRAM:
        raise ValueError('Only legacy token accounts supported')
    data = account_bytes(value)
    if len(data) != 165:
        raise ValueError('Exact legacy token layout required')
    mint, authority = base58(data[:32]), base58(data[32:64])
    address(authority)
    policy = holding_policy(value, scope.mint, authority)
    if policy['reasons'] or mint != scope.mint:
        raise ValueError('Raw token identity/control unresolved')
    if any(data[76:108]) or any(data[121:129]):
        raise ValueError('Delegate bytes or delegated amount unsupported')
    if int.from_bytes(data[109:113], 'little') != 0:
        raise ValueError('Native token account unsupported')
    return RawBinding(account, value['owner'], authority, mint,
                      int.from_bytes(data[64:72], 'little'), slot)


@dataclass(frozen=True)
class ExposureClassificationInput:
    """Core-compatible unresolved input plus separate provenance/binding context."""
    account: str
    owner: str | None  # Core owner is token authority, never chain_program.
    mint: str
    chain_program: str | None
    genesis_hash: str
    purpose: str
    evaluated_at: int
    valid_from_slot: int
    valid_through_slot: int
    evidence_refs: tuple[str, ...]
    reasons: tuple[str, ...]
    candidate_label: str

    @property
    def kind(self):
        return 'unresolved'

    @property
    def ownership_approval(self):
        return False

    @property
    def verified(self):
        return False

    @property
    def private_control_proven(self):
        return False

    def to_core(self):
        """Optional PR #12 dependency; never translates UNKNOWN into PRIVATE."""
        from .distribution_exposure import Classification, Kind
        return Classification(self.account, self.owner, self.mint, Kind.UNRESOLVED,
                              self.valid_from_slot, self.valid_through_slot,
                              self.evidence_refs, verified=False)


def adapt_classification(account, *, scope: ReplayScope, initial_raw, current_raw,
                         candidate=None, candidate_hash=None):
    """Check untrusted raw boundaries and candidate; source admission stays absent.

    Both raw responses are exact request/chain/slot-bound. They do not establish
    account lifetime continuity or history completeness. Any later positive
    adapter needs authenticated admission plus complete interval control replay.
    No trusted_hashes or caller 'validated'/'verified' booleans are accepted.
    """
    scope.validate()
    address(account)
    reasons = {'INDEPENDENT_SOURCE_ADMISSION_UNAVAILABLE',
               'ACCOUNT_INTERVAL_CONTINUITY_UNVERIFIED'}
    owner = None  # Missing bytes never fabricate a token authority label.
    program = None
    refs = []
    first, through = scope.start_slot, scope.cutoff_slot
    label = 'UNKNOWN'
    binding = None
    try:
        initial = _binding(initial_raw, account, scope, scope.start_slot)
        binding = _binding(current_raw, account, scope, scope.cutoff_slot)
        owner, program = binding.token_authority, binding.chain_program
        if (initial.token_authority, initial.chain_program, initial.mint) != (
                binding.token_authority, binding.chain_program, binding.mint):
            reasons.add('RAW_BOUNDARY_IDENTITY_CONFLICT')
        refs.extend((digest(initial_raw), digest(current_raw)))
    except (ValueError, KeyError, TypeError, AttributeError):
        reasons.add('RAW_ACCOUNT_BINDING_UNVERIFIED')
    # Copy before hashing/inspection; candidate dictionaries never become policy.
    try:
        record = deepcopy(candidate)
    except RecursionError:
        record = None
        reasons.add('CLASSIFICATION_CANDIDATE_MALFORMED')
    if not isinstance(record, dict) or not _hash(candidate_hash):
        reasons.add('CLASSIFICATION_CANDIDATE_MISSING_OR_MALFORMED')
    else:
        try:
            if digest(record) != candidate_hash:
                reasons.add('CLASSIFICATION_CANDIDATE_HASH_MISMATCH')
            else:
                refs.append(candidate_hash)
            if record.get('genesis_hash') != scope.genesis_hash:
                reasons.add('CLASSIFICATION_CHAIN_SCOPE_MISMATCH')
            if binding is None:
                reasons.add('CLASSIFICATION_IDENTITY_UNVERIFIED')
            else:
                # Check earliest and latest replay points, plus actual now.
                for at, slot in ((scope.start_time, scope.start_slot),
                                 (scope.end_time, scope.cutoff_slot),
                                 (scope.evaluated_at, scope.cutoff_slot)):
                    reason = _validate(record, account, program, scope.mint,
                                       scope.purpose, at, slot)
                    if reason:
                        reasons.add(reason)
                if record.get('token_authority') != owner:
                    reasons.add('CLASSIFICATION_TOKEN_AUTHORITY_MISMATCH')
            start, expiry = record.get('observed_slot'), record.get('expires_slot')
            if _integer(start) and _integer(expiry) and expiry > start:
                # OWN-2 expiry is exclusive; OWN-3 through is inclusive.
                first, through = start, expiry - 1
            if isinstance(record.get('label'), str):
                label = record['label']
        except (ValueError, TypeError):
            reasons.add('CLASSIFICATION_CANDIDATE_MALFORMED')
    return ExposureClassificationInput(account, owner, scope.mint, program,
        scope.genesis_hash, scope.purpose, scope.evaluated_at, first, through,
        tuple(sorted(set(refs))), tuple(sorted(reasons)), label)
