"""Candidate sources for the lean trader (L14).

A ``Source`` is anything that can say "these pools are new since cursor N": ``iter_new(cursor) -> (candidates,
next_cursor)``. The cursor is a non-negative int owned by the source; the caller persists it (one file per source,
``lean.candidates.write_cursor``: atomic, forward-only) AFTER the batch was handled. Candidates are
``lean.candidates.Candidate`` rows, so every source feeds the same screen -> entry-decision -> paper-fill path.

Implemented
-----------
``PumpGraduationSource``  the discovery database (``discovery/continuous.sqlite``, read-only) that the runner has always
                          read: pump.fun -> PumpSwap migrations. Wraps ``lean.candidates.scan_new`` unchanged.

NOT implemented (TODO, deliberately): Raydium CPMM, Raydium AMM v4, Meteora DAMM, Meteora DLMM new-pool discovery
------------------------------------------------------------------------------------------------------------------
The task allows only layouts that can be verified against a captured fixture. The repository holds NO captured account
for any of these programs (the only mentions are Jupiter route labels) and this worker may not call a provider, so a
decoder written from memory would be unverified and a wrong decode would turn a wrong number into a paper entry.
Two further blockers are independent of the decoder: ``lean.candidates.screen`` only understands the PumpSwap pool
layout (a non-PumpSwap pool fails ``POOL_BINDING_INVALID``), and ``lean.adapters.mark`` is a PumpSwap constant-product
net mark. To add one program: (1) capture a real pool account + its two vault accounts with provenance into
``fixtures/``; (2) write a pure decoder against it; (3) give the screen and the mark a pool-kind switch; (4) add a
``Source`` subclass here that lists new pools (``getSignaturesForAddress`` on the program, cursor = slot), and
register it in ``SOURCE_TYPES``. Estimated Helius cost of (4), for the budget: one ``getSignaturesForAddress`` per
poll (1 credit-class call, limit 100) plus one ``getTransaction`` per NEW signature; polling every 30 s is 120 polls
per hour per program, and every new signature adds one more call, so the hourly cost is ``120 + new_signatures``
calls per program (a busy AMM v4 sees thousands of signatures per hour: it needs a pre-filter on the initialize
instruction discriminator before any ``getTransaction``). That is why they are not switched on blind.

Failure policy: a source that raises, returns malformed data or has a corrupt cursor file is skipped for this pass
and an error is reported (``SOURCE_FAILED`` / ``SOURCE_CURSOR_INVALID``); other sources and the runner are
unaffected, the cursor is never reset and never moves backward.
"""
import os
import re
import time

from lean import candidates as C

DEFAULT_SOURCE = 'pump_graduation'
NAME_RE = re.compile(r'^[a-z][a-z0-9_]{0,39}$')


class SourceConfigError(ValueError):
    pass


class Source:
    """Interface. ``name`` is stored on every candidate row it produces."""
    name = ''

    def iter_new(self, cursor):
        """-> (candidates, next_cursor). May raise; the caller isolates it. ``next_cursor`` may exceed the last
        candidate's own position (skipped frames): the caller persists it after handling the batch."""
        raise NotImplementedError


class PumpGraduationSource(Source):
    """The existing discovery database as a Source (pump.fun -> PumpSwap migrations)."""

    def __init__(self, discovery_db, *, name=DEFAULT_SOURCE, cfg=None, limit=500, clock=time.time):
        self.name, self.discovery_db, self.cfg, self.limit, self.clock = name, os.fspath(discovery_db), cfg, limit, clock
        self.last_stats = {}

    def iter_new(self, cursor):
        scan = C.scan_new(self.discovery_db, cursor, now=self.clock(), limit=self.limit, cfg=self.cfg)
        self.last_stats = dict(scan.stats)
        return list(scan.candidates), scan.next_cursor


SOURCE_TYPES = {'pump_graduation': PumpGraduationSource}


def build_sources(specs, *, screen_cfg=None, limit=500, clock=time.time):
    """``specs``: [{'name': 'pump_b', 'type': 'pump_graduation', 'discovery_db': '/path'}]. Strict: unknown keys,
    unknown types (including the not-yet-implemented AMM programs), duplicate or reserved names are refused."""
    if specs is None:
        return []
    if not isinstance(specs, list):
        raise SourceConfigError('sources must be a list')
    out, seen = [], {DEFAULT_SOURCE}
    for spec in specs:
        if not isinstance(spec, dict) or set(spec) - {'name', 'type', 'discovery_db'}:
            raise SourceConfigError('source spec keys: name, type, discovery_db')
        name, kind, path = spec.get('name'), spec.get('type', 'pump_graduation'), spec.get('discovery_db')
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise SourceConfigError('source name must match %s' % NAME_RE.pattern)
        if name in seen:
            raise SourceConfigError('duplicate or reserved source name: %s' % name)
        if kind not in SOURCE_TYPES:
            raise SourceConfigError('SOURCE_TYPE_UNSUPPORTED: %s (supported: %s)' % (kind, sorted(SOURCE_TYPES)))
        if not isinstance(path, str) or not path:
            raise SourceConfigError('discovery_db required for source %s' % name)
        seen.add(name)
        out.append(SOURCE_TYPES[kind](path, name=name, cfg=screen_cfg, limit=limit, clock=clock))
    return out


class SourceSet:
    """Polls extra sources, one persisted cursor each (``cursor-<name>.json`` in the state dir)."""

    def __init__(self, sources, state_dir):
        self.sources, self.state_dir = list(sources), os.fspath(state_dir)
        names = [s.name for s in self.sources]
        if len(set(names)) != len(names) or DEFAULT_SOURCE in names:
            raise SourceConfigError('duplicate or reserved source name')
        self.last = {}

    def cursor_path(self, source):
        return os.path.join(self.state_dir, 'cursor-%s.json' % source.name)

    def poll(self, handle, *, exists, stopped, on_error):
        """Handle new candidates of every source; returns how many were handled.

        ``handle(source_name, candidate)``  screen+decide one candidate (must isolate its own failures);
        ``exists(mint)``  True when the candidate was already handled (e.g. before a restart, or by another source:
                          a mint is handled once, by whichever source saw it first);
        ``stopped()``  True when entries must stop (stop event, kill switch, halt);
        ``on_error(code, transient, source_name, message)``."""
        done = 0
        for source in self.sources:
            if stopped():
                break
            path = self.cursor_path(source)
            try:
                cursor = C.read_cursor(path)
            except (OSError, ValueError):
                on_error('SOURCE_CURSOR_INVALID', False, source.name, 'cursor file unreadable; source skipped')
                continue
            try:
                found, next_cursor = source.iter_new(cursor)
                if type(next_cursor) is not int or next_cursor < cursor:
                    raise ValueError('SOURCE_CURSOR_MOVED_BACKWARD_OR_INVALID')
                found = list(found)
            except C.DiscoveryUnavailable as error:
                on_error('DISCOVERY_UNAVAILABLE', True, source.name, str(error)[:120])
                continue
            except Exception as error:                          # isolation: a broken source never stops the others
                on_error('SOURCE_FAILED', False, source.name, type(error).__name__)
                continue
            handled_to, complete = cursor, True
            for candidate in found:
                if stopped():
                    complete = False
                    break
                if not exists(candidate.mint):
                    handle(source.name, candidate)
                    done += 1
                handled_to = max(handled_to, candidate.seq)
            if complete:
                handled_to = next_cursor
            if handled_to > cursor:
                C.write_cursor(path, handled_to)
            self.last[source.name] = {'cursor': max(handled_to, cursor), 'found': len(found)}
        return done


# ----------------------------------------------------------------------------------------------- pagination
def iter_rows(store, table, *, page=2000, **filters):
    """Every row of ``table`` (ascending id), page by page: ``Store.rows`` only returns the OLDEST 100000, so a table that
    grows past that would silently lose its newest rows."""
    after = 0
    while True:
        rows = store.rows_after(table, after, limit=page, **filters)
        if not rows:
            return
        yield from rows
        after = rows[-1]['id']


# ----------------------------------------------------------------------------------------------- funnel by source
def funnel_by_source(store_or_connection):
    """Per-source funnel from the store alone (read-only): candidates -> screened -> passed -> entered, the final
    rejection reasons, and the watchlist outcome. A candidate row without a ``source`` (written before L14) is
    ``pump_graduation``. The LAST screen decision of a mint is its screen result (a rescreen supersedes the first).

    Accepts a ``lean.store.Store`` or a read-only sqlite3 connection (what ``lean.report`` has). Every table is STREAMED
    in id order, so there is no row cap and memory is one entry per mint."""
    import json
    lock = getattr(store_or_connection, '_lock', None)
    connection = getattr(store_or_connection, 'db', store_or_connection)
    sources, last_screen, entered, watch = {}, {}, set(), []
    with (lock if lock is not None else _NullLock()):
        for mint, meta in connection.execute('SELECT mint, meta FROM candidates ORDER BY id'):
            try:
                meta = json.loads(meta)
            except (TypeError, ValueError):
                meta = {}
            sources[mint] = meta.get('source') if isinstance(meta, dict) and isinstance(meta.get('source'), str) else DEFAULT_SOURCE
        for mint, action, reasons in connection.execute("SELECT mint, action, reasons FROM decisions WHERE kind='screen' ORDER BY id"):
            last_screen[mint] = (action, reasons)
        entered.update(m for (m,) in connection.execute("SELECT DISTINCT mint FROM fills WHERE side='buy'"))
        for kind, payload in connection.execute("SELECT kind, payload FROM events WHERE kind IN ('watch_add','watch_rescreen','watch_end') ORDER BY id"):
            watch.append((kind, _payload_text(payload)))
    out = {}

    def bucket(source):
        return out.setdefault(source, {'candidates': 0, 'screened': 0, 'passed': 0, 'entered': 0, 'screen_rejected': 0,
                                       'screen_failed': 0, 'rejection_reasons': {}, 'watched': 0, 'rescreens': 0,
                                       'entered_after_watch': 0, 'watch_ended': {}})
    for source in sources.values():
        bucket(source)['candidates'] += 1
    for mint, (action, reasons) in last_screen.items():
        if mint not in sources:
            continue
        b = bucket(sources[mint])
        b['screened'] += 1
        if action == 'PASS':
            b['passed'] += 1
        elif action == 'FAILED':
            b['screen_failed'] += 1
        else:
            b['screen_rejected'] += 1
            try:
                listed = json.loads(reasons)
            except (TypeError, ValueError):
                listed = ['UNSPECIFIED']
            for reason in set(str(r) for r in (listed or ['UNSPECIFIED'])):
                b['rejection_reasons'][reason] = b['rejection_reasons'].get(reason, 0) + 1
    watched = set()
    for kind, payload in watch:
        mint = payload.get('mint')
        if mint not in sources:
            continue
        b = bucket(sources[mint])
        if kind == 'watch_add':
            watched.add(mint)
            b['watched'] += 1
        elif kind == 'watch_rescreen':
            b['rescreens'] += 1
        else:
            why = str(payload.get('why'))
            b['watch_ended'][why] = b['watch_ended'].get(why, 0) + 1
    for mint in entered:
        if mint in sources:
            b = bucket(sources[mint])
            b['entered'] += 1
            if mint in watched:
                b['entered_after_watch'] += 1
    return out


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _payload_text(text):
    import json
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
