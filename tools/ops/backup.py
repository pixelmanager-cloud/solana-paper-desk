"""Generic quiesced SQLite backup with a verifiable manifest (paper desk, offline).

    python -m tools.ops.backup --data DIR --destination /var/backups/solana-desk/<name> \
        --label TEXT [--require-quiesced UNIT ...] [--expect-count N]

Sources are only ever opened ``mode=ro``; only the copies are normalised.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SIDECARS = ('-wal', '-shm', '-journal')
QUIET_STATES = ('inactive', 'failed')


class BackupError(ValueError):
    pass


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def database_report(connection):
    """Table row counts and a schema digest read from an open connection."""
    schema = connection.execute(
        'SELECT type,name,tbl_name,coalesce(sql,\'\') FROM sqlite_master ORDER BY type,name').fetchall()
    tables = {}
    for kind, name, _, _ in schema:
        if kind == 'table' and not name.startswith('sqlite_'):
            quoted = '"' + name.replace('"', '""') + '"'
            tables[name] = connection.execute('SELECT count(*) FROM ' + quoted).fetchone()[0]
    text = json.dumps(schema, separators=(',', ':'))
    return tables, hashlib.sha256(text.encode()).hexdigest()


def discover(data):
    """Relative names of every ``*.sqlite`` under data; symlinks are refused."""
    data = Path(data)
    found = []
    for current, directories, files in os.walk(data, followlinks=False):
        for name in list(directories):
            if (Path(current) / name).is_symlink():
                raise BackupError('Symlinked directory refused: ' + str(Path(current) / name))
        for name in files:
            path = Path(current) / name
            if name.endswith(SIDECARS) or name.endswith('.lock'):
                continue
            if name.endswith('.sqlite'):
                found.append(path.relative_to(data).as_posix())
    return sorted(found)


def validate_sources(data, names):
    identities = set()
    for name in names:
        source = data / name
        info = source.lstat()
        identity = (info.st_dev, info.st_ino)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or source.resolve(strict=True) != source or identity in identities):
            raise BackupError('Distinct canonical single-link regular database required: ' + name)
        identities.add(identity)
    return identities


def systemctl_state(unit):
    return subprocess.check_output(
        ['systemctl', 'show', unit, '-p', 'ActiveState', '--value'], text=True).strip()


def check_quiesced(units, runner=systemctl_state):
    for unit in units:
        state = runner(unit)
        if state not in QUIET_STATES:
            raise BackupError('Writer not quiesced: %s is %r' % (unit, state))


def backup(data, destination, label, require_quiesced=(), expect_count=None, runner=systemctl_state):
    from desk.runtime_compatibility import implementation_hash
    data, destination = Path(data), Path(destination)
    if not data.is_dir() or data.resolve(strict=True) != data:
        raise BackupError('Canonical data directory required')
    if not isinstance(label, str) or not label or len(label) > 200:
        raise BackupError('Label required')
    names = discover(data)
    if not names:
        raise BackupError('No databases found')
    if expect_count is not None and len(names) != expect_count:
        raise BackupError('Database count %d != expected %d' % (len(names), expect_count))
    validate_sources(data, names)
    check_quiesced(require_quiesced, runner)
    parent = destination.parent
    if (destination.is_symlink() or destination.exists() or not parent.is_dir()
            or parent.resolve(strict=True) != parent):
        raise BackupError('Fresh destination in a canonical parent required')
    destination.mkdir(mode=0o700)
    try:
        databases = {}
        for name in names:
            source, target = data / name, destination / name
            if source.resolve(strict=True) != source:
                raise BackupError('Canonical original database required: ' + name)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as current:
                with closing(sqlite3.connect(target)) as copy:
                    current.backup(copy)
                    copy.commit()
                    if copy.execute('PRAGMA journal_mode=DELETE').fetchone() != ('delete',):
                        raise BackupError('Standalone backup journal mode required')
            target.chmod(0o600)
            with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as check:
                if check.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise BackupError('Backup integrity failure: ' + name)
                tables, schema = database_report(check)
            databases[name] = {'sha256': sha256_file(target), 'size': target.stat().st_size,
                               'tables': tables, 'schema_sha256': schema}
        validate_sources(data, names)
        manifest = {'kind': 'solana-desk-backup-v1', 'label': label,
                    'created_utc': datetime.now(timezone.utc).isoformat(),
                    'implementation_hash': implementation_hash(),
                    'source': str(data), 'databases': databases}
        with (destination / 'manifest.json').open('x') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(manifest, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        import shutil
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return {'path': str(destination), 'databases': len(databases), 'label': label}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--data', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--require-quiesced', nargs='*', default=[], metavar='UNIT')
    parser.add_argument('--expect-count', type=int)
    args = parser.parse_args(argv)
    try:
        result = backup(args.data, args.destination, args.label, args.require_quiesced, args.expect_count)
    except (BackupError, OSError, sqlite3.Error, subprocess.CalledProcessError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}), file=sys.stderr)
        return 1
    print(json.dumps({'ok': True, **result}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
