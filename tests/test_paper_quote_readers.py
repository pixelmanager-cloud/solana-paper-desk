"""Reader/pre-duplicate consistency across explicit quote and v3 experiments."""
import copy
import unittest
from decimal import localcontext
from desk import engine, quote_execution as qe
from desk.model import canonical
from desk.paper_checkpoint import read_checkpoint, RecoveryRequired, policy_version
from desk.paper_view import paper_status
from desk.experiment_report import experiment_report
from desk.ledger import Ledger
from tests import test_quote_execution as execution_fixture
from tests import test_quote_execution_v3_seam as seam_fixture
from tests.helpers import T


class QuoteReaderTests(unittest.TestCase):
    def setUp(self):
        self.f = execution_fixture.QuoteExecutionTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def save(self, state):
        self.f.ledger.db.execute('UPDATE state SET payload=? WHERE id=1', (canonical(state),))

    def test_entry_restart_view_and_duplicate_preserve_unverified_metadata(self):
        f = self.f; f.buy_fill()
        with localcontext() as context:
            context.prec = 512
            self.assertEqual(read_checkpoint(f.ledger.db), f.state())
        view = paper_status(f.path, now=T, expected_config=f.cfg)
        self.assertEqual(view['status'], 'LEDGER_PRESENT')
        self.assertEqual(view['execution_status'], 'EXECUTION_UNVERIFIED')
        self.assertFalse(view['automatic_entry_enabled'])
        before = list(f.ledger.db.iterdump())
        f.apply(f.market(), (f.buy, f.exit))
        self.assertEqual(list(f.ledger.db.iterdump()), before)
        f.apply(f.market(T+1, danger=True), (f.quote('sell', f.raw, 9500000, at=T+1),))
        self.assertEqual(read_checkpoint(f.ledger.db)['positions'], {})

    def test_corruption_rejects_before_duplicate_and_new_fill_without_mutation(self):
        f = self.f; f.buy_fill(); original = copy.deepcopy(f.state())
        changes = (
            lambda p: p.pop('quote_execution'),
            lambda p: p['quote_execution'].update(source_authenticated=True),
            lambda p: p['quote_execution'].update(actual_fill_verified=True),
            lambda p: p['last_quote_execution'].update(transaction_verified=True),
            lambda p: p.pop('last_quote_execution'),
            lambda p: p.update(cost_left=str(float(p['cost_left'])/2)),
            lambda p: p['quote_execution'].update(mint_decimals=7),
            lambda p: p['quote_execution'].update(quote_hash='0'*64),
            lambda p: p.update(qty='0.1'),
            lambda p: p.update(pool='So11111111111111111111111111111111111111112'),
            lambda p: p.update(taker='So11111111111111111111111111111111111111112'),
        )
        for change in changes:
            with self.subTest(change=change):
                state = copy.deepcopy(original); change(state['positions'][f.mint]); self.save(state)
                before = list(f.ledger.db.iterdump())
                with self.assertRaises(RecoveryRequired): read_checkpoint(f.ledger.db)
                for event, quotes in ((f.market(),(f.buy,f.exit)),
                        (f.market(T+1,danger=True),(f.quote('sell',f.raw,9500000,at=T+1),))):
                    with self.assertRaises(ValueError): f.apply(event,quotes)
                    self.assertEqual(list(f.ledger.db.iterdump()),before)
                self.assertEqual(paper_status(f.path,now=T)['status'],'RECOVERY_REQUIRED')
        self.save(original)

    def test_partial_remaining_basis_and_raw_dust_are_reconciled(self):
        f = self.f
        for decimals in (6, 30):
            with self.subTest(decimals=decimals):
                if decimals == 30:
                    # A fresh isolated experiment; no rewrite/adoption of old journal.
                    f = execution_fixture.QuoteExecutionTests(); f.setUp()
                    self.addCleanup(f.doCleanups)
                    f.buy = f.quote('buy',10000000,1000000,decimals=decimals)
                    f.raw = qe.output_raw(f.buy,f.cfg)
                    f.exit = f.quote('sell',f.raw,10000000,decimals=decimals)
                f.buy_fill()
                action_raw = f.raw*3//10
                full = f.quote('sell',f.raw,20000000,at=T+1,decimals=decimals)
                action = f.quote('sell',action_raw,6000000,at=T+1,decimals=decimals)
                f.apply(f.market(T+1),(full,action))
                state = read_checkpoint(f.ledger.db)
                self.assertEqual(qe.raw_quantity(state['positions'][f.mint]['qty'],decimals),f.raw-action_raw)
                self.assertGreater(float(state['positions'][f.mint]['cost_left']),0)
                before = copy.deepcopy(state)
                state['positions'][f.mint]['cost_left']='0'
                f.ledger.db.execute('UPDATE state SET payload=? WHERE id=1',(canonical(state),))
                with self.assertRaises(RecoveryRequired): read_checkpoint(f.ledger.db)
                f.ledger.db.execute('UPDATE state SET payload=? WHERE id=1',(canonical(before),))
                remaining = f.raw-action_raw
                f.apply(f.market(T+2,danger=True),(f.quote('sell',remaining,9000000,at=T+2,decimals=decimals),))
                self.assertEqual(read_checkpoint(f.ledger.db)['positions'],{})

    def test_closed_journal_corruption_is_not_hidden_by_empty_inventory(self):
        f = self.f; f.buy_fill()
        f.apply(f.market(T+1,danger=True),(f.quote('sell',f.raw,9500000,at=T+1),))
        row = f.ledger.db.execute("SELECT seq,payload FROM outcomes ORDER BY seq DESC LIMIT 1").fetchone()
        import json
        outcome = json.loads(row[1]); outcome['realized_pnl_sol']='100'
        f.ledger.db.execute('UPDATE outcomes SET payload=? WHERE seq=?',(canonical(outcome),row[0]))
        with self.assertRaises(RecoveryRequired): read_checkpoint(f.ledger.db)

    def test_outcome_sql_bounds_precede_body_loading(self):
        f = self.f; f.buy_fill()
        f.ledger.db.execute('INSERT INTO outcomes(event_id,payload) VALUES(?,?)',
                            (f.market()['event_id'],'x'*(2*1024*1024+1)))
        queries=[]; f.ledger.db.set_trace_callback(queries.append)
        try:
            with self.assertRaises(RecoveryRequired): read_checkpoint(f.ledger.db)
        finally:
            f.ledger.db.set_trace_callback(None)
        self.assertFalse(any('SELECT o.payload,e.payload' in q for q in queries))

    def test_explicit_configuration_rejects_legacy_alias_and_conflicts(self):
        cfg = self.f.cfg | {'paper_signal_policy_version':3}
        self.assertEqual(policy_version(cfg),3)
        self.assertEqual(policy_version(cfg|{'experimental_policy_version':3}),3)
        for fields in ({'experimental_policy_version':True},{'experimental_policy_version':1},
                {'paper_signal_policy_version':True},{'paper_signal_policy_version':'3'},
                {'paper_quote_execution_version':None},{'mode':'live'}):
            with self.subTest(fields=fields),self.assertRaises(ValueError): policy_version(cfg|fields)
        with self.assertRaises(ValueError): policy_version(self.f.cfg|{'experimental_policy_version':3})

    def test_legacy_experimental_quote_scores_replay_at_pinned_precision(self):
        from tests.test_paper_experimental_scoring import experimental
        f = self.f; f.cfg['experimental_policy_version']=1
        e = experimental(mint=f.mint,pool=f.pool,taker=f.wallet)
        out = f.apply(e,(f.buy,f.exit))
        self.assertTrue(any(o.get('side')=='buy' for o in out))
        with localcontext() as context:
            context.prec=512
            self.assertEqual(read_checkpoint(f.ledger.db),f.state())
        self.assertEqual(paper_status(f.path,now=T)['status'],'LEDGER_PRESENT')

    def test_actual_v3_entry_checkpoint_and_dashboard_without_history_synthesis(self):
        seam = seam_fixture.QuoteV3SeamTests(); self.addCleanup(seam.doCleanups); seam.setUp()
        f = seam.fixture; f.cfg = seam.cfg
        f.apply({'schema_version':1,'event_id':'paper-runner:init','ts':0,'kind':'clock','actor':'paper_monitor'})
        f.ledger.db.execute('INSERT INTO metadata VALUES(?,?)',('paper_runner','SYNTHETIC_TEST_ONLY'))
        event = seam.market()
        self.assertIsNone(event['flow']); self.assertNotIn('bundle_evidence',event)
        out = f.apply(event,(f.buy,f.exit))
        buy = next(o for o in out if o.get('side')=='buy')
        self.assertEqual(buy['entry_policy']['policy_version'],3)
        with localcontext() as context:
            context.prec = 512
            self.assertEqual(read_checkpoint(f.ledger.db),f.state())
        view = paper_status(f.path,now=T,expected_config=f.cfg)
        self.assertEqual(view['status'],'LEDGER_PRESENT')
        self.assertEqual(view['runner_status'],'SYNTHETIC_CHECKPOINT_RECORDED')
        self.assertEqual(view['runner_liveness'],'UNKNOWN')
        self.assertEqual(experiment_report(f.path,now=T)['buy_fill_count'],1)
        restarted = Ledger(f.path,must_exist=True)
        try:
            self.assertEqual(read_checkpoint(restarted.db),f.state())
            before = list(restarted.db.iterdump())
            restarted.apply(event,f.cfg,qe.bind_transition(event,(f.buy,f.exit)),engine.initial_state)
            self.assertEqual(list(restarted.db.iterdump()),before)
        finally:
            restarted.close()
        self.assertIn('UNRESOLVED_OWNERSHIP_HISTORY',view['positions'][0]['entry_policy']['risk_flags'])
        self.assertEqual(view['positions'][0]['execution_status'],'EXECUTION_UNVERIFIED')
        self.assertFalse(view['positions'][0]['quote_execution']['actual_fill_verified'])
        self.assertNotIn('original_quote_json',view['positions'][0]['quote_execution'])
        self.assertIn('original_quote_json',f.state()['positions'][f.mint]['quote_execution'])
