"""LINT4 mutation check (the integration hooks: lane rate, shared shutdown deadline, credit costs / shedding, the example's
coordinator decisions). NOT a test module (no ``test_`` prefix).

Each mutant is ONE textual replacement in one file. The file is restored after every run (also on Ctrl-C). A mutant is KILLED when
the LINT4 tests fail with it. Run from the repository root:

    TMPDIR=/private/tmp/ .venv/bin/python tests/lean/lint4_mutate.py

Exit code 0 only when every mutant is killed and the unmutated tree passes.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ['tests.lean.test_lint4', 'tests.lean.test_lint2b']

MUTANTS = [
    # --- lanes
    ('main', 'helius_rps never reaches the lanes',
     "low_shed_s=lanes.get('low_shed_s'), helius_rps=lanes.get('helius_rps'))", "low_shed_s=lanes.get('low_shed_s'))"),
    ('providers', 'helius_rps ignored by the limiters', "            rate, burst = RATES[provider]", "            rate, burst = DEFAULT_RATES[provider]"),
    ('providers', 'helius_rps not range-checked', "    if not math.isfinite(value) or not low <= value <= high:", "    if False:"),
    ('providers', 'a reset keeps the old rate', "        RATES.clear()\n        RATES.update(DEFAULT_RATES)\n", ""),
    ('providers', 'burst stays at the default', "            RATES['helius'] = (rps, rps)", "            RATES['helius'] = (rps, DEFAULT_RATES['helius'][1])"),
    ('main', 'helius_rps counted as a lane share', "if k not in ('low_shed_s', 'helius_rps', '_comment')}", "if k not in ('low_shed_s', '_comment')}"),
    # --- shutdown: one deadline for every auxiliary thread
    ('runner', 'each aux thread gets its own deadline', "            thread.join(max(0.0, aux_deadline - time.monotonic()))",
     "            thread.join(AUX_JOIN_S)"),
    ('runner', 'risk_external not started', "        if self.risk_external is not None:                                  # L21",
     "        if False:                                  # L21"),
    ('runner', 'bot_exit not wired to stop', "            self.bot_exit.stop = self.stop\n", ""),
    ('runner', 'the recorder outside the deadline', "        aux = [] if recorder is None else [recorder]", "        aux = []"),
    # --- credits
    ('ops', 'getAsset at 1 credit', "'getProgramAccounts': 10, 'getAsset': 10,", "'getProgramAccounts': 10, 'getAsset': 1,"),
    ('ops', 'flow not a low-priority lane', "'wallet_signals', 'flow', 'sell_evidence',", "'wallet_signals', 'sell_evidence',"),
    ('ops', 'rugcheck billed', "'rugcheck': {'default': 0}}", "'rugcheck': {'default': 1}}"),
]


def run_tests():
    return subprocess.run([sys.executable, '-m', 'unittest', '-q'] + TESTS, cwd=ROOT, capture_output=True, text=True).returncode


def main():
    files = {'main': ROOT / 'lean' / '__main__.py', 'providers': ROOT / 'lean' / 'providers.py', 'runner': ROOT / 'lean' / 'runner.py',
             'ops': ROOT / 'lean' / 'ops.py'}
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
            print('%-8s %s' % ('killed' if rc != 0 else 'SURVIVED', name))
            if rc == 0:
                survived.append(name)
    finally:
        for k, p in files.items():
            p.write_text(originals[k])
    print('%d/%d killed' % (len(MUTANTS) - len(survived), len(MUTANTS)))
    return 1 if survived else 0


if __name__ == '__main__':
    sys.exit(main())
