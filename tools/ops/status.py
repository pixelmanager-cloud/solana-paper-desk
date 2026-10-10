"""Read-only production status report (paper only; no network, no writes).

Every SQLite file is opened ``mode=ro`` with ``query_only`` after a canonical
path check (absolute, no symlink component, regular file, single link). Any
section that cannot be read fails closed: it is reported as an error, adds a
blocker and makes the process exit 2. Nothing is written except stdout.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from desk.model import canonical, digest  # noqa: E402

BOUND = 50
DASHBOARD_PORT = '8765'
LOOPBACK = ('127.0.0.1', '[::1]', '::1')
EXECUTION_LABEL = 'EXECUTION_UNVERIFIED'
RELEASE_PROBE = ("import sys,os;sys.path.insert(0,os.getcwd());"
                 "from desk.runtime_compatibility import implementation_hash;print(implementation_hash())")


class StatusError(Exception):
    pass


def canonical_path(value, *, directory=False):
    """Reject relative, symlinked/aliased, non-regular or multiply linked paths."""
    p = Path(value)
    if not p.is_absolute():
        raise StatusError('PATH_NOT_ABSOLUTE')
    if os.path.realpath(p) != os.path.normpath(p):
        raise StatusError('PATH_NOT_CANONICAL')
    info = os.lstat(p)
    import stat
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise StatusError('PATH_NOT_DIRECTORY')
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise StatusError('PATH_NOT_SINGLE_REGULAR_FILE')
    return p


def connect_ro(path):
    p = canonical_path(path)
    # A WAL database opened mode=ro with no -wal/-shm present makes SQLite create
    # them, possibly owned by this user and unusable by the desk service user.
    # With no sidecars there is no unflushed WAL to miss, so use immutable=1
    # (no sidecars, no locks). With sidecars present, mode=ro reads the live WAL.
    sidecars = any(Path(str(p) + suffix).exists() for suffix in ('-wal', '-shm', '-journal'))
    c = sqlite3.connect(p.as_uri() + ('?mode=ro' if sidecars else '?immutable=1'), uri=True, timeout=2)
    c.execute('PRAGMA query_only=1')
    return c


def _tables(c):
    return [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _has(c, name):
    return name in _tables(c)


def _json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def release_section(release_dir, runner):
    if release_dir is None:
        return {'status': 'SKIPPED'}
    d = canonical_path(release_dir, directory=True)
    out = runner([sys.executable, '-I', '-c', RELEASE_PROBE], str(d))
    digest_ = out.strip()
    if len(digest_) != 64 or any(ch not in '0123456789abcdef' for ch in digest_):
        raise StatusError('RELEASE_DIGEST_INVALID')
    return {'status': 'OK', 'release_dir': str(d), 'runtime_digest': digest_}


def default_runner(argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120, check=True,
                          env={'PATH': os.defpath}).stdout


def _fill_label(payload):
    if payload.get('execution_status') is not None:
        return payload['execution_status']
    return EXECUTION_LABEL if EXECUTION_LABEL in json.dumps(payload) else None


def ledger_section(ledger_path, config_path, release):
    with closing(connect_ro(ledger_path)) as c:
        counts = {t: c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in _tables(c)}
        meta = dict(c.execute('SELECT key,value FROM metadata')) if _has(c, 'metadata') else {}
        row = c.execute('SELECT payload FROM state WHERE id=1').fetchone() if _has(c, 'state') else None
        state = _json(row[0]) if row else None
        fills = []
        for seq, event_id, payload in c.execute('SELECT seq,event_id,payload FROM outcomes ORDER BY seq'):
            value = _json(payload)
            if isinstance(value, list):
                items = value
            else:
                items = [value]
            for item in items:
                if isinstance(item, dict) and item.get('type') == 'fill':
                    fills.append({'seq': seq, 'event_id': event_id, 'side': item.get('side'),
                                  'mint': item.get('mint'), 'reason': item.get('reason'),
                                  'amount_sol': item.get('amount_sol'), 'quantity': item.get('quantity'),
                                  'fee_sol': item.get('fee_sol'), 'provenance': item.get('provenance'),
                                  'execution_label': _fill_label(item)})
        max_seq = {t: c.execute(f'SELECT MAX(seq) FROM {t}').fetchone()[0] for t in ('events', 'outcomes') if t in counts}
    positions = []
    if state:
        for mint, p in sorted(state.get('positions', {}).items()):
            initial_qty = p.get('initial_qty')
            positions.append({'mint': mint, 'qty': p.get('qty'), 'initial_qty': initial_qty,
                              'opened_at': p.get('opened_at'), 'initial_cost_sol': p.get('initial_cost'),
                              'cost_left_sol': p.get('cost_left'), 'exit_blocked': p.get('exit_blocked'),
                              'mark_status': p.get('mark_status')})
    raw = Path(canonical_path(config_path)).read_bytes()
    parsed = json.loads(raw)
    file_digest = digest(parsed)
    stored_config = _json(meta.get('config'))
    config = {'file_sha256': hashlib.sha256(raw).hexdigest(), 'file_canonical_digest': file_digest,
              'stored_config_hash': meta.get('config_hash'),
              'file_equals_stored_config': stored_config == parsed if stored_config is not None else None,
              'digest_matches_stored_hash': file_digest == meta.get('config_hash') if meta.get('config_hash') else None}
    return {'status': 'OK', 'table_counts': counts, 'max_seq': max_seq,
            'events': counts.get('events'), 'outcomes': counts.get('outcomes'),
            'state': None if state is None else {k: state.get(k) for k in ('cash', 'realized_pnl', 'mode', 'last_ts')},
            'positions': positions, 'fills': fills,
            'metadata': {'config_hash': meta.get('config_hash'), 'implementation_hash': meta.get('implementation_hash')},
            'config': config,
            'implementation_matches_release': (None if not release or release.get('status') != 'OK'
                                               else meta.get('implementation_hash') == release['runtime_digest'])}


def passes_section(evidence_path):
    with closing(connect_ro(evidence_path)) as c:
        if not _has(c, 'paper_observation_passes'):
            return {'status': 'OK', 'table_present': False, 'null': 0, 'resolved': 0, 'null_ids': []}
        null, resolved = c.execute('SELECT COALESCE(SUM(outcome_hash IS NULL),0),COALESCE(SUM(outcome_hash IS NOT NULL),0) FROM paper_observation_passes').fetchone()
        ids = [r[0] for r in c.execute(f'SELECT id FROM paper_observation_passes WHERE outcome_hash IS NULL ORDER BY rowid LIMIT {BOUND}')]
    return {'status': 'OK', 'table_present': True, 'null': null, 'resolved': resolved,
            'null_ids': ids, 'null_ids_truncated': null > len(ids)}


def dispatcher_section(journal_path, last):
    with closing(connect_ro(journal_path)) as c:
        tables = _tables(c)
        out = {'status': 'OK', 'counts': {t: c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tables}}
        intents, results = [], []
        if 'intents' in tables:
            for ident, mint, payload in c.execute('SELECT id,mint,payload FROM (SELECT rowid r,* FROM intents ORDER BY r DESC LIMIT ?) ORDER BY r', (last,)):
                v = _json(payload) or {}
                intents.append({'id': ident, 'mint': mint, 'scan_id': v.get('scan_id'), 'at': v.get('at')})
        if 'results' in tables:
            for ident, payload in c.execute('SELECT id,payload FROM (SELECT rowid r,* FROM results ORDER BY r DESC LIMIT ?) ORDER BY r', (last,)):
                v = _json(payload) or {}
                r = v.get('result') if isinstance(v.get('result'), dict) else {}
                results.append({'id': ident, 'scan_id': v.get('scan_id'), 'at': v.get('at'),
                                'status': r.get('status'), 'blockers': r.get('blockers')})
        out.update(last_intents=intents, last_results=results)
    return out


def budgets_section(evidence_path, now):
    with closing(connect_ro(evidence_path)) as c:
        out = {'status': 'OK', 'monitoring': None, 'ownership': {'budgets': [], 'admission_states': {}}}
        if _has(c, 'paper_monitoring_budget'):
            row = c.execute('SELECT version,cap,window_seconds,high_water,total,blocked FROM paper_monitoring_budget WHERE id=1').fetchone()
            if row:
                version, cap, window, high_water, total, blocked = row
                used = c.execute('SELECT COUNT(*) FROM paper_monitoring_reservations WHERE at>?', (now - window,)).fetchone()[0]
                reservations = c.execute('SELECT COUNT(*) FROM paper_monitoring_reservations').fetchone()[0]
                pending = c.execute('SELECT COUNT(*) FROM paper_monitoring_reservations r LEFT JOIN paper_monitoring_outcomes o ON o.reservation_id=r.id WHERE o.reservation_id IS NULL').fetchone()[0]
                out['monitoring'] = {'version': version, 'cap': cap, 'window_seconds': window, 'high_water': high_water,
                                     'total': total, 'blocked': blocked, 'reservations': reservations,
                                     'used_in_window': used, 'remaining_in_window': max(cap - used, 0),
                                     'pending_reservations': pending, 'exhausted': used >= cap}
        if _has(c, 'ownership_budgets'):
            out['ownership']['budgets'] = [{'id': i, 'used': u, 'ceiling': ceil} for i, u, ceil in c.execute('SELECT id,used,ceiling FROM ownership_budgets ORDER BY id LIMIT ?', (BOUND,))]
        if _has(c, 'ownership_admissions'):
            out['ownership']['admission_states'] = dict(c.execute('SELECT state,COUNT(*) FROM ownership_admissions GROUP BY state'))
    return out


def pacing_section(pacing_path, now):
    with closing(connect_ro(pacing_path)) as c:
        states = [{'provider': p, 'next_at': n, 'blocked_until': b, 'high_water': h, 'pending': pend,
                   'blocked_now': b > now} for p, n, b, h, pend in c.execute('SELECT provider,next_at,blocked_until,high_water,pending FROM state ORDER BY provider')]
        waiters = c.execute('SELECT COUNT(*) FROM waiters').fetchone()[0]
    return {'status': 'OK', 'providers': states, 'waiters': waiters}


def inventory_section(data, check):
    out = []
    for root, dirs, files in os.walk(data, followlinks=False):
        for name in sorted(files):
            if not name.endswith('.sqlite'):
                continue
            p = Path(root) / name
            info = os.lstat(p)
            item = {'name': str(p.relative_to(data)), 'size': info.st_size, 'mtime': info.st_mtime,
                    'symlink': os.path.islink(p), 'nlink': info.st_nlink}
            if check:
                try:
                    with closing(connect_ro(p)) as c:
                        item['quick_check'] = c.execute('PRAGMA quick_check').fetchone()[0]
                except (StatusError, sqlite3.Error, OSError) as e:
                    item['quick_check'] = f'ERROR:{type(e).__name__}'
            out.append(item)
    return {'status': 'OK', 'stores': sorted(out, key=lambda x: x['name'])}


def systemd_section(runner):
    listing = runner(['systemctl', 'list-unit-files', 'desk-*', '--no-legend', '--no-pager'], None)
    units = sorted({line.split()[0] for line in listing.splitlines() if line.strip()})[:BOUND]
    fields = ('ActiveState', 'UnitFileState', 'Result', 'WorkingDirectory', 'DropInPaths')
    report = {}
    for unit in units:
        text = runner(['systemctl', 'show', unit, '--no-pager', '-p', ','.join(fields)], None)
        values = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        report[unit] = {k: values.get(k) for k in fields}
    return {'status': 'OK', 'units': report}


def dashboard_section(runner):
    try:
        text = runner(['ss', '-ltnH'], None)
    except (OSError, subprocess.SubprocessError):
        return {'status': 'UNAVAILABLE'}
    binds = []
    for line in text.splitlines():
        parts = line.split()
        local = parts[3] if len(parts) > 3 else ''
        host, _, port = local.rpartition(':')
        if port == DASHBOARD_PORT:
            binds.append(host)
    return {'status': 'OK', 'binds': binds, 'non_loopback': [h for h in binds if h not in LOOPBACK]}


def blockers_for(report):
    blockers = []
    ledger = report['ledger']
    if ledger.get('status') == 'OK':
        if ledger['positions']:
            blockers.append('HELD_POSITION')
        state = ledger.get('state')
        if state is None:
            blockers.append('LEDGER_STATE_MISSING')
        elif state['mode'] != 'RUNNING':
            blockers.append(f"MODE_{state['mode']}")
        if ledger['config']['file_equals_stored_config'] is False:
            blockers.append('CONFIG_FILE_DIFFERS_FROM_LEDGER')
    passes = report['observation_passes']
    if passes.get('status') == 'OK' and passes['null']:
        blockers.append('NULL_OBSERVATION_PASSES')
    b = report['budgets']
    if b.get('status') == 'OK':
        m = b['monitoring']
        if m and (m['exhausted'] or m['blocked'] or m['pending_reservations']):
            blockers.append('MONITORING_BUDGET_EXHAUSTED_OR_PENDING')
        if any(x['used'] >= x['ceiling'] for x in b['ownership']['budgets']):
            blockers.append('OWNERSHIP_BUDGET_EXHAUSTED')
    p = report['provider_pacing']
    if p.get('status') == 'OK' and (p['waiters'] or any(x['blocked_now'] or x['pending'] for x in p['providers'])):
        blockers.append('PACING_BLOCKED_OR_PENDING')
    if report['dashboard'].get('non_loopback'):
        blockers.append('DASHBOARD_NON_LOOPBACK')
    for name, section in report.items():
        if isinstance(section, dict) and section.get('status') == 'ERROR':
            blockers.append(f'UNREADABLE_{name.upper()}')
    return blockers


def _safe(fn, *args):
    try:
        return fn(*args)
    except (StatusError, sqlite3.Error, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as e:
        return {'status': 'ERROR', 'error': getattr(e, 'args', [''])[0] if isinstance(e, StatusError) else type(e).__name__}


def build_report(data, ledger, config, *, release_dir=None, systemd=False, check=False, last=5,
                 runner=default_runner, now=None):
    now = time.time() if now is None else now
    try:
        data_dir = canonical_path(data, directory=True)
    except (StatusError, OSError) as e:
        return {'status': 'ERROR', 'error': str(e), 'blockers': ['DATA_DIR_INVALID'], 'paper_only': True}
    release = _safe(release_section, release_dir, runner)
    report = {'paper_only': True, 'execution_label': EXECUTION_LABEL, 'generated_at': now, 'release': release}
    report['ledger'] = _safe(ledger_section, ledger, config, release)
    report['observation_passes'] = _safe(passes_section, data_dir / 'evidence.sqlite')
    report['dispatcher_journal'] = _safe(dispatcher_section, data_dir / 'entry-dispatch' / 'dispatch.sqlite', last)
    report['budgets'] = _safe(budgets_section, data_dir / 'evidence.sqlite', now)
    report['provider_pacing'] = _safe(pacing_section, data_dir / 'provider-pacing.sqlite', now)
    report['store_inventory'] = _safe(inventory_section, data_dir, check)
    if systemd:
        report['systemd'] = _safe(systemd_section, runner)
    report['dashboard'] = _safe(dashboard_section, runner)
    report['blockers'] = blockers_for(report)
    report['status'] = 'ERROR' if any(isinstance(v, dict) and v.get('status') == 'ERROR' for v in report.values()) else 'OK'
    return report


def main(argv=None, *, runner=default_runner, out=None):
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument('--data', required=True)
    p.add_argument('--ledger', required=True)
    p.add_argument('--config', required=True)
    p.add_argument('--release-dir')
    p.add_argument('--systemd', action='store_true')
    p.add_argument('--check', action='store_true', help='PRAGMA quick_check each store')
    p.add_argument('--last', type=int, default=5)
    a = p.parse_args(argv)
    report = build_report(a.data, a.ledger, a.config, release_dir=a.release_dir, systemd=a.systemd,
                          check=a.check, last=max(1, min(a.last, BOUND)), runner=runner)
    print(json.dumps(report, sort_keys=True, indent=2, default=str), file=out or sys.stdout)
    return 0 if report['status'] == 'OK' else 2


if __name__ == '__main__':
    raise SystemExit(main())
