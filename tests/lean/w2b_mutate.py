"""W2b mutation check: #8 (lean/prefetch.py and the collectors' prefetch / commit hooks) and #9 (the hazard-reject path sample and its separation
in the learner). NOT a test module (no ``test_`` prefix).

Each mutant is ONE textual replacement in one file; the file is restored after every run (also on Ctrl-C). A mutant is KILLED when the W2b
tests (tests/lean/test_w2b.py and tests/lean/test_e2e_w2b.py) fail with it. Run from the repository root:

    TMPDIR=/private/tmp/ .venv/bin/python tests/lean/w2b_mutate.py

Exit code 0 only when every mutant is killed and the unmutated tree passes.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ['tests.lean.test_w2b', 'tests.lean.test_e2e_w2b']

MUTANTS = [
    # --- #8 the use rules: a result is usable only if it finished AND is as-of before the decision
    ('prefetch', 'a result that finished after the decision is used', "            elif job.finished_at > decision_ts:", "            elif False:"),
    ('prefetch', 'a stale result is used', "            elif decision_ts - job.asof > self.cfg['max_stale_s']:", "            elif False:"),
    ('prefetch', 'as-of later than the start (lookahead)', "        job.asof = job.started_at - (self.cfg['settle_s'] if kind in WINDOWED else 0.0)",
     "        job.asof = job.started_at + 40.0"),
    ('prefetch', 'no settle time before the as-of', "        job.asof = job.started_at - (self.cfg['settle_s'] if kind in WINDOWED else 0.0)",
     "        job.asof = job.started_at"),
    ('prefetch', 'hazards are prefetched', "                if entry.gate == 'HAZARD' and self.cfg['skip_hazards']:", "                if False:"),
    ('prefetch', 'a job may start after the screen', "        if now > entry.received + self.min_age_s + SCREEN_LAG_S - self.needs(kind):", "        if False:"),
    ('prefetch', 'readiness counts a late job', "if job.state == 'done' and job.finished_at is not None and job.finished_at <= decision_ts and job.asof <= decision_ts:",
     "if job.state == 'done':"),
    # --- #8 the commits: the real times, never the commit's own
    ('prefetch', 'wallet row late_s from the commit time',
     "features.update(decision_ts=decision_ts, collected_at=pre['finished_at'], late_s=round(pre['finished_at'] - decision_ts, 3),",
     "features.update(decision_ts=decision_ts, collected_at=pre['finished_at'], late_s=round(self.clock() - decision_ts, 3),"),
    ('prefetch', 'wallet row collected at the commit time', "features.update(decision_ts=decision_ts, collected_at=pre['finished_at'],",
     "features.update(decision_ts=decision_ts, collected_at=self.clock(),"),
    ('prefetch', 'flow row collected at the commit time', "        body['started_at'] = pre['finished_at']", "        body['started_at'] = self.clock()"),
    ('flow', 'a prefetched flow window ends at the finish', "        end_s = math.floor(decision_ts)          # whole block-time seconds",
     "        end_s = math.floor(decision_ts if sink is None else self.clock())          # whole block-time seconds"),
    ('wallet_signals', 'the wallet window is not cut at the cut', "                    window = [s for s in window if s['block_time'] is not None and s['block_time'] <= decision_ts]",
     "                    pass"),
    ('features', 'the prefetched pool history is not cut', "creator=creator, need_holders=need_holders, screen_time=None, decision_ts=asof,",
     "creator=creator, need_holders=need_holders, screen_time=None, decision_ts=None,"),
    ('features', 'the prefetched row is stamped with the commit time', "                                default_why=why, calls=calls, flags=flags, collected_at=collected_at,",
     "                                default_why=why, calls=calls, flags=flags, collected_at=None,"),
    ('risk_external', 'a token created after the cut is counted', "creator_tokens if m != mint and ts is not None and ts < decision_ts]",
     "creator_tokens if m != mint and ts is not None and ts < decision_ts + 10 ** 7]"),
    ('risk_external', 'the prefetched RugCheck row is stamped with the commit time',
     "out = build_row(mint, screen_passed=passed, decision_ts=decision, collected_at=pre['finished_at'], calls=row['calls'], fields=fields,",
     "out = build_row(mint, screen_passed=passed, decision_ts=decision, collected_at=self.clock(), calls=row['calls'], fields=fields,"),
    ('learn', 'a prefetch supply is trusted whatever its lateness',
     "row.get('supply_source') == 'rpc_prefetch' and _num(row.get('late_s')) and row['late_s'] <= policy.eps_s)", "row.get('supply_source') == 'rpc_prefetch')"),
    ('runner', 'the runner never tells the prefetch about a decision', "            self.prefetch.on_decision(mint, self.clock(), result)", "            pass"),
    ('__main__', 'the hash covers the new keys while they are off', "    if prefetch is None or (isinstance(prefetch, dict) and not prefetch.get('enabled')):",
     "    if False:"),
    # --- #9 the hazard sample
    ('paths', 'the sample ignores its fraction', "    return int.from_bytes(digest[:8], 'big') / 2.0 ** 64 < fraction", "    return True"),
    ('paths', 'the sample is not deterministic', "    return int.from_bytes(digest[:8], 'big') / 2.0 ** 64 < fraction",
     "    import random\n    return random.random() < fraction"),
    ('paths', 'the fraction is not range checked', "not 0 <= sample <= 1:", "not 0 <= sample <= 100:"),
    ('paths', 'a hazard path is not labelled', "stage=HAZARD_STAGE, reasons=hazard_reasons(screen),", "stage='screen', reasons=hazard_reasons(screen),"),
    ('paths', 'the hazard reason is not named', "HAZARD_REASON_PREFIX = 'hazard_reject: '", "HAZARD_REASON_PREFIX = ''"),
    ('paths', 'hazards are not counted', "                    self._bump('hazard_seen')", "                    pass"),
    ('paths', 'a hazard is entered', "                if self.start(item['mint'], item['cid'], features, entered=False, stage=HAZARD_STAGE,",
     "                if self.start(item['mint'], item['cid'], features, entered=True, stage=HAZARD_STAGE,"),
    ('learn', 'hazard paths are mixed into the real rows',
     "hazard_rows.append(HazardRow(path.mint, decision, label, trade.pnl_lamports, tuple(path.screen_reasons), trade.status))\n                continue",
     "hazard_rows.append(HazardRow(path.mint, decision, label, trade.pnl_lamports, tuple(path.screen_reasons), trade.status))"),
    ('wallet_signals', 'a hazard path is an outcome for the smart wallets', "            if hazard:\n                continue", "            if False:\n                continue"),
    # --- round 2: clusters, the retries, the pause, the recorder's queue
    ('prefetch', 'the clusters row is stamped with the commit time', "row.update(decision_ts=decision_ts, collected_at=pre['finished_at'],",
     "row.update(decision_ts=decision_ts, collected_at=self.clock(),"),
    ('clusters', 'the prefetched clusters window is not cut', "                  and s['block_time'] is not None and s['block_time'] <= decision_ts]",
     "                  and s['block_time'] is not None]"),
    ('learn', 'a prefetch holder snapshot is trusted whatever its lateness',
     "return source == 'screen' or (source == 'prefetch' and _num(late) and late <= policy.eps_s)", "return source in ('screen', 'prefetch')"),
    ('prefetch', 'a transient RugCheck failure is committed',
     "done = (self._retry(job, why) if payload['fields'].get('rc_score') is None and why not in (None, 'RC_UNKNOWN_MINT')", "done = (self._retry(job, why) if False"),
    ('prefetch', 'a features job on a shed lane is committed', "done = self._retry(job, shed[0]) if shed and payload['calls'] == 0 else self._finish(job, payload)",
     "done = self._finish(job, payload)"),
    ('features', 'the screen holder shares are overwritten by the job', "    if not need_holders:\n        for name in ('top1_pct', 'top10_pct'):", "    if False:\n        for name in ('top1_pct', 'top10_pct'):"),
    ('prefetch', 'a failed screen is a decision', "            if getattr(screen, 'error', None):\n                return                                           # a failed screen",
     "            if False:\n                return                                           # a failed screen"),
    ('prefetch', 'the gate never pauses', "            if self.gate is not None and not self.gate():", "            if False:"),
    ('prefetch', 'every probe says free', "        entry.gate = 'HAZARD' if is_hazard_reject(result) else 'FREE'", "        entry.gate = 'FREE'"),
    ('prefetch', 'the probe keeps the market bands', "'max_age_seconds': 10 ** 9, 'min_market_cap_usd': 0,", "'max_age_seconds': 10 ** 9, 'min_market_cap_usd': 10 ** 9,"),
    ('runner', 'the prefetch thread is not started', "            specs.append(('prefetch', self.prefetch.run_loop, ()))", "            pass"),
    ('__main__', 'the prefetch is not gated by the trader', "        r.prefetch.gate = r.entries_allowed", "        pass"),
    ('paths', 'a shed lane drops the hazard samples', "self._hazard_queue.extendleft(reversed([item for item, _ in reading]))", "pass"),
    ('paths', 'an old hazard sample is started', "                if now - item['queued_at'] > HAZARD_MAX_WAIT_S:", "                if False:"),
    ('paths', 'the hazard queue is unbounded', "            if len(self._hazard_queue) >= HAZARD_QUEUE_MAX:", "            if False:"),
]


def run_tests():
    return subprocess.run([sys.executable, '-m', 'unittest', '-q'] + TESTS, cwd=ROOT, capture_output=True, text=True).returncode


def main(argv=None):
    only = [a.lower() for a in (sys.argv[1:] if argv is None else argv)]      # optional: only the mutants whose name contains one of these words
    mutants = [m for m in MUTANTS if not only or any(word in m[1].lower() for word in only)]
    files = {name: ROOT / 'lean' / (name + '.py') for name in ('prefetch', 'flow', 'wallet_signals', 'features', 'risk_external', 'learn', 'runner',
                                                               '__main__', 'paths', 'clusters')}
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
