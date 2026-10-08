"""Protected local coordinator receipts; no provider/capture/entry wiring.

Only trusted coordinator code may construct CoordinatorBoundary and open_writer.
Never populate either from candidate JSON or an evidence-store payload. POSIX UID
and private directory/file permissions define the local authority boundary;
Python types, source IDs and hashes do not authenticate a coordinator or Solana.
A same-UID/privileged writer can rewrite the whole ledger and is outside this
contract. Hash chaining detects accidental/partial tampering, not hostile full
rewrites, ledger rollback to an earlier full copy or omission before publication.

Linux single-host, stable canonical local filesystem paths only. No symlinks,
hard links, file/lock/root replacement or operator rotation while in use. No NFS,
FUSE, tmpfs, in-memory SQLite or URI identities. Approved source registry and store
identity are immutable. A future capture bridge must independently approve and
publish EVERY observation; candidate data cannot call this issuance path.

Append-only, DELETE-journal/FULL SQLite transactions publish receipt and journal
head atomically. Readers reconstruct ALL approved observations for pool/mint/slot,
never filter by refs, block time, freshness or a caller-selected subset of sources.
Malformed raw evidence, unavailable refs and stale/contradictory observations
remain visible to admission. Corrupt receipt metadata/journal abort reconstruction;
there is no salvage, silent row omission or empty-policy fallback.
"""
from contextlib import closing, contextmanager
from collections.abc import Callable
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from .evidence import EvidenceStore
from .model import canonical, digest
from .pool_vault_admission import AcquisitionReceipt, EvidenceRefs, GENESIS, NETWORK, PROFILE, TrustedSourcePolicy
from .programs import address

MAX_RECORDS = 10000
MAX_PAYLOAD_BYTES = 8192
SCHEMA = {
    'ledger_descriptor': 'CREATE TABLE ledger_descriptor(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL,hash TEXT NOT NULL)',
    'ledger_head': 'CREATE TABLE ledger_head(id INTEGER PRIMARY KEY CHECK(id=1),count INTEGER NOT NULL,hash TEXT NOT NULL)',
    'coordinator_receipts': 'CREATE TABLE coordinator_receipts(seq INTEGER PRIMARY KEY,publication_id TEXT UNIQUE NOT NULL,pool TEXT NOT NULL,mint TEXT NOT NULL,slot TEXT NOT NULL,source_id TEXT NOT NULL,payload TEXT NOT NULL,payload_hash TEXT UNIQUE NOT NULL,previous_hash TEXT NOT NULL,row_hash TEXT NOT NULL)',
    'receipt_no_update': "CREATE TRIGGER receipt_no_update BEFORE UPDATE ON coordinator_receipts BEGIN SELECT RAISE(ABORT,'Append-only coordinator receipt'); END",
    'receipt_no_delete': "CREATE TRIGGER receipt_no_delete BEFORE DELETE ON coordinator_receipts BEGIN SELECT RAISE(ABORT,'Append-only coordinator receipt'); END",
    'receipt_no_replace': "CREATE TRIGGER receipt_no_replace BEFORE INSERT ON coordinator_receipts WHEN EXISTS(SELECT 1 FROM coordinator_receipts WHERE seq=NEW.seq OR publication_id=NEW.publication_id OR payload_hash=NEW.payload_hash) BEGIN SELECT RAISE(ABORT,'Receipt identity already exists'); END",
    'descriptor_no_update': "CREATE TRIGGER descriptor_no_update BEFORE UPDATE ON ledger_descriptor BEGIN SELECT RAISE(ABORT,'Immutable coordinator boundary'); END",
    'descriptor_no_delete': "CREATE TRIGGER descriptor_no_delete BEFORE DELETE ON ledger_descriptor BEGIN SELECT RAISE(ABORT,'Immutable coordinator boundary'); END",
    'descriptor_no_replace': "CREATE TRIGGER descriptor_no_replace BEFORE INSERT ON ledger_descriptor WHEN EXISTS(SELECT 1 FROM ledger_descriptor) BEGIN SELECT RAISE(ABORT,'Immutable coordinator boundary'); END",
    'head_no_delete': "CREATE TRIGGER head_no_delete BEFORE DELETE ON ledger_head BEGIN SELECT RAISE(ABORT,'Preserve coordinator head'); END",
    'head_no_replace': "CREATE TRIGGER head_no_replace BEFORE INSERT ON ledger_head WHEN EXISTS(SELECT 1 FROM ledger_head) BEGIN SELECT RAISE(ABORT,'Preserve coordinator head'); END",
}


class LedgerError(ValueError):
    """Fail closed; caller must withhold policy/admission on any ledger error."""


@dataclass(frozen=True)
class ApprovedSource:
    source_id: str
    source_kind: str
    network: str = NETWORK
    genesis_hash: str = GENESIS
    profile: str = PROFILE


@dataclass(frozen=True)
class CoordinatorBoundary:
    ledger_id: str
    root: Path
    ledger_path: Path
    evidence_path: Path
    sources: frozenset[ApprovedSource]
    allow_synthetic_fixtures: bool = False
    max_age_seconds: int = 60


@dataclass(frozen=True)
class PolicyView:
    """Context-bound policy/reader pair; future bridge must not cache its policy."""
    policy: TrustedSourcePolicy
    load_evidence: Callable[[str], dict]


def _need(condition, message):
    if not condition:
        raise LedgerError(message)


def _identity(value):
    return (isinstance(value, str) and 1 <= len(value) <= 128
            and all(c.isascii() and (c.isalnum() or c in '_.:-') for c in value))


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _canonical_path(value):
    path = Path(value)
    _need(path.is_absolute() and str(path) == str(path.resolve()), 'Canonical absolute path required; aliases unsupported')
    return path


def _local_fs(path):
    # Positive filesystem allowlist; an unknown platform/mount is unsupported.
    try:
        selected = None
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            fields = line.split(); marker = fields.index('-')
            mount = Path(fields[4].replace('\\040', ' ').replace('\\134', '\\'))
            if path == mount or mount in path.parents:
                if selected is None or len(str(mount)) >= selected[0]:
                    selected = (len(str(mount)), fields[marker+1])
        _need(selected is not None and selected[1] in {'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'zfs', 'overlay'},
              'Stable local Linux filesystem required')
    except (OSError, ValueError):
        raise LedgerError('Stable local Linux filesystem required') from None


def _file(path, *, private):
    _canonical_path(path)
    info = path.lstat()
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid(),
          'Owned regular single-link file required')
    mode = stat.S_IMODE(info.st_mode)
    _need(mode == 0o600 if private else not mode & 0o022, 'Unsafe file permissions')
    return [info.st_dev, info.st_ino]


def _lock_identity(path):
    # Lock contents/mtime never change in normal operation. Include mtime so
    # rapid inode recycling cannot make a deleted/recreated empty lock identical.
    return _file(path, private=True) + [path.lstat().st_mtime_ns]


def _boundary(config):
    _need(type(config) is CoordinatorBoundary and _identity(config.ledger_id), 'Trusted local coordinator boundary required')
    root, ledger, evidence = map(_canonical_path, (config.root, config.ledger_path, config.evidence_path))
    _need(ledger.parent == root and ledger != evidence and ledger.name not in ('', '.', '..'), 'Ledger/store identity overlap')
    info = root.lstat()
    _need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
          'Coordinator directory must be owned and private (0700)')
    _local_fs(root); _local_fs(evidence)
    _need(type(config.sources) is frozenset and 1 <= len(config.sources) <= 256
          and all(type(s) is ApprovedSource for s in config.sources), 'Immutable approved source registry required')
    _need(len({s.source_id for s in config.sources}) == len(config.sources), 'Source identity alias/conflict')
    _need(type(config.allow_synthetic_fixtures) is bool and type(config.max_age_seconds) is int
          and 1 <= config.max_age_seconds <= 300, 'Invalid policy options')
    for source in config.sources:
        _need(_identity(source.source_id) and source.profile == PROFILE and source.network == NETWORK
              and source.genesis_hash == GENESIS and source.source_kind in ('coordinator_capture', 'synthetic_fixture'),
              'Unsupported approved source identity')
        _need(source.source_kind != 'synthetic_fixture' or config.allow_synthetic_fixtures,
              'Synthetic source requires explicit opt-in')
    _file(evidence, private=False)
    with closing(sqlite3.connect(evidence.as_uri() + '?mode=ro', uri=True, timeout=5)) as c:
        _need([row[1] for row in c.execute('PRAGMA table_info(pages)')] == ['hash', 'payload', 'raw_bytes'],
              'Bound evidence database schema required')
    return root, ledger, evidence, [info.st_dev, info.st_ino]


def _receipt(receipt, config):
    _need(type(receipt) is AcquisitionReceipt and type(receipt.refs) is EvidenceRefs,
          'Typed independently approved receipt required; no candidate JSON import')
    source = next((s for s in config.sources if s.source_id == receipt.source_id), None)
    _need(source is not None and (receipt.source_kind, receipt.network, receipt.genesis_hash)
          == (source.source_kind, source.network, source.genesis_hash), 'Receipt source identity mismatch')
    for key in (receipt.pool, receipt.mint):
        address(key)
    _need(type(receipt.slot) is int and 0 <= receipt.slot < 2**64, 'Exact finalized slot required')
    # Preserve semantically malformed times for admission to reject; do not
    # silently discard an approved observation just because its time is invalid.
    _need(all(type(v) is int and -2**63 <= v < 2**63 for v in (receipt.snapshot_time, receipt.captured_at)),
          'Exact signed integer receipt times required')
    _need(all(_hash(h) for h in receipt.refs.hashes()), 'All four exact evidence references required')
    return receipt


def _decode(payload, config):
    _need(isinstance(payload, str) and len(payload.encode()) <= MAX_PAYLOAD_BYTES, 'Malformed receipt payload')
    try:
        data = json.loads(payload)
    except (ValueError, RecursionError):
        raise LedgerError('Unreadable receipt metadata; no salvage') from None
    _need(isinstance(data, dict) and set(data) == {'version', 'profile', 'receipt'}
          and type(data['version']) is int and data['version'] == 1 and data['profile'] == PROFILE,
          'Unsupported receipt payload version/profile')
    raw = data['receipt']
    _need(isinstance(raw, dict) and set(raw) == set(AcquisitionReceipt.__dataclass_fields__), 'Receipt shape mismatch')
    refs = raw['refs']
    _need(isinstance(refs, dict) and set(refs) == set(EvidenceRefs.__dataclass_fields__), 'Receipt reference shape mismatch')
    return _receipt(AcquisitionReceipt(**{**raw, 'refs': EvidenceRefs(**refs)}), config)


class PoolReceiptLedger:
    """Open read-only by default; explicit trusted-coordinator writer required.

    The immutable boundary is supplied locally, never read from candidate data.
    No provider authenticity check or capture executor is implemented. Publication
    asserts coordinator approval under that boundary; it does not inspect/repair
    raw evidence, allowing missing/malformed captures to remain explicit blockers.
    """
    def __init__(self, config, *, _writer=False):
        self.config = config
        self.root, self.path, self.evidence_path, self.root_identity = _boundary(config)
        self.lock_path = self.path.with_name(self.path.name + '.coordinator.lock')
        _need(self.lock_path != self.evidence_path, 'Lock/store identity overlap')
        self._writer = _writer
        self.evidence_identity = _file(self.evidence_path, private=False)
        if _writer:
            lock_was_present = self.lock_path.exists()
            ledger_was_present = self.path.exists()
            _need(not ledger_was_present or lock_was_present, 'Missing coordinator lock; never recreate for an existing ledger')
            with self._lock(create=not ledger_was_present):
                new = not self.path.exists()
                _need(not new or not lock_was_present, 'Missing ledger behind existing coordinator lock; never reset')
                if new:
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                    os.close(fd)
                self.ledger_identity = _file(self.path, private=True)
                self.lock_identity = _lock_identity(self.lock_path)
                self.descriptor = self._descriptor()
                with self._connection(write=True) as c:
                    if new:
                        for sql in SCHEMA.values():
                            c.execute(sql)
                        body = canonical(self.descriptor); seed = digest(self.descriptor)
                        c.execute('INSERT INTO ledger_descriptor VALUES(1,?,?)', (body, seed))
                        c.execute('INSERT INTO ledger_head VALUES(1,0,?)', (seed,))
                    self._audit(c)
                if new:
                    fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                    try: os.fsync(fd)
                    finally: os.close(fd)
        else:
            self.ledger_identity = _file(self.path, private=True)
            self.lock_identity = _lock_identity(self.lock_path)
            self.descriptor = self._descriptor()
            with self._lock(), self._connection() as c:
                self._audit(c)

    @classmethod
    def open_writer(cls, boundary):
        """Trusted coordinator code only; never expose this method to candidates."""
        return cls(boundary, _writer=True)

    @property
    def writer(self):
        return self._writer

    def _descriptor(self):
        return {'version': 1, 'ledger_id': self.config.ledger_id, 'profile': PROFILE,
                'root': str(self.root), 'root_identity': self.root_identity,
                'ledger_path': str(self.path), 'ledger_identity': self.ledger_identity,
                'lock_path': str(self.lock_path), 'lock_identity': self.lock_identity,
                'evidence_path': str(self.evidence_path), 'evidence_identity': self.evidence_identity,
                'sources': sorted((asdict(s) for s in self.config.sources), key=lambda s: s['source_id']),
                'allow_synthetic_fixtures': self.config.allow_synthetic_fixtures,
                'max_age_seconds': self.config.max_age_seconds}

    def _guard(self):
        _need(self._descriptor() == self.descriptor, 'Coordinator boundary changed after opening')
        _, _, _, root = _boundary(self.config)
        _need(root == self.root_identity and _file(self.evidence_path, private=False) == self.evidence_identity,
              'Coordinator root/evidence store identity changed')
        _need(_file(self.path, private=True) == self.ledger_identity
              and _lock_identity(self.lock_path) == self.lock_identity, 'Ledger/lock identity changed')

    @contextmanager
    def _lock(self, *, create=False):
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        if create: flags |= os.O_CREAT
        fd = os.open(self.lock_path, flags, 0o600)
        try:
            _file(self.lock_path, private=True)
            before = os.fstat(fd)
            _need([before.st_dev, before.st_ino] == _file(self.lock_path, private=True), 'Coordinator lock identity changed')
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(fd, (fcntl.LOCK_EX if self.writer else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    _need(time.monotonic() < deadline, 'Coordinator lock timeout')
                    time.sleep(0.01)
            if hasattr(self, 'ledger_identity'): self._guard()
            yield
            if hasattr(self, 'ledger_identity'): self._guard()
        finally:
            os.close(fd)

    @contextmanager
    def _connection(self, *, write=False):
        self._guard()
        c = sqlite3.connect(self.path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=5,
                            isolation_level=None)
        try:
            if write:
                _need(c.execute('PRAGMA journal_mode').fetchone()[0] == 'delete', 'DELETE journal required')
                c.execute('PRAGMA synchronous=FULL')
            else:
                c.execute('PRAGMA query_only=ON')
            c.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield c
            self._guard()
            c.execute('COMMIT')
        except BaseException:
            if c.in_transaction: c.execute('ROLLBACK')
            raise
        finally:
            c.close()

    def _audit(self, c):
        schema = {name: sql for name, sql in c.execute('SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL')}
        _need(all(schema.get(name) == sql for name, sql in SCHEMA.items()), 'Ledger schema/triggers missing or altered')
        descriptor = c.execute('SELECT body,hash FROM ledger_descriptor WHERE id=1').fetchone()
        _need(descriptor == (canonical(self.descriptor), digest(self.descriptor)), 'Coordinator boundary/store identity mismatch')
        head = c.execute('SELECT count,hash FROM ledger_head WHERE id=1').fetchone()
        _need(head is not None and type(head[0]) is int and 0 <= head[0] <= MAX_RECORDS, 'Journal head missing/invalid')
        rows = c.execute('SELECT seq,publication_id,pool,mint,slot,source_id,payload,payload_hash,previous_hash,row_hash FROM coordinator_receipts ORDER BY seq LIMIT ?',
                         (MAX_RECORDS+1,)).fetchall()
        _need(len(rows) == head[0], 'Journal record count mismatch')
        previous = digest(self.descriptor); receipts = []
        for seq, row in enumerate(rows, 1):
            number, publication, pool, mint, slot, source, payload, payload_hash, prior, row_hash = row
            receipt = _decode(payload, self.config)
            _need(_identity(publication) and number == seq and payload == canonical(json.loads(payload))
                  and digest(json.loads(payload)) == payload_hash and prior == previous
                  and (pool, mint, slot, source) == (receipt.pool, receipt.mint, str(receipt.slot), receipt.source_id)
                  and row_hash == digest({'seq': number, 'publication_id': publication,
                                          'payload_hash': payload_hash, 'previous_hash': prior}), 'Receipt journal integrity mismatch')
            previous = row_hash; receipts.append((row, receipt))
        _need(previous == head[1], 'Journal head/hash mismatch')
        return receipts, previous

    @staticmethod
    def _append(c, publication_id, receipt, payload, seq, previous):
        payload_hash = digest(json.loads(payload))
        row_hash = digest({'seq': seq, 'publication_id': publication_id, 'payload_hash': payload_hash, 'previous_hash': previous})
        c.execute('INSERT INTO coordinator_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (seq, publication_id, receipt.pool, receipt.mint, str(receipt.slot), receipt.source_id,
                   payload, payload_hash, previous, row_hash))
        c.execute('UPDATE ledger_head SET count=?,hash=? WHERE id=1', (seq, row_hash))
        return row_hash

    def publish(self, publication_id, receipt):
        """Append an independently approved exact receipt; never import candidate JSON."""
        _need(self.writer, 'Read-only ledger cannot issue receipts')
        _need(_identity(publication_id), 'Stable publication identity required')
        _receipt(receipt, self.config)
        payload = canonical({'version': 1, 'profile': PROFILE, 'receipt': asdict(receipt)})
        _need(len(payload.encode()) <= MAX_PAYLOAD_BYTES, 'Receipt payload too large')
        with self._lock(), self._connection(write=True) as c:
            records, previous = self._audit(c)
            for row, _ in records:
                if row[1] == publication_id:
                    _need(row[6] == payload, 'Publication identity collision')
                    return row[9]
                _need(row[7] != digest(json.loads(payload)), 'Receipt publication identity alias')
            _need(len(records) < MAX_RECORDS, 'Receipt ledger capacity reached')
            return self._append(c, publication_id, receipt, payload, len(records)+1, previous)

    def policy(self, *, pool, mint, slot):
        """Return an immutable snapshot, never a policy to cache for later admission.

        No refs/time/source subset argument. Use policy_view for a future bridge
        that needs reconstruction and admission to share one publication guard.
        """
        with self.policy_view(pool=pool, mint=mint, slot=slot) as view:
            return view.policy

    @contextmanager
    def policy_view(self, *, pool, mint, slot):
        """Hold the ledger guard across policy reconstruction and offline replay.

        All approved scoped observations are retained. Corruption anywhere aborts
        reconstruction; >256 observations fails closed rather than truncating.
        Publication waits until this view exits. Its bound reader is unavailable
        after exit. Protect caller use: a detached/cached TrustedSourcePolicy is
        not automatically invalidated by Python if the caller ignores this API.
        This is a storage primitive, not entry/admission runtime wiring.
        """
        address(pool); address(mint)
        _need(type(slot) is int and 0 <= slot < 2**64, 'Exact policy scope required')
        with self._lock(), self._connection() as c:
            records, head = self._audit(c)
            receipts = frozenset(r for _, r in records if (r.pool, r.mint, r.slot) == (pool, mint, slot))
            _need(len(receipts) <= 256, 'Admission receipt scope capacity exceeded')
            policy = TrustedSourcePolicy(self.config.ledger_id + ':' + head,
                                         frozenset(s.source_id for s in self.config.sources), receipts,
                                         max_age_seconds=self.config.max_age_seconds,
                                         allow_synthetic_fixtures=self.config.allow_synthetic_fixtures)
            active = True
            def load(key):
                _need(active, 'Policy view has ended')
                self._guard()
                result = EvidenceStore(self.evidence_path, read_only=True).load(key)
                self._guard()
                return result
            try:
                yield PolicyView(policy, load)
            finally:
                active = False

    def load_evidence(self, key):
        """Offline reader pinned to this ledger's canonical evidence-store identity."""
        with self._lock():
            self._guard()
            result = EvidenceStore(self.evidence_path, read_only=True).load(key)
            self._guard()
            return result
