"""Real history replay reuses only immutable source bytes, never observations."""
from contextlib import closing
import unittest
from unittest.mock import patch
from desk import paper_terminal_reconciliation as terminal
from desk.replay_history import replay_history
from tests import test_replay_history as fixtures

class ReplayByteCacheTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.HistoryReplayTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.store=self.f.store
        self.observations,self.coverage=self.f.capture([{'data':[self.f.raw]}])

    def test_actual_replay_reuses_bytes_but_redecodes_and_returns_fresh_objects(self):
        import desk.history as history
        with patch.object(terminal,'digest',wraps=terminal.digest) as checksum,patch.object(history,'decode',wraps=history.decode) as decode:
            with terminal.verified_bytes_scope():
                first=replay_history(self.coverage,self.store)
                checks=checksum.call_count;calls=decode.call_count
                first[0][0]['signature']='caller mutation'
                second=replay_history(self.coverage,self.store)
                self.assertEqual(second,(self.observations,self.coverage))
                self.assertEqual(checksum.call_count,checks)
                self.assertGreater(decode.call_count,calls)  # no observation/proof cache
            with terminal.verified_bytes_scope():replay_history(self.coverage,self.store)
            self.assertEqual(checksum.call_count,2*checks)
        self.assertIsNone(terminal._GATE_BYTES.get())

    def test_hit_rechecks_payload_scalar_and_deletion(self):
        key=self.coverage['pages'][0]['payload_hash']
        for sql in ("UPDATE pages SET payload=x'00' WHERE hash=?",'UPDATE pages SET raw_bytes=raw_bytes+1 WHERE hash=?','DELETE FROM pages WHERE hash=?'):
            with closing(self.store.connect()) as c:before=c.execute('SELECT hash,payload,raw_bytes FROM pages WHERE hash=?',(key,)).fetchone()
            try:
                with terminal.verified_bytes_scope():
                    replay_history(self.coverage,self.store)
                    with closing(self.store.connect()) as c:c.execute(sql,(key,))
                    with self.assertRaises(ValueError):replay_history(self.coverage,self.store)
            finally:
                with closing(self.store.connect()) as c:c.execute('INSERT OR REPLACE INTO pages VALUES(?,?,?)',before)

    def test_subclass_bounds_not_bypassed_by_warm_terminal_cache(self):
        from desk.paper_target_export import _Store,ExportBlocked
        key=self.store.save({'padding':'x'*(2*1024*1024)})
        restricted=_Store(self.store.path,read_only=True)
        with terminal.verified_bytes_scope():
            terminal._load(self.store,key)
            with self.assertRaises(ExportBlocked):restricted.load(key)

    def test_bounded_fallback_revalidates_and_standalone_load_remains_uncached(self):
        with patch.object(terminal,'MAX_GATE_CACHE_BYTES',1),terminal.verified_bytes_scope(),patch.object(terminal,'digest',wraps=terminal.digest) as checksum:
            replay_history(self.coverage,self.store);first=checksum.call_count
            replay_history(self.coverage,self.store)
            self.assertEqual(checksum.call_count,2*first)
            self.assertEqual(terminal._GATE_BYTES.get()['pages'],{})
        with patch.object(terminal,'_load',side_effect=AssertionError('scope required')):
            self.assertEqual(replay_history(self.coverage,self.store),(self.observations,self.coverage))
