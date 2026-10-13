"""W2b2 mutation check: each mutant REVERTS one fix of the W2b pre-deploy review (#1 distinct prefetch features, #2 snapshot stamps and age, #3 flow
gate and budget, #4 hazard coverage, #5 rollback signals, #6 job reuse, #7 discovery lock, #8 raw bodies). NOT a test module (no ``test_`` prefix).

A mutant is KILLED when the W2b / W2b2 tests fail with it: that is the fail-first evidence of the fix (the test fails without it, passes with it).
Run from the repository root (optionally with words: only the mutants whose name contains one of them):

    TMPDIR=/private/tmp/ .venv/bin/python tests/lean/w2b2_mutate.py [word ...]

Exit code 0 only when every mutant is killed and the unmutated tree passes.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ['tests.lean.test_w2b2', 'tests.lean.test_w2b', 'tests.lean.test_e2e_w2b']

MUTANTS = [
    # --- #1 a prefetched row is a different feature
    ('learn', 'the version ignores the window mode', "    return version == base and not is_prefetch(row) or version == base + 'p' and is_prefetch(row)",
     "    return version in (base, base + 'p')"),
    ('learn', 'the prefetch columns are pooled', "values[name + PREFETCH_SUFFIX if mode == 'prefetch' and (split is None or name in split) else name] = value",
     "values[name] = value"),
    ('learn', 'a prefetched features row is split whole', "split = got_row.get('prefetch_fields') if kind == 'features' and mode == 'prefetch' else None",
     "split = None"),
    ('learn', 'a features row of the wrong version is read', "        if version != FEATURES_PREFETCH_VERSION:\n            counts.skip('FEATURES_BAD_PREFETCH_VERSION')\n            return out",
     "        pass"),
    ('features', 'a prefetched features row keeps the decision version', "payload.update(features_version=FEATURES_PREFETCH_VERSION, window_mode='prefetch',",
     "payload.update(window_mode='prefetch',"),
    ('flow', 'a prefetched flow row keeps the decision version', "'version': VERSION + ('p' if row.get('window_mode') == 'prefetch' else ''),",
     "'version': VERSION,"),
    ('risk_external', 'a prefetched RugCheck row keeps the decision version', "out['version'], out['window_mode'] = VERSION + 'p', 'prefetch'",
     "out['window_mode'] = 'prefetch'"),
    ('prefetch', 'a prefetched wallet row keeps the decision version', "version=features['version'] + 'p', window_mode='prefetch')",
     "window_mode='prefetch')"),
    # --- #2 snapshots are read by the job, stamped, and aged
    ('learn', 'the snapshot age is not checked', "    if not _num(at) or decision_ts - at > policy.max_snapshot_age_s:", "    if not _num(at):"),
    ('prefetch', 'a soft reject takes the probe holder list', "need_holders=entry.soft is True)", "need_holders=False)"),
    ('features', 'the holder snapshot is stamped with another time', "snapshot_at['top1_pct'] = snapshot_at['top10_pct'] = timed.at['getTokenLargestAccounts']",
     "snapshot_at['top1_pct'] = snapshot_at['top10_pct'] = 0.0"),
    ('prefetch', 'clusters use the probe holder list', "screen_data=(dict(probe.features or {}), None, bodies.get('accounts_mint_pool')),",
     "screen_data=(dict(probe.features or {}), bodies.get('holders'), bodies.get('accounts_mint_pool')),"),
    ('clusters', 'the clusters holder read is not stamped', "                    holders_at = self.clock()                        # W2b2 #2: when this snapshot was read",
     "                    pass"),
    # --- #3 flow only where it can be used, on its own budget
    ('prefetch', 'flow is prefetched for a soft reject', "                if kind == 'flow' and entry.soft is True:", "                if False:"),
    ('prefetch', 'the market bands are never judged', "usd = self.sol_usd() if self.sol_usd is not None else None", "usd = None"),
    ('flow', 'the prefetch spends the post-screen flow budget', "budget=self.prefetch_budget if sink is not None and self.prefetch_budget is not None else self.budget,",
     "budget=self.budget,"),
    # --- #4 hazard paths are not coverage
    ('learn', 'hazard skips are coverage skips', "            skip = hazard_counts if hazard else counts", "            skip = counts"),
    ('learn', 'hazard paths are in the path total', "            if hazard:\n                hazard_paths += 1\n            else:\n                n_paths += 1",
     "            if hazard:\n                hazard_paths += 1\n            n_paths += 1"),
    # --- #5 rollback signals
    ('prefetch', 'the probe false-positive rate is not reported', "        return None if not n else round(probe['fp'] / n, 4)", "        return None"),
    ('runner', 'the low lane is not in health', "                'low_lane': self._low_lane_health(),", ""),
    ('held_risk', 'a shed held-risk probe is not split by why', "            key = 'shed_' + why", "            key = 'shed_other'"),
    # --- #6 job reuse
    ('prefetch', 'a job finishing after the decision is done', "        job.finished_at = self.clock()\n        if job.cancelled:", "        job.finished_at = self.clock()\n        if False:"),
    ('prefetch', 'a running job is not told of the decision', "                        job.cancelled = True                  # W2b2 #6", "                        pass                  # W2b2 #6"),
    ('prefetch', 'take is reusable', "            elif job.used:                                   # W2b2 #6", "            elif False:                                   # W2b2 #6"),
    # --- #7 the discovery lock
    ('prefetch', 'discovery reads the store under the lock', "        handled = {c.mint for c in fresh if self._handled(c.mint)}",
     "        with self._lock:\n            handled = {c.mint for c in fresh if self._handled(c.mint)}"),
    # --- #8 raw bodies carry the candidate
    ('wallet_signals', 'a prefetch writes its raw bodies at once', "            calls.raws = sink.setdefault('raws', [])", "            pass"),
    ('risk_external', 'a prefetch writes its RugCheck body at once', "                        self._deferred_raw[mint] = (body, self.clock())",
     "                        self._store_raw(mint, candidate_id, body, self.clock())"),
]


def run_tests():
    return subprocess.run([sys.executable, '-m', 'unittest', '-q'] + TESTS, cwd=ROOT, capture_output=True, text=True).returncode


def main(argv=None):
    only = [a.lower() for a in (sys.argv[1:] if argv is None else argv)]
    mutants = [m for m in MUTANTS if not only or any(word in m[1].lower() for word in only)]
    files = {name: ROOT / 'lean' / (name + '.py') for name in ('learn', 'features', 'flow', 'risk_external', 'prefetch', 'clusters', 'runner', 'held_risk',
                                                               'wallet_signals')}
    originals = {k: p.read_text() for k, p in files.items()}
    if run_tests() != 0:
        print('BASELINE FAILS: fix the tests first')
        return 2
    survived = []
    try:
        for target, name, old, new in mutants:
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
            print('%-8s %s' % ('killed' if rc != 0 else 'SURVIVED', name))
            if rc == 0:
                survived.append(name)
    finally:
        for k, p in files.items():
            p.write_text(originals[k])
    print('%d/%d killed' % (len(mutants) - len(survived), len(mutants)))
    return 1 if survived else 0


if __name__ == '__main__':
    sys.exit(main())
