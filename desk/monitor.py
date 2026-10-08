"""Local paper mark watchdog. No discovery, entry, fill, RPC or signing capability."""
import time,sqlite3
from pathlib import Path
from contextlib import closing
from .paper_checkpoint import read_checkpoint,RecoveryRequired
from .ledger import Ledger
from .engine import initial_state,transition


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
        return {'status':'RECOVERY_REQUIRED','automatic_entry_enabled':False,'recovery_reason':str(exc),
                'notice':'Paper ledger is incomplete or corrupt. Recovery is required; original records are preserved.'}
    except (sqlite3.Error,ValueError,KeyError,TypeError,OverflowError):
        return {'status':'LEDGER_UNAVAILABLE','automatic_entry_enabled':False,
                'notice':'Paper ledger could not be verified; no monitor work performed.'}
    ledger=Ledger(path,must_exist=True)
    try:
        event={'schema_version':1,'event_id':f'paper-monitor:{now}','ts':now,'kind':'clock','actor':'paper_monitor'}
        def checked_transition(state,event,cfg):
            # Recheck the saved snapshot inside apply's write transaction.
            read_checkpoint(ledger.db)
            return transition(state,event,cfg)
        try:
            outcomes=ledger.apply(event,cfg,checked_transition,initial_state)
        except RecoveryRequired as exc:
            return {'status':'RECOVERY_REQUIRED','automatic_entry_enabled':False,'recovery_reason':str(exc),
                    'notice':'Paper ledger is incomplete or corrupt. Recovery is required; original records are preserved.'}
        return {'status':'MARKS_EXPIRED','automatic_entry_enabled':False,'outcomes':outcomes}
    finally:ledger.close()
