"""Read-only 24/7 health check for the fresh-start paper desk.

Opens every database ``mode=ro`` and never writes to any of them. It prints one JSON line
and exits 2 when any check is CRITICAL (0 otherwise), so a timer unit shows as failed.
Optional ``--out`` atomically writes the same report for ``tools.ops.notify``. No network,
no provider I/O, no signing.

Layout read (matches ``tools.ops.fresh_start``): <root>/research.sqlite, evidence.sqlite,
paper-ledger.sqlite, entry-dispatch/dispatch.sqlite; shared pacing and discovery databases
are passed explicitly.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

OK, WARN, CRITICAL = 'OK', 'WARN', 'CRITICAL'
RANK = {OK: 0, WARN: 1, CRITICAL: 2}

# kind: service = long running (must stay active); oneshot = run by its timer; timer.
# manual: documented as enabled by the coordinator only (never required at boot).
UNITS = {
    'desk-continuous-discovery.service': {'kind': 'service'},
    'desk-dashboard.service': {'kind': 'service'},
    'desk-paper-entry-dispatcher.service': {'kind': 'oneshot', 'critical_failed': True},
    'desk-paper-held-cycle.service': {'kind': 'oneshot', 'critical_failed': True},
    'desk-paper-monitor.service': {'kind': 'oneshot'},
    'desk-decisions.service': {'kind': 'oneshot'},
    'desk-backup.service': {'kind': 'oneshot'},
    'desk-paper-entry-dispatcher.timer': {'kind': 'timer', 'manual': True, 'tick': ('entry_tick', 'desk-paper-entry-dispatcher.service')},
    'desk-paper-held-cycle.timer': {'kind': 'timer', 'tick': ('held_tick', 'desk-paper-held-cycle.service')},
    'desk-paper-monitor.timer': {'kind': 'timer', 'manual': True},
    'desk-decisions.timer': {'kind': 'timer', 'tick': ('decisions_tick', 'desk-decisions.service')},
    'desk-backup.timer': {'kind': 'timer'},
}
# (warn, critical) seconds / counts / fractions; override with --threshold name=value.
DEFAULTS = {
    'restarts_warn': 3, 'restarts_critical': 10,
    'discovery_frame_warn': 120, 'discovery_frame_critical': 300,
    'discovery_event_warn': 1800, 'discovery_event_critical': 7200,
    'entry_tick_warn': 300, 'entry_tick_critical': 900,
    'held_tick_warn': 600, 'held_tick_critical': 1800,
    'decisions_tick_warn': 180, 'decisions_tick_critical': 600,
    'mark_age_warn': 360, 'mark_age_critical': 900,
    'monitoring_headroom_warn': 0.20, 'monitoring_headroom_critical': 0.05,
    'monitoring_cap': 3600,
    'pacing_pending_stale': 300,
    'disk_free_fraction_warn': 0.20, 'disk_free_fraction_critical': 0.10,
    'disk_free_bytes_critical': 1 << 30,
    'backup_age_warn': 30 * 3600, 'backup_age_critical': 54 * 3600,
    'max_hold_seconds': 21600,
    'research_cpu_fraction': 0.9, 'research_cpu_consecutive': 3,
    'rotation_warn_fraction': 0.8,
}
# Research/optional units that carry a CPUQuota (T39). Optional: a unit that is not installed is skipped.
RESEARCH_UNITS = ('desk-counterfactual.service', 'desk-fill-realism-worker.service')
RESEARCH_PROPS = ('ActiveState', 'CPUUsageNSec', 'CPUQuotaPerSecUSec', 'ActiveEnterTimestamp')
SYSTEMCTL_PROPS = ('ActiveState', 'SubState', 'Result', 'ExecMainStatus', 'NRestarts', 'UnitFileState', 'LastTriggerUSec', 'ActiveEnterTimestamp')


def check(name, severity, detail, **extra):
    return dict({'check': name, 'severity': severity, 'detail': detail}, **extra)


def ro(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError('missing or non-regular database: %s' % path)
    from tools.research import funnel_report
    resolved = path.resolve()
    # the shared T02F rule: immutable only for a quiet WAL store (a plain ro open would create -shm/-wal beside it)
    flag = 'immutable=1' if funnel_report.open_mode(resolved) == 'immutable' else 'mode=ro'
    return sqlite3.connect(resolved.as_uri() + '?' + flag, uri=True, timeout=5)


def graded(name, value, warn, critical, detail, **extra):
    severity = CRITICAL if value >= critical else WARN if value >= warn else OK
    return check(name, severity, detail, value=value, **extra)


def guarded(name, function, *args):
    """A failing probe is itself a finding, never a silent pass."""
    try:
        return function(*args)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return [check(name, CRITICAL, 'probe failed: %s: %s' % (type(exc).__name__, str(exc)[:160]))]


def parse_systemd_time(text):
    text = (text or '').strip()
    if not text or text == 'n/a' or text.startswith('0'):
        return None
    if text.startswith('@'):
        return float(text[1:])
    parsed = datetime.datetime.strptime(text, '%a %Y-%m-%d %H:%M:%S UTC')
    return parsed.replace(tzinfo=datetime.timezone.utc).timestamp()


def default_runner(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)


def show(runner, unit, props=SYSTEMCTL_PROPS):
    result = runner(['systemctl', '--timestamp=utc', 'show', unit] + ['-p' + p for p in props])
    if result.returncode != 0:
        raise OSError('systemctl show %s failed' % unit)
    return dict(line.partition('=')[::2] for line in result.stdout.splitlines() if '=' in line)


def check_units(runner, now, t, units=None):
    out = []
    for unit, spec in (units or UNITS).items():
        try:
            s = show(runner, unit)
        except (OSError, subprocess.SubprocessError) as exc:
            out.append(check('unit:' + unit, CRITICAL, str(exc)))
            continue
        active, result = s.get('ActiveState', ''), s.get('Result', '')
        enabled = s.get('UnitFileState', '')
        name = 'unit:' + unit
        if spec['kind'] == 'timer':
            if active != 'active':
                severity = OK if spec.get('manual') and enabled != 'enabled' else CRITICAL
                out.append(check(name, severity, 'timer %s (%s)%s' % (active, enabled, ' manual, intentionally off' if severity == OK else '')))
                continue
            out.append(check(name, OK, 'timer active'))
            if 'tick' in spec:
                label = spec['tick'][0]
                out.append(tick_recency(label, s, now, t))
            continue
        if spec['kind'] == 'service':
            severity = OK if active in ('active', 'activating') else CRITICAL
            out.append(check(name, severity, 'ActiveState=%s SubState=%s Result=%s' % (active, s.get('SubState'), result)))
        else:
            failed = active == 'failed' or (result not in ('', 'success') and active != 'active')
            severity = (CRITICAL if spec.get('critical_failed') else WARN) if failed else OK
            out.append(check(name, severity, 'ActiveState=%s Result=%s' % (active, result), failed=failed))
        restarts = int(s.get('NRestarts') or 0)
        if restarts:
            if clean_exit(s):
                out.append(check('restarts:' + unit, OK, 'NRestarts=%d, last run exited cleanly (exit 0): not counted' % restarts, value=restarts))
            else:
                out.append(graded('restarts:' + unit, restarts, t['restarts_warn'], t['restarts_critical'],
                                  'NRestarts=%d after non-zero exit (Result=%s ExecMainStatus=%s)' % (restarts, result, s.get('ExecMainStatus'))))
    return out


_USEC = {'us': 1e-6, 'ms': 1e-3, 's': 1.0, 'min': 60.0}


def parse_quota(text):
    """CPUQuotaPerSecUSec as cores ('1s' = 1 core, '500ms' = half); None when unlimited or unparseable."""
    total, found = 0.0, False
    for number, unit in re.findall(r'(\d+(?:\.\d+)?)\s*(us|ms|min|s)', text or ''):
        total += float(number) * _USEC[unit]
        found = True
    return total if found and total > 0 else None


def _load_cpu_state(path):
    """(previous samples, refused). Missing, malformed or hostile content gives ({}, False): it is never trusted, the file is
    rewritten. A symlink or an unreadable file gives ({}, True): it is neither followed nor replaced."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            data = json.loads(stream.read(1 << 20))
    except FileNotFoundError:
        return {}, False
    except OSError:
        return {}, True
    except ValueError:
        return {}, False
    if not isinstance(data, dict):
        return {}, False
    clean = {}
    for unit, row in data.items():
        if (isinstance(row, dict) and isinstance(row.get('streak'), int) and not isinstance(row['streak'], bool)
                and 0 <= row['streak'] <= 1000 and isinstance(row.get('usage'), (int, float)) and row['usage'] >= 0
                and isinstance(row.get('ts'), (int, float)) and isinstance(row.get('run'), str)):
            clean[unit] = row
    return clean, False


def _save_cpu_state(path, state):
    out = Path(path)
    tmp = out.with_name('.' + out.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps(state, sort_keys=True) + '\n')
    os.replace(tmp, out)


def check_research_cpu(runner, now, t, state_path, units=RESEARCH_UNITS):
    """WARN when a research unit runs at (a fraction of) its CPUQuota for N consecutive checks.

    The sample is cores used: the CPUUsageNSec delta over the wall-clock gap since the previous check of the SAME run, or the
    run's lifetime average for a first sample or a new run (the counter restarts with every start of a oneshot). An inactive
    unit gives no sample: the streak is neither advanced nor reset. State is the only thing written (``--cpu-state``)."""
    previous, refused = _load_cpu_state(state_path)
    stored = dict(previous)
    out = []
    for unit in units:
        try:
            s = show(runner, unit, RESEARCH_PROPS)
        except (OSError, subprocess.SubprocessError):
            continue   # not installed: optional
        name = 'research_cpu:' + unit
        quota = parse_quota(s.get('CPUQuotaPerSecUSec'))
        if quota is None:
            out.append(check(name, WARN, 'no CPUQuota configured: this research unit can take every core from trading'))
            continue
        if s.get('ActiveState') != 'active':
            continue
        try:
            usage = int(s.get('CPUUsageNSec', '')) / 1e9
        except ValueError:
            continue
        if usage >= 2 ** 63 / 1e9:   # systemd reports UINT64_MAX when accounting is off
            continue
        run = s.get('ActiveEnterTimestamp', '')
        started = parse_systemd_time(run)
        prev = previous.get(unit)
        fraction = None
        if prev and prev['run'] == run and 0 < now - prev['ts'] <= 6 * 3600 and usage >= prev['usage']:
            fraction = (usage - prev['usage']) / (now - prev['ts'])
        elif started is not None and now - started >= 10:
            fraction = usage / (now - started)
        if fraction is None:
            stored[unit] = {'run': run, 'usage': usage, 'ts': now, 'streak': prev['streak'] if prev else 0}
            continue
        saturated = fraction + 1e-9 >= t['research_cpu_fraction'] * quota
        streak = (prev['streak'] + 1 if prev else 1) if saturated else 0
        stored[unit] = {'run': run, 'usage': usage, 'ts': now, 'streak': streak}
        need = int(t['research_cpu_consecutive'])
        if streak >= need:
            out.append(check(name, WARN, 'at %.0f%% of its %.2f-core CPUQuota for %d consecutive checks (%.2f cores)'
                             % (100 * fraction / quota, quota, streak, fraction), fraction=round(fraction, 4), quota=quota, streak=streak))
        else:
            out.append(check(name, OK, '%.2f cores of %.2f (streak %d/%d)' % (fraction, quota, streak, need),
                             fraction=round(fraction, 4), quota=quota, streak=streak))
    if refused:
        out.append(check('research_cpu_state', WARN, '%s is a symlink or unreadable: ignored and not rewritten' % state_path))
        return out
    try:
        if stored != previous or not previous:
            _save_cpu_state(state_path, stored)
    except OSError as exc:
        out.append(check('research_cpu_state', WARN, 'cannot write %s: %s' % (state_path, type(exc).__name__)))
    return out


def clean_exit(s):
    """True only when systemd positively reports the last run ended with exit 0.

    Restart=always units (discovery's daily --seconds 86400 exit) accumulate NRestarts
    with exit 0; those are expected. Anything unknown or non-zero counts as a failure.
    """
    status = (s.get('ExecMainStatus') or '').strip()
    return (status == '0' and (s.get('Result') or '') in ('', 'success')
            and s.get('SubState') not in ('auto-restart', 'failed'))


def tick_recency(label, show_result, now, t):
    try:
        last = parse_systemd_time(show_result.get('LastTriggerUSec'))
        entered = parse_systemd_time(show_result.get('ActiveEnterTimestamp'))
    except ValueError as exc:
        return check(label, WARN, 'unparseable systemd timestamp: %s' % exc)
    reference = last if last is not None else entered
    if reference is None:
        return check(label, WARN, 'timer has no trigger or activation time')
    age = max(0.0, now - reference)
    return graded(label, int(age), t[label + '_warn'], t[label + '_critical'],
                  'last trigger %ds ago%s' % (age, '' if last is not None else ' (never triggered; since activation)'))


def check_discovery(path, now, t):
    with closing(ro(path)) as c:
        frame = c.execute('SELECT max(at) FROM completions').fetchone()[0]
        event = c.execute('SELECT max(received_at) FROM raw_events').fetchone()[0]
    out = []
    if frame is None:
        out.append(check('discovery_frames', CRITICAL, 'no discovery completions recorded'))
    else:
        out.append(graded('discovery_frames', int(now - frame), t['discovery_frame_warn'], t['discovery_frame_critical'],
                          'latest listener completion %ds ago' % (now - frame)))
    if event is None:
        out.append(check('discovery_events', WARN, 'no discovery events recorded yet'))
    else:
        out.append(graded('discovery_events', int(now - event), t['discovery_event_warn'], t['discovery_event_critical'],
                          'latest migration event %ds ago' % (now - event)))
    return out


def read_state(ledger):
    with closing(ro(ledger)) as c:
        row = c.execute('SELECT payload FROM state WHERE id=1').fetchone()
        events = c.execute('SELECT count(*) FROM events').fetchone()[0]
        fills = []
        for (payload,) in c.execute("SELECT payload FROM outcomes ORDER BY seq"):
            o = json.loads(payload)
            if o.get('type') == 'fill':
                fills.append(o)
    return (json.loads(row[0]) if row else None), events, fills


def check_ledger(ledger, now, t, cfg):
    state, events, fills = read_state(ledger)
    if state is None:
        if events:
            return [check('ledger', CRITICAL, 'checkpoint missing but events exist; recovery required')]
        return [check('ledger', OK, 'empty ledger, no checkpoint yet')]
    out = []
    positions = state.get('positions', {})
    n = len(positions)
    blocked = {m: p.get('exit_blocked') for m, p in positions.items() if p.get('exit_blocked')}
    mode = state.get('mode')
    if mode == 'EXIT_ONLY' or blocked:
        out.append(check('exits', CRITICAL if blocked else WARN,
                         'mode=%s; %d position(s) with unresolved exit: %s' % (mode, len(blocked), sorted(set(blocked.values()))),
                         positions=sorted(blocked), open_positions=n))
    elif mode == 'ENTRY_PAUSED':
        out.append(check('mode', WARN, 'ENTRY_PAUSED; no new entries until resumed', open_positions=n))
    elif mode != 'RUNNING':
        out.append(check('mode', CRITICAL, 'unexpected mode %r' % (mode,), open_positions=n))
    else:
        out.append(check('mode', OK, 'RUNNING, %d open position(s)' % n, open_positions=n))
    for mint, p in positions.items():
        age = now - float(p.get('mark_at', 0))
        out.append(graded('mark_age', int(age), t['mark_age_warn'], t['mark_age_critical'],
                          'position %s last mark %ds ago (price TTL %ss; marks are only refreshed by held passes)'
                          % (mint[:8], age, cfg.get('price_ttl_seconds', 'unknown')), mint=mint))
        held = now - float(p.get('opened_at', now))
        if held > t['max_hold_seconds']:
            out.append(check('max_hold', CRITICAL, 'position %s held %ds, over max hold %ds' % (mint[:8], held, t['max_hold_seconds']), mint=mint))
    return out


def check_evidence(evidence, now, t):
    out = []
    with closing(ro(evidence)) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'paper_observation_passes' in tables:
            null_ids = [r[0] for r in c.execute('SELECT id FROM paper_observation_passes WHERE outcome_hash IS NULL LIMIT 20')]
            out.append(check('observation_passes', CRITICAL if null_ids else OK,
                             'NULL-outcome passes latch the global terminal gate: %s' % null_ids if null_ids else 'no unresolved passes',
                             null_pass_ids=null_ids))
        if 'paper_monitoring_budget' not in tables:
            out.append(check('monitoring_budget', WARN, 'monitoring budget not provisioned'))
            return out
        cap, window, blocked = c.execute('SELECT cap,window_seconds,blocked FROM paper_monitoring_budget WHERE id=1').fetchone()
        used = c.execute('SELECT count(*) FROM paper_monitoring_reservations WHERE at>?', (now - window,)).fetchone()[0]
        pending = c.execute('SELECT count(*) FROM paper_monitoring_reservations r LEFT JOIN paper_monitoring_outcomes o '
                            'ON o.reservation_id=r.id WHERE o.reservation_id IS NULL').fetchone()[0]
    effective = max(cap, t['monitoring_cap'])
    headroom = 1 - used / effective
    severity = CRITICAL if headroom < t['monitoring_headroom_critical'] else WARN if headroom < t['monitoring_headroom_warn'] else OK
    out.append(check('monitoring_budget', severity, '%d/%d requests in last %ds (table cap %d)' % (used, effective, window, cap),
                     used=used, cap=effective, headroom=round(headroom, 4)))
    if blocked:
        out.append(check('monitoring_latch', CRITICAL, 'monitoring blocked: %s' % blocked))
    if pending:
        out.append(check('monitoring_pending', WARN, '%d monitoring reservation(s) without an outcome' % pending, pending=pending))
    return out


# T24S item 3: every 80 % rotation warning the desk can raise, surfaced as WARN. (table, rows ceiling) - the ceilings are the desk's own
# constants (imported when the desk package is importable, these literals otherwise): rejection cap 512, no-entry table 8192, original
# passes / closure rows 10000, evidence pages 100000 and the store's 256 MiB write budget.
CAPACITY = (
    ('paper_history_preparation_rejections', 'history_preparation_rejection', 'MAX_REJECTIONS', 512),
    ('paper_cycle_no_entry', 'paper_cycle_no_entry', 'MAX_ROWS', 8192),
    ('paper_pass_closures', 'paper_pass_closure', 'PASS_CEILING', 10000),
    ('paper_observation_passes', 'paper_pass_closure', 'PASS_CEILING', 10000),
    ('paper_pass_inventory', 'pass_inventory', 'MAX_ROWS', 16384),
)
EVIDENCE_PAGE_LIMIT, EVIDENCE_BYTE_LIMIT = 100_000, 256 * 1024 * 1024
REFUSAL_SUFFIX, REFUSAL_MAX_BYTES = '.publish-refused.jsonl', 1024 * 1024


def _ceiling(module, name, fallback):
    try:
        import importlib
        value = getattr(importlib.import_module('desk.' + module), name)
        return value if type(value) is int and value > 0 else fallback
    except (ImportError, AttributeError):
        return fallback


def read_refusal_log(path):
    """Bounded read of ``<evidence>.publish-refused.jsonl`` without following a symlink: (rows, malformed, size) or None."""
    try:
        fd = os.open(str(path) + REFUSAL_SUFFIX, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat_is_regular(info.st_mode):
            raise OSError('refusal log is not a regular file')
        raw = os.read(fd, REFUSAL_MAX_BYTES + 1)
    finally:
        os.close(fd)
    rows, malformed = [], 0
    for line in raw.splitlines():
        try:
            row = json.loads(line)
            if type(row) is not dict or row.get('kind') not in ('publish_refused_v1', 'rotation_warning_v1'):
                raise ValueError
            rows.append(row)
        except ValueError:
            malformed += 1
    return rows, malformed, info.st_size


def stat_is_regular(mode):
    import stat
    return stat.S_ISREG(mode)


def check_capacity(evidence, now, t):
    """WARN at ``rotation_warn_fraction`` of every capacity the desk rotates on, and for every publish-refused / rotation row."""
    out = []
    fraction = t['rotation_warn_fraction']
    with closing(ro(evidence)) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, module, name, fallback in CAPACITY:
            if table not in tables:
                continue
            limit = _ceiling(module, name, fallback)
            rows = c.execute('SELECT count(*) FROM "%s"' % table).fetchone()[0]
            severity = WARN if rows >= int(limit * fraction) else OK
            out.append(check('capacity_' + table, severity,
                             '%d of %d rows (%.0f%%)%s' % (rows, limit, 100.0 * rows / limit,
                                                        '; rotate the store set while flat (RUNBOOK "Rotate when flat")' if severity == WARN else ''),
                             rows=rows, limit=limit))
        pages = c.execute('SELECT count(*) FROM pages').fetchone()[0] if 'pages' in tables else 0
        size = c.execute('PRAGMA page_count').fetchone()[0] * c.execute('PRAGMA page_size').fetchone()[0]
    out.append(check('capacity_evidence_pages', WARN if pages >= int(EVIDENCE_PAGE_LIMIT * fraction) else OK,
                     '%d of %d evidence pages' % (pages, EVIDENCE_PAGE_LIMIT), rows=pages, limit=EVIDENCE_PAGE_LIMIT))
    out.append(check('capacity_evidence_bytes', WARN if size >= int(EVIDENCE_BYTE_LIMIT * fraction) else OK,
                     '%d of %d evidence store bytes' % (size, EVIDENCE_BYTE_LIMIT), bytes=size, limit=EVIDENCE_BYTE_LIMIT))
    log = read_refusal_log(evidence)
    rows, malformed, log_size = log if log is not None else ([], 0, 0)
    refused = [r for r in rows if r.get('kind') == 'publish_refused_v1']
    warned = sorted({str(r.get('table')) for r in rows if r.get('kind') == 'rotation_warning_v1'})
    if refused or malformed:
        last = refused[-1] if refused else {}
        out.append(check('publish_refused', WARN, '%d refused no-entry publication(s)%s%s' % (
            len(refused), ', last %s/%s' % (last.get('blocker'), last.get('reason')) if refused else '',
            ', %d malformed line(s)' % malformed if malformed else ''), count=len(refused), malformed=malformed,
            last_pass=last.get('pass_id'), last_blocker=last.get('blocker')))
    else:
        out.append(check('publish_refused', OK, 'no refused publications'))
    out.append(check('rotation_warnings', WARN if warned else OK,
                     'rotation warning(s) logged for: %s' % ', '.join(warned) if warned else 'no rotation warnings logged', tables=warned))
    out.append(check('publish_refused_log_size', WARN if log_size >= int(REFUSAL_MAX_BYTES * fraction) else OK,
                     '%d of %d bytes (logging stops at the limit)' % (log_size, REFUSAL_MAX_BYTES), bytes=log_size))
    return out


def check_pacing(path, now, t):
    out = []
    with closing(ro(path)) as c:
        rows = c.execute('SELECT provider,next_at,blocked_until,high_water,pending FROM state').fetchall()
        waiters = c.execute('SELECT count(*) FROM waiters WHERE expires>?', (now,)).fetchone()[0]
    for provider, next_at, blocked_until, high_water, pending in rows:
        if blocked_until > now:
            out.append(check('pacing:' + provider, CRITICAL, 'blocked for another %ds' % (blocked_until - now)))
        elif pending and now - next_at > t['pacing_pending_stale']:
            out.append(check('pacing:' + provider, CRITICAL, 'slot pending %ds, likely orphaned by a killed process' % (now - next_at)))
        else:
            out.append(check('pacing:' + provider, OK, 'clear (waiters=%d)' % waiters))
    return out or [check('pacing', CRITICAL, 'no provider pacing state')]


def check_disk(path, t, usage=shutil.disk_usage):
    u = usage(path)
    fraction = u.free / u.total
    severity = CRITICAL if fraction < t['disk_free_fraction_critical'] or u.free < t['disk_free_bytes_critical'] \
        else WARN if fraction < t['disk_free_fraction_warn'] else OK
    return [check('disk', severity, '%.1f%% free (%d MiB)' % (fraction * 100, u.free >> 20), free_bytes=u.free)]


def check_backup(root, now, t):
    root = Path(root)
    newest = None
    for child in root.iterdir() if root.is_dir() else ():
        if child.is_symlink() or not child.is_dir():
            continue
        stamp = child.stat().st_mtime
        manifest = child / 'manifest.json'
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text())
                created = datetime.datetime.fromisoformat(data.get('created_utc') or data['created_at'])
                stamp = (created if created.tzinfo else created.replace(tzinfo=datetime.timezone.utc)).timestamp()
            except (ValueError, TypeError, KeyError, OSError):
                pass
        newest = stamp if newest is None else max(newest, stamp)
    if newest is None:
        return [check('backup', CRITICAL, 'no backup found under %s' % root)]
    return [graded('backup', int(now - newest), t['backup_age_warn'], t['backup_age_critical'], 'newest backup %dh old' % ((now - newest) // 3600))]


def build_report(args, runner=default_runner, clock=time.time, usage=shutil.disk_usage):
    try:
        return _build_report(args, runner, clock, usage)
    except sqlite3.Error as exc:
        return {'kind': 'desk_healthcheck_v1', 'ts': int(clock()), 'status': CRITICAL, 'read_only': True,
                'execution_status': 'EXECUTION_UNVERIFIED',
                'checks': [check('healthcheck', CRITICAL, 'database error: %s: %s' % (type(exc).__name__, str(exc)[:160]))]}


def _build_report(args, runner, clock, usage):
    now = clock()
    t = dict(DEFAULTS)
    for item in args.threshold:
        key, _, value = item.partition('=')
        if key not in t:
            raise ValueError('unknown threshold %r' % key)
        t[key] = float(value) if '.' in value else int(value)
    root = Path(args.root)
    cfg = json.loads(Path(args.config).read_text()) if args.config else {}
    if 'max_hold_seconds' in cfg and 'max_hold_seconds' not in {i.partition('=')[0] for i in args.threshold}:
        t['max_hold_seconds'] = int(cfg['max_hold_seconds'])
    checks = []
    if not args.no_systemd:
        checks += guarded('units', check_units, runner, now, t)
        if args.cpu_state:
            checks += guarded('research_cpu', check_research_cpu, runner, now, t, args.cpu_state)
    checks += guarded('discovery', check_discovery, args.discovery_db, now, t)
    checks += guarded('ledger', check_ledger, root / 'paper-ledger.sqlite', now, t, cfg)
    checks += guarded('evidence', check_evidence, root / 'evidence.sqlite', now, t)
    checks += guarded('capacity', check_capacity, root / 'evidence.sqlite', now, t)
    checks += guarded('pacing', check_pacing, args.pacing_db, now, t)
    checks += guarded('disk', check_disk, root, t, usage)
    if args.backup_root:
        checks += guarded('backup', check_backup, args.backup_root, now, t)
    escalate_held_failure(checks)
    status = max((c['severity'] for c in checks), key=RANK.get, default=OK)
    return {'kind': 'desk_healthcheck_v1', 'ts': int(now), 'status': status, 'read_only': True,
            'execution_status': 'EXECUTION_UNVERIFIED', 'checks': checks}


def escalate_held_failure(checks):
    """A failing held pass is only urgent when a position is open (no sell can happen)."""
    if any(c.get('open_positions') for c in checks):
        return
    for c in checks:
        if c['check'] == 'unit:desk-paper-held-cycle.service' and c.get('failed'):
            c['severity'] = WARN


def main(argv=None, runner=default_runner, clock=time.time, usage=shutil.disk_usage):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True, help='fresh-start store root')
    p.add_argument('--discovery-db', required=True)
    p.add_argument('--pacing-db', required=True)
    p.add_argument('--backup-root')
    p.add_argument('--config', help='experiment config (price TTL, max hold)')
    p.add_argument('--out', help='also write the report here (atomic, 0600)')
    p.add_argument('--no-systemd', action='store_true')
    p.add_argument('--cpu-state', help='file remembering research-unit CPU samples between checks (enables the research_cpu check)')
    p.add_argument('--threshold', action='append', default=[], metavar='NAME=VALUE')
    args = p.parse_args(argv)
    try:
        report = build_report(args, runner, clock, usage)
    except (OSError, ValueError, sqlite3.Error) as exc:
        report = {'kind': 'desk_healthcheck_v1', 'ts': int(clock()), 'status': CRITICAL,
                  'checks': [check('healthcheck', CRITICAL, 'could not run: %s' % str(exc)[:200])]}
    line = json.dumps(report, sort_keys=True, separators=(',', ':'))
    print(line)
    if args.out:
        out = Path(args.out)
        tmp = out.with_name('.' + out.name + '.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(line + '\n')
        os.replace(tmp, out)
    return 2 if report['status'] == CRITICAL else 0


if __name__ == '__main__':
    sys.exit(main())
