"""Bounded supervised synthetic paper loop; no live adapter or provider I/O.

Run with ``python -m desk.paper_runner``. Only explicitly initialized new
experiments are supported. Existing Ledger event IDs are the restart cursor.
"""
import argparse
from contextlib import closing
import json
from itertools import islice
from pathlib import Path
import sqlite3

from .engine import initial_state, transition
from .ledger import Ledger
from .model import canonical, load_config, validate_event, PAPER_EXPERIMENTAL
from .monitor import tick
from .paper_checkpoint import read_checkpoint
from .paper_view import paper_status

PROVENANCE = 'SYNTHETIC_TEST_ONLY'
MAX_EVENTS = 16
MAX_FIXTURE_BYTES = 256 * 1024
INIT = {'schema_version':1,'event_id':'paper-runner:init','ts':0,
        'kind':'clock','actor':'paper_monitor'}


def _validate_fixture_event(event,cfg):
    # Only the explicit operator config selects validation. Event risk metadata
    # is evidence to validate, never permission to select experimental mode.
    version=cfg.get('experimental_policy_version') if cfg is not None else None
    if version is not None:
        if type(version) is not int or version not in (1,2) or cfg.get('mode')!='paper':
            raise ValueError('Unsupported experimental paper policy configuration')
        validate_event(event,mode=PAPER_EXPERIMENTAL,policy_version=version)
    else:
        validate_event(event)


class FixtureAdapter:
    """Explicit offline fixture boundary, never live authorization from JSON."""
    def __init__(self, fixture, *, cfg=None):
        if (type(fixture) is not dict or set(fixture)!={'provenance','events'}
                or fixture['provenance']!=PROVENANCE or type(fixture['events']) is not list
                or len(fixture['events'])>MAX_EVENTS):
            raise ValueError('Bounded SYNTHETIC_TEST_ONLY fixture required')
        encoded=canonical(fixture['events'])
        if len(encoded.encode())>MAX_FIXTURE_BYTES:raise ValueError('Fixture size ceiling')
        self.events = json.loads(encoded)
        for event in self.events:
            _validate_fixture_event(event,cfg)
            if event['kind']!='market' or event['provenance']!=PROVENANCE:
                raise ValueError('Fixture market provenance required')
        if len({event['event_id'] for event in self.events})!=len(self.events):
            raise ValueError('Duplicate fixture event identity')

    @classmethod
    def from_file(cls,path,*,cfg=None):
        with Path(path).open('rb') as source:
            raw=source.read(MAX_FIXTURE_BYTES+1)
        if len(raw)>MAX_FIXTURE_BYTES:raise ValueError('Fixture size ceiling')
        return cls(json.loads(raw),cfg=cfg)

    def observations(self,positions,now,limit):
        return [event for event in self.events if event['ts']==now and event['mint'] in positions][:limit]

    def candidates(self,positions,now,limit):
        return [event for event in self.events if event['ts']==now and event['mint'] not in positions][:limit]


def initialize(path,cfg):
    """Exclusive new experiment creation; never adopt/migrate an existing DB."""
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb'):pass
    ledger=Ledger(path,must_exist=True)
    try:
        def initialize_transition(state,event,config):
            ledger.db.execute('INSERT INTO metadata VALUES(?,?)',('paper_runner',PROVENANCE))
            return transition(state,event,config)
        ledger.apply(INIT,cfg,initialize_transition,initial_state)
    finally:ledger.close()
    return paper_status(path,now=0)


def _state(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as connection:
        connection.execute('BEGIN')
        state=read_checkpoint(connection)
        marker=connection.execute("SELECT value FROM metadata WHERE key='paper_runner'").fetchone()
        if state is None or marker!=(PROVENANCE,):
            raise ValueError('Runner requires its explicitly initialized new synthetic experiment')
        if any(position['provenance']!=PROVENANCE for position in state['positions'].values()):
            raise ValueError('Synthetic runner cannot manage real-data positions')
        return state


def _batch(values,now,limit,*,positions,observations,cfg=None):
    result=[]
    for event in values:
        if len(result)>=limit:raise ValueError('Adapter event ceiling exceeded')
        # Clone before validation/apply so an adapter cannot mutate delivered IDs.
        encoded=canonical(event)
        if len(encoded.encode())>MAX_FIXTURE_BYTES:raise ValueError('Adapter event size ceiling')
        event=json.loads(encoded)
        _validate_fixture_event(event,cfg)
        if (event['kind']!='market' or event['provenance']!=PROVENANCE or event['ts']!=now
                or (event['mint'] in positions)!=observations):
            raise ValueError('Adapter must supply current synthetic events for the requested phase')
        result.append(event)
    if len({event['mint'] for event in result})!=len(result):
        raise ValueError('One observation per mint per run required')
    return result


def run_once(path,cfg,adapter,*,now,controls=(),limit=MAX_EVENTS):
    """One bounded supervision pass; adapters do not directly touch the ledger.

    Only trusted local FixtureAdapter code is callable here. Future live adapter
    admission is a separate prerequisite; market JSON cannot select that mode.
    Each apply is independently atomic, so restart redelivers exact event IDs.
    """
    if type(now) is not int or not 0<=now<=253402300799:
        raise ValueError('Valid integer UTC clock required')
    if type(limit) is not int or not 1<=limit<=MAX_EVENTS:raise ValueError('Invalid event bound')
    if not isinstance(adapter,FixtureAdapter):raise ValueError('Only the synthetic fixture adapter is implemented')
    controls=list(islice(controls,limit+1))
    if len(controls)>limit:raise ValueError('Control ceiling exceeded')
    copied_controls=[]
    for control in controls:
        encoded=canonical(control)
        if len(encoded.encode())>MAX_FIXTURE_BYTES:raise ValueError('Control size ceiling')
        control=json.loads(encoded)
        copied_controls.append(control)
        validate_event(control)
        if control['kind']!='control' or control['ts']!=now:
            raise ValueError('Current operator controls required')
    controls=copied_controls
    state=_state(path)
    result={'status':'COMPLETE','provenance':PROVENANCE,'automatic_entry_enabled':False,
            'delivered_event_ids':[],'outcomes':[],'monitor':None,'paper':None}
    if now<state['last_ts']:
        return {**result,'status':'CLOCK_BEHIND_LEDGER','paper':paper_status(path,now=now)}
    ledger=Ledger(path,must_exist=True)
    try:
        # Existing deterministic bootstrap ID validates config/code identity and
        # checkpoint before any work; duplicates cause no new event or outcome.
        ledger.apply(INIT,cfg,transition,initial_state)
        def deliver(event):
            result['outcomes'].extend(ledger.apply(event,cfg,transition,initial_state))
            result['delivered_event_ids'].append(event['event_id'])
        for control in controls:deliver(control)
        positions=tuple(sorted(state['positions']))
        remaining=limit-len(controls)
        try:
            observations=_batch(adapter.observations(positions,now,remaining),now,remaining,
                                positions=positions,observations=True,cfg=cfg)
        except (ValueError,TypeError,KeyError,OSError):
            observations=[]
            result['status']='OBSERVATIONS_UNAVAILABLE'
        for event in observations:deliver(event)
        if {event['mint'] for event in observations}!=set(positions):
            result['status']='OBSERVATIONS_UNAVAILABLE'
        remaining-=len(observations)
        result['monitor']=tick(path,cfg,now=now)
        if result['monitor']['status'] not in ('NO_CHANGE','MARKS_EXPIRED'):
            result['status']=result['monitor']['status']
        if result['status']=='COMPLETE' and remaining:
            try:
                candidates=_batch(adapter.candidates(positions,now,remaining),now,remaining,
                                  positions=positions,observations=False,cfg=cfg)
            except (ValueError,TypeError,KeyError,OSError):
                candidates=[]
                result['status']='CANDIDATES_UNAVAILABLE'
            for event in candidates:deliver(event)
    finally:ledger.close()
    result['paper']=paper_status(path,now=now)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description='Supervised offline synthetic paper runner; no live authorization')
    parser.add_argument('--config',default='config/paper.json')
    commands=parser.add_subparsers(dest='command',required=True)
    init=commands.add_parser('init',help='Create a NEW synthetic experiment; refuses existing files')
    init.add_argument('--db',required=True)
    once=commands.add_parser('once',help='One bounded offline fixture pass; never continuously polls')
    once.add_argument('--db',required=True)
    once.add_argument('--fixture',required=True)
    once.add_argument('--now',type=int,required=True)
    once.add_argument('--limit',type=int,default=MAX_EVENTS)
    once.add_argument('--control',choices=['PAUSE_ENTRY','EXIT_ONLY','LIQUIDATE','RESUME'])
    args=parser.parse_args(argv)
    cfg=load_config(args.config)
    try:
        if args.command=='init':result=initialize(args.db,cfg)
        else:
            adapter=FixtureAdapter.from_file(args.fixture,cfg=cfg)
            controls=[] if not args.control else [{'schema_version':1,'event_id':f'paper-runner:control:{args.control}:{args.now}',
                'ts':args.now,'kind':'control','actor':'operator','command':args.control}]
            result=run_once(args.db,cfg,adapter,now=args.now,controls=controls,limit=args.limit)
    except (ValueError,OSError,sqlite3.Error):
        parser.exit(2,'Paper runner unavailable; inspect the synthetic fixture/new experiment and saved checkpoint.\n')
    print(json.dumps(result,indent=2,allow_nan=False))
    return 0 if result.get('status') in ('COMPLETE','LEDGER_PRESENT') else 1


if __name__=='__main__':
    raise SystemExit(main())
