"""Exact dependency rollover proposal; original sentinel judgment stays declined.

Golden hashes independently compared using old decode.py at4d42b13 (SHA
 d7a0a3c0c9a525b5c1e38a890540e715bf67cdff1f294ceead1503e6ef3ec93d)
versus the proposed decoder. No alternative historical decoder is shipped.
"""
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch
from desk import decode as decoder,paper_migration_no_entry as recovery
from desk import _migration_decline_legacy_v1 as legacy
from desk.model import digest
from desk.programs import unbase58
from desk.security import base58
from tests.test_graduation_witness import fixture


class LegacyDecoderRolloverTests(unittest.TestCase):
    def sentinel(self):
        raw,mint,pool=fixture('migrate')
        ix=raw['meta']['innerInstructions'][0]['instructions'][0]
        ix['data']=base58(unbase58(ix['data'])[:-32]+bytes(32))
        return raw,mint,pool
    def test_exact_dependency_bytes_and_identical_original_decline_golden(self):
        self.assertEqual(hashlib.sha256(Path(decoder.__file__).read_bytes()).hexdigest(),recovery.LEGACY_DEPENDENCIES['decode.py'])
        self.assertEqual(hashlib.sha256(Path(legacy.__file__).read_bytes()).hexdigest(),recovery.LEGACY_HASH)
        raw,mint,pool=self.sentinel();before=digest(raw)
        self.assertEqual(digest(decoder.decode(raw)),'985f75e894b8c9fa266c8c28e9e57b71d8e82c389e3a94bb45f4831ee6d0f9dc')
        result=legacy.extract_graduation([raw],mint=mint,pool=pool,now=2000,provenance='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE')
        self.assertEqual(digest(result),'f44bcab0a7b50f090bce018306f29e57efac99ad6c46f195194c7ccb1ff9b8f6')
        self.assertEqual(result['status'],'UNKNOWN');self.assertEqual(result['witnesses'],[])
        self.assertFalse(result['entry_authorized']);self.assertEqual(digest(raw),before)
        hint={'mint':mint,'pool':pool,'signature':raw['transaction']['signatures'][0],'slot':raw['slot']}
        measured,reason=recovery._decline([raw],hint,2000,True)
        self.assertEqual(measured,result);self.assertEqual(reason,'HISTORICAL_LEGACY_SENTINEL_DECLINED')
    def test_unknown_dependency_still_refuses_no_fallback_allowlist(self):
        raw,mint,pool=self.sentinel()
        hint={'mint':mint,'pool':pool,'signature':raw['transaction']['signatures'][0],'slot':raw['slot']}
        with patch.dict(recovery.LEGACY_DEPENDENCIES,{'decode.py':'f'*64}):
            with self.assertRaisesRegex(ValueError,'Historical decoder dependency changed'):
                recovery._decline([raw],hint,2000,True)
