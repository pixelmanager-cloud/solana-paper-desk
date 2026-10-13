"""M2 mutation check for the selection learner's statistics (lean/learn.py). NOT a test module (no ``test_`` prefix).

Each mutant is ONE textual replacement in lean/learn.py. The file is restored after every run (also on Ctrl-C). A mutant is KILLED when
the learner tests fail with it. Run from the repository root:

    TMPDIR=/private/tmp/ .venv/bin/python tests/lean/m2_mutate.py

Exit code 0 only when every mutant is killed and the unmutated tree passes. (The lookahead-guard and label mutants are in
lint3_mutate.py.)
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ['tests.lean.test_learn', 'tests.lean.test_e2e_learn', 'tests.lean.test_e2e_risk_external']

MUTANTS = [
    # --- bug (a): the test must be kept vs rejected against a random rule, never the rejected PnL against 0
    ('Welch of the rejected PnL against 0', "    w = welch(kept_sol, rej_sol)\n", "    w = welch([0.0] * len(rej_sol), rej_sol)\n"),
    ('Welch direction flipped', "return {'t': round(t, 6), 'df': round(df, 3), 'p': t_sf(t, df)}", "return {'t': round(t, 6), 'df': round(df, 3), 'p': t_sf(-t, df)}"),
    ('null = rejecting nothing (sums 0)', "cols = [self._col(block)[k] for block, k in sorted(counts.items())]",
     "cols = [self._col(block)[0] for block, k in sorted(counts.items())]"),
    ('null ignores the per-block counts', "cols = [self._col(block)[k] for block, k in sorted(counts.items())]",
     "cols = [self._col(block)[min(len(self.blocks[block]), sum(counts.values()))] for block, k in sorted(counts.items())]"),
    ('mining objective = the old "rejected PnL < 0"', "return len(idx) * total_all - n * sum(pnl[i] for i in idx)",
     "return -n * sum(pnl[i] for i in idx)"),
    # --- multiple testing and the development null
    ('Benjamini-Hochberg removed', "        if q > fdr_q:\n", "        if e['p'] > fdr_q:\n"),
    ('development null gate removed', "            if not e['oos']['beats_null']:\n", "            if False:\n"),
    ('out-of-fold support not required', "if m['n_rejected'] < floor or m['n_kept'] < floor or m['welch_p'] is None:", "if m['welch_p'] is None:"),
    ('out-of-fold NO_GAIN removed', "if not (m['diff_kept_minus_rejected_sol'] or 0) > 0:", "if False:"),
    # --- bug (c): "stop trading" rules and kept-set profitability
    ('kept mean > 0 not required', "return 'CONFIRMED' if (metrics['kept_mean_pnl_sol'] or 0) > 0 else 'BLEED_REDUCER'", "return 'CONFIRMED'"),
    ('reject cap removed from mining', "return min_n <= len(idx) <= max_reject_fraction * n", "return min_n <= len(idx)"),
    ('reject cap removed out of fold', "if (m['reject_fraction'] or 0) > max_reject_fraction:", "if False:"),
    ('reject cap removed from the chosen union', "if after['reject_fraction'] > max_reject_fraction or", "if False or"),
    ('confirm: kept group size not required', "metrics['n_rejected'] >= min_confirm and metrics['n_kept'] >= min_confirm",
     "metrics['n_rejected'] >= min_confirm"),
    ('confirm: Welch gate removed', "and metrics['welch_p'] is not None and metrics['welch_p'] <= alpha\n", "\n"),
    ('confirm: null gate removed', "and metrics.get('beats_null') is True and metrics.get('perm_p') is not None and metrics['perm_p'] <= alpha)",
     ")"),
    # --- bug (b): windows, purge / embargo, adaptivity, single use
    ('no purge at the confirm boundary', "d = bisect.bisect_right(ts, start - embargo_s, 0, c0)", "d = c0"),
    ('no embargo before a test block', "before = bisect.bisect_right(ts, ts[a] - embargo_s, 0, a)", "before = a"),
    ('a fold trains on the future (blocked K-fold)', "mined = mine(enterable[t0:t1], feature_names,",
     "mined = mine(enterable[t0:t1] + enterable[b:plan['dev']], feature_names,"),
    ('support not adaptive', "return max(min(MIN_N_FLOOR, min_n), min(min_n, math.ceil(SUPPORT_SHARE * n_train - 1e-9)))", "return min_n"),
    ('a consumed confirm block is tested again', "    elif not plan['fresh']:\n", "    elif False:\n"),
    ('the ledger ignores tested blocks', "if evidence.get('confirm_consumed') is True:", "if False:"),
    ('an unreadable ledger file is ignored', "            out['unreadable'] += 1\n            bump(_name_time(path.name))\n",
     "            out['unreadable'] += 1\n"),
    ('ETA off by the last step', "return dict(out, eta_hours=round(hi / rate, 2), eta_ts=round(ts[-1] + 3600.0 * hi / rate, 3), needed_enterable=n + hi)",
     "return dict(out, eta_hours=round(lo / rate, 2), eta_ts=round(ts[-1] + 3600.0 * lo / rate, 3), needed_enterable=n + lo)"),
    ('a BLEED_REDUCER is proposed', "result['proposal'] = str(write_proposal(proposals_dir, result, chosen if result['confirmed'] else [],",
     "result['proposal'] = str(write_proposal(proposals_dir, result, chosen if status in ('CONFIRMED', 'BLEED_REDUCER') else [],"),
]


def run_tests():
    return subprocess.run([sys.executable, '-m', 'unittest', '-q'] + TESTS, cwd=ROOT, capture_output=True, text=True).returncode


def main():
    path = ROOT / 'lean' / 'learn.py'
    original = path.read_text()
    if run_tests() != 0:
        print('BASELINE FAILS: fix the tests first')
        return 2
    survived = []
    try:
        for name, old, new in MUTANTS:
            if original.count(old) != 1:
                print('BAD MUTANT (pattern found %d times): %s' % (original.count(old), name))
                survived.append(name)
                continue
            path.write_text(original.replace(old, new))
            try:
                rc = run_tests()
            finally:
                path.write_text(original)
            print('%-50s %s' % (name, 'killed' if rc != 0 else 'SURVIVED'), flush=True)
            if rc == 0:
                survived.append(name)
    finally:
        path.write_text(original)
    print('%d mutants, %d killed, %d survived %s' % (len(MUTANTS), len(MUTANTS) - len(survived), len(survived), survived))
    return 0 if not survived else 1


if __name__ == '__main__':
    sys.exit(main())
