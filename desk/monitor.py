"""Local paper mark watchdog. No discovery, entry, fill, RPC or signing capability."""
import time,sqlite3
from pathlib import Path
from contextlib import closing
from .paper_checkpoint import read_checkpoint,RecoveryRequired
from .ledger import Ledger
from .engine import initial_state,transition



def recovery(reason):
    return {'status':'RECOVERY_REQUIRED','automatic_entry_enabled':False,'recovery_reason':reason,
            'notice':'Paper ledger is incomplete or corrupt. Recovery is required; original records are preserved.'}


def _database_unavailable(error):
    # Operational failures/corruption are unavailable; SQL programming and
    # constraint defects must remain visible to the caller.
    return isinstance(error,sqlite3.OperationalError) or type(error) is sqlite3.DatabaseError


def unavailable():
    return {'status':'LEDGER_UNAVAILABLE','automatic_entry_enabled':False,
            'notice':'Paper ledger could not be verified; no monitor work performed.'}


# Ledger currently exposes these integrity failures as ValueError, not a typed
# exception. Match its complete diagnostic contract; unrelated failures escape.
_LEDGER_RECOVERY_ERRORS = {
    'ledger checkpoint missing: recovery required; original records preserved':'CHECKPOINT_MISSING',
    'ledger checkpoint invalid: recovery required; original records preserved':'CHECKPOINT_INVALID',
    'ledger event journal incomplete: recovery required; original records preserved':'EVENT_JOURNAL_INCOMPLETE',
}


def tick(path,cfg,*,now=None):
    now=int(time.time()) if now is None else now
    if type(now) is not int or now<0:raise ValueError('Integer clock required')
    if not Path(path).is_file():return {'status':'NOT_CONFIGURED','automatic_entry_enabled':False}
    # Inspect without Ledger's schema/WAL initialization. A damaged restore must
    # not be altered merely to decide whether watchdog work is needed.
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as c:
            c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
            state=read_checkpoint(c)
            if state is None:return {'status':'EMPTY_LEDGER','automatic_entry_enabled':False}
            if now<state['last_ts']:return {'status':'CLOCK_BEHIND_LEDGER','automatic_entry_enabled':False}
            needs_update=any((not 0<=now-p['mark_at']<=cfg['price_ttl_seconds']) and
                             (p.get('mark_status')!='STALE' or not p.get('exit_blocked')) for p in state['positions'].values())
            if not needs_update:return {'status':'NO_CHANGE','automatic_entry_enabled':False}
    except RecoveryRequired as exc:
        return recovery(str(exc))
    except (sqlite3.Error,ValueError,KeyError,TypeError,OverflowError):
        return unavailable()
    try:
        ledger=Ledger(path,must_exist=True)
    except sqlite3.Error as exc:
        if not _database_unavailable(exc):raise
        return unavailable()
    try:
        event={'schema_version':1,'event_id':f'paper-monitor:{now}','ts':now,'kind':'clock','actor':'paper_monitor'}
        def checked_transition(state,event,cfg):
            # Recheck the saved snapshot inside apply's write transaction.
            read_checkpoint(ledger.db)
            return transition(state,event,cfg)
        try:
            outcomes=ledger.apply(event,cfg,checked_transition,initial_state)
        except RecoveryRequired as exc:
            return recovery(str(exc))
        except ValueError as exc:
            reason=_LEDGER_RECOVERY_ERRORS.get(str(exc))
            if reason is None:raise
            return recovery(reason)
        except sqlite3.Error as exc:
            if not _database_unavailable(exc):raise
            return unavailable()
        return {'status':'MARKS_EXPIRED','automatic_entry_enabled':False,'outcomes':outcomes}
    finally:ledger.close()
