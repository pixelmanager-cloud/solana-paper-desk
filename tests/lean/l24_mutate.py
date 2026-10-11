"""L24 mutation check for the linked-wallet cluster collector (lean/clusters.py) and its learner guard (lean/learn.py). NOT a test
module (no ``test_`` prefix).

Each mutant is ONE textual replacement in one file. The file is restored after every run (also on Ctrl-C). A mutant is KILLED when
the L24 tests fail with it. Run from the repository root:

    TMPDIR=/private/tmp/ .venv/bin/python tests/lean/l24_mutate.py

Exit code 0 only when every mutant is killed and the unmutated tree passes.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ['tests.lean.test_clusters', 'tests.lean.test_e2e_clusters']

MUTANTS = [
    # --- the graph
    ('clusters', 'hubs link', "blocked = set(hubs) | set(cfg['ignore_funders']) | tips", "blocked = set(cfg['ignore_funders']) | tips"),
    ('clusters', 'L13 funder map ignored', "funders = {w: s for w, s in l13_funders.items() if s is not None}", "funders = {}"),
    ('clusters', 'window same-slot crowd linked', "        for slot, wallets in sorted(by_slot.items()):\n            if 2 <= len(wallets) <= cfg['same_slot_max_group']:",
     "        for slot, wallets in sorted(by_slot.items()):\n            if 2 <= len(wallets):"),
    ('clusters', 'history same-slot removed', "    for _, wallets in sorted(by_hslot.items()):\n        if 2 <= len(wallets) <= cfg['same_slot_max_group']:",
     "    for _, wallets in sorted(by_hslot.items()):\n        if False:"),
    ('clusters', 'shared history signature removed', "            graph.link(wallets, 'same_tx')", "            pass"),
    ('clusters', 'bundle without adjacency', "tx['pos'] == current[-1]['pos'] + 1", "True"),
    ('clusters', 'bundle without a tip', "if len(run) >= 2 and any(tx['links']['tip'] for tx in run):", "if len(run) >= 2:"),
    ('clusters', 'co-signers not linked', "graph.link(set(tx['buyers']) | set(tx['links']['signers']), 'same_tx')", "pass"),
    ('clusters', 'SOL transfers not linked', "if src in known and dst in known:", "if False:"),
    ('clusters', 'singletons are clusters', "groups = [g for g in graph.components(nodes) if len(g) >= 2]", "groups = graph.components(nodes)"),
    ('clusters', 'creator cluster = creator only',
     "out['creator_cluster_supply_pct'] = _pct(sum(a for w, a in holders.items() if graph.find(w) == root), supply_raw)",
     "out['creator_cluster_supply_pct'] = _pct(holders.get(creator, 0), supply_raw)"),
    ('clusters', 'every examined wallet fresh', "out['fresh'] = oldest['block_time'] >= decision_ts - cfg['fresh_s']", "out['fresh'] = True"),
    ('clusters', 'truncated old history unknown', "            out['fresh'] = False                      # even the oldest", "            pass  # "),
    # --- no lookahead
    ('clusters', 'window not cut at the decision', "\n                  and s['block_time'] is not None and s['block_time'] <= decision_ts]", "]"),
    ('clusters', 'history after the decision used',
     "for s in page if s['block_time'] is not None and s['block_time'] <= decision_ts]", "for s in page]"),
    ('clusters', 'funding after the decision used', "if oldest['block_time'] is None or oldest['block_time'] > decision_ts or oldest['failed']:",
     "if oldest['failed']:"),
    ('clusters', 'hub evidence after the decision used', "(FUNDING_EVENT, asof - self.cfg['hub_window_s'], asof))",
     "(FUNDING_EVENT, asof - self.cfg['hub_window_s'], asof + 10 ** 9))"),
    ('clusters', 'screen holders not used', "                if holders_body is not None:", "                if False:"),
    ('clusters', 'collection snapshot not flagged', "        if holders_source == 'rpc_at_collection':", "        if False:"),
    ('clusters', 'pool vault kept', "rows = [(a, n) for a, n in rows if a not in skip and n > 0]", "rows = [(a, n) for a, n in rows if n > 0]"),
    # --- budget / lane
    ('clusters', 'no call cap', "calls = W._Calls(cfg['max_calls'], None, self.stop)", "calls = W._Calls(10 ** 6, None, self.stop)"),
    ('clusters', 'stop ignored', "calls = W._Calls(cfg['max_calls'], None, self.stop)", "calls = W._Calls(cfg['max_calls'], None, None)"),
    ('clusters', 'a stopped candidate writes a row', "            if stop.code == 'STOPPED':", "            if False:"),
    ('clusters', 'reserve for the other low-lane users ignored', "return available() < 1 + reserve", "return False"),
    ('clusters', 'no pacing on no_token', "if why != 'no_token' or waited >= self.cfg['shed_wait_s']:", "if True:"),
    ('clusters', 'no retry before learning', "                if attempts <= cfg['max_retries']:", "                if False:"),
    ('clusters', 'never waits for L13', "if self.signals_enabled and not has_signals and now - decision_ts < self.cfg['signals_wait_s']:", "if False:"),
    # --- the learner guard
    ('learn', 'old cluster versions accepted', "if row.get('version') != CLUSTERS_VERSION:", "if False:"),
    ('learn', 'late cluster snapshot accepted', "if not (_num(late) and late <= policy.max_late_s):", "if False:"),
    ('learn', 'unknown cluster flags accepted', "elif flag not in (None, *OK_FLAGS):", "elif False:"),
    ('learn', 'holders not from the screen accepted', "elif name != 'jito_bundled_buys' and row.get('holders_source') != 'screen':", "elif False:"),
    ('learn', 'uncut cluster window accepted', "if name == 'jito_bundled_buys' and (not _num(after) or after > 0):", "if False:"),
    ('learn', 'post-decision supply accepted for clusters', "if name in CLUSTER_SUPPLY_FEATURES and row.get('supply_source') != 'screen':", "if False:"),
    ('learn', 'latest clusters row regardless of its decision',
     "crow = _pick(cluster_rows.get(path.mint, []), decision, path.candidate_id, policy, counts, 'CLUSTERS')",
     "crow = (cluster_rows.get(path.mint) or [None])[-1]"),
]


def run_tests():
    return subprocess.run([sys.executable, '-m', 'unittest', '-q'] + TESTS, cwd=ROOT, capture_output=True, text=True).returncode


def main():
    files = {'clusters': ROOT / 'lean' / 'clusters.py', 'learn': ROOT / 'lean' / 'learn.py'}
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
