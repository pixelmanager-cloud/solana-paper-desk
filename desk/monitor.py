"""Local paper mark watchdog. No discovery, entry, fill, RPC or signing capability."""
import json,time,sqlite3
from pathlib import Path
from .ledger import Ledger
from .engine import initial_state,transition


def tick(path,cfg,*,now=None):
    now=int(time.time()) if now is None else now
    if type(now) is not int or now<0:raise ValueError('Integer clock required')
    if not Path(path).is_file():return {'status':'NOT_CONFIGURED','automatic_entry_enabled':False}
    ledger=Ledger(path,must_exist=True)
    try:
        row=ledger.db.execute('SELECT payload FROM state WHERE id=1').fetchone()
        if not row:return {'status':'EMPTY_LEDGER','automatic_entry_enabled':False}
        state=json.loads(row[0])
        if now<state['last_ts']:return {'status':'CLOCK_BEHIND_LEDGER','automatic_entry_enabled':False}
        needs_update=any((not 0<=now-p['mark_at']<=cfg['price_ttl_seconds']) and
                         (p.get('mark_status')!='STALE' or not p.get('exit_blocked')) for p in state['positions'].values())
        if not needs_update:return {'status':'NO_CHANGE','automatic_entry_enabled':False}
        event={'schema_version':1,'event_id':f'paper-monitor:{now}','ts':now,'kind':'clock','actor':'paper_monitor'}
        outcomes=ledger.apply(event,cfg,transition,initial_state)
        return {'status':'MARKS_EXPIRED','automatic_entry_enabled':False,'outcomes':outcomes}
    finally:ledger.close()
