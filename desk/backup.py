"""Daily bounded snapshots of desk databases, independent of provider credentials."""
import argparse,fcntl,json,os,re,shutil,tempfile
from datetime import datetime,timezone
from pathlib import Path
from .storage import snapshot,verify

DATABASES=('research.sqlite','launches.sqlite','raw.sqlite','evidence.sqlite')
PAPER_DATABASE='active-paper.sqlite'
DECISION_DATABASE='paper-decisions.sqlite'
PATTERN=re.compile(r'^daily-\d{8}T\d{6}Z$')


def run(data,root,keep=7):
    data=Path(data).resolve();root=Path(root).resolve()
    if type(keep) is not int or not 1<=keep<=30:raise ValueError('Retention outside supported range')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (root/'.daily.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        sources=[data/n for n in DATABASES]
        paper=data/PAPER_DATABASE
        if paper.is_symlink():raise ValueError('Paper database must not be symlinked')
        paper_present=paper.exists()
        if paper_present:sources.append(paper)
        decisions=data/DECISION_DATABASE
        if decisions.is_symlink():raise ValueError('Decision journal must not be symlinked')
        decisions_present=decisions.exists()
        if decisions_present:sources.append(decisions)
        if any(not p.is_file() or p.is_symlink() for p in sources):raise ValueError('Required database missing or symlinked')
        # WAL pages may not yet be in the main database; reserve extra headroom.
        size=sum(p.stat().st_size+(Path(str(p)+'-wal').stat().st_size if Path(str(p)+'-wal').exists() else 0) for p in sources)
        if shutil.disk_usage(root).free<2*size+64*1024*1024:raise ValueError('Insufficient backup disk headroom')
        name='daily-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        destination=root/name
        if destination.exists():raise ValueError('Backup name already exists')
        temporary=Path(tempfile.mkdtemp(prefix='.daily-',dir=root))
        try:
            reports=[snapshot(p,temporary/p.name) for p in sources]
            for r in reports:verify(temporary/r['file'],r['sha256'])
            if decisions.is_symlink() or decisions.exists()!=decisions_present:raise ValueError('Decision journal membership changed during backup')
            if paper.is_symlink() or paper.exists()!=paper_present:raise ValueError('Paper ledger membership changed during backup')
            manifest={'kind':'solana-desk-daily-v1','created_at':datetime.now(timezone.utc).isoformat(),
                      'decision_journal':'included' if decisions_present else 'not_configured','databases':reports,'paper_ledger':'included' if paper_present else 'not_configured','scope':'Per-database consistent snapshots; not one cross-database transaction.'}
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
                m=json.loads((p/'manifest.json').read_text())
                if m.get('kind')!='solana-desk-daily-v1':continue
                names={r['file'] for r in m['databases']}
                if not set(DATABASES)<=names<=set(DATABASES)|{PAPER_DATABASE,DECISION_DATABASE} or {x.name for x in p.iterdir()}!=names|{'manifest.json'}:continue
                if any(x.is_symlink() or not x.is_file() for x in p.iterdir()):continue
                complete.append(p)
            except (OSError,ValueError,KeyError,TypeError):continue
        removed=[]
        for p in complete[:-keep]:shutil.rmtree(p);removed.append(p.name)
        return {'backup':str(destination),'databases':len(reports),'removed':removed}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--data',required=True);parser.add_argument('--root',required=True);parser.add_argument('--keep',type=int,default=7)
    args=parser.parse_args();print(json.dumps(run(args.data,args.root,args.keep)))
