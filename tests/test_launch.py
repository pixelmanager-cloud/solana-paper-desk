import copy,json,unittest
from pathlib import Path
from desk.decode import decode
from desk.launch import launch_anchor
class LaunchTests(unittest.TestCase):
    def setUp(self):
        p=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-launch.json').read_text())
        self.d=decode(p['payload']);self.d['commitment']='finalized_provider_response'
        self.event=next(x for x in self.d['program_observations'] if x.get('name')=='CreateEvent')
        self.mint=self.event['fields']['mint'];self.d['block_time']=self.event['fields']['timestamp']
    def test_corroborated_creation_only(self):
        r=launch_anchor(self.d,self.mint);self.assertTrue(r['verified']);self.assertFalse(r['transfer_history_complete'])
    def test_confirmed_stream_is_not_finalized(self):
        self.d['commitment']='confirmed';self.assertFalse(launch_anchor(self.d,self.mint)['verified'])
    def test_spoofed_curve(self):
        self.event['fields']['bonding_curve']=self.mint
        self.assertIn('LAUNCH_CURVE_PDA_MISMATCH',launch_anchor(self.d,self.mint)['reasons'])
    def test_missing_initialization(self):
        self.d['mint_initializations']=[];self.assertFalse(launch_anchor(self.d,self.mint)['verified'])
    def test_event_from_different_instruction(self):
        self.event['instruction']='8.1';self.assertIn('LAUNCH_INSTRUCTION_SCOPE_MISMATCH',launch_anchor(self.d,self.mint)['reasons'])
    def test_unknown_event_suffix(self):
        self.event['schema_complete']=False;self.assertFalse(launch_anchor(self.d,self.mint)['verified'])
    def test_missing_chain_time(self):
        self.d['block_time']=None;self.assertFalse(launch_anchor(self.d,self.mint)['verified'])
