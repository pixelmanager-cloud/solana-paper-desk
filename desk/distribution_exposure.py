"""Offline OWN-3 core. Inputs require trusted OWN-1/2 adapters, not summary flags.

Amounts are raw integers. Interval accounting does not invent FIFO provenance:
for a send of n from balance b with cohort interval [lo, hi], the guaranteed
send is max(0, n-(b-lo)) and the possible send is min(n, hi). Services stop
attribution; their downstream sends are unresolved. No common-control verdict
or entry eligibility is produced. Artifact references are bindings, not proof;
callers must validate raw evidence before constructing these typed inputs.
"""
from dataclasses import dataclass
from enum import Enum


class Kind(Enum):
    PRIVATE = 'private'
    SERVICE = 'service'
    POOL = 'pool'
    UNRESOLVED = 'unresolved'


@dataclass(frozen=True, order=True)
class Position:
    slot: int
    transaction_index: int
    instruction_index: int


@dataclass(frozen=True)
class Balance:
    account: str
    owner: str
    amount: int


@dataclass(frozen=True)
class ValidatedSnapshot:
    mint: str
    start_slot: int
    cutoff_slot: int
    supply: int
    initial: tuple[Balance, ...]
    current: tuple[Balance, ...]
    snapshot_ref: str
    history_ref: str
    validated: bool = False
    history_complete: bool = False


@dataclass(frozen=True)
class Classification:
    account: str
    owner: str
    mint: str
    kind: Kind
    valid_from_slot: int
    valid_through_slot: int
    evidence_refs: tuple[str, ...] = ()
    verified: bool = False


@dataclass(frozen=True)
class Transfer:
    record_id: str
    position: Position
    source: str
    destination: str
    amount: int
    evidence_ref: str


@dataclass(frozen=True)
class Seed:
    account: str
    amount: int


@dataclass(frozen=True)
class Limits:
    max_transfers: int = 10000
    max_accounts: int = 10000
    max_depth: int = 3


@dataclass(frozen=True)
class HolderExposure:
    account: str
    owner: str
    balance: int
    kind: Kind
    lower_bound: int
    possible: int
    unresolved: int


@dataclass(frozen=True)
class Exposure:
    holders: tuple[HolderExposure, ...]
    lower_bound: int
    unresolved: int
    excluded: int
    reasons: tuple[str, ...]
    processed_transfers: int
    evidence_refs: tuple[str, ...]


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError('Exact nonnegative integer required')


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Nonempty identity/evidence reference required')


def trace_exposure(snapshot: ValidatedSnapshot, transfers: tuple[Transfer, ...],
                   seeds: tuple[Seed, ...], classifications: tuple[Classification, ...] = (),
                   limits: Limits = Limits()) -> Exposure:
    """Replay one fixed-supply interval; seeds apply at the initial boundary.

    Positions must come from verified finalized block/instruction ordering.
    Mint/burn, account control changes and cohort buys inside this interval must
    be normalized by a future adapter or rejected. Duplicate/conflicting records,
    balances and order positions are rejected rather than arbitrarily chosen.
    Resource ceilings are hard limits; partial results cannot become lower bounds.
    """
    for value, ceiling in ((limits.max_transfers, 10000), (limits.max_accounts, 10000),
                           (limits.max_depth, 3)):
        _integer(value, 1)
        if value > ceiling:
            raise ValueError('Traversal ceiling exceeded')
    _text(snapshot.mint)
    for ref in (snapshot.snapshot_ref, snapshot.history_ref):
        if not isinstance(ref, str) or (ref and not ref.strip()):
            raise ValueError("Artifact reference must be a string")
    _integer(snapshot.start_slot); _integer(snapshot.cutoff_slot)
    _integer(snapshot.supply, 1)
    if snapshot.cutoff_slot < snapshot.start_slot:
        raise ValueError('Snapshot interval reversed')
    def balances(rows):
        result = {}
        if len(rows) > limits.max_accounts:
            raise ValueError('Account limit exceeded')
        for row in rows:
            _text(row.account); _text(row.owner); _integer(row.amount)
            if row.account in result:
                raise ValueError('Duplicate account')
            result[row.account] = row
        if sum(r.amount for r in rows) != snapshot.supply:
            raise ValueError('Supply mismatch')
        return result
    initial, current = balances(snapshot.initial), balances(snapshot.current)
    reasons = set()
    refs = {r for r in (snapshot.snapshot_ref, snapshot.history_ref) if r}
    trusted = (snapshot.validated is True and snapshot.history_complete is True
               and bool(snapshot.snapshot_ref) and bool(snapshot.history_ref))
    if not trusted:
        reasons.add('SNAPSHOT_OR_HISTORY_UNVERIFIED')
    if not seeds:
        trusted = False
        reasons.add('COHORT_SEEDS_MISSING')
    # Account continuity includes zero/closed endpoints; control changes unsupported.
    if initial.keys() != current.keys() or any(initial[a].owner != current[a].owner for a in initial):
        trusted = False
        reasons.add('ACCOUNT_CONTINUITY_UNRESOLVED')
    if len(classifications) > limits.max_accounts:
        raise ValueError('Classification limit exceeded')
    labels = {}
    for c in classifications:
        if c.account in labels:
            labels[c.account] = None
            reasons.add('CLASSIFICATION_CONFLICT')
        else:
            labels[c.account] = c
    def kind(account):
        c = labels.get(account)
        row = current.get(account)
        if (c is None or row is None or c.verified is not True or not isinstance(c.kind, Kind)
                or c.account != account or c.owner != row.owner or c.mint != snapshot.mint
                or type(c.valid_from_slot) is not int or type(c.valid_through_slot) is not int
                or c.valid_from_slot > snapshot.start_slot or c.valid_through_slot < snapshot.cutoff_slot
                or not c.evidence_refs or any(not isinstance(r, str) or not r.strip() for r in c.evidence_refs)):
            return Kind.UNRESOLVED
        refs.update(c.evidence_refs)
        return c.kind
    kinds = {a: kind(a) for a in current}
    state = {a: [r.amount, 0, 0, 0] for a, r in initial.items()}
    seeded = set()
    for seed in seeds:
        _integer(seed.amount, 1); _text(seed.account)
        if seed.account in seeded or seed.account not in state or seed.amount > state[seed.account][0]:
            raise ValueError('Invalid or duplicate seed')
        seeded.add(seed.account)
        state[seed.account][1:3] = [seed.amount, seed.amount]
    if len(transfers) > limits.max_transfers:
        trusted = False
        reasons.add('TRANSFER_LIMIT')
    records, positions = set(), set()
    processed = 0
    # Bound sorting and replay, never truncate silently.
    for t in sorted(transfers[:limits.max_transfers], key=lambda t: t.position):
        _text(t.record_id); _text(t.evidence_ref); _integer(t.amount, 1)
        p = t.position
        for v in (p.slot, p.transaction_index, p.instruction_index):
            _integer(v)
        if t.record_id in records or p in positions:
            raise ValueError('Duplicate/conflicting transfer or order position')
        records.add(t.record_id); positions.add(p); refs.add(t.evidence_ref)
        if not snapshot.start_slot < p.slot <= snapshot.cutoff_slot:
            raise ValueError('Transfer outside snapshot interval')
        if t.source not in state or t.destination not in state:
            trusted = False; reasons.add('MISSING_PATH_ENDPOINT'); continue
        source, destination = state[t.source], state[t.destination]
        b, lo, hi, depth = source
        if t.amount > b:
            trusted = False; reasons.add('MISSING_BALANCE_PATH'); continue
        processed += 1
        if t.source == t.destination:
            continue
        sent_lo, sent_hi = max(0, t.amount - (b - lo)), min(t.amount, hi)
        next_depth = depth + 1 if sent_hi else 0
        if kinds.get(t.source, Kind.UNRESOLVED) in (Kind.SERVICE, Kind.POOL, Kind.UNRESOLVED):
            sent_lo, sent_hi = 0, t.amount
            reasons.add('SERVICE_OR_UNCLASSIFIED_PATH_UNRESOLVED')
        if next_depth > limits.max_depth:
            sent_lo = 0
            reasons.add('DEPTH_LIMIT')
        source[:3] = [b - t.amount, max(0, lo - t.amount), min(hi, b - t.amount)]
        destination[0] += t.amount
        destination[1] += sent_lo
        destination[2] += sent_hi
        destination[3] = max(destination[3], next_depth)
    if any(a not in state or state[a][0] != r.amount for a, r in current.items()):
        trusted = False; reasons.add('ENDING_BALANCE_MISMATCH')
    holders = []
    excluded = 0
    for a, row in sorted(current.items()):
        k = kinds[a]
        if trusted and k in (Kind.SERVICE, Kind.POOL):
            excluded += row.amount
            lo = hi = unresolved = 0
        elif not trusted or k is Kind.UNRESOLVED:
            lo, hi, unresolved = 0, row.amount, row.amount
            if k is Kind.UNRESOLVED and row.amount:
                reasons.add('CLASSIFICATION_UNRESOLVED')
        else:
            lo, hi = state[a][1:3]
            unresolved = hi - lo
        holders.append(HolderExposure(a, row.owner, row.amount, k, lo, hi, unresolved))
    return Exposure(tuple(holders), sum(h.lower_bound for h in holders),
                    sum(h.unresolved for h in holders), excluded, tuple(sorted(reasons)),
                    processed, tuple(sorted(refs)))
