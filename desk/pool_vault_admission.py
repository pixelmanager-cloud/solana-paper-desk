"""Offline admission for canonical legacy PumpSwap vaults at one finalized slot.

Three distinct checks: independently trusted acquisition receipts, content/request
integrity, then raw protocol identity. JSON and SHA256 are NOT chain authentication.
The policy must come from the coordinator's protected acquisition ledger, never
from a candidate report, normalized label or evidence-store contents. Its issuer
must independently attest the RPC source/cluster and exact capture references.
Include every known source-approved observation of this pool/mint/finalized slot;
never prune contradictory receipts by selected refs, source or capture freshness.
All scoped captures must agree on raw bank state and the slot's block time;
unavailable or malformed competing evidence blocks admission as ambiguous.
Synthetic receipts are explicitly opt-in, visibly marked and cannot authorize a
production exclusion. This module performs no I/O except a supplied offline reader.

Only six-account atomic batches, initialized legacy SPL Token mints/accounts and
known Pool layouts are supported. Token-2022, unknown profiles and authority gaps
remain unresolved. A label is valid only at the exact slot/block-time pair; no
historical interval, liquidity guarantee, common control or entry approval follows.
minContextSlot is a request floor S, not a historical selector. The point slot T
is the response context: require T >= S, retain exact S in request/manifest refs,
and bind block time and all conflict checks to T. See the pinned primary-source
semantics in docs/pool-point-rpc-semantics.md.
"""
from dataclasses import dataclass
from collections.abc import Callable, Mapping

from .model import canonical, digest
from .pools import ATA, parse_pool
from .programs import address
from .providers import PUMP, PUMPSWAP, SOL
from .security import TOKEN_PROGRAM, account_bytes, base58

PROFILE = 'pumpswap-legacy-vault-point-v1'
NETWORK = 'mainnet-beta'
GENESIS = '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d'
MAX_EVIDENCE_BYTES = 64 * 1024


@dataclass(frozen=True)
class EvidenceRefs:
    snapshot: str
    snapshot_request: str
    block_time: str
    block_time_request: str

    def hashes(self):
        return (self.snapshot, self.snapshot_request, self.block_time, self.block_time_request)


@dataclass(frozen=True)
class AcquisitionReceipt:
    """OUT-OF-BAND source attestation; never deserialize from candidate JSON.

    This is RPC-source trust, not a cryptographic proof of Solana bank state.
    captured_at is coordinator wall time; snapshot_time is the slot's estimated
    block time. All four refs are bound independently, including request manifests.
    """
    source_id: str
    source_kind: str  # coordinator_capture or synthetic_fixture
    network: str
    genesis_hash: str
    pool: str
    mint: str
    slot: int
    snapshot_time: int
    captured_at: int
    refs: EvidenceRefs


@dataclass(frozen=True)
class TrustedSourcePolicy:
    policy_id: str
    allowed_source_ids: frozenset[str]
    receipts: frozenset[AcquisitionReceipt]
    profile: str = PROFILE
    max_age_seconds: int = 60
    allow_synthetic_fixtures: bool = False


class _Reject(Exception):
    pass


def _require(condition, reason):
    if not condition:
        raise _Reject(reason)


def _int(value):
    return type(value) is int and 0 <= value < 2**64


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 2048


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _policy(policy):
    _require(type(policy) is TrustedSourcePolicy, 'TRUSTED_SOURCE_POLICY_REQUIRED')
    _require(policy.profile == PROFILE and _text(policy.policy_id)
             and type(policy.allowed_source_ids) is frozenset
             and all(_text(s) for s in policy.allowed_source_ids)
             and type(policy.receipts) is frozenset and len(policy.receipts) <= 256
             and all(type(r) is AcquisitionReceipt for r in policy.receipts)
             and _int(policy.max_age_seconds) and 1 <= policy.max_age_seconds <= 300
             and type(policy.allow_synthetic_fixtures) is bool, 'SOURCE_POLICY_INVALID')


def _raw_account(value, program, length):
    _require(isinstance(value, dict) and value.get('owner') == program
             and value.get('executable') is False and _int(value.get('lamports'))
             and value['lamports'] > 0, 'ACCOUNT_PROGRAM_OR_METADATA_UNSUPPORTED')
    data = value.get('data')
    _require(isinstance(data, list) and len(data) == 2 and isinstance(data[0], str)
             and len(data[0]) <= 2048, 'ACCOUNT_ENCODING_UNSUPPORTED')
    raw = account_bytes(value)
    _require(len(raw) in length, 'ACCOUNT_LAYOUT_UNSUPPORTED')
    if 'space' in value:
        _require(type(value['space']) is int and value['space'] == len(raw), 'ACCOUNT_SPACE_MISMATCH')
    return raw


def _mint(value, *, lp_authority=None, native=False):
    raw = _raw_account(value, TOKEN_PROGRAM, (82,))
    mint_option = int.from_bytes(raw[:4], 'little')
    freeze_option = int.from_bytes(raw[46:50], 'little')
    _require(mint_option in (0, 1) and freeze_option == 0 and raw[45] == 1,
             'MINT_AUTHORITY_OR_STATE_UNSUPPORTED')
    _require(raw[44] <= 9, 'MINT_DECIMALS_UNSUPPORTED')
    if lp_authority is None:
        _require(mint_option == 0, 'ACTIVE_MINT_AUTHORITY')
    else:
        _require(mint_option == 1 and base58(raw[4:36]) == lp_authority,
                 'LP_MINT_AUTHORITY_MISMATCH')
    if native:
        _require(raw[44] == 9, 'NATIVE_MINT_DECIMALS_MISMATCH')
    return int.from_bytes(raw[36:44], 'little')


def _vault(value, mint, authority, *, native):
    raw = _raw_account(value, TOKEN_PROGRAM, (165,))
    _require(base58(raw[:32]) == mint and base58(raw[32:64]) == authority,
             'VAULT_MINT_OR_AUTHORITY_MISMATCH')
    _require(raw[108] == 1, 'VAULT_FROZEN_OR_UNINITIALIZED')
    _require(int.from_bytes(raw[72:76], 'little') == 0
             and int.from_bytes(raw[121:129], 'little') == 0, 'VAULT_DELEGATE_UNSUPPORTED')
    close_option = int.from_bytes(raw[129:133], 'little')
    _require(close_option in (0, 1) and (close_option == 0 or base58(raw[133:165]) == authority),
             'VAULT_CLOSE_AUTHORITY_UNSUPPORTED')
    native_option = int.from_bytes(raw[109:113], 'little')
    amount = int.from_bytes(raw[64:72], 'little')
    reserve = int.from_bytes(raw[113:121], 'little')
    if native:
        _require(native_option == 1 and reserve > 0 and value['lamports'] == reserve + amount,
                 'NATIVE_VAULT_RESERVE_UNSUPPORTED')
    else:
        _require(native_option == 0, 'BASE_VAULT_NATIVE_UNSUPPORTED')
    return amount


def _load_capture(refs, load, cache):
    _require(type(refs) is EvidenceRefs and all(_hash(h) for h in refs.hashes()),
             'EVIDENCE_REFERENCES_INVALID')
    evidence = []
    for key in refs.hashes():
        if key not in cache:
            try:
                payload = load(key)
                _require(isinstance(payload, dict) and len(canonical(payload).encode()) <= MAX_EVIDENCE_BYTES,
                         'EVIDENCE_SHAPE_OR_SIZE_UNSUPPORTED')
                _require(digest(payload) == key, 'EVIDENCE_CONTENT_HASH_MISMATCH')
                cache[key] = payload
            except _Reject:
                raise
            except Exception:
                raise _Reject('EVIDENCE_UNREADABLE') from None
        evidence.append(cache[key])
    return evidence


def _bound_capture(evidence, refs, keys, slot, at):
    snapshot, request, clock, clock_request = evidence
    raw_params = snapshot.get('params')
    _require(isinstance(raw_params, list) and len(raw_params) == 2
             and isinstance(raw_params[1], dict), 'RPC_REQUEST_OR_RESPONSE_MISMATCH')
    floor = raw_params[1].get('minContextSlot')
    _require(_int(floor), 'RPC_SLOT_TYPE_INVALID')
    # Replay the ORIGINAL finalized request, not one rewritten to the returned
    # bank. Each competing capture may have a different floor for the same T.
    params = [keys, {'encoding': 'base64', 'commitment': 'finalized', 'minContextSlot': floor}]
    for envelope, manifest, method, expected_params, response_hash in (
            (snapshot, request, 'getMultipleAccounts', params, refs.snapshot),
            (clock, clock_request, 'getBlockTime', [slot], refs.block_time)):
        _require(envelope.get('kind') == 'rpc_response_v1' and envelope.get('method') == method
                 and envelope.get('params') == expected_params and 'result' in envelope
                 and 'error' not in envelope, 'RPC_REQUEST_OR_RESPONSE_MISMATCH')
        _require(manifest == {'kind': 'pool_vault_request_v1', 'network': NETWORK,
                 'genesis_hash': GENESIS, 'method': method, 'params': expected_params,
                 'response_hash': response_hash}, 'RPC_MANIFEST_BINDING_MISMATCH')
    # Python equality treats bool == int; validate nested RPC slot types too.
    _require(type(snapshot['params'][1]['minContextSlot']) is int
             and type(clock['params'][0]) is int
             and type(request['params'][1]['minContextSlot']) is int
             and type(clock_request['params'][0]) is int, 'RPC_SLOT_TYPE_INVALID')
    response = snapshot['result']
    _require(isinstance(response, dict) and isinstance(response.get('context'), dict)
             and type(response['context'].get('slot')) is int
             and response['context']['slot'] == slot, 'ATOMIC_SNAPSHOT_SLOT_MISMATCH')
    _require(slot >= floor, 'ATOMIC_SNAPSHOT_BELOW_REQUEST_FLOOR')
    _require(type(clock['result']) is int and clock['result'] == at,
             'SNAPSHOT_BLOCK_TIME_MISMATCH')
    values = response.get('value')
    _require(isinstance(values, list) and len(values) == 6, 'ATOMIC_ACCOUNT_SET_INCOMPLETE')
    return values


def _capture_state(values):
    """Compare raw bank state, not transport metadata or normalized assertions.

    Requests/account order/slot/block time are checked separately. API versions,
    envelope annotations, omitted space and receipt source/capture wall time do
    not change identity. Raw bytes, owner, executable and lamports do. Unknown
    or malformed competing state is ambiguous, never an ignorable observation.
    """
    state = []
    for index, value in enumerate(values):
        program = PUMPSWAP if index == 0 else TOKEN_PROGRAM
        lengths = (243, 287, 300, 301) if index == 0 else ((82,) if index < 4 else (165,))
        raw = _raw_account(value, program, lengths)
        state.append((value['owner'], value['executable'], value['lamports'], raw))
    return tuple(state)


def admit_pool_vault(*, account: str, pool: str, mint: str, snapshot_slot: int,
                     snapshot_time: int, now: int, refs: EvidenceRefs,
                     policy: TrustedSourcePolicy | None,
                     load: Callable[[str], Mapping]) -> dict:
    """Reconstruct one exact base/WSOL vault; untrusted inputs return UNKNOWN.

    load must be an offline content-addressed reader, such as EvidenceStore.load.
    Callers must protect policy construction and independently supply snapshot
    slot/time and current wall time. Merely passing a policy made from candidate
    hashes is a trust-boundary violation, not an authenticated chain observation.
    No generic interval-classification record is emitted for the exposure adapter.
    """
    result = {'profile': PROFILE, 'account': account, 'pool': pool, 'mint': mint,
              'label': 'UNKNOWN', 'snapshot_slot': snapshot_slot, 'snapshot_time': snapshot_time,
              'request_min_context_slot': None,
              'acquisition_provenance_trusted': False, 'content_integrity_verified': False,
              'request_binding_verified': False, 'protocol_identity_verified': False,
              'chain_authenticated': False, 'snapshot_label_admitted': False,
              'production_snapshot_exclusion_allowed': False,
              'historical_interval_exclusion_allowed': False, 'continuity_verified': False,
              'private_control_proven': False, 'eligible_for_trading': False,
              'ownership_approval': False, 'reasons': [], 'evidence_hashes': [],
              'notice': 'Point-in-time protocol label under explicit RPC-source trust; no chain authentication, historical continuity or entry approval.'}
    try:
        _policy(policy)
        for key in (account, pool, mint):
            address(key)
        _require(all(_int(v) for v in (snapshot_slot, snapshot_time, now))
                 and snapshot_time < 2**63 and now < 2**63, 'QUERY_TIME_OR_SLOT_INVALID')
        _require(type(refs) is EvidenceRefs and all(_hash(h) for h in refs.hashes()),
                 'EVIDENCE_REFERENCES_INVALID')
        result['evidence_hashes'] = list(refs.hashes())
        # Ref selection establishes endorsement only, never permission to ignore
        # other independently trusted observations of the same immutable bank.
        receipts = [r for r in policy.receipts if r.refs == refs]
        _require(receipts, 'ACQUISITION_RECEIPT_MISSING_OR_CONFLICTING')
        for receipt in receipts:
            _require(receipt.source_id in policy.allowed_source_ids
                     and receipt.source_kind in ('coordinator_capture', 'synthetic_fixture')
                     and receipt.network == NETWORK and receipt.genesis_hash == GENESIS,
                     'ACQUISITION_SOURCE_UNTRUSTED')
            _require(receipt.source_kind != 'synthetic_fixture' or policy.allow_synthetic_fixtures,
                     'SYNTHETIC_SOURCE_NOT_ALLOWED')
            _require(receipt.pool == pool and receipt.mint == mint
                     and _int(receipt.slot) and receipt.slot == snapshot_slot
                     and _int(receipt.snapshot_time) and receipt.snapshot_time == snapshot_time,
                     'ACQUISITION_SCOPE_MISMATCH')
            _require(_int(receipt.captured_at) and snapshot_time <= receipt.captured_at
                     and receipt.captured_at - snapshot_time <= 300
                     and receipt.captured_at <= now < receipt.captured_at + policy.max_age_seconds,
                     'ACQUISITION_TIME_STALE_OR_FUTURE')
        # Equivalent endorsements are harmless; pick deterministically, prefer
        # an explicitly approved coordinator source, and retain earliest expiry.
        receipt = min(receipts, key=lambda r: (r.source_kind != 'coordinator_capture',
                                              r.captured_at, r.source_id))
        scoped = [r for r in policy.receipts
                  if r.source_id in policy.allowed_source_ids
                  and (r.source_kind == 'coordinator_capture'
                       or (r.source_kind == 'synthetic_fixture' and policy.allow_synthetic_fixtures))
                  and r.network == NETWORK and r.genesis_hash == GENESIS
                  and r.pool == pool and r.mint == mint and _int(r.slot) and r.slot == snapshot_slot]
        # Block time is deliberately NOT a grouping key: another trusted time
        # for this same finalized slot is a contradiction, not another scope.
        _require(all(_int(r.snapshot_time) and r.snapshot_time == snapshot_time for r in scoped),
                 'ACQUISITION_CAPTURE_CONFLICT')
        _require(all(_int(r.captured_at) and snapshot_time <= r.captured_at <= min(now, snapshot_time + 300)
                     for r in scoped), 'ACQUISITION_CAPTURE_AMBIGUOUS')
        result.update(acquisition_provenance_trusted=True, source_id=receipt.source_id,
                      source_ids=sorted({r.source_id for r in receipts}),
                      source_kind=receipt.source_kind, policy_id=policy.policy_id,
                      captured_at=receipt.captured_at,
                      expires_at=min(r.captured_at + policy.max_age_seconds for r in receipts))
        cache = {}
        evidence = _load_capture(refs, load, cache)
        result['content_integrity_verified'] = True
        from solders.pubkey import Pubkey
        pk = Pubkey.from_string
        creator = Pubkey.find_program_address([b'pool-authority', bytes(pk(mint))], pk(PUMP))[0]
        expected, bump = Pubkey.find_program_address([b'pool', bytes(2), bytes(creator),
                        bytes(pk(mint)), bytes(pk(SOL))], pk(PUMPSWAP))
        _require(str(expected) == pool and mint != SOL, 'CANONICAL_POOL_PDA_MISMATCH')
        lp = str(Pubkey.find_program_address([b'pool_lp_mint', bytes(expected)], pk(PUMPSWAP))[0])
        vaults = [str(Pubkey.find_program_address([bytes(expected), bytes(pk(TOKEN_PROGRAM)),
                  bytes(pk(asset))], pk(ATA))[0]) for asset in (mint, SOL)]
        keys = [pool, mint, SOL, lp, *vaults]
        _require(len(set(keys)) == 6 and account in vaults, 'VAULT_ACCOUNT_ATA_MISMATCH')
        values = _bound_capture(evidence, refs, keys, snapshot_slot, snapshot_time)
        result.update(request_binding_verified=True,
                      request_min_context_slot=evidence[0]['params'][1]['minContextSlot'])
        selected_state = _capture_state(values)
        checked_refs = {refs}
        # At most 256 receipts x four content-addressed reads (cached), all
        # offline and <=64 KiB each. Contradictions never expire out of this
        # immutable slot merely because the other capture's freshness expired.
        for other in scoped:
            if other.refs in checked_refs:
                continue
            try:
                other_evidence = _load_capture(other.refs, load, cache)
                other_values = _bound_capture(other_evidence, other.refs, keys, snapshot_slot, snapshot_time)
                other_state = _capture_state(other_values)
            except (_Reject, ValueError, KeyError, TypeError, IndexError, OverflowError):
                raise _Reject('ACQUISITION_CAPTURE_AMBIGUOUS') from None
            _require(other_state == selected_state, 'ACQUISITION_CAPTURE_CONFLICT')
            checked_refs.add(other.refs)
        result.update(supporting_capture_count=len(checked_refs),
                      conflict_check_evidence_hashes=sorted(cache))
        pool_raw = _raw_account(values[0], PUMPSWAP, (243, 287, 300, 301))
        fields = parse_pool(values[0])
        _require(fields['unknown_trailing_bytes'] == 0 and fields['pool_bump'] == bump
                 and fields['index'] == 0 and fields['creator'] == str(creator)
                 and fields['base_mint'] == mint and fields['quote_mint'] == SOL
                 and fields['lp_mint'] == lp and fields['pool_base_token_account'] == vaults[0]
                 and fields['pool_quote_token_account'] == vaults[1], 'POOL_RAW_IDENTITY_MISMATCH')
        # Identity admission is intentionally narrower than the generic parser.
        _require(not fields['is_mayhem_mode'] and not fields['is_cashback_coin']
                 and not fields['is_holder_reward'] and not fields['can_edit_creator_fee'],
                 'POOL_PROFILE_UNSUPPORTED')
        supply = _mint(values[1])
        _require(supply > 0, 'BASE_MINT_SUPPLY_INVALID')
        _mint(values[2], native=True)
        _mint(values[3], lp_authority=pool)
        amounts = [_vault(values[4], mint, pool, native=False),
                   _vault(values[5], SOL, pool, native=True)]
        _require(amounts[0] <= supply, 'VAULT_AMOUNT_EXCEEDS_BASE_SUPPLY')
        result.update(label='POOL_VAULT', snapshot_label_admitted=True,
                      protocol_identity_verified=True, token_program=TOKEN_PROGRAM,
                      token_authority=pool, vault_mint=mint if account == vaults[0] else SOL,
                      amount_raw=str(amounts[vaults.index(account)]), pool_layout_bytes=len(pool_raw),
                      production_snapshot_exclusion_allowed=receipt.source_kind == 'coordinator_capture')
    except _Reject as exc:
        result['reasons'] = [str(exc)]
    except (ValueError, KeyError, TypeError, IndexError, OverflowError, ImportError):
        result['reasons'] = ['RAW_EVIDENCE_OR_QUERY_MALFORMED']
    return result
