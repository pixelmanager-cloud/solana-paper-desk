import os,hashlib,subprocess,time,sys,re
from pathlib import Path
sha=sys.argv[1]
if not re.fullmatch('[0-9a-f]{40}',sha):raise ValueError('Exact reviewed release SHA required')
release='/opt/solana-desk-releases/'+sha
source='bf98b9c7be3d1480bf3bf75f2dcb0a7df452bafcc2504fe3f7c118d19762ffb2'
root=Path('/opt/solana-desk-tests')
expected={'backup-dispatch-recovery.py': '9d5dfd9d79736bd62b82beee9bf935ce5d7cc5b3597cd40142012ced19e0599b', 'activate-empty-successor-reviewed.py': 'b72a4abb1956add2d2bf8bddc4f0dad966b21d716c35839908de60f8ea74c331', 'verify-empty-successor-originals.py': '86f4c3006d34f2655b97e5712a3fa105e7d6f9e883092c8d570311b1ff74f79c', 'verify-empty-successor-active-context.py': 'a4dcc739128943161971fbd16d20e5fd3f94db981292cb179467bfb92f1e08b4', 'start-empty-successor-reviewed.py': 'cc91a3f2c5f251a278309b98638007f774e53fd36faac456e18a06c4f9944ad8'}
for name,wanted in expected.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=wanted:raise ValueError('Reviewed artifact changed: '+name)
units=('desk-dashboard.service','desk-paper-held-cycle.service','desk-paper-monitor.service','desk-decisions.service','desk-backup.service','desk-discovery.service','desk-recorder.service','desk-continuous-discovery.service','desk-paper-held-cycle.timer','desk-paper-monitor.timer','desk-decisions.timer','desk-backup.timer','desk-discovery.timer','desk-paper-entry-dispatcher.service','desk-paper-entry-dispatcher.timer')
subprocess.run(['systemctl','stop',*units],check=True)
env=dict(os.environ,PYTHONPATH=release,PYTHONDONTWRITEBYTECODE='1',DESK_PROVIDER_PACING_DB='/var/lib/solana-desk/provider-pacing.sqlite')
def run(name,*args):
 started=time.monotonic();print('BEGIN '+name,flush=True)
 subprocess.run(['/opt/solana-desk/.venv/bin/python','-B',str(root/name),*args],cwd=release,env=env,check=True)
 print('PASS '+name+' elapsed_seconds='+str(time.monotonic()-started),flush=True)
backup_args=('--source',source,'--phase','post','--new-ledger','/var/lib/solana-desk/paper-kraken-77de75a2.sqlite')
run('backup-dispatch-recovery.py',*backup_args,'--destination','/var/backups/solana-desk/pre-empty-successor-'+sha+'')
run('activate-empty-successor-reviewed.py','--source',source,'--pin-hash','6f13bf2fbd53e54c8c71d188ae89e817bd1d63c4673331b10b4cf52e35a6752a','--recovery-hash','915d412be08ee751d1774fe7908615f497b07e5b1a7dd3616cebb9132e567cf8')
run('verify-empty-successor-originals.py','--backup','/var/backups/solana-desk/pre-empty-successor-'+sha+'','--source',source)
run('verify-empty-successor-active-context.py','--pin',str(root/'kraken-reviewed-successor-pin.json'),'--pin-hash','494cbf72ecb0316cc792d237c1e084f3dd52e64521b483605b3ee182ae0e89f0','--config','/etc/solana-paper/paper-kraken-reviewed.json','--runtime-source',source,'--successor-pin',str(root/'empty-successor-reviewed-pin.json'),'--successor-hash','6f13bf2fbd53e54c8c71d188ae89e817bd1d63c4673331b10b4cf52e35a6752a')
run('backup-dispatch-recovery.py',*backup_args,'--destination','/var/backups/solana-desk/post-empty-successor-'+sha+'')
run('start-empty-successor-reviewed.py',sha,source)
