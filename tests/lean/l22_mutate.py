"""L22 mutation check for the honeypot sell-simulation filter. NOT a test module (no ``test_`` prefix).

Each mutant is ONE textual replacement in one file. The file is restored after every run (also on Ctrl-C). A mutant is KILLED when
the L22 tests fail with it. Run from the repository root:

    TMPDIR=/private/tmp/ .venv/bin/python tests/lean/l22_mutate.py

Exit code 0 only when every mutant is killed and the unmutated tree passes.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ['tests.lean.test_honeypot_sim', 'tests.lean.test_e2e_honeypot_sim', 'tests.lean.test_route_check', 'tests.lean.test_providers']

MUTANTS = [
    # --- scope
    ('hp', 'plain pump SPL simulated anyway', "in_scope=not (plain and source_is_pump)", "in_scope=True"),
    ('hp', 'non-pump source ignored', "in_scope=not (plain and source_is_pump)", "in_scope=not plain"),
    ('hp', 'live authority counts as plain', "facts['legacy_layout'] and not facts['mint_authority']", "facts['legacy_layout']"),
    ('hp', 'unreadable mint passes as not risky', "return dict(out, in_scope=True, risky=True,", "return dict(out, in_scope=True, risky=False,"),
    ('hp', 'transfer hook not risky', "(EXT_TRANSFER_HOOK in facts['extensions'] or facts['transfer_fee_bps'] > RISKY_FEE_BPS)",
     "(facts['transfer_fee_bps'] > RISKY_FEE_BPS)"),
    ('hp', 'fee threshold off by one', "facts['transfer_fee_bps'] > RISKY_FEE_BPS", "facts['transfer_fee_bps'] >= RISKY_FEE_BPS"),
    # --- decision
    ('hp', 'BLOCKED not rejected', "    if outcome == BLOCKED:\n        return REJECT_BLOCKED", "    if outcome == BLOCKED:\n        return None"),
    ('hp', 'risky unverified not rejected', "if outcome in (INCONCLUSIVE, NO_BUILD) and scoped.get('risky'):", "if False:"),
    ('hp', 'reject does not stop the entry', "            note(entry_reasons=[reason])\n        return True", "            note(entry_reasons=[reason])\n        return False"),
    # --- classification
    ('hp', 'transfer hook not blocked', "if parent in TOKEN_PROGRAMS and program not in TOKEN_PROGRAMS:", "if False:"),
    ('hp', 'frozen log ignored', "if any('frozen' in str(line).lower()", "if False and any('frozen' in str(line).lower()"),
    ('hp', 'benign token codes blocked', "if code is not None and code not in BENIGN_TOKEN_CODES:", "if code is not None:"),
    ('hp', 'setup failure counted as the swap', "if index != swap_index:", "if False:"),
    ('hp', 'fee payer error not retried', "'InsufficientFundsForFee', 'AccountNotFound'", "'AccountNotFound'"),
    ('hp', 'no retry', "for holder in holders[:2]:", "for holder in holders[:1]:"),
    ('hp', 'unbounded retries', "for holder in holders[:2]:", "for holder in holders[:3]:"),
    ('hp', 'zero-output build is not NO_BUILD', "if facts['conclusive'] or error.code == 'QUOTE_INVALID':", "if facts['conclusive']:"),
    ('hp', 'transient build is NO_BUILD', "if facts['conclusive'] or error.code == 'QUOTE_INVALID':", "if True:"),
    ('hp', 'funded-taker answer not retried', "if facts['error_code'] in UNSUPPORTED_ERROR_CODES:", "if False:"),
    # --- size, holders, budget
    ('hp', 'full-size block ignored', "if big['outcome'] == BLOCKED:", "if False:"),
    ('hp', 'full size without the balance', "if full and small['balance'] >= full:", "if full:"),
    ('hp', 'creator not excluded', "skip = set(BURN_OWNERS) | {features.get('pool'), _creator(screen)}", "skip = set(BURN_OWNERS)"),
    ('hp', 'PDA owner as fee payer', "if owner in skip or balance <= 0 or not _on_curve(owner):", "if owner in skip or balance <= 0:"),
    ('hp', 'pool vault not excluded', "            exclude.add(features['pool_base_token_account'])", "            pass"),
    ('hp', 'burn account not excluded', "exclude = _burn_accounts(mint, program) if program in TOKEN_PROGRAMS else set()", "exclude = set()"),
    ('hp', 'no hourly cap', "if len(self.recent) >= self.cfg['max_per_hour']:", "if False:"),
    ('hp', 'cap not persisted', "                    self.recent.append(float(row['ts']))", "                    pass"),
    ('hp', 'no decision_ts in meta', "'decision_ts': decision_ts,\n                'simulated'", "'decision_ts': None,\n                'simulated'"),
    ('hp', 'signed simulation', "'encoding': 'base64', 'sigVerify': False, 'replaceRecentBlockhash': True, 'commitment': 'confirmed'}])",
     "'encoding': 'base64', 'sigVerify': True, 'replaceRecentBlockhash': True, 'commitment': 'confirmed'}])"),
    # --- the provider gate
    ('providers', 'simulate gate removed', "if method == 'simulateTransaction' and not _unsigned_simulation(params):", "if False:"),
    ('providers', 'signed transaction accepted', "and not any(signatures)", "and True"),
    ('providers', 'sigVerify not enforced', "options.get('sigVerify') is not False", "False"),
    ('providers', 'blockhash replacement not enforced', "or options.get('replaceRecentBlockhash') is not True):", "):"),
    # --- wiring
    ('runner', 'runner hook removed', "if self.honeypot is not None and self.honeypot.reject(self, candidate, cid, result, gate):", "if False:"),
    ('main', 'filter never built', "r.honeypot = honeypot_sim.build(cfg.get('honeypot_sim'), the_store, clock=clock)", "r.honeypot = None"),
]


def run_tests():
    return subprocess.run([sys.executable, '-m', 'unittest', '-q'] + TESTS, cwd=ROOT, capture_output=True, text=True).returncode


def main():
    files = {'hp': ROOT / 'lean' / 'honeypot_sim.py', 'providers': ROOT / 'lean' / 'providers.py', 'runner': ROOT / 'lean' / 'runner.py',
             'main': ROOT / 'lean' / '__main__.py'}
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
            print('%-9s %-45s %s' % (target, name, 'killed' if rc != 0 else 'SURVIVED'))
            if rc == 0:
                survived.append(name)
    finally:
        for k, p in files.items():
            p.write_text(originals[k])
    print('%d mutants, %d killed, %d survived %s' % (len(MUTANTS), len(MUTANTS) - len(survived), len(survived), survived))
    return 0 if not survived else 1


if __name__ == '__main__':
    sys.exit(main())
