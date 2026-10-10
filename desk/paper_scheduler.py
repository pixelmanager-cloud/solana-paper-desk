"""Whole-invocation scheduler protection; never replaces inner evidence locks."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
from .job_persistence import canonical_job_path

@contextmanager
def lease(research_db):
    research=canonical_job_path(research_db)
    parent=research.parent
    info=parent.stat()
    if info.st_uid!=os.geteuid() or info.st_mode & 0o022:
        raise ValueError('Protected scheduler directory required')
    path=parent/'paper-scheduler.lock'
    expected=os.environ.get('DESK_PAPER_SCHEDULER_IDENTITY')
    if expected is None:raise ValueError('Reviewed scheduler inode identity required')
    flags=os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC
    fd=os.open(path,flags)
    try:
        info=os.fstat(fd)
        if expected!=str(info.st_dev)+':'+str(info.st_ino):
            raise ValueError('Scheduler lock identity mismatch')
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid()
                or stat.S_IMODE(info.st_mode)!=0o600):
            raise ValueError('Protected regular scheduler lock required')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        current=path.lstat()
        if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):
            raise ValueError('Scheduler lock identity changed')
        yield fd
    finally:
        # Never unlink or explicitly unlock: inherited executions retain the
        # same open description until their last descriptor closes.
        os.close(fd)
