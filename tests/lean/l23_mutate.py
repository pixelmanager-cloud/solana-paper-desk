"""L23 mutation check for the bot-exit monitor (lean/bot_exit.py and its runner / strategy hooks). NOT a test module (no ``test_``
prefix).

Each mutant is ONE textual replacement in one file. The file is restored after every run (also on Ctrl-C). A mutant is KILLED when
the bot-exit tests fail with it. Run from the repository root:

    TMPDIR=/private/tmp/ .venv/bin/python tests/lean/l23_mutate.py

Exit code 0 only when every mutant is killed and the unmutated tree passes.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ['tests.lean.test_bot_exit']

MUTANTS = [
    # --- shadow must never trade; live must
    ('bot_exit', 'shadow mode exits too', "            if live:\n                self._live[key]", "            if True:\n                self._live[key]"),
    ('bot_exit', 'the trigger ignores the mode', "        if self.cfg.mode != 'live':\n            return None", "        if False:\n            return None"),
    ('strategy', 'live exit reason is DANGER', '"BOT_EXIT" if bot_exit else None', '"DANGER" if bot_exit else None'),
    ('runner', 'runner ignores the live trigger', "danger=bool(rug),\n                                   bot_exit=bool(bot))",
     "danger=bool(rug),\n                                   bot_exit=False)"),
    ('bot_exit', 'a live trigger outlives its position', "            for key in [k for k in self._live if k not in live_keys]:\n                del self._live[key]",
     "            for key in []:\n                del self._live[key]"),
    ('bot_exit', 'live signal lost on restart', "if kind == LIVE_KIND and self.cfg.mode == 'live':", "if False:"),
    ('bot_exit', 'an old shadow signal becomes a live exit', "if kind == LIVE_KIND and self.cfg.mode == 'live':", "if self.cfg.mode == 'live':"),
    ('bot_exit', 'a lifecycle signals twice', "if fired and key not in self._signaled and", "if fired and"),
    # --- evidence: never fire on missing or partial data
    ('bot_exit', 'an unknown metric holds', "            got = metrics.get(metric)\n", "            got = metrics.get(metric) or 0\n"),
    ('bot_exit', 'gaps ignored', "return not any(g_lo < hi and g_hi > lo for g_lo, g_hi in self.gaps)", "return True"),
    ('bot_exit', 'coverage ignored', "if self.covered_since is None or self.covered_since > lo:", "if self.covered_since is None:"),
    ('bot_exit', 'a full page leaves no gap', "                watch.gaps.append((previous, oldest))", "                pass"),
    ('bot_exit', 'failed txs counted as activity', "ok_recent, ok_base = sum(1 for f in recent if not f), sum(1 for f in base if not f)",
     "ok_recent, ok_base = len(recent), len(base)"),
    ('bot_exit', 'a quiet baseline has a bot rate', " and ok_base >= self.cfg.min_baseline_tx:", ":"),
    ('bot_exit', 'sell share on too few samples', "if len(swaps) >= self.cfg.min_sample:", "if swaps:"),
    ('bot_exit', 'whale fraction of the wrong reserve', "Decimal(s['sol']) / s['reserve_pre']", "Decimal(s['sol']) / (s['reserve_pre'] * 10)"),
    ('bot_exit', 'anyone is the creator', "swap['trader'] == watch.creator", "True"),
    ('bot_exit', 'anyone is a pumper', "watch.entered_at <= swap['t'] <= watch.entered_at + self.cfg.pumper_window_s", "True"),
    ('bot_exit', 'sampling drops the newest', "return [items[int(Decimal(i) * step)] for i in range(n)]", "return list(items[1:n + 1])"),
    # --- the low lane: bounded, never waiting, never draining it
    ('bot_exit', 'no pass budget', "        if budget[0] <= 0:\n            raise _Skip()", "        if False:\n            raise _Skip()"),
    ('bot_exit', 'samples not prioritised', "return (ratio is None, ratio if ratio is not None else Decimal(0), key)", "return (0, 0, key)"),
    ('bot_exit', 'samples prioritised backwards', "return (ratio is None, ratio if ratio is not None else Decimal(0), key)",
     "return (ratio is not None, -(ratio if ratio is not None else Decimal(0)), key)"),
    ('bot_exit', 'reserve ignored', "if self.cfg.reserve_tokens and callable(available) and available() < 1 + self.cfg.reserve_tokens:", "if False:"),
    ('bot_exit', 'a shed is not counted', "            if providers.is_shed(error):\n                self._count('sheds')",
     "            if providers.is_shed(error):\n                pass"),
    # --- the shadow value and the report
    ('bot_exit', 'no hypothetical quote in shadow', "quote=not live)", "quote=False)"),
    ('bot_exit', 'value without the paper slippage', "int(gross_lamports) * (paper.MAX_BPS - pcfg.slippage_bps) // paper.MAX_BPS",
     "int(gross_lamports)"),
    ('bot_exit', 'report: saved and cost swapped', "status='SAVED' if saved > 0 else 'COST' if saved < 0 else 'EVEN'",
     "status='SAVED' if saved < 0 else 'COST' if saved > 0 else 'EVEN'"),
    ('bot_exit', 'report: false positives not counted', "out['saved_count' if saved > 0 else 'cost_count'] += 1 if saved != 0 else 0",
     "out['saved_count'] += 1 if saved > 0 else 0"),
]


def run_tests():
    return subprocess.run([sys.executable, '-m', 'unittest', '-q'] + TESTS, cwd=ROOT, capture_output=True, text=True).returncode


def main():
    files = {'bot_exit': ROOT / 'lean' / 'bot_exit.py', 'runner': ROOT / 'lean' / 'runner.py', 'strategy': ROOT / 'lean' / 'strategy.py'}
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
