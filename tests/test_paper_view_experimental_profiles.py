"""Explicit pinned-config projection of synthetic profiles, never live approval."""
import copy
import json
import unittest
from unittest.mock import patch

from desk import paper_view
from desk.model import canonical,validate_event
from tests.helpers import T,config,control,event
from tests.test_paper_experimental_scoring import experimental
from tests import test_paper_runner_experimental_config as fixtures


class ExperimentalPaperViewTests(unittest.TestCase):
    setUp=fixtures.ExperimentalRunnerConfigTests.setUp
    cli=fixtures.ExperimentalRunnerConfigTests.cli
    records=fixtures.ExperimentalRunnerConfigTests.records

    def test_actual_cli_profile_one_two_entry_exit_and_readonly_projection(self):
        for version,make in ((1,experimental),(2,fixtures.experimental_history)):
            with self.subTest(version=version):
                self.path=self.root/f'view-profile-{version}.sqlite'
                self.cfg={**config(),'experimental_policy_version':version}
                self.config_path.write_text(json.dumps(self.cfg))
                self.fixture_path.write_text(json.dumps({'provenance':'SYNTHETIC_TEST_ONLY','events':[
                    make(),make(ts=T+1,danger=True)]}))
                init=self.cli('init','--db',str(self.path))
                self.assertEqual(init.returncode,0,init.stderr)
                self.assertEqual(json.loads(init.stdout)['status'],'LEDGER_PRESENT')
                fills=[]
                for now in (T,T+1):
                    result=self.cli('once','--db',str(self.path),'--fixture',str(self.fixture_path),'--now',str(now))
                    self.assertEqual(result.returncode,0,result.stderr)
                    report=json.loads(result.stdout);fills.extend(report['outcomes'])
                    self.assertEqual(report['paper']['status'],'LEDGER_PRESENT')
                    self.assertEqual(report['paper']['runner_status'],'SYNTHETIC_CHECKPOINT_RECORDED')
                    self.assertEqual(report['paper']['runner_liveness'],'UNKNOWN')
                    self.assertFalse(report['paper']['automatic_entry_enabled'])
                    self.assertEqual(len(report['paper']['positions']),1 if now==T else 0)
                    before=self.records()
                    saved=paper_view.paper_status(self.path,now=now,expected_config=self.cfg)
                    self.assertEqual(saved['status'],'LEDGER_PRESENT')
                    self.assertEqual(saved['cash_sol'],report['paper']['cash_sol'])
                    self.assertEqual(self.records(),before)
                buy=next(r for r in fills if r.get('side')=='buy')
                sell=next(r for r in fills if r.get('side')=='sell')
                self.assertEqual(buy['quantity'],sell['quantity'])
                self.assertEqual(buy['entry_policy']['policy_version'],version)
                self.assertFalse(buy['entry_policy']['source_authenticated'])
                # Original seam: strict validation falsely rejects legitimate
                # persisted null-history events even after their full exit.
                def old_strict(payload,cfg):
                    decoded=json.loads(payload);validate_event(decoded);return decoded
                with patch.object(paper_view,'_event_json',side_effect=old_strict):
                    old=paper_view.paper_status(self.path,now=T+1)
                self.assertEqual(old['status'],'RECOVERY_REQUIRED')
                self.assertEqual(old['recovery_reason'],'RUNNER_EVENT_INVALID')
                self.assertEqual(paper_view.paper_status(self.path,now=T+1)['status'],'LEDGER_PRESENT')

    def test_event_json_cannot_select_profile_or_widen_version(self):
        first=experimental();second=fixtures.experimental_history()
        for cfg,payload in ((config(),first),(config(),second),
                            ({**config(),'experimental_policy_version':1},second),
                            ({**config(),'experimental_policy_version':2},first)):
            with self.subTest(cfg=cfg.get('experimental_policy_version'),event=payload['paper_experimental']['policy_version']):
                with self.assertRaises(ValueError):paper_view._event_json(canonical(payload),cfg)
        for payload in (first,second):
            version=payload['paper_experimental']['policy_version']
            self.assertEqual(paper_view._event_json(canonical(payload),{**config(),'experimental_policy_version':version}),payload)
        forged=copy.deepcopy(second);forged['paper_experimental']['policy_version']=1
        with self.assertRaises(ValueError):
            paper_view._event_json(canonical(forged),{**config(),'experimental_policy_version':1})

    def test_persisted_config_selection_checks_match_engine(self):
        for version in (True,'1',0,3):
            with self.subTest(version=version),self.assertRaises(ValueError):
                paper_view._event_json(canonical(event()),{**config(),'experimental_policy_version':version})
        for mode in ('live',None):
            with self.subTest(mode=mode),self.assertRaises(ValueError):
                paper_view._event_json(canonical(experimental()),{**config(),'mode':mode,'experimental_policy_version':1})
        self.assertEqual(paper_view._event_json(canonical(event()),config()),event())

    def test_operator_control_and_clock_grammar_stays_strict_for_both_profiles(self):
        clock={'schema_version':1,'event_id':'clock','ts':T,'kind':'clock','actor':'paper_monitor'}
        for version in (1,2):
            cfg={**config(),'experimental_policy_version':version}
            for valid in (clock,control(T,'PAUSE_ENTRY')):
                self.assertEqual(paper_view._event_json(canonical(valid),cfg),valid)
                extras=({'paper_experimental':experimental()['paper_experimental']},{'actor':'candidate'},{'flow':None}) if valid['kind']=='clock' else ({'actor':'candidate'},{'command':'PAUSE'})
                for extra in extras:
                    with self.subTest(version=version,kind=valid['kind'],extra=extra),self.assertRaises(ValueError):
                        paper_view._event_json(canonical(valid|extra),cfg)

    def test_malformed_market_remains_rejected_in_selected_profiles(self):
        for version,make in ((1,experimental),(2,fixtures.experimental_history)):
            cfg={**config(),'experimental_policy_version':version}
            for changes in ({'danger':None},{'price_at':T+1},{'reserve_sol':'NaN'},{'schema_version':True},{'kind':'bogus'}):
                with self.subTest(version=version,changes=changes),self.assertRaises(ValueError):
                    paper_view._event_json(canonical(make(**changes)),cfg)
            payload=canonical(make())[:-1]+',"kind":"market"}'
            with self.assertRaises(ValueError):paper_view._event_json(payload,cfg)
