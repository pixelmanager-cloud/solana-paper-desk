"""Portable owner evidence for tests (T22G): a /proc/locks-format table and a /proc-like tree injected into
desk.monitoring_budget, so lock-ownership logic runs identically on Linux and macOS.

``free(testcase)`` = "nothing holds anything" (the owner is gone). ``held(testcase, path, pid=...)`` = another
process holds the flock on ``path``'s inode. ``deleted_holder(testcase, path, pid=...)`` = another process holds a
descriptor on the DELETED inode that ``path`` used to name (the delete-and-recreate hazard).
"""
import os
import tempfile
from unittest.mock import patch

from desk import monitoring_budget as mb

OTHER_PID = 424242


def _install(testcase, table, proc=None):
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    locks = os.path.join(tmp.name, 'locks')
    with open(locks, 'w') as stream:
        stream.write(table)
    root = os.path.join(tmp.name, 'proc')
    os.mkdir(root)
    for pid, target in (proc or {}).items():
        os.makedirs(f'{root}/{pid}/fd')
        os.symlink(target, f'{root}/{pid}/fd/7')
    for patcher in (patch.object(mb, 'LOCK_TABLE', locks), patch.object(mb, 'PROC_ROOT', root)):
        patcher.start()
        testcase.addCleanup(patcher.stop)


def free(testcase):
    _install(testcase, '')


def held(testcase, path, pid=OTHER_PID):
    _install(testcase, f'1: FLOCK  ADVISORY  WRITE {pid} 08:01:{os.stat(path).st_ino} 0 EOF\n')


def deleted_holder(testcase, path, pid=OTHER_PID):
    _install(testcase, '', {pid: str(path) + ' (deleted)'})


def unreadable(testcase):
    """The kernel lock table cannot be read: owner unknown."""
    _install(testcase, '')
    patcher = patch.object(mb, 'LOCK_TABLE', '/nonexistent/locks')
    patcher.start()
    testcase.addCleanup(patcher.stop)
