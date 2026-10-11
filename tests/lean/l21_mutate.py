"""L21 mutation check for the external risk recorder (lean/risk_external.py), its RugCheck client (lean/providers.py) and its learner
guard (lean/learn.py). NOT a test module (no ``test_`` prefix).

Each mutant is ONE textual replacement in one file. The file is restored after every run (also on Ctrl-C). A mutant is KILLED when the
L21 tests fail with it. Run from the repository root:

    TMPDIR=/private/tmp/ .venv/bin/python tests/lean/l21_mutate.py

Exit code 0 only when every mutant is killed and the unmutated tree passes.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ['tests.lean.test_risk_external', 'tests.lean.test_e2e_risk_external']

MUTANTS = [
    # --- the recorder: what is collected, when, and how it is flagged
    ('risk', 'hazard rejects collected', "if getattr(screen, 'error', None) or not hazard_free(screen):", "if getattr(screen, 'error', None):"),
    ('risk', 'failed screens collected', "if getattr(screen, 'error', None) or not hazard_free(screen):", "if not hazard_free(screen):"),
    ('risk', 'no dedupe per mint and state', "                if key in self._seen:", "                if False:"),
    ('risk', 'late candidates still called', "        elif self._late(decision):", "        elif False:"),
    ('risk', 'backoff window ignored', "if self.monotonic() < self._backoff_until:", "if False:"),
    ('risk', 'a 429 opens no backoff', "self._backoff_until = self.monotonic() + max(self.cfg['backoff_s'], float(retry_after))", "pass"),
    ('risk', 'creator tokens after the decision counted', "and ts is not None and ts < decision_ts]", "and ts is not None]"),
    ('risk', 'creator 7-day window', "if t[1] >= decision_ts - 7 * DAY)", "if True)"),
    ('risk', 'creator sample uncapped', "len(picks) < sample", "True"),
    ('risk', 'a partial sample is a value', "        if read == len(picks):", "        if True:"),
    ('risk', 'copycat reads tokens collected after the decision', "decision_ts - window_s <= at <= decision_ts", "decision_ts - window_s <= at"),
    ('risk', 'copycat window ignored', "decision_ts - window_s <= at <= decision_ts", "at <= decision_ts"),
    ('risk', 'copycat counts itself', "if other != mint and", "if True and"),
    ('risk', 'pool vault counted as a holder', "if h.get('owner') in pools or h.get('address') in pools:", "if False:"),
    ('risk', 'no AT_COLLECTION flags', "'flags': {name: AT_COLLECTION for name in FIELDS if out_fields[name] is not None}", "'flags': {}"),
    ('risk', 'late_s not measured', "late = None if decision_ts is None or collected_at is None else round(collected_at - decision_ts, 3)", "late = 0.0"),
    ('risk', 'a later screen borrowed as the decision', "and d['ts'] <= submitted_at + 1e-6]", "]"),
    ('risk', 'another candidate\'s decision borrowed', "if (candidate_id is None or d['candidate_id'] == candidate_id) and", "if True and"),
    ('risk', 'stop not checked before a request', "        if self._stop is not None and self._stop.is_set():\n            raise _Stopped",
     "        if False:\n            raise _Stopped"),
    ('risk', 'large bodies kept', "if len(body) <= self.cfg['raw_max_bytes']:", "if True:"),
    # --- the client
    ('providers', 'unknown mint not typed', "if error.meta.get('http_status') in RUGCHECK_UNKNOWN:", "if False:"),
    ('providers', 'another mint\'s report accepted', "if not isinstance(parsed, dict) or parsed.get('mint') != mint:", "if not isinstance(parsed, dict):"),
    ('providers', 'no own pacing', "limiter = RateLimiter(rate_per_s, 1,", "limiter = RateLimiter(1000, 1000,"),
    ('providers', 'unchecked mint in the URL', "    def report(self, mint):\n        _pubkey(mint)", "    def report(self, mint):\n        pass"),
    # --- the learner guard
    ('learn', 'late risk snapshots accepted', "if not (_num(late) and late <= policy.max_late_s):", "if not _num(late):"),
    ('learn', 'unflagged risk values accepted', "        if flag != 'AT_COLLECTION':", "        if False:"),
    ('learn', 'other risk versions accepted', "if row.get('version') != RISK_VERSION:", "if False:"),
    ('learn', 'risk rows after the decision accepted', "rrow = _pick(risk_rows.get(path.mint, []), decision,", "rrow = _pick(risk_rows.get(path.mint, []), 10 ** 12,"),
    ('learn', 'risk rows never read', "risk_rows = _observations(live, 'risk_external', counts, 'RISK_EXTERNAL')", "risk_rows = {}"),
]


def run_tests():
    return subprocess.run([sys.executable, '-m', 'unittest', '-q'] + TESTS, cwd=ROOT, capture_output=True, text=True).returncode


def main():
    files = {'risk': ROOT / 'lean' / 'risk_external.py', 'providers': ROOT / 'lean' / 'providers.py', 'learn': ROOT / 'lean' / 'learn.py'}
    originals = {k: p.read_text() for k, p in files.items()}
    if run_tests() != 0:
        print('BASELINE FAILS: fix the tests first')
        return 2
    survived = []
    try:
        for target, name, old, new in MUTANTS:
            text = originals[target]
            if text.count(old) != 1:
                print('BAD MUTANT (pattern found %d times): %s' % (text.count(old), name))
                survived.append(name)
                continue
            files[target].write_text(text.replace(old, new))
            try:
                rc = run_tests()
            finally:
                files[target].write_text(text)
            print('%-8s %s' % ('killed' if rc else 'SURVIVED', name))
            if not rc:
                survived.append(name)
    finally:
        for k, p in files.items():
            p.write_text(originals[k])
    print('%d/%d killed' % (len(MUTANTS) - len(survived), len(MUTANTS)))
    return 1 if survived else 0


if __name__ == '__main__':
    sys.exit(main())
