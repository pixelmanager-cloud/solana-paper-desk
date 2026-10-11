"""Lean operations: credit tracker, systemd watchdog, regime log wiring and the morning report. PAPER ONLY.

* ``CreditTracker`` counts provider calls per method (every HTTP attempt), prices them with a config table of ESTIMATED
  credits, keeps the month's usage and an hourly history in the store (``events`` kind ``credit_usage``, flushed at most once a
  minute: a crash loses at most that minute), projects the month from the last 24 h and, when the projection exceeds
  ``credit_budget_month``, SHEDS the low-priority data lanes (``paths``, ``features``, ``wallet_signals``): ``allow(lane)``
  returns False for them and a ``CREDIT_SHED`` error row is recorded once per lane and episode. Screening and exits are
  never shed or slowed.
* ``Watchdog`` sends ``WATCHDOG=1`` through ``$NOTIFY_SOCKET`` (stdlib ``socket``, no dependency) only while BOTH loop
  heartbeats are fresh. A stalled loop therefore stops the pings and systemd restarts the service (``WatchdogSec=``).
* ``Ops`` ties them to the runner with a few hook calls (``beat``, ``health``, ``start``) and drives the regime log
  (``lean.regime``). Ops are optional: a ``lean.json`` without an ``ops`` block behaves exactly as before.
* ``python -m lean.ops report`` is the morning report: the L06 report (and the L08 replay report when the module and a grid
  are present) written to ``<out-dir>/YYYY-MM-DD.html``. No notifications.
"""
import argparse
import calendar
import contextlib
import dataclasses
import datetime
import json
import math
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

LOW_PRIORITY_LANES = frozenset({'low', 'paths', 'features', 'wallet_signals'})   # 'low' = the shared provider low lane (LINT1)
PROVIDERS = ('helius', 'jupiter', 'kraken')
# ESTIMATES, to be calibrated against the provider dashboard (operator-editable via ``ops.credit_costs``): a standard Helius
# RPC request is 1 credit, getProgramAccounts is dearer; Jupiter and Kraken are rate limited, not credit billed here.
DEFAULT_COSTS = {'helius': {'default': 1, 'getProgramAccounts': 10}, 'jupiter': {'default': 0}, 'kraken': {'default': 0}}
OPS_DEFAULTS = {
    'credit_budget_month': None,       # credits per UTC month; None = count and project but never shed
    'credit_costs': None,              # {provider: {method|default: credits}} merged over DEFAULT_COSTS
    'credit_flush_s': 60,
    'watchdog_stale_s': 180,           # a loop heartbeat older than this stops the WATCHDOG=1 pings
    'regime_interval_s': 300,
    'housekeeping_s': 5,
}
FLUSH_KIND = 'credit_usage'
HOURLY_KEEP_S = 48 * 3600
RATE_WINDOW_S = 24 * 3600
MIN_RATE_COVERAGE_S = 600
UNSHED_FRACTION = 0.9                  # hysteresis: stop shedding below 90% of the budget


class OpsError(ValueError):
    pass


# ----------------------------------------------------------------------------------------------- configuration
def load_ops_config(raw):
    """Strict ``ops`` block of lean.json: unknown keys, wrong types and out-of-range values are refused."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OpsError('ops must be an object')
    unknown = set(raw) - set(OPS_DEFAULTS) - {'enabled', '_comment'}
    if unknown:
        raise OpsError('ops keys unknown=%s' % sorted(unknown))
    if raw.get('enabled', True) is False:
        return None
    cfg = {k: raw.get(k, v) for k, v in OPS_DEFAULTS.items()}
    budget = cfg['credit_budget_month']
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget <= 0):
        raise OpsError('credit_budget_month must be a positive number or null')
    for key, low, high in (('credit_flush_s', 5, 3600), ('watchdog_stale_s', 30, 3600), ('regime_interval_s', 30, 3600), ('housekeeping_s', 1, 60)):
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise OpsError('%s must be a number between %s and %s' % (key, low, high))
    cfg['credit_costs'] = merge_costs(cfg['credit_costs'])
    return cfg


def watchdog_only_config():
    """No ``ops`` block but a systemd watchdog is armed (WatchdogSec= in the unit): keep pinging so the unit never kills a
    healthy runner. No metering, no regime log, no credit rows."""
    return {**OPS_DEFAULTS, 'credit_costs': merge_costs(None), 'watchdog_only': True}


def merge_costs(overrides):
    costs = {p: dict(t) for p, t in DEFAULT_COSTS.items()}
    if overrides is None:
        return costs
    if not isinstance(overrides, dict) or set(overrides) - set(PROVIDERS):
        raise OpsError('credit_costs must map known providers to cost tables')
    for provider, table in overrides.items():
        if not isinstance(table, dict):
            raise OpsError('credit_costs[%s] must be an object' % provider)
        for method, cost in table.items():
            if (not isinstance(method, str) or not 0 < len(method) <= 64 or isinstance(cost, bool)
                    or not isinstance(cost, (int, float)) or not math.isfinite(cost) or not 0 <= cost <= 1000):
                raise OpsError('credit_costs[%s][%r] invalid' % (provider, method))
            costs[provider][method] = cost
    return costs


# --------------------------------------------------------------------------------------------- credit tracker
def month_key(ts):
    t = time.gmtime(ts)
    return '%04d-%02d' % (t.tm_year, t.tm_mon)


def month_end(ts):
    t = time.gmtime(ts)
    days = calendar.monthrange(t.tm_year, t.tm_mon)[1]
    return calendar.timegm((t.tm_year, t.tm_mon, days, 23, 59, 59)) + 1


class CreditTracker:
    """Thread-safe call/credit counters for the current UTC month with a 24 h rate projection."""

    def __init__(self, *, costs=None, budget=None, clock=time.time, on_shed=None, snapshot=None):
        self.costs = merge_costs(costs)
        self.budget, self.clock, self.on_shed = budget, clock, on_shed
        self._lock = threading.Lock()
        self.month = month_key(clock())
        self.calls = {}                      # provider -> method -> HTTP attempts
        self.credits = 0.0
        self.hourly = {}                     # hour epoch -> credits
        self.first_seen = clock()
        self.shedding = False
        self.denied = {}                     # lane -> denials this episode
        self._announced = set()
        self.dirty = False
        if snapshot:
            self._restore(snapshot)

    # -- persistence
    def _restore(self, snapshot):
        if snapshot.get('kind') != 'credit_usage_v1' or snapshot.get('month') != self.month:
            return                            # a new month starts at zero (a budget window, not a charge counter)
        self.calls = {p: {m: int(n) for m, n in t.items()} for p, t in snapshot['calls'].items()}
        self.credits = float(snapshot['credits'])
        self.hourly = {int(h): float(c) for h, c in snapshot['hourly'].items()}
        self.first_seen = float(snapshot.get('first_seen', self.first_seen))
        self.shedding = bool(snapshot.get('shedding', False))

    def snapshot(self):
        with self._lock:
            return {'kind': 'credit_usage_v1', 'month': self.month, 'at': self.clock(), 'credits': round(self.credits, 6),
                    'calls': {p: dict(t) for p, t in self.calls.items()}, 'hourly': {str(h): round(c, 6) for h, c in self.hourly.items()},
                    'first_seen': self.first_seen, 'shedding': self.shedding}

    # -- counting
    def cost(self, provider, method):
        table = self.costs.get(provider, {})
        return table.get(method, table.get('default', 1))

    def observe_call(self, provider, endpoint, attempts):
        """LINT1: ``providers.CALL_OBSERVERS`` callback (every lane). ``rpc:getX`` / ``das:getX`` -> ``getX``."""
        self.record(provider, endpoint.split(':', 1)[-1], attempts)

    def record(self, provider, method, attempts=1):
        """Count ``attempts`` HTTP attempts of one logical call. Never raises."""
        try:
            attempts = max(1, int(attempts))
            now = self.clock()
            with self._lock:
                if month_key(now) != self.month:
                    self._rollover(now)
                bucket = self.calls.setdefault(provider, {})
                bucket[method] = bucket.get(method, 0) + attempts
                credit = self.cost(provider, method) * attempts
                self.credits += credit
                hour = int(now // 3600)
                self.hourly[hour] = self.hourly.get(hour, 0.0) + credit
                for old in [h for h in self.hourly if h * 3600 < now - HOURLY_KEEP_S]:
                    del self.hourly[old]
                self.dirty = True
        except Exception:                # accounting of usage must never break a trade path
            pass

    def _rollover(self, now):
        self.month, self.calls, self.credits, self.hourly = month_key(now), {}, 0.0, {}
        self.first_seen, self.shedding, self.denied, self._announced = now, False, {}, set()

    # -- projection / shedding
    def projection(self, now=None):
        """(projected month credits, status). Rate = credits of the last 24 h (at least 10 minutes of data), so a quiet start or a
        burst does not decide the month."""
        now = self.clock() if now is None else now
        with self._lock:
            if month_key(now) != self.month:
                return None, 'INSUFFICIENT_DATA'
            covered = min(RATE_WINDOW_S, max(0.0, now - self.first_seen))
            if covered < MIN_RATE_COVERAGE_S:
                return None, 'INSUFFICIENT_DATA'
            recent = sum(c for h, c in self.hourly.items() if (h + 1) * 3600 > now - RATE_WINDOW_S)
            rate = recent / covered
            return self.credits + rate * max(0.0, month_end(now) - now), 'OK'

    def status(self, now=None):
        now = self.clock() if now is None else now
        projected, state = self.projection(now)
        if self.budget is None:
            return projected, 'NO_BUDGET'
        if projected is None:
            return None, 'INSUFFICIENT_DATA'
        with self._lock:
            if self.shedding and projected < self.budget * UNSHED_FRACTION:
                self.shedding, self.denied, self._announced = False, {}, set()
            elif not self.shedding and projected > self.budget:
                self.shedding = True
            return projected, 'SHEDDING' if self.shedding else 'OK'

    def allow(self, lane):
        """May a data-collection lane spend credits now? Only the low-priority lanes can be refused."""
        if lane not in LOW_PRIORITY_LANES:
            return True
        _projected, state = self.status()
        if state != 'SHEDDING':
            return True
        announce = False
        with self._lock:
            self.denied[lane] = self.denied.get(lane, 0) + 1
            if lane not in self._announced:
                self._announced.add(lane)
                announce = True
        if announce and self.on_shed is not None:
            try:
                self.on_shed(lane)
            except Exception:
                pass
        return False

    def health(self, now=None):
        projected, state = self.status(now)
        with self._lock:
            return {'month': self.month, 'credits_used': round(self.credits, 3), 'projected_month': None if projected is None else round(projected, 1),
                    'budget_month': self.budget, 'status': state, 'shed_lanes': sorted(LOW_PRIORITY_LANES) if state == 'SHEDDING' else [],
                    'denied': dict(self.denied), 'calls': {p: dict(t) for p, t in self.calls.items()},
                    'costs_are_estimates': True}


class Metered:
    """Transparent proxy over a provider client that counts every call (attempts from the response ``meta``)."""

    def __init__(self, inner, provider, tracker):
        object.__setattr__(self, '_inner', inner)
        object.__setattr__(self, '_provider', provider)
        object.__setattr__(self, '_tracker', tracker)

    def __repr__(self):
        return repr(self._inner)

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if not callable(attribute) or name.startswith('_'):
            return attribute

        def call(*args, **kwargs):
            method = name
            if name == 'rpc' and args and isinstance(args[0], str):
                method = args[0]
            elif name == 'get_multiple_accounts':
                method = 'getMultipleAccounts'
            try:
                result = attribute(*args, **kwargs)
            except BaseException as error:
                meta = getattr(error, 'meta', None) or {}
                self._tracker.record(self._provider, method, meta.get('attempts', 1) if isinstance(meta, dict) else 1)
                raise
            meta = result[2] if isinstance(result, tuple) and len(result) == 3 and isinstance(result[2], dict) else {}
            self._tracker.record(self._provider, method, meta.get('attempts', 1))
            return result
        return call


def meter(providers, tracker):
    """The same Providers bundle with every client counted."""
    return dataclasses.replace(providers, **{p: Metered(getattr(providers, p), p, tracker) for p in PROVIDERS})


# ------------------------------------------------------------------------------------------------- watchdog
def sd_notify(message, *, environ=None):
    """Send ``message`` to $NOTIFY_SOCKET (stdlib only). True if sent; a missing or broken socket is a silent False."""
    environ = os.environ if environ is None else environ
    path = environ.get('NOTIFY_SOCKET')
    if not path or path[0] not in '/@':
        return False
    address = '\0' + path[1:] if path[0] == '@' else path
    try:
        with contextlib.closing(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | getattr(socket, "SOCK_CLOEXEC", 0))) as s:
            s.settimeout(1.0)
            s.connect(address)
            s.sendall(message.encode())
        return True
    except OSError:
        return False


def watchdog_interval(environ=None):
    """Seconds between pings from systemd's WATCHDOG_USEC (half the timeout, 1..60 s); None when no watchdog is armed."""
    environ = os.environ if environ is None else environ
    usec = environ.get('WATCHDOG_USEC')
    pid = environ.get('WATCHDOG_PID')
    if not usec or not usec.isdecimal() or int(usec) <= 0 or (pid and pid.isdecimal() and int(pid) != os.getpid()):
        return None
    return min(60.0, max(1.0, int(usec) / 2e6))


class Heartbeats:
    def __init__(self, clock, names=('candidates', 'positions')):
        self.clock = clock
        self._lock = threading.Lock()
        started = clock()
        self.last = {name: started for name in names}      # startup grace: fresh until stale_s after start

    def beat(self, name):
        with self._lock:
            self.last[name] = self.clock()

    def ages(self):
        now = self.clock()
        with self._lock:
            return {name: round(now - at, 3) for name, at in self.last.items()}


class Watchdog:
    def __init__(self, heartbeats, *, stale_s, notify=sd_notify, on_stall=None, environ=None):
        self.heartbeats, self.stale_s, self.notify, self.on_stall = heartbeats, stale_s, notify, on_stall
        self.interval = watchdog_interval(environ)
        self.status, self.pings, self.stalled = 'DISARMED' if self.interval is None else 'OK', 0, set()

    def check(self):
        """One watchdog decision: ping iff every loop heartbeat is fresh. Returns True when pinged."""
        ages = self.heartbeats.ages()
        stale = {name for name, age in ages.items() if age > self.stale_s}
        if stale:
            self.status = 'STALLED'
            fresh_stall = stale - self.stalled
            self.stalled |= stale
            if fresh_stall and self.on_stall is not None:
                try:
                    self.on_stall(sorted(fresh_stall), ages)
                except Exception:
                    pass
            return False                         # no ping: systemd restarts the service after WatchdogSec
        self.stalled.clear()
        self.status = 'OK' if self.interval is not None else 'DISARMED'
        if self.interval is not None and self.notify('WATCHDOG=1'):
            self.pings += 1
            return True
        return False

    def health(self):
        return {'status': self.status, 'interval_s': self.interval, 'pings': self.pings, 'stale_after_s': self.stale_s,
                'heartbeat_age_s': self.heartbeats.ages(), 'stalled_loops': sorted(self.stalled)}


# ------------------------------------------------------------------------------------------------ container
class Ops:
    """What the runner talks to. ``beat`` / ``health`` / ``start`` are the only runner-facing calls."""

    def __init__(self, cfg, *, clock=time.time, notify=sd_notify, environ=None):
        self.cfg, self.clock = cfg, clock
        if notify is sd_notify:                   # bind the systemd variables once: the watchdog must not depend on later env changes
            frozen = {k: v for k, v in (os.environ if environ is None else environ).items() if k in ('NOTIFY_SOCKET', 'WATCHDOG_USEC', 'WATCHDOG_PID')}
            environ = frozen
            notify = lambda message, _env=frozen: sd_notify(message, environ=_env)
        self.credits = CreditTracker(costs=cfg['credit_costs'], budget=cfg['credit_budget_month'], clock=clock)
        self.heartbeats = Heartbeats(clock)
        self.watchdog = Watchdog(self.heartbeats, stale_s=cfg['watchdog_stale_s'], notify=notify, environ=environ)
        self.regime = None
        self.runner = None
        self._last_flush = None
        self.lane_clock = {}                      # LINT1: {clock, sleep} of the transports (tests); {} = the process defaults

    # runner-facing
    def beat(self, name):
        self.heartbeats.beat(name)

    def health(self):
        return {'ops': {'credits': self.credits.health(), 'watchdog': self.watchdog.health(),
                        'regime': None if self.regime is None else self.regime.last}}

    def meter(self, providers):
        """LINT1: counting moved to the transport (``providers.CALL_OBSERVERS``) so every lane is counted, the shared LOW lane
        (paths, held-risk probes, wallet signals) included; wrapping here as well would count main/exit calls twice."""
        if not self.cfg.get('watchdog_only'):
            from lean import providers as _providers
            _providers.CALL_OBSERVERS.add(self.credits)
        return providers

    def shed_low_lane(self):
        """LINT1: over budget, shed the SHARED Helius low lane (every data-collection user at once); one CREDIT_SHED row per
        episode (``allow('low')``). Screening and exits are never touched. Re-applied every housekeeping tick while shedding."""
        if self.cfg.get('watchdog_only') or self.credits.allow('low'):
            return False
        from lean import providers as _providers
        _providers.shed_low('helius', 2 * self.cfg['housekeeping_s'], **self.lane_clock)
        return True

    def attach(self, runner):
        """Bind to the runner's store: restore this month's usage, hook CREDIT_SHED/WATCHDOG_STALL rows, build the regime log."""
        from lean.regime import RegimeLog
        self.runner = runner
        store = runner.store
        latest = None if self.cfg.get('watchdog_only') else store.latest_event(FLUSH_KIND)
        if latest:
            self.credits._restore(latest[1])
        self.credits.on_shed = lambda lane: self._error('CREDIT_SHED', 'credits', lane)
        self.watchdog.on_stall = lambda loops, ages: self._error('WATCHDOG_STALL', 'watchdog', ','.join(loops))
        if not self.cfg.get('watchdog_only'):
            self.regime = RegimeLog(store, interval_s=self.cfg['regime_interval_s'], clock=self.clock)
        self._last_flush = self.clock()

    def _error(self, code, scope, message):
        try:
            self.runner.store.add_error(code, transient=True, scope=scope, message=message, ts=self.clock())
        except Exception:
            pass

    def flush(self, force=False):
        """Persist the credit snapshot (at most every ``credit_flush_s`` unless forced)."""
        now = self.clock()
        if self.cfg.get('watchdog_only'):
            return False
        if not force and (not self.credits.dirty or now - (self._last_flush or 0) < self.cfg['credit_flush_s']):
            return False
        snapshot = self.credits.snapshot()
        try:
            self.runner.store.record(FLUSH_KIND, snapshot, code_version=self.runner.code_version,
                                     strategy_version=self.runner.strategy_version, ts=now)
        except Exception:
            return False
        self.credits.dirty, self._last_flush = False, now
        return True

    def housekeeping_once(self):
        """Regime sample when due, credit flush, shedding status refresh. Never raises (a feature, not a gate)."""
        try:
            if self.regime is not None and self.regime.due():
                self.regime.sample(self.runner._sol_usd_value)
            self.credits.status()
            self.shed_low_lane()                 # LINT1
            if self.watchdog.interval is None:
                self.watchdog.check()            # no systemd watchdog: still keep the health status and the stall row
            self.flush()
        except Exception:
            pass

    def start(self, runner):
        """Threads for ``Runner.run``: the watchdog and the housekeeping loop. Both stop with ``runner.stop``."""
        threads = []

        def housekeeping():
            while not runner.stop.is_set():
                self.housekeeping_once()
                runner.stop.wait(self.cfg['housekeeping_s'])
            self.flush(force=True)

        threads.append(threading.Thread(target=housekeeping, name='ops-housekeeping'))
        if self.watchdog.interval is not None:
            def watch():
                while not runner.stop.is_set():
                    self.watchdog.check()
                    runner.stop.wait(self.watchdog.interval)
                self.watchdog.notify('STOPPING=1')
            threads.append(threading.Thread(target=watch, name='ops-watchdog'))
        for t in threads:
            t.start()
        return threads


# --------------------------------------------------------------------------------------------- morning report
KST = datetime.timezone(datetime.timedelta(hours=9))


def report_date(now):
    """The file date: the KST calendar date of the run (23:00 UTC is 08:00 KST of the next day)."""
    return datetime.datetime.fromtimestamp(now, KST).strftime('%Y-%m-%d')


def _replay_section(db, out_dir, date, now, grid):
    """Optional L08 replay/tuning page: only when ``lean.tune`` is importable AND a grid file is given. Never raises."""
    if not grid:
        return None, 'not configured (no --grid)'
    try:
        import importlib
        tune = importlib.import_module('lean.tune')
    except ImportError:
        return None, 'lean.tune (L08) is not installed'
    try:
        with tempfile.TemporaryDirectory(prefix='lean-replay-', dir=str(out_dir)) as scratch:
            buffer = _Capture()
            with contextlib.redirect_stdout(buffer):
                code = tune.main(['--db', str(db), '--grid', str(grid), '--out-dir', scratch, '--calibrate', '--now', str(now)])
            if code != 0:
                return None, 'replay report failed (rc=%s)' % code
            produced = sorted(Path(scratch).glob('*.html'))
            if not produced:
                return None, 'replay report produced no page'
            target = Path(out_dir) / ('%s-replay.html' % date)
            write_exclusive(target, produced[-1].read_text(encoding='utf-8'))
            return target.name, 'ok'
    except FileExistsError:
        return '%s-replay.html' % date, 'already written'
    except Exception as error:
        return None, 'replay report error: %s' % type(error).__name__


class _Capture:
    def __init__(self):
        self.parts = []

    def write(self, text):
        self.parts.append(text)
        return len(text)

    def flush(self):
        pass


def write_exclusive(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(text)


def morning_report(db, out_dir, *, now=None, grid=None, counterfactual_db=None, horizon=3600):
    """Write ``<out_dir>/YYYY-MM-DD.html`` (+ ``.json``) from the L06 report; idempotent per day (an existing page is kept)."""
    from lean import report
    now = time.time() if now is None else now
    out_dir = Path(out_dir)
    if out_dir.is_symlink():
        raise OpsError('out dir must not be a symlink')
    out_dir.mkdir(mode=0o700, exist_ok=True)
    if out_dir.is_symlink() or not out_dir.is_dir():
        raise OpsError('out dir invalid')
    date = report_date(now)
    page, data = out_dir / (date + '.html'), out_dir / (date + '.json')
    if page.exists():
        return {'status': 'EXISTS', 'html': str(page)}
    summary = report.build(db, counterfactual_db=counterfactual_db, horizon=horizon, now=now)
    replay_name, replay_note = _replay_section(db, out_dir, date, now, grid)
    summary['morning'] = {'date_kst': date, 'replay_page': replay_name, 'replay_status': replay_note}
    html = report.render_html(summary)
    link = ('<p><a href="%s">Replay and tuning report (L08)</a></p>' % replay_name) if replay_name else \
        ('<p>Replay and tuning report: %s.</p>' % report.e(replay_note))
    html = html.replace('</main>', link + '</main>', 1)
    write_exclusive(data, json.dumps(summary, sort_keys=True, allow_nan=False, default=str) + '\n')
    try:
        write_exclusive(page, html)
    except BaseException:
        os.unlink(data)
        raise
    return {'status': 'OK', 'html': str(page), 'json': str(data), 'replay': replay_note}


def main(argv=None):
    p = argparse.ArgumentParser(description='Lean operations tools (paper only)')
    sub = p.add_subparsers(dest='command', required=True)
    r = sub.add_parser('report', help='morning report: <out-dir>/YYYY-MM-DD.html from the L06 report (+ L08 replay when configured)')
    r.add_argument('--db', required=True, help='lean.sqlite (opened read-only)')
    r.add_argument('--out-dir', required=True)
    r.add_argument('--grid', help='L08 tuning grid JSON; with lean.tune present a replay page is added')
    r.add_argument('--counterfactual', help='optional T26 counterfactual.sqlite')
    r.add_argument('--now', type=float, help='epoch seconds (default: the clock)')
    a = p.parse_args(argv)
    try:
        result = morning_report(a.db, a.out_dir, now=a.now, grid=a.grid, counterfactual_db=a.counterfactual)
    except Exception as error:
        print(json.dumps({'status': 'ERROR', 'code': type(error).__name__, 'detail': str(error)[:200]}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
