"""Re-hash and integrity-check a tools.ops.backup directory against its manifest.

    python -m tools.ops.verify_backup DIR      (exit 0 only if everything matches)
"""
import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from tools.ops.backup import BackupError, database_report, sha256_file


def verify(directory):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise BackupError('Backup directory must be a real directory')
    manifest_path = directory / 'manifest.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise BackupError('manifest.json missing or symlinked')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('kind') != 'solana-desk-backup-v1' or not isinstance(manifest.get('databases'), dict):
        raise BackupError('Unrecognised manifest')
    expected = set(manifest['databases'])
    actual = set()
    for path in directory.rglob('*'):
        if path.is_symlink():
            raise BackupError('Symlink in backup: ' + str(path))
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    if actual != expected | {'manifest.json'}:
        raise BackupError('File set differs from manifest: %s' % sorted(actual ^ (expected | {'manifest.json'})))
    for name, entry in manifest['databases'].items():
        path = directory / name
        if path.stat().st_size != entry['size'] or sha256_file(path) != entry['sha256']:
            raise BackupError('Hash or size mismatch: ' + name)
        # immutable: verification never creates sidecar files in the archive
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
            if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise BackupError('Integrity failure: ' + name)
            tables, schema = database_report(connection)
        if tables != entry['tables'] or schema != entry['schema_sha256']:
            raise BackupError('Tables or schema differ from manifest: ' + name)
    return {'databases': len(expected), 'label': manifest.get('label')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('directory')
    args = parser.parse_args(argv)
    try:
        result = verify(args.directory)
    except (BackupError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(json.dumps({'ok': False, 'error': str(error)}), file=sys.stderr)
        return 1
    print(json.dumps({'ok': True, **result}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
