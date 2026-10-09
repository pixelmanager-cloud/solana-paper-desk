"""Offline public-capture replay; timestamps here are fixture replay clocks.

The mint account below is taken from a saved unsigned simulation's post-state.
That provenance is retained explicitly; it is not a fresh mainnet acquisition or
execution witness. Production readers must supply actual read-only observations.
"""
import copy
from decimal import Decimal, localcontext
import json
from pathlib import Path
import unittest

from desk.live_observation import (ProviderObservation, ObservationError,
    ingest_mint, ingest_pool, ingest_quote)
from desk.model import digest
from desk.providers import SOL

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'


class LiveObservationTests(unittest.TestCase):
    def setUp(self):
        self.public = json.loads((FIXTURES / 'mainnet-roundtrip-simulation.json').read_text())
        self.now = self.public['result']['observed_at']
        self.mint = self.public['result']['mint']; self.taker = self.public['result']['wallet']
        sequence = self.public['result']['sequence']
        index = sequence['watch'].index(self.mint)
        account = sequence['result']['value']['transactionResults'][0]['postExecutionAccounts'][index]
        self.mint_payload = {'mint': self.mint, 'slot': sequence['slot'], 'observed_at': self.now,
                             'account': account, 'policy': {'decision': 'FORGED_NORMALIZED_PASS'}}
        self.token = ingest_mint(self.reader(self.mint_payload, 'fixture:unsigned-simulation-post-state'),
                                 mint=self.mint, now=self.now)

    def reader(self, payload, source='fixture:public-jupiter-replay', at=None):
        return lambda: ProviderObservation(source, self.now if at is None else at, payload)

    def quote_payload(self, direction='buy'):
        response = copy.deepcopy(self.public['quote_responses'][0 if direction == 'buy' else 1])
        return {'kind': 'unsigned_route_probe', 'observed_at': self.now,
                'request': {'inputMint': response['inputMint'], 'outputMint': response['outputMint'],
                            'amount': response['inAmount'], 'taker': self.taker, 'slippageBps': '100'},
                'response': response, 'unknown_provider_field': {'retain': [None, 'original']}}

    def ingest(self, payload=None, direction='buy', **kwargs):
        payload = self.quote_payload(direction) if payload is None else payload
        return ingest_quote(self.reader(payload), mint=self.token, direction=direction,
                            amount_raw=int(self.public['quote_responses'][0 if direction == 'buy' else 1]['inAmount']),
                            taker=self.taker, now=self.now, **kwargs)

    def test_public_buy_and_exact_partial_exit_quantities(self):
        buy = self.ingest(); sell = self.ingest(direction='sell')
        self.assertEqual((buy.input_raw, buy.estimated_output_raw, buy.minimum_output_raw),
                         (10000000, 2548941, 2523452))
        self.assertEqual(buy.input_units, Decimal('0.01'))
        self.assertEqual(buy.estimated_output_units, Decimal('2.548941'))
        self.assertEqual(sell.input_raw, buy.minimum_output_raw)
        self.assertEqual(sell.input_units, Decimal('2.523452'))
        self.assertEqual(sell.estimated_output_units, Decimal(self.public['quote_responses'][1]['outAmount']) / 10**9)
        self.assertIsNone(buy.executable_fill_proof); self.assertIsNone(sell.executable_fill_proof)
        self.assertIn('OWNERSHIP_HISTORY_UNKNOWN', buy.risk_flags)
        self.assertFalse(hasattr(buy, 'flow'))
        with localcontext() as ctx:
            ctx.prec = 100
            self.assertEqual(buy.estimated_sol_per_token, Decimal('0.01') / Decimal('2.548941'))

    def test_original_unknown_fields_hash_and_mutation_isolation(self):
        payload = self.quote_payload(); result = self.ingest(payload)
        self.assertEqual(result.source.raw_hash, digest(payload))
        self.assertEqual(json.loads(result.source.original_json), payload)
        payload['unknown_provider_field']['retain'][1] = 'changed'
        self.assertEqual(json.loads(result.source.original_json)['unknown_provider_field']['retain'][1], 'original')
        self.assertEqual(result.source.observed_at, self.now)
        self.assertEqual(result.mint_source.source_id, 'fixture:unsigned-simulation-post-state')

    def test_quote_identity_amount_mode_and_route_corruption(self):
        mutations = [lambda p: p['request'].update(taker=SOL),
                     lambda p: p['request'].update(amount='1'),
                     lambda p: p['response'].update(inputMint=self.mint),
                     lambda p: p['response'].update(inAmount='1'),
                     lambda p: p['response'].update(swapMode='ExactOut'),
                     lambda p: p['response'].update(outAmount=True),
                     lambda p: p['response'].update(otherAmountThreshold='0'),
                     lambda p: p['response'].update(otherAmountThreshold='999999999'),
                     lambda p: p['response'].update(slippageBps=101),
                     lambda p: p['response'].update(routePlan=[]),
                     lambda p: p['response'].update(routePlan=[None]),
                     lambda p: p.update(response=None),
                     lambda p: p.update(request=[]),
                     lambda p: p['response']['routePlan'][0].update(percent=50),
                     lambda p: p['response']['routePlan'][1]['swapInfo'].update(inAmount='1'),
                     lambda p: p['response']['routePlan'][1]['swapInfo'].update(outputMint=SOL)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                payload = self.quote_payload(); mutate(payload)
                with self.assertRaises(ValueError): self.ingest(payload)
        payload = self.quote_payload(); del payload['response']['outAmount']
        with self.assertRaises(ValueError): self.ingest(payload)

    def test_quote_pool_binding_and_no_fill_promotion(self):
        pool = self.quote_payload()['response']['routePlan'][-1]['swapInfo']['ammKey']
        result = self.ingest(expected_pool=pool)
        self.assertIn(pool, result.route_pools)
        with self.assertRaises(ValueError): self.ingest(expected_pool=SOL)
        intermediate_pool = self.quote_payload()['response']['routePlan'][0]['swapInfo']['ammKey']
        with self.assertRaises(ValueError): self.ingest(expected_pool=intermediate_pool)
        payload = self.quote_payload(); payload['response'].update(executable_fill_proof=True, verified=True)
        self.assertIsNone(self.ingest(payload).executable_fill_proof)

    def test_time_source_and_stale_mint_failures(self):
        for at in (self.now - 11, self.now + 1, True, None):
            with self.subTest(at=at), self.assertRaises(ValueError):
                ingest_quote(self.reader(self.quote_payload(), at=at) if at is not None else lambda: ProviderObservation('x', None, self.quote_payload()),
                             mint=self.token, direction='buy', amount_raw=10000000, taker=self.taker, now=self.now)
        with self.assertRaises(ValueError):
            ingest_mint(self.reader(self.mint_payload, ''), mint=self.mint, now=self.now)
        with self.assertRaises(ValueError):
            ingest_quote(self.reader(self.quote_payload(), at=self.now+11), mint=self.token,
                         direction='buy', amount_raw=10000000, taker=self.taker, now=self.now+11)
        payload = self.quote_payload(); payload['observed_at'] += 1
        with self.assertRaises(ValueError): self.ingest(payload)

    def test_raw_mint_hazards_override_normalized_policy(self):
        import base64
        for offset in (0, 46):
            payload = copy.deepcopy(self.mint_payload)
            raw = bytearray(base64.b64decode(payload['account']['data'][0])); raw[offset] = 1
            payload['account']['data'][0] = base64.b64encode(raw).decode()
            with self.assertRaises(ValueError):
                ingest_mint(self.reader(payload), mint=self.mint, now=self.now)
        for key, value in (('mint', SOL), ('slot', True)):
            payload = copy.deepcopy(self.mint_payload); payload[key] = value
            with self.assertRaises(ValueError): ingest_mint(self.reader(payload), mint=self.mint, now=self.now)

    def test_public_pool_known_token2022_hazard_rejects(self):
        public = json.loads((FIXTURES / 'mainnet-pool-fee-snapshot.json').read_text())
        with self.assertRaisesRegex(ValueError, 'TOKEN_2022'):
            ingest_pool(self.reader(public['capture'], 'fixture:public-pool-replay'),
                        mint=public['mint'], pool=public['pool'], now=self.now)

    def test_malformed_raw_mint_is_an_observation_error(self):
        for data in ([[], 'base64'], [None, 'base64'], ['!invalid!', 'base64'], [], None):
            payload = copy.deepcopy(self.mint_payload)
            payload['account']['data'] = data
            original = copy.deepcopy(payload)
            with self.subTest(data=data), self.assertRaises(ObservationError):
                ingest_mint(self.reader(payload), mint=self.mint, now=self.now)
            self.assertEqual(payload, original)

    def persisted_pool(self, store):
        from tests.test_pools import PoolTests
        from desk.pools import verify_pool
        helper = PoolTests(); helper.setUp()
        result = verify_pool(str(helper.pool), str(helper.mint), helper.rpc, capture=store.save)
        return helper, store.load(result['evidence_hash'])

    def test_persisted_atomic_vault_controls_reject_corrupt_bytes(self):
        import base64
        import tempfile
        from desk.evidence import EvidenceStore
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / 'evidence.sqlite')
            helper, original = self.persisted_pool(store)
            for index in (0, 1):
                for start, end, value, reason in ((121, 129, 1, 'delegated amount'),
                                                (109, 113, 2, 'native option'),
                                                (109, 113, 2**32-1, 'native option')):
                    payload = copy.deepcopy(original)
                    raw = bytearray(base64.b64decode(payload['result']['value'][index]['data'][0]))
                    self.assertEqual(raw[72:76], bytes(4))
                    raw[start:end] = value.to_bytes(end-start, 'little')
                    payload['result']['value'][index]['data'][0] = base64.b64encode(raw).decode()
                    key = store.save(payload)
                    with self.subTest(vault=index, start=start, value=value), self.assertRaisesRegex(ObservationError, reason):
                        ingest_pool(self.reader(store.load(key), 'fixture:persisted-corrupt-controls'),
                                    mint=str(helper.mint), pool=str(helper.pool), now=self.now)
                    self.assertEqual(store.load(key), payload)
                    self.assertEqual(payload['params'], original['params'])
            self.assertEqual(store.load(digest(original)), original)

    def test_persisted_legitimate_wsol_native_option_and_original_identity(self):
        import base64
        import tempfile
        from desk.evidence import EvidenceStore
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / 'evidence.sqlite')
            helper, payload = self.persisted_pool(store)
            params = copy.deepcopy(payload['params'])
            # Supported WSOL vault's Some(native rent reserve); no delegate.
            raw = bytearray(base64.b64decode(payload['result']['value'][1]['data'][0]))
            raw[109:113] = (1).to_bytes(4, 'little')
            raw[113:121] = (2039280).to_bytes(8, 'little')
            payload['result']['value'][1]['data'][0] = base64.b64encode(raw).decode()
            key = store.save(payload)
            observation = ingest_pool(self.reader(store.load(key), 'fixture:persisted-native-wsol'),
                                      mint=str(helper.mint), pool=str(helper.pool), now=self.now)
            self.assertEqual(observation.source.raw_hash, key)
            self.assertEqual(json.loads(observation.source.original_json), payload)
            self.assertEqual(store.load(key)['params'], params)
            self.assertIsNone(observation.executable_fill_proof)
            self.assertIn('OWNERSHIP_HISTORY_UNKNOWN', observation.risk_flags)

    def test_pool_requests_never_rewritten(self):
        public = json.loads((FIXTURES / 'mainnet-pool-snapshot.json').read_text())
        for change in ('mint', 'pool', 'floor', 'commitment', 'missing'):
            payload = copy.deepcopy(public['capture']); mint, pool = public['mint'], public['pool']
            if change == 'mint': mint = SOL
            if change == 'pool': pool = SOL
            if change == 'floor': payload['params'][1]['minContextSlot'] += 1
            if change == 'commitment': payload['params'][1]['commitment'] = 'processed'
            if change == 'missing': payload['result']['value'].pop()
            with self.subTest(change=change), self.assertRaises(ValueError):
                ingest_pool(self.reader(payload), mint=mint, pool=pool, now=self.now)

    def test_supported_atomic_pool_units_and_exact_original_replay(self):
        # Existing protocol fixture builder, explicitly synthetic source identity.
        from tests.test_pools import PoolTests
        from desk.pools import verify_pool
        helper = PoolTests(); helper.setUp()
        captures = []
        def save(envelope):
            captures.append(envelope); return digest(envelope)
        result = verify_pool(str(helper.pool), str(helper.mint), helper.rpc, capture=save)
        payload = captures[0]
        observation = ingest_pool(self.reader(payload, 'fixture:synthetic-pool'),
                                  mint=str(helper.mint), pool=str(helper.pool), now=self.now)
        self.assertEqual(observation.reserve_tokens_raw, 1000000)
        self.assertEqual(observation.reserve_lamports, 1000000)
        self.assertEqual(observation.reserve_tokens, Decimal('1'))
        self.assertEqual(observation.reserve_sol, Decimal('0.001'))
        self.assertEqual(observation.spot_sol_per_token, Decimal('0.001'))
        self.assertEqual(observation.slot, result['slot'])
        self.assertEqual(observation.source.raw_hash, digest(payload))
        self.assertIsNone(observation.executable_fill_proof)
        # A candidate's safe normalized summary cannot override raw controls.
        payload['verified'] = True
        import base64
        raw = bytearray(base64.b64decode(payload['result']['value'][0]['data'][0]))
        raw[72] = 1
        payload['result']['value'][0]['data'][0] = base64.b64encode(raw).decode()
        with self.assertRaises(ValueError):
            ingest_pool(self.reader(payload), mint=str(helper.mint), pool=str(helper.pool), now=self.now)
