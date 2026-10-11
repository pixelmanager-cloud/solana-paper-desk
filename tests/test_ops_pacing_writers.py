"""SYNTHETIC_TEST_ONLY: T32I item 3 - the pacing-policy tool's built-in writer list is derived from the RENDERED units.

`tools.ops.pacing_policy apply --execute` refuses while any unit that opens the shared pacing database is active. A unit missing from
`DEFAULT_WRITERS` is a unit the tool would not wait for: it could write a pacing ticket while the policy rows are appended. The expected
set is therefore computed from `deploy/fresh` (rendered through `fresh_start.render_units`' placeholders) and `fresh_start.unit_arguments`,
not typed in again here.
"""
import re
import tempfile
import unittest
from pathlib import Path

from tools.ops import fresh_start as fs, pacing_policy

FRESH = Path(__file__).resolve().parents[1] / 'deploy' / 'fresh'
STORE_USERS = ('tools.paper_scheduler', 'tools.ops.backup', 'tools.ops.healthcheck')      # open the shared pacing store (env, arg or backup)
REQUIRED_FAMILIES = ('desk-paper-monitor', 'desk-decisions', 'desk-backup', 'desk-healthcheck', 'desk-paper-entry-dispatcher',
                     'desk-paper-held-cycle', 'desk-dashboard', 'desk-counterfactual')


def code_lines(text):
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]


def derived_writers():
    """Every rendered unit whose settings or ExecStart reach the shared pacing store, plus the timer/path that starts it."""
    services = {}
    for path in sorted(FRESH.glob('desk-*.service')):
        body = '\n'.join(code_lines(path.read_text()))
        if re.search(r'PACING|pacing', body) or any(module in body for module in STORE_USERS):
            services[path.name] = body
    units = set(services)
    for path in sorted([*FRESH.glob('desk-*.timer'), *FRESH.glob('desk-*.path')]):
        unit = re.search(r'(?m)^Unit=(\S+)', path.read_text())
        target = unit.group(1) if unit else path.stem + '.service'
        if target in services:
            units.add(path.name)
    return units


class WriterListTests(unittest.TestCase):
    def test_the_default_writers_cover_every_rendered_unit_that_reaches_the_pacing_store(self):
        expected = derived_writers()
        self.assertTrue(expected, 'the derivation found nothing: the rule is broken')
        missing = sorted(expected - set(pacing_policy.DEFAULT_WRITERS))
        self.assertEqual(missing, [], 'units the policy tool would NOT wait for')

    def test_the_families_the_t32h_review_named_are_present_with_their_timers(self):
        have = set(pacing_policy.DEFAULT_WRITERS)
        for family in REQUIRED_FAMILIES:
            self.assertIn(family + '.service', have, family)
        for family in ('desk-paper-monitor', 'desk-decisions', 'desk-backup', 'desk-healthcheck'):
            self.assertIn(family + '.timer', have, family)
        self.assertIn('desk-paper-held-cycle.path', have)

    def test_every_unit_with_the_pacing_environment_in_the_cutover_arguments_is_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = {'config': '/c.json', 'pool_fee_bps': '25', 'taker': 'T', 'amount_raw': 1, 'backup_dir': '/b', 'enable_held': True,
                    'shared': {'pacing_db': '/p/provider-pacing.sqlite', 'discovery_db': '/p/d.sqlite'},
                    'stores': {k: '/r/' + k for k in ('research_db', 'evidence_db', 'ledger_db', 'decisions_db', 'journal', 'manifest', 'scheduler_lock')}}
            arguments = fs.unit_arguments(plan, scheduler_identity='1:2')
        users = {name + '.service' for name, value in arguments.items()
                 if any(e.startswith('DESK_PROVIDER_PACING_DB=') for e in value['environment'])}
        self.assertTrue(users)
        self.assertEqual(sorted(users - set(pacing_policy.DEFAULT_WRITERS)), [])

    def test_no_duplicates_and_only_desk_units(self):
        writers = pacing_policy.DEFAULT_WRITERS
        self.assertEqual(len(writers), len(set(writers)))
        self.assertTrue(all(re.fullmatch(r'desk-[a-z-]+\.(service|timer|path)', w) for w in writers), writers)

    def test_the_derivation_notices_a_missing_unit(self):
        """Mutation guard for the test itself: drop one derived unit from the list and the coverage check must fail."""
        trimmed = tuple(w for w in pacing_policy.DEFAULT_WRITERS if w != 'desk-decisions.service')
        self.assertIn('desk-decisions.service', derived_writers())
        self.assertNotEqual(sorted(derived_writers() - set(trimmed)), [])


if __name__ == '__main__':
    unittest.main()
