import unittest,tempfile,threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from desk.engine import transition,initial_state
from desk.ledger import Ledger
from tests.helpers import event,config,T
class EvidenceBoundaryTests(unittest.TestCase):
    def test_asserted_real_data_proofs_cannot_enable_entry(self):
        cfg=config();e=event();state,outputs=transition(initial_state(cfg),e,cfg)
        quantity=next(o['quantity'] for o in outputs if o['type']=='fill')
        e['provenance']='MAINNET_OBSERVATION';e['taker']='claimed-wallet'
        e['sellability']={'kind':'sell_simulation','mint':e['mint'],'wallet':e['taker'],'quantity_tokens':quantity,
          'observed_at':T,'slot':123,'transaction_hash':'a'*64,'route_hash':'b'*64,'simulation_ok':True,
          'transaction_policy_ok':True,'wallet_account_ok':True,'net_proceeds_sol':'1'}
        state,out=transition(initial_state(cfg),e,cfg)
        self.assertFalse(state['positions']);self.assertEqual(state['cash'],cfg['initial_equity_sol'])
        self.assertIn('LIVE_FEATURE_ADAPTER_NOT_READY',out[-1]['reasons'])
    def test_concurrent_conflicting_raw_payload_is_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'capture.sqlite';Ledger(path).close();barrier=threading.Barrier(2)
            class Cursor:
                def __init__(self,cursor):self.cursor=cursor
                def fetchone(self):
                    result=self.cursor.fetchone();barrier.wait(timeout=5);return result
            class Connection:
                def __init__(self,db):self.db=db;self.first=True
                def execute(self,query,args=()):
                    cursor=self.db.execute(query,args)
                    if self.first and query.startswith('SELECT slot,payload'):
                        self.first=False;return Cursor(cursor)
                    return cursor
                def close(self):self.db.close()
            def write(n):
                ledger=Ledger(path);ledger.db=Connection(ledger.db)
                try:
                    try:return ledger.record_raw('same-signature',T,100,{'value':n})
                    except ValueError:return 'conflict'
                finally:ledger.close()
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(write,[1,2]))
            self.assertCountEqual(results,[True,'conflict'])
