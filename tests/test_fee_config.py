import base64,copy,json,unittest
from pathlib import Path
from desk.fee_config import parse_config,config_address
class FeeConfigTests(unittest.TestCase):
    def setUp(self):
        self.p=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-fee-config.json').read_text())
        self.a=copy.deepcopy(self.p['response']['value'])
    def mutate(self,raw):self.a['data'][0]=base64.b64encode(raw).decode()
    def test_program_owned_mainnet_configuration_decodes_exactly(self):
        self.assertEqual(config_address(),self.p['address']);r=parse_config(self.a)
        self.assertTrue(r['configuration_complete']);self.assertEqual(len(r['fields']['protocol_fee_recipients']),8)
    def test_wrong_program_rejected(self):
        self.a['owner']='11111111111111111111111111111111'
        with self.assertRaises(ValueError):parse_config(self.a)
    def test_truncation_not_defaulted_to_safe_configuration(self):
        self.mutate(base64.b64decode(self.a['data'][0])[:100]);self.assertFalse(parse_config(self.a)['configuration_complete'])
    def test_unknown_extra_bytes_rejected(self):
        self.mutate(base64.b64decode(self.a['data'][0])+b'\x00')
        self.assertIn('FEE_CONFIG_UNKNOWN_BYTES',parse_config(self.a)['reasons'])
    def test_invalid_fee_rate_rejected(self):
        raw=bytearray(base64.b64decode(self.a['data'][0]));raw[40:48]=(10001).to_bytes(8,'little');self.mutate(raw)
        self.assertIn('FEE_CONFIG_RATE_INVALID',parse_config(self.a)['reasons'])
