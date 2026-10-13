"""LINT3 mutation check for the selection learner (lean/labels.py, lean/learn.py). NOT a test module (no ``test_`` prefix).

Each mutant is ONE textual replacement in one file. The file is restored after every run (also on Ctrl-C). A mutant is KILLED when
the learner tests fail with it. Run from the repository root:

    TMPDIR=/private/tmp/ .venv/bin/python tests/lean/lint3_mutate.py

Exit code 0 only when every mutant is killed and the unmutated tree passes.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ['tests.lean.test_labels', 'tests.lean.test_learn', 'tests.lean.test_e2e_learn']

MUTANTS = [
    # --- labels: the mark, the decision, the gaps, the reference, the live exits
    ('labels', 'mark without the pool fee', "pool_fee_bps=point.vault_fee_bps, pcfg=pcfg) * paper.LAMPORTS)", "pool_fee_bps=0, pcfg=pcfg) * paper.LAMPORTS)"),
    ('labels', 'label reads marks before the decision', "points = [p for p in path.points if p.ts >= decision_ts]", "points = list(path.points)"),
    ('labels', 'gaps ignored', "if fraction > cfg.max_gap_fraction:", "if False:"),
    ('labels', 'holes ignored', "if hole > cfg.max_hole_s:", "if False:"),
    ('labels', 'an unfinished path can be FLAT', "if path.ended is None:", "if False:"),
    ('labels', 'liquidity baseline = first mark', "base_liquidity = int(spendable0)", "base_liquidity = None"),
    ('labels', 'a win after the stop still wins', "elif t_win is not None and (t_stop is None or t_win < t_stop):", "elif t_win is not None:"),
    ('labels', 'reference without the paper fee', "return Reference(fill.qty_raw, fill.sol_lamports + fill.fee_lamports, decimals, basis)",
     "return Reference(fill.qty_raw, fill.sol_lamports, decimals, basis)"),
    ('labels', 'reference without the paper slippage', "fill = paper.buy(paper.Quote(path.mint, 'buy', cost, qty, decimals, path.start_ts), A.sol(cost), pcfg)",
     "fill = paper.buy(paper.Quote(path.mint, 'buy', cost, qty, decimals, path.start_ts), A.sol(cost), paper.PaperConfig(fee_lamports=pcfg.fee_lamports, slippage_bps=0))"),
    ('labels', 'ladder state not carried', "        state = A.apply_updates(state, final.after_fill)\n", "        pass\n"),
    ('labels', 'a dead end is not written off', "if last is not None and last.dead:", "if False:"),
    ('labels', 'exits trade marks before the decision', "if point.ts < decision_ts or (until is not None", "if False or (until is not None"),
    ('labels', 'peak / touched not carried', "            state = A.apply_updates(state, decision.position_updates)\n", "            pass\n"),
    # --- learn: the lookahead guard
    ('learn', 'rows after the decision accepted', "if at > decision_ts + policy.eps_s:", "if False:"),
    ('learn', 'late AT_COLLECTION accepted', "if not _num(late) or late > policy.max_late_s:", "if not _num(late) or late > 10 ** 9:"),
    ('learn', 'unknown flags accepted', "elif flag is not None and flag not in OK_FLAGS:", "elif False:"),
    ('learn', 'pre-v3 rows accepted', "row['features_version'] < MIN_FEATURES_VERSION", "row['features_version'] < 0"),
    # LINT4: + the trailing comment, so the pattern stays unique after L20 added the same guard to flow_values
    ('learn', 'uncut wallet window accepted', "if not _num(after) or after > 0:                     # the window was not cut at the decision (unknown",
     "if False:                     # the window was not cut at the decision (unknown"),
    ('learn', 'smart table after the decision accepted', "if name == 'smart_buyers_count' and (", "if False and ("),
    ('learn', 'post-decision supply accepted', "if name == 'bundled_supply_pct' and row.get('supply_source') != 'screen':", "if False:"),
    ('learn', 'old wallet versions accepted', "if row.get('version') != WALLET_VERSION:", "if False:"),
    ('learn', 'another candidate\'s row accepted', "if candidate_id is not None and row.get('_candidate_id') not in (None, candidate_id):", "if False:"),
    ('learn', 'decision = screen even when an entry followed', "return entries[-1] if entries else screens[-1]", "return screens[-1]"),
    ('learn', 'a decision after the path start', "rows = [d for d in decisions if d[0] <= start_ts and", "rows = [d for d in decisions if True and"),
    ('learn', 'insufficient paths learned', "if not label.usable:", "if False:"),
    ('learn', 'pnl not the live-exit replay', "{n: values[n] for n in feature_names}, trade.pnl_lamports,", "{n: values[n] for n in feature_names}, 0,"),
    ('learn', 'anything enterable', "return bool(path.reasons) and all(str(r) in PORTFOLIO_REASONS for r in path.reasons)", "return True"),
    # --- learn: walk-forward and statistics
    ('learn', 'select on the confirm window', "chosen, evidence = select_rules(candidates, select_rows,", "chosen, evidence = select_rules(candidates, confirm_rows,"),
    ('learn', 'confirm gate removed', "elif not confirmed(union['confirm'], min_confirm=min_confirm, z_confirm=z_confirm):", "elif False:"),
    ('learn', 'select significance removed', "if z is None or z > -z_select:", "if z is None:"),
    ('learn', 'mine support removed', "if len(idx) >= min_n and len(idx) <= max_reject_fraction * n and total(idx) < 0:",
     "if len(idx) >= 1 and len(idx) <= max_reject_fraction * n and total(idx) < 0:"),
    ('learn', 'unknown values match', "            return False                      # an unknown value never matches", "            return True"),
    ('learn', 'unconfirmed rules proposed', "chosen if result['confirmed'] else []", "chosen"),
]


def run_tests():
    return subprocess.run([sys.executable, '-m', 'unittest', '-q'] + TESTS, cwd=ROOT, capture_output=True, text=True).returncode


def main():
    files = {'labels': ROOT / 'lean' / 'labels.py', 'learn': ROOT / 'lean' / 'learn.py'}
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
            print('%-8s %-50s %s' % (target, name, 'killed' if rc != 0 else 'SURVIVED'))
            if rc == 0:
                survived.append(name)
    finally:
        for k, p in files.items():
            p.write_text(originals[k])
    print('%d mutants, %d killed, %d survived %s' % (len(MUTANTS), len(MUTANTS) - len(survived), len(survived), survived))
    return 0 if not survived else 1


if __name__ == '__main__':
    sys.exit(main())
