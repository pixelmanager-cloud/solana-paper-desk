"""Synthetic public-schema trades; no provider, credential or production data."""
import base64
import copy
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from desk import kraken_usd_observation as usd, provider_pacing as pace, kraken_pacing_migration as migration
from desk.model import canonical,digest

RAW=b'{"error":[],"result":{"SOLUSD":[["109.08000","0.66455880",1000.2493582,"s","l","",34299138]],"last":"1000249358226"}}'

class KrakenUsdTests(unittest.TestCase):
    def parse(self,raw=RAW,now='1002.220369',at='1002.220369',status=200):
        return usd.parse_kraken_usd(usd.KrakenTradesResponse('GET',usd.URL,raw,at,status),bounds=usd.TrustedTimeBounds(now))
    def test_exact_decimal_recent_trade_no_slot_and_age_boundaries(self):
        v=self.parse();self.assertEqual(v.status,'MEASURED');self.assertEqual(v.usd_price,Decimal('109.08000'));self.assertEqual(v.trade_at,'1000.2493582');self.assertIsNone(v.block_id)
        self.assertEqual(self.parse(now='1030.2493582',at='1030.2493582').status,'MEASURED')
        self.assertEqual(self.parse(now='1030.2493583',at='1030.2493583').status,'UNKNOWN')
        self.assertEqual(self.parse(now='1012.220369').status,'MEASURED')
        self.assertEqual(self.parse(now='1012.220370').status,'UNKNOWN')
        self.assertEqual(self.parse(now='1000.2',at='1000.2').status,'UNKNOWN')
        tiny=RAW.replace(b'1000.2493582',b'970.2203689999999999999999999999999999999999999999')
        self.assertEqual(self.parse(tiny).status,'UNKNOWN')
    def test_duplicate_error_nonpositive_wrong_pair_and_malformed_numeric_reject(self):
        for raw in [RAW.replace(b'"error":[]',b'"error":[],"error":[]'),RAW.replace(b'"error":[]',b'"error":["EGeneral:Permission denied"]'),RAW.replace(b'SOLUSD',b'SOLEUR'),RAW.replace(b'109.08000',b'0.00000'),RAW.replace(b'1000.2493582',b'NaN'),RAW.replace(b'1000.2493582',b'1e9999'),RAW.replace(b'34299138',b'true'),RAW.replace(b'"last":"1000249358226"',b'"last":1000249358226')]:
            with self.subTest(raw=raw):self.assertEqual(self.parse(raw).status,'UNKNOWN')
        self.assertEqual(self.parse(status=429).status,'UNKNOWN')
        with self.assertRaises(ValueError):self.parse(b'x'*65537)
        with self.assertRaises(ValueError):self.parse(at=1002.0)
    def test_version_config_default_and_malformed_optins(self):
        self.assertEqual(usd.selected({'mode':'paper'}),0)
        cfg={'mode':'paper','paper_signal_policy_version':3,'paper_quote_execution_version':1,'paper_usd_valuation_version':1}
        self.assertEqual(usd.selected(cfg),1)
        for key,value in [('mode','live'),('paper_usd_valuation_version',True),('paper_usd_valuation_version',2),('paper_quote_execution_version',0)]:
            with self.assertRaises(ValueError):usd.selected(cfg|{key:value})

class KrakenPacingTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.t=tempfile.TemporaryDirectory(dir='work');self.addCleanup(self.t.cleanup)
        self.path=Path(self.t.name).resolve()/'pace.sqlite';pace.initialize(self.path)
        self.policy=self.path.parent/'policy.json';self.policy.write_text(canonical({'version':1,'pins':[]}))
        p=patch.object(migration,'POLICY',self.policy);p.start();self.addCleanup(p.stop)
    def rows(self):
        with sqlite3.connect(self.path) as c:return [list(r) for r in c.execute('SELECT * FROM state ORDER BY provider')]
    def test_explicit_migration_preserves_nonzero_state_replay_and_shared_two_second_cadence(self):
        with sqlite3.connect(self.path) as c:c.execute("UPDATE state SET next_at=100,blocked_until=90,high_water=99 WHERE provider='jupiter'")
        old=self.rows();pin=migration.review_plan(self.path)
        with self.assertRaisesRegex(ValueError,'not reviewed'):migration.migrate(self.path)
        self.assertEqual(self.rows(),old)
        self.policy.write_text(canonical({'version':1,'pins':[pin]}));self.assertEqual(migration.migrate(self.path)['status'],'MIGRATED')
        self.assertEqual(self.rows()[:2],old);self.assertEqual(migration.migrate(self.path)['status'],'ALREADY_MIGRATED')
        clock=[100.0]
        p=pace.Pacer(self.path,clock=lambda:clock[0],monotonic=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n))
        first=p.acquire('kraken',timeout_seconds=3);p.finish('kraken',first)
        second=p.acquire('kraken',timeout_seconds=3);self.assertGreaterEqual(clock[0],102.0)
        p.throttle('kraken',{'Retry-After':'60'},ticket=second)
        with sqlite3.connect(self.path) as c:self.assertGreaterEqual(c.execute("SELECT blocked_until FROM state WHERE provider='kraken'").fetchone()[0],162)
    def test_pending_refuses_and_partial_guardless_migration_refuses(self):
        with sqlite3.connect(self.path) as c:c.execute("UPDATE state SET pending=? WHERE provider='jupiter'",('a'*32,))
        old=self.rows()
        with self.assertRaises(ValueError):migration.review_plan(self.path)
        self.assertEqual(self.rows(),old)
        with sqlite3.connect(self.path) as c:c.execute('UPDATE state SET pending=NULL');c.execute(migration.SQL)
        with self.assertRaises(pace.PacingError):pace.Pacer(self.path)

    def test_archived_old_pacer_refuses_new_table_without_state_mutation(self):
        import hashlib,types
        source=Path('tests/fixtures/provider-pacing-before-kraken.py.txt').read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest(),'134eb81875a50998402e079a72cc047076df4ec6b6428d1d6e51fae02cd3750c')
        module=types.ModuleType('archived_pre_kraken_pacer');exec(compile(source,'archived-provider-pacing.py','exec'),module.__dict__)
        pin=migration.review_plan(self.path);self.policy.write_text(canonical({'version':1,'pins':[pin]}));migration.migrate(self.path)
        before=self.rows()
        with self.assertRaises(module.PacingError):module.Pacer(self.path)
        self.assertEqual(self.rows(),before)

    def test_migration_guard_corruption_lower_cadence_and_failed_publication_refuse(self):
        pin=migration.review_plan(self.path);self.policy.write_text(canonical({'version':1,'pins':[pin]}))
        before=self.rows();guards=dict(migration.GUARDS)
        guards['crash']=f"CREATE TRIGGER crash BEFORE INSERT ON {migration.TABLE} BEGIN SELECT RAISE(ABORT,'synthetic crash'); END"
        with patch.object(migration,'GUARDS',guards),self.assertRaises(sqlite3.IntegrityError):migration.migrate(self.path)
        self.assertEqual(self.rows(),before)
        with sqlite3.connect(self.path) as c:self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name=?",(migration.TABLE,)).fetchone())
        migration.migrate(self.path)
        checked=pace.Pacer(self.path,clock=lambda:100,monotonic=lambda:1)
        with sqlite3.connect(self.path) as c:c.execute("UPDATE policy SET cadence=0.1 WHERE provider='kraken'")
        before=self.rows()
        with self.assertRaises(pace.PacingError):checked.acquire('kraken',timeout_seconds=3)
        self.assertEqual(self.rows(),before)
        with self.assertRaises(pace.PacingError):pace.Pacer(self.path)
        with sqlite3.connect(self.path) as c:c.execute("UPDATE policy SET cadence=2 WHERE provider='kraken'");c.execute(f'DROP TRIGGER {migration.TABLE}_update')
        with self.assertRaises(pace.PacingError):pace.Pacer(self.path)
