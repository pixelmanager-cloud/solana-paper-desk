"""Coordinator cutover after independent pins, CI and preservation PASS.

Prepared for independent coordinator review; never run as part of this review.
"""
import os,re,sys,json,tempfile,shlex,subprocess,time,pwd,stat
from pathlib import Path
sha,source=sys.argv[1:3]
if not re.fullmatch('[0-9a-f]{64}',source):raise ValueError('Exact reviewed desk source required')
if os.geteuid()!=0 or not re.fullmatch('[0-9a-f]{40}',sha):raise ValueError('Root and full release SHA required')
release='/opt/solana-desk-releases/'+sha
old='/opt/solana-desk-releases/0dcc2117b094bfced0e92feebac9f2df47cfd1d4'
ledger='/var/lib/solana-desk/paper-kraken-77de75a2.sqlite'
config='/etc/solana-paper/paper-kraken-reviewed.json'
pacing='/var/lib/solana-desk/provider-pacing.sqlite'
services=['desk-dashboard','desk-paper-held-cycle','desk-paper-monitor','desk-decisions','desk-backup']
timers=['desk-paper-held-cycle.timer','desk-paper-monitor.timer','desk-decisions.timer','desk-backup.timer','desk-discovery.timer']
extra=['desk-discovery.service','desk-recorder.service','desk-continuous-discovery.service']
all_units=[s+'.service' for s in services]+timers+extra+['desk-paper-entry-dispatcher.service','desk-paper-entry-dispatcher.timer']

def call(*args):return subprocess.check_output(args,text=True).strip()
def prop(unit,key):return call('systemctl','show',unit,'-p',key,'--value')
def run(*args):subprocess.run(args,check=True)
def effective_command(unit):
 raw=prop(unit,'ExecStart')
 match=re.fullmatch(r'\{ path=([^;{}]+) ; argv\[\]=(.*?) ; ignore_errors=(?:yes|no) ; [^{}]* \}',raw)
 if raw.count('argv[]=')!=1 or match is None:raise ValueError('Exactly one effective command required: '+unit)
 args=shlex.split(match.group(2))
 if not args or args[0]!=match.group(1).strip():raise ValueError('Effective executable mismatch: '+unit)
 return args

def cadences():
 result=[]
 for unit,seconds in (('desk-paper-entry-dispatcher.timer',10),('desk-paper-held-cycle.timer',15)):
  path=Path('/etc/systemd/system')/(unit+'.d')/'90-reviewed-cadence.conf'
  expected='[Timer]\nOnActiveSec=\nOnUnitInactiveSec=\nOnActiveSec='+str(seconds)+'s\nOnUnitInactiveSec='+str(seconds)+'s\nAccuracySec=1s\nRandomizedDelaySec=0\n'
  if path.resolve(strict=True)!=path or not path.is_file() or path.read_text()!=expected:raise ValueError('Exact unchanged cadence required: '+unit)
  result.append((unit,path,expected))
 return result

def limits():
 for unit,timeout in (('desk-paper-entry-dispatcher.service','10min'),('desk-paper-held-cycle.service','2min')):
  if prop(unit,'TimeoutStartUSec')!=timeout or prop(unit,'MemoryMax')!='402653184':raise ValueError('Reviewed timeout/memory changed: '+unit)

def effective_cadences():
 for unit,seconds in (('desk-paper-entry-dispatcher.timer',10),('desk-paper-held-cycle.timer',15)):
  raw=prop(unit,'TimersMonotonic')
  pattern=r'\{ (OnUnitInactiveUSec|OnActiveUSec)=([^;{}]+) ; next_elapse=[^{}]* \}'
  matches=list(re.finditer(pattern,raw))
  if re.sub(pattern,'',raw).strip() or len(matches)!=2 or sorted((m[1],m[2].strip()) for m in matches)!=sorted((k,str(seconds)+'s') for k in ('OnUnitInactiveUSec','OnActiveUSec')):
   raise ValueError('Exact effective timer intervals required: '+unit)
  if tuple(prop(unit,k) for k in ('AccuracyUSec','RandomizedDelayUSec','Persistent'))!=('1s','0','no'):
   raise ValueError('Effective timer policy mismatch: '+unit)

os.umask(0o077)
complete=False
try:
 for unit in all_units:
  if prop(unit,'ActiveState') not in ('inactive','failed'):raise ValueError('Not quiescent: '+unit)
 cadence=cadences()
 effective_cadences()
 limits()
 if Path(release).resolve(strict=True)!=Path(release):raise ValueError('Canonical release required')
 probe="from desk.runtime_compatibility import implementation_hash\nfrom desk.paper_cycle_cli import _config\nfrom desk.model import digest\nfrom desk.paper_cycle import _state\nfrom pathlib import Path\nfrom desk.engine import initial_state\nc=_config('/etc/solana-paper/paper-kraken-reviewed.json')\nif implementation_hash()!=%r:raise ValueError('Source mismatch')\nif digest(c)!='3dc3108e1c23ebbad97b88338e37f4012d3dd6b259f5a911624d799da1c11dff':raise ValueError('Config mismatch')\nif _state(Path('/var/lib/solana-desk/paper-kraken-77de75a2.sqlite'),c)!=initial_state(c):raise ValueError('Initial ledger mismatch')\n"%source
 subprocess.run(['/opt/solana-desk/.venv/bin/python','-B','-c',probe],cwd=release,check=True,env=dict(os.environ,DESK_PROVIDER_PACING_DB=pacing))
 base='[Service]\nWorkingDirectory='+old+'\nEnvironment=DESK_PROVIDER_PACING_DB='+pacing+'\nEnvironment=DESK_PAPER_LEDGER_DB='+ledger+'\n'
 suffix={s:'' for s in services}
 suffix['desk-paper-held-cycle']='ExecStart=\nExecStart=/opt/solana-desk/.venv/bin/python -m desk.paper_monitor_service --config '+config+' --research-db /var/lib/solana-desk/research.sqlite --evidence-db /var/lib/solana-desk/evidence.sqlite --ledger-db '+ledger+' --pool-fee-bps 25 --systemd-credentials\n'
 suffix['desk-paper-monitor']='ExecStart=\nExecStart=/opt/solana-desk/.venv/bin/python -m desk paper-monitor --db '+ledger+' --config '+config+'\n'
 plans=[]
 for s in services:
  p=Path('/etc/systemd/system')/(s+'.service.d')/'99-profile2-reviewed.conf'
  raw=p.read_text()
  if p.resolve(strict=True)!=p or raw!=base+suffix[s]:raise ValueError('Unexpected dropin: '+s)
  plans.append((p,raw,raw.replace(old,release)))
 dispatcher_path=Path('/etc/systemd/system/desk-paper-entry-dispatcher.service')
 expected_dispatcher=Path('/opt/solana-desk-tests/desk-paper-entry-dispatcher-03da7c9.service').read_text().replace('/opt/solana-desk-releases/03da7c947128cfa94111b8faf8e1714203399b39',old)
 if dispatcher_path.resolve(strict=True)!=dispatcher_path or dispatcher_path.read_text()!=expected_dispatcher or expected_dispatcher.count(old)!=1:raise ValueError('Original dispatcher unit mismatch')
 plans.append((dispatcher_path,expected_dispatcher,expected_dispatcher.replace(old,release)))
 lock=Path('/var/lib/solana-desk/paper-scheduler.lock')
 account=pwd.getpwnam('solana-desk');parent=lock.parent.stat()
 if parent.st_uid!=account.pw_uid or stat.S_IMODE(parent.st_mode)!=0o700:raise ValueError('Scheduler parent protection mismatch')
 info=lock.lstat()
 if lock.resolve(strict=True)!=lock or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=account.pw_uid or stat.S_IMODE(info.st_mode)!=0o600:raise ValueError('Existing protected scheduler lock required')
 identity=str(info.st_dev)+':'+str(info.st_ino)
 modes={'desk-paper-entry-dispatcher':'entry','desk-paper-held-cycle':'held','desk-paper-monitor':'expire','desk-decisions':'decisions'}
 expected_commands={}
 for name in (*modes,'desk-dashboard'):
  unit=name+'.service';directory=Path('/etc/systemd/system')/(unit+'.d')
  directory.mkdir(mode=0o755,exist_ok=True)
  path=directory/'zz-paper-scheduler-reviewed.conf'
  if path.resolve(strict=True)!=path:raise ValueError('Canonical scheduler override required')
  text='[Service]\nEnvironment=DESK_PAPER_SCHEDULER_IDENTITY='+identity+'\n'
  current=effective_command(unit)
  if name=='desk-dashboard':text+='Environment=DESK_PAPER_SCHEDULER_LOCK='+str(lock)+'\n'
  else:
   prefix=['/opt/solana-desk/.venv/bin/python','-m','tools.paper_scheduler','--research-db','/var/lib/solana-desk/research.sqlite','--mode',modes[name],'--']
   if current[:len(prefix)]!=prefix or len(current)==len(prefix):raise ValueError('Existing reviewed scheduler command required')
   text+='ExecStart=\nExecStart='+shlex.join(current)+'\n'
  if path.read_text()!=text:raise ValueError('Existing exact scheduler override mismatch')
  expected_commands[name]=current
 backup=Path(tempfile.mkdtemp(prefix='paper-ready-unit-',dir='/var/backups/solana-desk'))
 for p,raw,new in plans:(backup/(p.parent.name+'-'+p.name)).write_text(raw)
 for unit,p,text in cadence:(backup/(p.parent.name+'-'+p.name)).write_text(text)
 for p,raw,new in plans:
  if (p.read_text() if p.exists() else '')!=raw:raise ValueError('Dropin changed')
  tmp=p.with_name(p.name+'.paper-ready-new')
  with tmp.open('x') as f:f.write(new);f.flush();os.fsync(f.fileno())
  tmp.chmod(0o644);tmp.replace(p)
 run('systemctl','daemon-reload')
 for s in services:
  unit=s+'.service';env=dict(x.split('=',1) for x in shlex.split(prop(unit,'Environment')))
  if prop(unit,'WorkingDirectory')!=release or env.get('DESK_PAPER_LEDGER_DB')!=ledger or env.get('DESK_PROVIDER_PACING_DB')!=pacing:raise ValueError('Effective binding mismatch: '+s)
  cmd=prop(unit,'ExecStart')
  if old in cmd:raise ValueError('Old release command binding')
  if s in ('desk-paper-held-cycle','desk-paper-monitor') and (config not in cmd or ledger not in cmd):raise ValueError('Effective config mismatch')
 if prop('desk-paper-entry-dispatcher.service','WorkingDirectory')!=release or prop('desk-paper-entry-dispatcher.timer','ActiveState') not in ('inactive','failed'):raise ValueError('Dispatcher release/timer mismatch')
 for name in (*modes,'desk-dashboard'):
  unit=name+'.service';env=dict(x.split('=',1) for x in shlex.split(prop(unit,'Environment')))
  if env.get('DESK_PAPER_SCHEDULER_IDENTITY')!=identity:raise ValueError('Effective scheduler identity mismatch')
  if name=='desk-dashboard' and env.get('DESK_PAPER_SCHEDULER_LOCK')!=str(lock):raise ValueError('Dashboard scheduler absent')
  if effective_command(unit)!=expected_commands[name]:raise ValueError('Exact effective command mismatch: '+name)
 limits()
 if cadences()!=cadence:raise ValueError('Cadence files changed')
 effective_cadences()
 run('systemctl','start','desk-dashboard.service')
 for s in ('desk-paper-held-cycle','desk-paper-monitor','desk-decisions'):
  before=int(prop(s+'.service','ExecMainStartTimestampMonotonic') or '0')
  run('systemctl','start',s+'.service')
  after=int(prop(s+'.service','ExecMainStartTimestampMonotonic') or '0')
  if prop(s+'.service','ConditionResult')!='yes' or after<=before:raise ValueError('One-shot did not execute: '+s)
  if prop(s+'.service','Result')!='success' or prop(s+'.service','ExecMainStatus')!='0':raise ValueError('Service failed: '+s)
 run('systemctl','start',*timers[:-1],'desk-continuous-discovery.service')
 for unit in ['desk-dashboard.service','desk-continuous-discovery.service']+timers[:-1]:
  if prop(unit,'ActiveState')!='active':raise ValueError('Not active: '+unit)
 for attempt in range(20):
  rows=call('ss','-H','-lnt','( sport = :8765 )').splitlines()
  if rows:break
  time.sleep(.1)
 if not rows or any(r.split()[3]!='127.0.0.1:8765' for r in rows):raise ValueError('Loopback listener mismatch')
 for unit in ('desk-paper-entry-dispatcher.service','desk-paper-entry-dispatcher.timer'):
  if prop(unit,'ActiveState') not in ('inactive','failed'):raise ValueError('Dispatcher unexpectedly active after starts: '+unit)
 complete=True
 print(json.dumps({'status':'REVIEWED_SERVICES_STARTED_LOOPBACK_VERIFIED_DISPATCH_TIMER_OFF','release':release,'unit_backup':str(backup)}))
finally:
 if not complete:
  stopped=subprocess.run(['systemctl','stop',*all_units],check=False)
  failures=[]
  for unit in all_units:
   try:
    state=prop(unit,'ActiveState')
    if state not in ('inactive','failed'):failures.append(unit+':'+state)
   except Exception as exc:failures.append(unit+':verification-failed:'+type(exc).__name__)
  if stopped.returncode or failures:raise RuntimeError('CRITICAL cleanup failure: stop_exit='+str(stopped.returncode)+'; '+','.join(failures))
