"""Regime log: every ``interval`` seconds one ``observations(kind='regime')`` row. A FEATURE ONLY, never a gate.

PAPER ONLY. Each row holds SOL/USD, its 1h and 24h change and the discovery candidates seen in the last hour:

    {"kind": "lean_regime_v1", "at": ts, "sol_usd": "150.1", "sol_usd_change_1h": "0.0123" | null,
     "sol_usd_change_24h": ... | null, "candidates_1h": n, "window_s": s, "candidates_per_hour": n | null, "error": null | code}

* SOL/USD comes from the runner's own cached value (``runner._sol_usd_value``: one Kraken request per ``sol_usd_ttl_s`` at
  most, the same limiter as screening). The 1h/24h change is measured against this log's OWN earlier samples (the stored
  regime rows), so it is ``null`` until a sample about an hour / a day old exists, and across a restart it continues from
  the store. A reference sample must lie within max(2 intervals, 5% of the window) of the wanted time (10 min for 1h, 72 min
  for 24h); otherwise the change is ``null`` (unknown), never extrapolated.
* ``candidates_1h`` counts the candidate rows the runner stored in the last hour; ``candidates_per_hour`` scales that to an
  hour only when the observed window is at least 10 minutes.
* Nothing in the trader reads these rows. A provider failure is recorded in the row (``error``) and never raised.
"""
from collections import deque
from contextlib import closing
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3

KIND = 'regime'
RECORD = 'lean_regime_v1'
HISTORY_SECONDS = 25 * 3600
MIN_WINDOW_FOR_RATE = 600


def _open_ro(path):
    """A separate read-only connection to the (live, WAL) store: no blob loads and no use of the writer's lock."""
    resolved = Path(path).resolve()
    connection = sqlite3.connect(resolved.as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.execute('PRAGMA query_only=1')
    return connection


def change(history, now, seconds, price, *, tolerance):
    """Relative change of ``price`` against the sample closest to ``now - seconds``, or None when no sample is within
    ``tolerance`` seconds of that time (or the reference is not a positive price)."""
    want = now - seconds
    best = None
    for at, value in history:
        gap = abs(at - want)
        if gap <= tolerance and (best is None or gap < best[0]):
            best = (gap, value)
    if best is None or best[1] <= 0:
        return None
    return price / best[1] - 1


class RegimeLog:
    def __init__(self, store, *, interval_s=300, clock, started_at=None):
        if not isinstance(interval_s, (int, float)) or isinstance(interval_s, bool) or not 30 <= interval_s <= 3600:
            raise ValueError('regime interval_s must be between 30 and 3600')
        self.store, self.interval, self.clock = store, float(interval_s), clock
        self.started_at = clock() if started_at is None else started_at
        self.last_at = None
        self.last = None
        self.history = deque()                 # (ts, Decimal sol_usd), oldest first
        self._load()

    def _load(self):
        """Earlier samples from the store so the 1h/24h changes survive a restart."""
        horizon = self.clock() - HISTORY_SECONDS
        try:
            with closing(_open_ro(self.store.path)) as c:
                rows = c.execute('SELECT ts,meta FROM observations WHERE kind=? AND ts>=? ORDER BY id', (KIND, horizon)).fetchall()
        except (sqlite3.Error, OSError):
            return
        for ts, meta in rows:
            try:
                price = json.loads(meta).get('sol_usd')
                if price is not None:
                    self.history.append((float(ts), Decimal(price)))
            except (ValueError, InvalidOperation, AttributeError, TypeError):
                continue
        if rows:
            self.last_at = float(rows[-1][0])

    def due(self, now=None):
        now = self.clock() if now is None else now
        return self.last_at is None or now - self.last_at >= self.interval

    def _candidates_last_hour(self, now):
        with closing(_open_ro(self.store.path)) as c:
            return c.execute('SELECT COUNT(*) FROM candidates WHERE ts>=?', (now - 3600,)).fetchone()[0]

    def sample(self, sol_usd_value, now=None):
        """Build, store and return one regime record. ``sol_usd_value`` is a zero-argument callable returning a Decimal (or
        raising): a failure is recorded as ``error`` with ``sol_usd`` null."""
        now = self.clock() if now is None else now
        record = {'kind': RECORD, 'at': now, 'sol_usd': None, 'sol_usd_change_1h': None, 'sol_usd_change_24h': None,
                  'candidates_1h': None, 'window_s': None, 'candidates_per_hour': None, 'error': None}
        price = None
        try:
            price = Decimal(sol_usd_value())
            if not price.is_finite() or price <= 0:
                raise ValueError('price')
            record['sol_usd'] = format(price, 'f')
            for name, seconds in (('sol_usd_change_1h', 3600), ('sol_usd_change_24h', 86400)):
                value = change(self.history, now, seconds, price, tolerance=max(2 * self.interval, 0.05 * seconds))
                record[name] = None if value is None else format(value.quantize(Decimal('0.000001')), 'f')
        except Exception as error:               # a feature: never raises, never gates
            record['error'] = str(getattr(error, 'code', type(error).__name__))[:64]
            price = None
        try:
            count = self._candidates_last_hour(now)
            window = min(3600.0, max(0.0, now - self.started_at))
            record['candidates_1h'], record['window_s'] = count, round(window, 3)
            if window >= MIN_WINDOW_FOR_RATE:
                record['candidates_per_hour'] = round(count * 3600.0 / window, 3)
        except (sqlite3.Error, OSError) as error:
            record['error'] = record['error'] or type(error).__name__
        raw = json.dumps(record, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        self.store.add_observation(KIND, raw, meta={'sol_usd': record['sol_usd'], 'error': record['error']}, ts=now)
        if price is not None:
            self.history.append((now, price))
            while self.history and self.history[0][0] < now - HISTORY_SECONDS:
                self.history.popleft()
        self.last_at, self.last = now, record
        return record
