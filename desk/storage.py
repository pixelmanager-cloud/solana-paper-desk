"""Consistent SQLite snapshots with integrity and checksum verification; no secrets."""
import hashlib,os,sqlite3,uuid,time
from contextlib import closing
from pathlib import Path


def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def snapshot(source,destination):
    source=Path(source).resolve();destination=Path(destination).resolve()
    if not source.is_file() or destination.exists() or source==destination:
        raise ValueError('Existing source and unused destination required')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.parent/('.snapshot-'+uuid.uuid4().hex)
    fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    try:
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=20)) as src:
            with closing(sqlite3.connect(temporary)) as dst:
                deadline=time.monotonic()+30
                def progress(status,remaining,total):
                    if status not in (sqlite3.SQLITE_OK,sqlite3.SQLITE_DONE,sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):
                        raise sqlite3.DatabaseError(f'Snapshot SQLite status {status}')
                    if time.monotonic()>deadline:raise TimeoutError('Snapshot exceeded 30-second budget')
                src.backup(dst,pages=256,progress=progress,sleep=0.1)
                if dst.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('Snapshot integrity check failed')
        with temporary.open('rb') as f:os.fsync(f.fileno())
        # Hard link provides an atomic no-overwrite publish on the same filesystem.
        os.link(temporary,destination)
        directory_fd=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory_fd)
        finally:os.close(directory_fd)
        report={'file':destination.name,'bytes':destination.stat().st_size,'sha256':sha256(destination),'integrity':'ok'}
        return report
    finally:temporary.unlink(missing_ok=True)


def verify(path,expected_hash):
    path=Path(path).resolve()
    if sha256(path)!=expected_hash:raise ValueError('Snapshot checksum mismatch')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
        if c.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('Snapshot integrity check failed')
    return True
