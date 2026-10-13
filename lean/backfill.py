"""Discovery gap backfill (C4): find pump.fun -> PumpSwap graduations that the discovery feed missed. PAPER ONLY, read-only RPC.

The discovery listener (frozen ``discovery/continuous.py``) records every transaction that mentions the pump.fun migration
authority, and stores each as ``raw_events.source_id = 'confirmed:<signature>'``. Its websocket has gaps (reconnects, multi-hour
outages). This module polls the SAME address over plain RPC and feeds the runner the migrations that are absent from that
database, as a second source through the existing L14 ``lean.sources.Source`` interface:

  1. ``getSignaturesForAddress(authority, {until: <last handled signature>, limit, commitment: confirmed})``, newest first,
     at most ``max_pages`` pages per poll, one poll per ``interval_s`` (default 120 s). LOW LANE only.
  2. Oldest first, each signature younger than the screen's ``min_age_seconds`` stops the pass (not ready yet, like the discovery
     scan); a failed transaction is counted and skipped.
  3. A signature already in the discovery database is counted as ``seen_by_feed`` and is NOT fetched (no credit spent).
  4. Every other signature costs one ``getTransaction(jsonParsed)``; it is decoded with the discovery decoder and the SAME
     migration test as ``lean.candidates`` (one decoded CompletePumpAmmMigrationEvent as the child of ``migrate`` /
     ``migrate_v2``, canonical pool) so the Candidate has exactly the shape a discovery frame produces. ``migrated_at`` is the
     on-chain ``blockTime`` (a frame's is the local receive time), so a late backfill is OLDER and the screen's own age window
     (``TOO_OLD`` above ``max_age_seconds``) rejects it: it is recorded, never entered.
  5. The candidate row carries ``source='backfill'`` and the detection latency (``now - blockTime``) in its meta.

Cursor. ``SourceSet`` persists an int cursor per source (here: the highest processed SLOT). The exact resume point is the last
handled signature, kept in ``backfill-state.json`` next to the cursor: a new ``until`` is only committed after the runner has
persisted a cursor at or beyond it, so a crash between "batch returned" and "batch handled" re-fetches the overlap (the runner
dedupes by mint) instead of losing it. Without a usable ``until`` (first start) the walk stops at the cursor slot or at
``lookback_s`` (default 6 h), never at genesis.

Gaps are measured, not hidden: per UTC day ``authority_txs`` (distinct signatures examined), ``seen_by_feed``,
``missing_from_feed``, ``missed_migrations`` (missing AND decoded as a migration), ``not_migration``, ``fetch_failed``,
``overflow_polls`` (the page budget ended before reaching the resume point: older signatures were NOT examined) and
``beyond_max_age``. ``health()`` reports them with ``gap_rate = missing_from_feed / authority_txs``.

Budget (Helius credits are the plan's, the table is configurable): per poll 1..``max_pages`` getSignaturesForAddress plus one
getTransaction per NEW signature absent from the feed, at most ``max_tx_per_poll``. At the default 120 s that is 720 polls/day
(~720 calls when the feed is healthy: every authority signature is already in the database and costs nothing more).
"""
import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field

from desk.model import digest
from desk.programs import address
from discovery import continuous as discovery
from lean import candidates as C
from lean.providers import LANE_SHED, ProviderError
from lean.sources import NAME_RE, Source

NAME = 'backfill'
STATE_FILE = 'backfill-state.json'
STATE_VERSION = 1
KEEP_SIGNATURES = 1000
AUTHORITY = discovery.AUTHORITY
DEFAULTS = {
    'interval_s': 120, 'limit': 100, 'max_pages': 3, 'max_tx_per_poll': 40, 'max_attempts_per_signature': 5,
    'lookback_s': 21_600, 'authority': AUTHORITY, 'credits_per_signatures_call': 1, 'credits_per_transaction': 1,
    'day_history': 14,
}
DAY = 86_400


class ConfigError(ValueError):
    pass


def make_config(raw):
    """Strict ``backfill`` settings: unknown keys, wrong types and out-of-range values are refused at load."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError('backfill must be an object or null')
    unknown = set(raw) - set(DEFAULTS) - {'enabled', 'name', '_comment'}
    if unknown:
        raise ConfigError('backfill: unknown keys %s' % sorted(unknown))
    cfg = {**DEFAULTS, 'enabled': True, 'name': NAME, **{k: v for k, v in raw.items() if k != '_comment'}}

    def integer(key, low, high):
        value = cfg[key]
        if type(value) is not int or not low <= value <= high:
            raise ConfigError('backfill.%s must be an integer in [%d, %d]' % (key, low, high))
    if type(cfg['enabled']) is not bool:
        raise ConfigError('backfill.enabled must be true or false')
    if not isinstance(cfg['name'], str) or not NAME_RE.match(cfg['name']):
        raise ConfigError('backfill.name must match %s' % NAME_RE.pattern)
    integer('interval_s', 10, 3600)
    integer('limit', 1, 1000)
    integer('max_pages', 1, 10)
    integer('max_tx_per_poll', 1, 200)
    integer('max_attempts_per_signature', 1, 20)
    integer('lookback_s', 300, 7 * DAY)
    integer('credits_per_signatures_call', 0, 1000)
    integer('credits_per_transaction', 0, 1000)
    integer('day_history', 1, 60)
    try:
        address(cfg['authority'])
    except (ValueError, TypeError, KeyError):
        raise ConfigError('backfill.authority must be a base58 address') from None
    return cfg


@dataclass(frozen=True)
class BackfillCandidate(C.Candidate):
    """A lean.candidates.Candidate plus the labels ``Runner._handle_candidate`` stores in the candidate row's meta."""
    source: str = NAME
    latency_s: float = 0.0
    beyond_max_age: bool = False

    @property
    def meta(self):
        return {'source': self.source, 'latency_s': round(self.latency_s, 3), 'beyond_max_age': self.beyond_max_age}


def _zero_day():
    return {'polls': 0, 'authority_txs': 0, 'seen_by_feed': 0, 'missing_from_feed': 0, 'missed_migrations': 0, 'not_migration': 0,
            'failed_tx': 0, 'fetch_failed': 0, 'overflow_polls': 0, 'beyond_max_age': 0, 'calls_signatures': 0, 'calls_transaction': 0,
            'sheds': 0}


class BackfillSource(Source):
    """The C4 source. ``helius`` is a LOW-LANE client (``lean.providers.low(keys).helius``): a call either goes out at once or
    raises LANE_SHED having sent nothing, which this source treats as "try again later"."""

    def __init__(self, helius, discovery_db, state_dir, *, cfg=None, screen_cfg=None, clock=time.time, name=None):
        self.cfg = make_config(cfg or {})
        self.name = name or self.cfg['name']
        self.helius, self.discovery_db, self.clock = helius, os.fspath(discovery_db), clock
        screen = screen_cfg or {}
        self.min_age = screen.get('min_age_seconds', C.DEFAULTS['min_age_seconds'])
        self.max_age = screen.get('max_age_seconds', C.DEFAULTS['max_age_seconds'])
        self.state_path = os.path.join(os.fspath(state_dir), STATE_FILE)
        self.state = self._load()
        self.next_poll_at = 0.0
        self.attempts = {}
        self.counted = dict.fromkeys(self.state.get('counted', []))      # signatures already in the counters (survives a restart)
        self.outcome = dict.fromkeys(self.state.get('outcome', []))
        self.stats = {'polls': 0, 'sheds': 0, 'errors': 0, 'last_poll_at': None, 'last_error': None, 'last_found': 0}
        self.last_stats = {}

    # ------------------------------------------------------------------------------------------------- state
    def _load(self):
        try:
            with open(self.state_path, 'r', encoding='utf-8') as stream:
                state = json.load(stream)
        except FileNotFoundError:
            return {'version': STATE_VERSION, 'until': None, 'slot': 0, 'pending': None, 'days': {}}
        if (not isinstance(state, dict) or state.get('version') != STATE_VERSION or type(state.get('slot')) is not int
                or not isinstance(state.get('days'), dict)):
            raise ConfigError('backfill state file is corrupt: %s (not reset; fix or remove it by hand)' % self.state_path)
        state.setdefault('until', None)
        state.setdefault('pending', None)
        return state

    def _save(self):
        days = self.state['days']
        for old in sorted(days)[:-self.cfg['day_history']]:
            del days[old]
        self.state['counted'] = list(self.counted)[-KEEP_SIGNATURES:]
        self.state['outcome'] = list(self.outcome)[-KEEP_SIGNATURES:]
        tmp = '%s.tmp.%d' % (self.state_path, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, json.dumps(self.state, sort_keys=True).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.state_path)

    def _day(self, now):
        key = time.strftime('%Y-%m-%d', time.gmtime(now))
        return self.state['days'].setdefault(key, _zero_day())

    # ----------------------------------------------------------------------------------------------- polling
    def iter_new(self, cursor):
        now = self.clock()
        pending = self.state.get('pending')
        if pending and cursor >= pending['slot']:                       # the runner persisted a cursor past the last batch
            self.state.update(until=pending['until'], slot=pending['slot'], pending=None)
            self._save()
        if now < self.next_poll_at:
            return [], cursor
        self.next_poll_at = now + self.cfg['interval_s']
        day = self._day(now)
        day['polls'] += 1
        self.stats['polls'] += 1
        self.stats['last_poll_at'] = now
        try:
            found, high_slot, last_sig = self._poll(cursor, now, day)
        except ProviderError as error:
            if error.code == LANE_SHED:
                day['sheds'] += 1
                self.stats['sheds'] += 1
                self.next_poll_at = now + min(self.cfg['interval_s'], 15)       # nothing was sent: look again soon
                self._save()
                return [], cursor
            self.stats['errors'] += 1
            self.stats['last_error'] = error.code
            self._save()
            raise
        if last_sig is not None:
            self.state['pending'] = {'until': last_sig, 'slot': high_slot}
        self._save()
        self.stats['last_found'] = len(found)
        return found, high_slot                                              # starts at the cursor: never moves backward

    def _poll(self, cursor, now, day):
        """-> (candidates oldest-first, highest processed slot, last handled signature). Raises ProviderError."""
        until = self.state['until']
        entries, overflow = self._signatures(until, cursor, now, day)
        if overflow:
            day['overflow_polls'] += 1
        entries.reverse()                                                   # oldest first
        ready, tx_budget, found = [], self.cfg['max_tx_per_poll'], []
        for entry in entries:
            if now - entry['blockTime'] < self.min_age:
                break                                                       # not old enough yet; later ones are younger still
            ready.append(entry)
        high_slot, last_sig = cursor, None
        known = self._known_signatures([e['signature'] for e in ready])
        for entry in ready:
            sig, slot = entry['signature'], entry['slot']
            first = sig not in self.counted                               # counters never double count a re-examined signature
            if first:
                self.counted[sig] = True
                day['authority_txs'] += 1
            if entry.get('err') is not None:
                if first:
                    day['failed_tx'] += 1
            elif sig in known:
                if first:
                    day['seen_by_feed'] += 1
            else:
                if first:
                    day['missing_from_feed'] += 1
                if tx_budget <= 0:
                    break                                                   # budget spent: resume from here next poll
                tx_budget -= 1
                try:
                    candidate = self._fetch(entry, now, day)
                except _Retry:
                    break
                except ProviderError as error:
                    if error.code != LANE_SHED:
                        raise
                    day['sheds'] += 1                                      # nothing was sent: keep what this pass already found
                    self.stats['sheds'] += 1
                    self.next_poll_at = now + min(self.cfg['interval_s'], 15)
                    break
                if candidate is _FAILED:
                    self.outcome[sig] = True                               # already counted as fetch_failed; moved past
                    candidate = None
                elif sig not in self.outcome:
                    self.outcome[sig] = True
                    if candidate is None:
                        day['not_migration'] += 1
                    else:
                        day['missed_migrations'] += 1
                        day['beyond_max_age'] += 1 if candidate.beyond_max_age else 0
                if candidate is not None:
                    found.append(candidate)
            high_slot, last_sig = max(high_slot, slot), sig
        for name in ('counted', 'outcome'):                                   # bounded; the newest are kept (and persisted by _save)
            book = getattr(self, name)
            if len(book) > 2 * KEEP_SIGNATURES:
                setattr(self, name, dict.fromkeys(list(book)[-KEEP_SIGNATURES:]))
        return found, high_slot, last_sig

    def _signatures(self, until, cursor, now, day):
        """Newest-first entries newer than ``until`` (or newer than the cursor slot / lookback), bounded pages."""
        out, before, overflow = [], None, False
        floor_time = now - self.cfg['lookback_s']
        for page in range(self.cfg['max_pages']):
            params = {'limit': self.cfg['limit'], 'commitment': 'confirmed'}
            if until:
                params['until'] = until
            if before:
                params['before'] = before
            result, _raw, _meta = self.helius.rpc('getSignaturesForAddress', [self.cfg['authority'], params])
            day['calls_signatures'] += 1
            rows = _valid_rows(result)
            reached = False
            for row in rows:
                if not until and (row['blockTime'] < floor_time or (cursor and row['slot'] < cursor)):
                    reached = True
                    break
                out.append(row)
            if reached or len(rows) < self.cfg['limit']:
                return out, False
            before = rows[-1]['signature']
            overflow = page == self.cfg['max_pages'] - 1
        return out, overflow

    def _known_signatures(self, signatures):
        """Which of these signatures the discovery feed already stored. Unreadable database = the poll fails (transient): without
        it we cannot tell a missed migration from a seen one, and we must not spend credits on guesses."""
        known = set()
        if not signatures:
            return known
        try:
            with closing(C._connect_ro(self.discovery_db)) as db:
                for i in range(0, len(signatures), 200):
                    chunk = signatures[i:i + 200]
                    marks = ','.join('?' * len(chunk))
                    for (source_id,) in db.execute('SELECT source_id FROM raw_events WHERE source_id IN (%s)' % marks,
                                                   ['confirmed:' + s for s in chunk]):
                        known.add(source_id[len('confirmed:'):])
        except C.DiscoveryUnavailable:
            raise
        except (FileNotFoundError, PermissionError, sqlite3.Error) as error:
            raise C.DiscoveryUnavailable('DISCOVERY_UNAVAILABLE: %s' % type(error).__name__) from None
        return known

    def _fetch(self, entry, now, day):
        sig = entry['signature']
        try:
            result, _raw, _meta = self.helius.rpc('getTransaction', [sig, {'encoding': 'jsonParsed', 'commitment': 'confirmed',
                                                                           'maxSupportedTransactionVersion': 0}])
        except ProviderError as error:
            if error.code == LANE_SHED:
                raise
            self.attempts[sig] = self.attempts.get(sig, 0) + 1
            day['calls_transaction'] += 1
            if self.attempts[sig] >= self.cfg['max_attempts_per_signature']:
                day['fetch_failed'] += 0 if sig in self.outcome else 1       # a re-examined signature counts once
                return _FAILED
            raise _Retry from None
        day['calls_transaction'] += 1
        if result is None:                                                  # not available yet (or pruned): retry a few times
            self.attempts[sig] = self.attempts.get(sig, 0) + 1
            if self.attempts[sig] >= self.cfg['max_attempts_per_signature']:
                day['fetch_failed'] += 0 if sig in self.outcome else 1       # a re-examined signature counts once
                return _FAILED
            raise _Retry
        return migration_candidate(result, sig, now=now, max_age=self.max_age, name=self.name)

    # ------------------------------------------------------------------------------------------------ health
    def health(self):
        days = self.state['days']
        today = days.get(time.strftime('%Y-%m-%d', time.gmtime(self.clock())), _zero_day())
        costs = (self.cfg['credits_per_signatures_call'], self.cfg['credits_per_transaction'])

        def view(d):
            txs = d['authority_txs']
            return {**d, 'gap_rate': (d['missing_from_feed'] / txs) if txs else None,
                    'credits_estimate': d['calls_signatures'] * costs[0] + d['calls_transaction'] * costs[1]}
        return {'enabled': True, 'source': self.name, 'interval_s': self.cfg['interval_s'], 'cursor_slot': self.state['slot'],
                'resume_signature_set': self.state['until'] is not None, **self.stats, 'today': view(today),
                'days': {k: view(v) for k, v in sorted(days.items())}}


class _Retry(Exception):
    pass


_FAILED = object()


def _valid_rows(result):
    if not isinstance(result, list):
        raise ProviderError('RESPONSE_INVALID', False, provider='helius')
    rows = []
    for row in result:
        if (not isinstance(row, dict) or not isinstance(row.get('signature'), str) or type(row.get('slot')) is not int
                or type(row.get('blockTime')) is not int):
            continue                                                         # no blockTime = no age = never used
        rows.append(row)
    return rows


def migration_candidate(tx, signature, *, now, max_age, name=NAME):
    """A ``getTransaction`` (jsonParsed) result -> BackfillCandidate, or None when it is not a clean migration. The decision is
    ``lean.candidates``' own (``_frame_candidate``: exactly one decoded CompletePumpAmmMigrationEvent as the depth-2 child of
    migrate / migrate_v2, canonical pool, valid addresses); this wrapper only checks that the transaction is the one asked for."""
    if (not isinstance(tx, dict) or type(tx.get('slot')) is not int or type(tx.get('blockTime')) is not int
            or not isinstance(tx.get('transaction'), dict) or not isinstance(tx.get('meta'), dict)):
        return None
    if tx['meta'].get('err') is not None or (tx['transaction'].get('signatures') or [None])[0] != signature:
        return None
    payload = json.dumps(tx, sort_keys=True)
    stats = {'frames': 0, 'undecodable': 0, 'not_migration': 0, 'ambiguous': 0, 'unsupported_pool': 0, 'altered': 0}
    base = C._frame_candidate(tx['slot'], tx['blockTime'], tx['slot'], digest(json.loads(payload)), payload, stats)
    if base is None:
        return None
    latency = max(0.0, now - tx['blockTime'])
    return BackfillCandidate(base.seq, base.mint, base.pool, base.signature, base.slot, base.migrated_at, base.payload_hash,
                             source=name, latency_s=latency, beyond_max_age=latency > max_age)


def build(cfg, *, helius_low, discovery_db, state_dir, screen_cfg=None, clock=time.time):
    """None when the block is absent or ``enabled`` is false; else the source (the caller adds it to its SourceSet)."""
    cfg = make_config(cfg)
    if cfg is None or not cfg['enabled']:
        return None
    return BackfillSource(helius_low, discovery_db, state_dir, cfg=cfg, screen_cfg=screen_cfg, clock=clock)
