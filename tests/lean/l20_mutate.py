"""L20 mutation check (lean/sell_evidence.py, lean/flow.py, the L20 hooks in lean/execution.py, lean/paper.py, lean/learn.py,
lean/report.py, lean/notify.py). NOT a test module (no ``test_`` prefix).

Each mutant is ONE textual replacement in one file. The file is restored after every run (also on Ctrl-C). A mutant is KILLED when
the L20 tests fail with it. Run from the repository root:

    TMPDIR=/private/tmp/ .venv/bin/python tests/lean/l20_mutate.py

Exit code 0 only when every mutant is killed and the unmutated tree passes.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ['tests.lean.test_flow', 'tests.lean.test_sell_evidence', 'tests.lean.test_e2e_l20', 'tests.lean.test_execution', 'tests.lean.test_paper']

MUTANTS = [
    # --- sellability: what counts as evidence
    ('se', 'the coin creator counts', "exclude = {self.taker} | ({self.creator(mint)} - {None})", "exclude = {self.taker}"),
    ('se', 'an incomplete sample proves absence', "    if failed:                                  # the planned sample", "    if False:                                   # the planned sample"),
    ('se', 'sells outside the window count', "now - cfg['sell_window_s'] <= r['block_time'] <= now + 5]", "True]"),
    ('se', 'failed transactions are candidates', "window = [r for r in rows if not r['failed'] and", "window = [r for r in rows if"),
    ('se', 'sells are not by DISTINCT signers', "sellers.add(view['signer'])", "sellers.add(entry['signature'])"),
    ('se', 'an UNKNOWN result is cached', "if result['verdict'] != UNKNOWN:", "if True:"),
    ('se', 'the cache never expires', "0 <= now - hit[1] <= self.cfg['cache_s']", "0 <= now - hit[1]"),
    ('se', 'a pending probe is waited for at fill time', "if not handle.event.is_set():", "if not handle.event.wait(5):"),
    ('se', 'no hourly cap', "budget=self.budget, wait=self._pace.wait", "budget=HourBudget(10 ** 9, self.clock), wait=self._pace.wait"),
    ('se', 'an even (aliasing) sample', "plan = stratified(list(reversed(window)), cfg['max_tx_samples'])[::-1]",
     "plan = list(reversed(window))[::max(1, len(window) // cfg['max_tx_samples'])][:cfg['max_tx_samples']][::-1]"),
    ('flow', 'a sell without proceeds counts', "and view['signer_token_delta'] < 0 and view['signer_sol_delta'] > 0)", "and view['signer_token_delta'] < 0)"),
    ('flow', 'a buy counts as a sell', "not view['failed'] and view['side'] == 'sell' and view['signer'] is not None",
     "not view['failed'] and view['signer'] is not None"),
    ('flow', 'the fee is not added back', "                    lamports += fee\n", "                    pass\n"),
    # --- booking
    ('exec', 'flag_only also zeroes', "if meta.get('sell_status') == SE.SELL_UNVERIFIED and meta.get('unverified_sell_policy') == 'zero':",
     "if meta.get('sell_status') == SE.SELL_UNVERIFIED:"),
    ('exec', 'unverified sells booked at the quote', "            return self._unverified_write_off(fill, position, state, meta, bps)\n", "            pass\n"),
    ('exec', 'the probe wait is unbounded', "self.sell_evidence.wait(list(self._probes.values()), remaining)", "self.sell_evidence.wait(list(self._probes.values()), 30)"),
    ('exec', 'the delay is slept on top of the probe wait', "                    remaining = due - runner.clock()\n                # --- end L20 ---",
     "                    remaining = self.cfg.exec_delay_s + 0.5\n                # --- end L20 ---"),
    ('exec', 'the rent of an unverified close is not written off', "'ata_rent_written_off_lamports': rent if closing else 0, 'ata_refund_lamports': 0,\n                   'ata_rent_basis_sold_lamports': basis",
     "'ata_rent_written_off_lamports': 0, 'ata_refund_lamports': 0,\n                   'ata_rent_basis_sold_lamports': basis"),
    ('exec', 'no probe in phase 1', "                    self._start_probe(mint, (states.get(mint) or {}).get('state'))          # L20: returns at once\n", ""),
    ('paper', 'write_off ignores the quantity', "    qty = position.qty_raw if qty_raw is None else qty_raw\n", "    qty = position.qty_raw\n"),
    # --- flow features: no lookahead and the figures
    ('flow', 'the decision second is used', "        cut = end_s - 1\n", "        cut = end_s + 60\n"),
    ('flow', 'the window start is ignored', "window = [r for r in reversed(pre) if r['block_time'] >= covered and not r['failed']]",
     "window = [r for r in reversed(pre) if not r['failed']]"),
    ('flow', 'a partial oldest second is kept', "covered = max(start_s, min(times) + 1)", "covered = max(start_s, min(times))"),
    ('flow', 'the body time is not re-checked', "if view is not None and (view['block_time'] is None or view['block_time'] > cut):", "if False:"),
    ('flow', 'monotonic counts flat moves', "moves = [(b > a) for a, b in zip(prices, prices[1:]) if b != a]", "moves = [(b > a) for a, b in zip(prices, prices[1:])]"),
    ('flow', 'cv uses the max, not the mean', "out['trade_size_cv'] = round(sd / mean, 4)", "out['trade_size_cv'] = round(sd / max(sizes), 4)"),
    ('flow', 'a roundtrip is any wallet', "if sides == {'buy', 'sell'})", "if sides)"),
    ('flow', 'top-1 instead of top-5', "sorted(counts.values(), reverse=True)[:5]", "sorted(counts.values(), reverse=True)[:1]"),
    ('flow', 'the up streak never resets', "streak = streak + 1 if b > a else 0", "streak = streak + 1 if b > a else streak"),
    ('flow', 'the runup is from the first price', "            low = min(low, p)\n", ""),
    ('flow', 'return_vol ignores the transaction gap', "/ max(1, (b.get('position') or 0) - (a.get('position') or 0))", "/ 1"),
    ('flow', 'an aliasing sample', "                sample = stratified(window, cfg['max_txs'])",
     "                sample = window[::max(1, len(window) // cfg['max_txs'])][:cfg['max_txs']]"),
    # --- learner guards
    ('learn', 'an uncut flow window accepted', "        if not _num(after) or after > 0:                     # the window ran past the decision", "        if False:"),
    ('learn', 'any flow flag accepted', "if flag is not None and flag not in FLOW_OK_FLAGS:", "if False:"),
    ('learn', 'old flow rows accepted', "if row.get('version') != FLOW_VERSION:", "if False:"),
    ('learn', 'flow rows never read', "            xrow = _pick(flow_rows.get(path.mint, []), decision, path.candidate_id, policy, counts, 'FLOW')     # L20",
     "            xrow = None"),
    # --- report / notify
    ('report', 'unverified sells not a reason of their own', "exit_reason[row['fill_id']] = 'SELL_UNVERIFIED:' + exit_reason[row['fill_id']]", "pass"),
    ('notify', 'no warning in the SELL message', "out['sell_unverified'] = execution.get('sell_status') == 'SELL_UNVERIFIED'", "out['sell_unverified'] = False"),
]


def run_tests():
    return subprocess.run([sys.executable, '-m', 'unittest', '-q'] + TESTS, cwd=ROOT, capture_output=True, text=True).returncode


def main():
    files = {'se': 'sell_evidence', 'flow': 'flow', 'exec': 'execution', 'paper': 'paper', 'learn': 'learn', 'report': 'report', 'notify': 'notify'}
    files = {k: ROOT / 'lean' / (v + '.py') for k, v in files.items()}
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
            print('%-7s %-55s %s' % (target, name, 'killed' if rc != 0 else 'SURVIVED'))
            if rc == 0:
                survived.append(name)
    finally:
        for k, p in files.items():
            p.write_text(originals[k])
    print('%d mutants, %d killed, %d survived %s' % (len(MUTANTS), len(MUTANTS) - len(survived), len(survived), survived))
    return 0 if not survived else 1


if __name__ == '__main__':
    sys.exit(main())
