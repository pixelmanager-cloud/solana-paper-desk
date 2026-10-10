"""Coordinator quiesced pre/post backup, including all retired ledgers and original dispatcher journal."""
import argparse
from contextlib import closing
import stat
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from desk.runtime_compatibility import implementation_hash

p = argparse.ArgumentParser()
p.add_argument('--source', required=True)
p.add_argument('--destination', required=True)
p.add_argument('--phase', choices=('post',), required=True)
p.add_argument('--new-ledger', required=True)
a = p.parse_args()
if implementation_hash() != a.source:
    raise ValueError('Exact reviewed code required')
units = ('desk-dashboard.service', 'desk-paper-held-cycle.service', 'desk-paper-monitor.service',
         'desk-decisions.service', 'desk-backup.service', 'desk-discovery.service',
         'desk-recorder.service', 'desk-continuous-discovery.service',
         'desk-paper-held-cycle.timer', 'desk-paper-monitor.timer', 'desk-decisions.timer',
         'desk-backup.timer', 'desk-discovery.timer',
         'desk-paper-entry-dispatcher.service', 'desk-paper-entry-dispatcher.timer')
for unit in units:
    state = subprocess.check_output(['systemctl', 'show', unit, '-p', 'ActiveState', '--value'], text=True).strip()
    if state not in ('inactive', 'failed'):
        raise ValueError('Writer not quiesced: ' + unit)
base = Path('/var/lib/solana-desk')
new = Path(a.new_ledger)
names = ['research.sqlite', 'evidence.sqlite', 'active-paper.sqlite', 'token2022-paper.sqlite',
         'token2022-boost-00b7c302.sqlite', 'paper-decisions.sqlite', 'launches.sqlite', 'raw.sqlite', 'provider-pacing.sqlite',
         'discovery/continuous.sqlite', 'entry-dispatch/dispatch.sqlite']
if new.parent != base or new.name in names:
    raise ValueError('Distinct canonical new ledger required')
if a.phase == 'post':
    if not new.is_file() or new.resolve(strict=True) != new:
        raise ValueError('Canonical regular new ledger required')
    names.append(new.name)
elif new.exists() or new.is_symlink():
    raise ValueError('Pre-backup must precede new ledger creation')
destination = Path(a.destination)
if (destination.parent != Path('/var/backups/solana-desk')
        or destination.parent.resolve(strict=True) != destination.parent):
    raise ValueError('Protected backup directory required')
destination.mkdir(mode=0o700)
# Validate every source before creating copies. Reject aliases and special files.
identities = set()
for name in names:
    source = base / name
    info = source.lstat()
    identity = (info.st_dev, info.st_ino)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or source.resolve(strict=True) != source or identity in identities):
        raise ValueError('Distinct canonical single-link regular database required')
    identities.add(identity)
manifest = {}
for name in names:
    source, target = base / name, destination / name
    if target.parent != destination:
        target.parent.mkdir(mode=0o700, exist_ok=True)
    if source.resolve(strict=True) != source:
        raise ValueError('Canonical original database required')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as current:
        with closing(sqlite3.connect(target)) as backup:
            current.backup(backup)
            backup.commit()
            # Only the copy is normalized; original WAL state is never changed.
            if backup.execute('PRAGMA journal_mode=DELETE').fetchone() != ('delete',):
                raise ValueError('Standalone backup journal mode required')
    with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as verified:
        if verified.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('Backup integrity failure')
    target.chmod(0o600)
    manifest[name] = hashlib.sha256(target.read_bytes()).hexdigest()
if len(manifest) != 12:
    raise ValueError('Backup database cardinality mismatch')
(destination / 'manifest.json').write_text(json.dumps({'source': a.source,
    'phase': a.phase, 'new_ledger': str(new), 'databases': manifest}, indent=2) + '\n')
print(json.dumps({'phase': a.phase, 'databases': len(manifest), 'path': str(destination)}))
