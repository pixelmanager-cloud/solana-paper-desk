"""Daily bounded snapshots of desk databases, independent of provider credentials."""
import argparse,fcntl,json,os,re,shutil,sqlite3,stat,tempfile
from datetime import datetime,timezone
from contextlib import closing
from pathlib import Path
from .storage import snapshot,verify,sha256

DATABASES=('research.sqlite','launches.sqlite','raw.sqlite','evidence.sqlite')
PAPER_DATABASE='active-paper.sqlite'
DECISION_DATABASE='paper-decisions.sqlite'
PATTERN=re.compile(r'^daily-\d{8}T\d{6}Z$')
PAPER_LEDGER_ENV='DESK_PAPER_LEDGER_DB'


def _additional_name(name):
    return (isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.sqlite',name)
            and name not in (*DATABASES,PAPER_DATABASE,DECISION_DATABASE))


def _protected_identity(path):
    # An explicit selection never follows a link or provisions a missing file.
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1
                or info.st_uid not in (0,os.getuid()) or info.st_mode & 0o137):
            raise ValueError('Selected paper ledger must be a protected regular single-link file')
        identity=(info.st_dev,info.st_ino)
        if path.is_symlink() or identity!=(path.stat().st_dev,path.stat().st_ino):
            raise ValueError('Selected paper ledger identity changed')
        return identity
    finally:os.close(fd)


def selected_paper_ledger(data):
    """One additional existing operator-selected ledger, or unchanged default.

    Return path and inode identity so a caller can reject replacement before it
    publishes a read or backup. Linux stable-path contract; no rename while used.
    """
    if PAPER_LEDGER_ENV not in os.environ:return None
    value=os.environ[PAPER_LEDGER_ENV]
    if not value or len(value)>4096:raise ValueError('Invalid selected paper ledger')
    path=Path(value);data=Path(data).resolve()
    if (not path.is_absolute() or str(path)!=value or path.resolve()!=path
            or path.parent!=data or not _additional_name(path.name)):
        raise ValueError('Selected paper ledger must have a distinct canonical path in the data folder')
    parent=data.stat()
    if parent.st_uid not in (0,os.getuid()) or parent.st_mode & 0o022:
        raise ValueError('Selected paper ledger directory must be protected')
    return path,_protected_identity(path)


def _retained_manifest(directory,data):
    """Recognize and verify legacy or one-additional-ledger backups before pruning."""
    if (directory/'manifest.json').is_symlink():return False
    with (directory/'manifest.json').open() as stream:raw=stream.read(65537)
    if len(raw)>65536:return False
    m=json.loads(raw)
    if not isinstance(m,dict) or m.get('kind')!='solana-desk-daily-v1':return False
    reports=m['databases']
    if not isinstance(reports,list) or not 4<=len(reports)<=7:return False
    allowed=set(DATABASES)|{PAPER_DATABASE,DECISION_DATABASE}
    extra=m.get('additional_paper_ledger')
    if extra is not None:
        if (not isinstance(extra,dict) or set(extra)!={'file','source'}
                or not _additional_name(extra['file']) or extra['source']!=str(data/extra['file'])):return False
        allowed.add(extra['file'])
    names={r['file'] for r in reports}
    if (len(names)!=len(reports) or not set(DATABASES)<=names<=allowed
            or extra is not None and extra['file'] not in names
            or {x.name for x in directory.iterdir()}!=names|{'manifest.json'}):return False
    if any(x.is_symlink() or not x.is_file() for x in directory.iterdir()):return False
    for r in reports:
        file=directory/r['file']
        if (type(r['bytes']) is not int or r['bytes']!=file.stat().st_size
                or r['integrity']!='ok' or not isinstance(r['sha256'],str)
                or not re.fullmatch('[0-9a-f]{64}',r['sha256'])):return False
        if sha256(file)!=r['sha256']:return False
        # Retained backups are closed standalone snapshots, not active databases.
        # Immutable read avoids creating WAL sidecars in old valid archives.
        with closing(sqlite3.connect(file.as_uri()+'?mode=ro&immutable=1',uri=True)) as c:
            if c.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:return False
    return True


def run(data,root,keep=7):
    data=Path(data).resolve();root=Path(root).resolve()
    if type(keep) is not int or not 1<=keep<=30:raise ValueError('Retention outside supported range')
    selected=selected_paper_ledger(data)
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (root/'.daily.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        sources=[data/n for n in DATABASES]
        paper=data/PAPER_DATABASE
        if paper.is_symlink():raise ValueError('Paper database must not be symlinked')
        paper_present=paper.exists()
        if selected is not None and not paper_present:
            raise ValueError('Original paper ledger required alongside selected ledger')
        if paper_present:sources.append(paper)
        decisions=data/DECISION_DATABASE
        if decisions.is_symlink():raise ValueError('Decision journal must not be symlinked')
        decisions_present=decisions.exists()
        if decisions_present:sources.append(decisions)
        if selected is not None:sources.append(selected[0])
        if any(not p.is_file() or p.is_symlink() for p in sources):raise ValueError('Required database missing or symlinked')
        identities=[(p.stat().st_dev,p.stat().st_ino) for p in sources]
        if len(set(identities))!=len(identities):raise ValueError('Database sources must be distinct')
        # WAL pages may not yet be in the main database; reserve extra headroom.
        size=sum(p.stat().st_size+(Path(str(p)+'-wal').stat().st_size if Path(str(p)+'-wal').exists() else 0) for p in sources)
        if shutil.disk_usage(root).free<2*size+64*1024*1024:raise ValueError('Insufficient backup disk headroom')
        name='daily-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        destination=root/name
        if destination.exists():raise ValueError('Backup name already exists')
        temporary=Path(tempfile.mkdtemp(prefix='.daily-',dir=root))
        try:
            reports=[]
            for p,identity in zip(sources,identities):
                if p.is_symlink() or (p.stat().st_dev,p.stat().st_ino)!=identity:
                    raise ValueError('Database source identity changed during backup')
                if selected is not None and selected_paper_ledger(data)!=selected:
                    raise ValueError('Selected paper ledger identity changed during backup')
                report=snapshot(p,temporary/p.name)
                copied=temporary/p.name
                # SQLite backup copies the source's WAL header. Normalize only
                # the copy to a standalone file before verification/publication.
                with closing(sqlite3.connect(copied)) as c:
                    if c.execute('PRAGMA journal_mode=DELETE').fetchone()!=('delete',):
                        raise ValueError('Standalone backup journal unavailable')
                with copied.open('rb') as file:os.fsync(file.fileno())
                report.update(bytes=copied.stat().st_size,sha256=sha256(copied))
                reports.append(report)
            for r in reports:verify(temporary/r['file'],r['sha256'])
            if decisions.is_symlink() or decisions.exists()!=decisions_present:raise ValueError('Decision journal membership changed during backup')
            if paper.is_symlink() or paper.exists()!=paper_present:raise ValueError('Paper ledger membership changed during backup')
            if (any(p.is_symlink() or (p.stat().st_dev,p.stat().st_ino)!=identity
                    for p,identity in zip(sources,identities))
                    or selected_paper_ledger(data)!=selected):
                raise ValueError('Database source identity changed during backup')
            manifest={'kind':'solana-desk-daily-v1','created_at':datetime.now(timezone.utc).isoformat(),
                      'decision_journal':'included' if decisions_present else 'not_configured','databases':reports,'paper_ledger':'included' if paper_present else 'not_configured','scope':'Per-database consistent snapshots; not one cross-database transaction.'}
            if selected is not None:
                manifest['additional_paper_ledger']={'file':selected[0].name,'source':str(selected[0])}
            with (temporary/'manifest.json').open('x') as f:
                os.chmod(f.name,0o600);json.dump(manifest,f,indent=2);f.flush();os.fsync(f.fileno())
            os.rename(temporary,destination)
            fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        finally:
            if temporary.exists():shutil.rmtree(temporary)
        # Remove only our own complete daily snapshots after the new one is verified.
        complete=[]
        for p in sorted(root.iterdir()):
            if p.is_symlink() or not p.is_dir() or not PATTERN.fullmatch(p.name):continue
            try:
                if not _retained_manifest(p,data):continue
                complete.append(p)
            except (OSError,ValueError,KeyError,TypeError,RecursionError,sqlite3.Error):continue
        removed=[]
        for p in complete[:-keep]:shutil.rmtree(p);removed.append(p.name)
        return {'backup':str(destination),'databases':len(reports),'removed':removed}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--data',required=True);parser.add_argument('--root',required=True);parser.add_argument('--keep',type=int,default=7)
    args=parser.parse_args();print(json.dumps(run(args.data,args.root,args.keep)))
