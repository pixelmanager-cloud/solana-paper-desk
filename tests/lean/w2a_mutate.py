"""W2a mutation check: the shadow arms (lean/arms.py), their hooks in the runner / strategy / loader / report / notifier. NOT a test module
(no ``test_`` prefix).

Each mutant is ONE textual replacement in one file; the file is restored after every run (also on Ctrl-C). A mutant is KILLED when
tests/lean/test_arms.py or tests/lean/test_e2e_arms.py fails with it. Run from the repository root:

    TMPDIR=/private/tmp/ .venv/bin/python tests/lean/w2a_mutate.py [name-substring]

Exit code 0 only when every mutant is killed and the unmutated tree passes.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ['tests.lean.test_arms', 'tests.lean.test_e2e_arms', 'tests.lean.test_arms_w2a2']
FILES = {'arms': 'lean/arms.py', 'runner': 'lean/runner.py', 'strategy': 'lean/strategy.py', 'main': 'lean/__main__.py',
         'notify': 'lean/notify.py', 'report': 'lean/report.py'}

MUTANTS = [
    # --- pricing
    ('arms', 'a scaled quote ignores the size', "    out = quote.out_amount * new_in // quote.in_amount", "    out = quote.out_amount"),
    ('arms', 'the main quote is never reused', "        plan = None if deferred else ev.plan", "        plan = None"),
    ('arms', 'any size reuses the main quote', "            if 1 / self.cfg['max_scale'] <= ratio <= self.cfg['max_scale']:", "            if True:"),
    ('arms', 'an aborted entry is re-quoted', "        elif ev.aborted and not deferred:", "        elif False:"),
    ('arms', 'a shed is not recorded as a shed', "            return 'SKIP', [SKIP_SHED], {'shed': got[1]}", "            return 'SKIP', ['ARM_QUOTE_FAILED:LANE_SHED'], {'shed': got[1]}"),
    ('arms', 'the low-lane entry quote is not shared',
     "        if (shared is not None and 0 <= int(now_f) - shared[1] <= ttl and (not_before", "        if (False and shared is not None and 0 <= int(now_f) - shared[1] <= ttl and (not_before"),
    ('arms', 'entry quote on the main lane', "            quote, _raw, _meta = self.low.jupiter.quote(SOL_MINT, ev.mint, lamports, self.runner.taker)",
     "            quote, _raw, _meta = self.runner.providers.jupiter.quote(SOL_MINT, ev.mint, lamports, self.runner.taker)"),
    ('arms', 'marks read on the exit lane', "                result, _raw, _meta = self.low.helius.get_multiple_accounts(keys)",
     "                result, _raw, _meta = self.runner.exit_providers.helius.get_multiple_accounts(keys)"),
    ('arms', 'exit quote on the exit lane',
     "                quote, _raw, _meta = self.low.jupiter.quote(pend.mint, SOL_MINT, qty, self.runner.taker)\n                self.calls['jupiter_exit_quote'] += 1",
     "                quote, _raw, _meta = self.runner.exit_providers.jupiter.quote(pend.mint, SOL_MINT, qty, self.runner.taker)\n                self.calls['jupiter_exit_quote'] += 1"),
    # --- exits
    ('arms', 'no execution delay on an arm exit', "decided_at=now_f, due_at=now_f + delay,", "decided_at=now_f, due_at=now_f,"),
    ('arms', 'a missing route falls back to the mark', "                if error.transient and now_f - pend.decided_at >= self.cfg['exit_wait_s']:",
     "                if now_f - pend.decided_at >= self.cfg['exit_wait_s']:"),
    ('arms', 'main exit quotes ignored', "            if kind in EXIT_QUOTE_KINDS and at >= floor_after_delay and", "            if False and kind in EXIT_QUOTE_KINDS and at >= floor_after_delay and"),
    # --- the entry filter
    ('arms', 'rows written after the decision are read', "                if ts <= upto_ts or observed_before(meta, upto_ts):", "                if True:"),
    ('arms', 'AT_COLLECTION figures are used', "            elif ((meta or {}).get('flags') or {}).get(feature) == 'AT_COLLECTION':", "            elif False:"),
    ('arms', 'a null figure passes', "        if value is None:\n            return 'NOT_READY', {**info, 'why': why, 'conditions': seen}",
     "        if value is None:\n            value = Decimal(0)"),
    ('arms', 'the filter never rejects', "        if not OPS[cond['op']](value, Decimal(cond['value'])):", "        if False:"),
    ('arms', '<= excludes the boundary', "'<=': operator.le,", "'<=': operator.lt,"),
    ('arms', 'wait_s is ignored', "                if arm.filter['wait_s'] > 0 and now_f - ev.ts < arm.filter['wait_s']:", "                if False:"),
    # --- isolation, dedupe, hazards
    ('arms', 'a main hazard is entered', "        if ev.hazard:\n            return self._skip(arm, ev, ['MAIN_HAZARD:' + ev.hazard], {}, now_f)",
     "        if False:\n            return self._skip(arm, ev, ['MAIN_HAZARD:' + ev.hazard], {}, now_f)"),
    ('arms', 'an arm failure propagates', "            log.exception('arm %s %s failed', arm.name, what)\n        return None",
     "            raise\n        return None"),
    ('arms', 'a candidate is decided twice',
     "                if arm.halted or ev.cid in arm.decided or arm.holds(ev.cid):", "                if arm.halted:"),
    # --- storage
    ('arms', 'the log accepts updates', "BEGIN SELECT RAISE(ABORT,'arms store is append-only'); END", "BEGIN SELECT 1; END"),
    ('arms', 'the schema is not checked', "        if schema != {**TABLES, **INDEXES, **GUARDS}:", "        if False:"),
    # --- reading
    ('arms', 'the paired difference has the wrong sign', "    diffs = [a['ret'] - b['ret'] for a, b in both]", "    diffs = [b['ret'] - a['ret'] for a, b in both]"),
    ('arms', 'the -50% tail excludes its boundary', "p_le_minus_50=sum(1 for r in returns if r <= Decimal('-0.5')) / n",
     "p_le_minus_50=sum(1 for r in returns if r < Decimal('-0.5')) / n"),
    ('arms', 'ATA rent counted in the cost', "            t['cost'] += row['sol_lamports'] + row['fee_lamports']", "            t['cost'] += sol + fee"),
    # --- config
    ('arms', 'the name main is allowed', "or not NAME_RE.fullmatch(name) or name == 'main':", "or not NAME_RE.fullmatch(name):"),
    ('arms', 'duplicate names are allowed', "        if name in names:\n            raise ArmsConfigError('%s: duplicate arm name %s' % (where, name))",
     "        if False:\n            raise ArmsConfigError('%s: duplicate arm name %s' % (where, name))"),
    # --- runner hooks
    ('runner', 'the arms are not told of a candidate', "        self._w2a_candidate(candidate, cid, result, features)\n", "        pass\n"),
    ('runner', 'the arms are not told of the quote plan', "        self._w2a_plan((buy_q, buy_ref, buy_at), (sell_q, sell_at), xmeta)\n", "        pass\n"),
    ('runner', 'a finished candidate is not handed over', "        self._w2a_after(candidate)\n        # --- end W2a ---\n\n    # --- W2a hook ---",
     "        pass\n        # --- end W2a ---\n\n    # --- W2a hook ---"),
    ('runner', 'the main accounts are not captured', "                        self.arms.seen_accounts(mint, accounts, mark_at)", "                        pass"),
    ('runner', 'the main sell quotes are not captured', "            self.arms.seen_sell_quote(kind, mint, quote, ref, observed_at)", "            pass"),
    ('runner', 'the marks read does not wake the arms', "        self._w2a_marks_ready()                                               # --- W2a hook ---", "        pass"),
    ('runner', 'an idle pass does not wake the arms',
     "                self._w2a_marks_ready()                    # --- W2a hook: arms may hold positions the main arm does not ---", "                pass"),
    ('runner', 'the honeypot reject is not reported',
     "hazard='HONEYPOT_SIM_BLOCKED' if honeypot is not None and honeypot.get('rejected', 0) > note['hazard0'] else None,", "hazard=None,"),
    ('runner', 'the latency abort is not reported', "aborted=self.counts.get('entry_aborted_latency', 0) > note['aborted0'],", "aborted=False,"),
    ('runner', 'a retried candidate is handed over at once',
     "            if any(queued.mint == candidate.mint for queued, _attempt in self.retry):\n                return\n            from lean import arms as _arms",
     "            from lean import arms as _arms"),
    ('runner', 'the arm thread is not started', "            specs.append(('arms', self.arms.run_loop, (self.stop,)))", "            pass"),
    ('runner', 'run does not hand the work to the thread', "            self.arms.threaded = True\n", "            pass\n"),
    ('runner', 'health lacks the arms', "                'arms': self._aux_health('arms', self.arms),                                    # W2a\n", ""),
    # --- strategy, loader, notifier, report
    ('strategy', 'the full take-profit needs more than its ratio', "ratio >= cfg.full_take_profit_ratio:", "ratio > cfg.full_take_profit_ratio:"),
    ('strategy', 'the full take-profit never fires', '                reason = "TAKE_PROFIT"      # W2a: the whole', '                reason = None      # W2a: the whole'),
    # --- W2a2 (the pre-deploy review)
    ('main', 'the arms change the config hash', "plain = {k: v for k, v in cfg.items() if k != 'arms'}", "plain = dict(cfg)"),
    ('runner', 'the start event lacks the arms hash', "                                        'arms_config_hash': self.arms_config_hash,\n", ""),
    ('runner', 'health lacks the arms hash', "'lean_config_hash': self.lean_config_hash, 'arms_config_hash': self.arms_config_hash,",
     "'lean_config_hash': self.lean_config_hash,"),
    ('arms', 'a low-lane entry always fills at q0', "        if q1.out_amount >= q0.out_amount:                               # the market did not move", "        if True:                               # the market did not move"),
    ('arms', 'a low-lane entry fills at the better quote', "        if q1.out_amount >= q0.out_amount:                               # the market did not move", "        if q1.out_amount <= q0.out_amount:                               # the market did not move"),
    ('arms', 'a low-lane entry does not wait', "due_at=now_f + delay, fee_bps=fee_bps,", "due_at=now_f, fee_bps=fee_bps,"),
    ('arms', 'a worse q1 is not cost-checked', "            if check.action != 'BUY':\n                return self._abort(", "            if False:\n                return self._abort("),
    ('arms', 'a q1 failure is a plain skip', "return self._abort(arm, ev, ['Q1_UNAVAILABLE:%s' % got[1]], detail, now_f)", "return self._skip(arm, ev, ['Q1_UNAVAILABLE:%s' % got[1]], detail, now_f)"),
    ('arms', 'a q1 shed is not recorded', "return self._skip(arm, ev, [SKIP_SHED], {**detail, 'shed': got[1], 'stage': 'q1'}, now_f)",
     "return self._skip(arm, ev, ['Q1_SHED'], {**detail, 'shed': got[1], 'stage': 'q1'}, now_f)"),
    ('arms', 'the sell leg has a fixed buy fee', "net_in = quote.in_amount * 10000 // (10000 + shape['fee_bps'])", "net_in = quote.in_amount * 10000 // (10000 + 120)"),
    ('arms', 'the sell leg keeps the screen price', "effective = base * net_in // quote.out_amount - net_in", "effective = gross + virtual"),
    ('arms', 'MARK_FILL uses the decision-time mark', "        latest = arm.marks.get(pend.mint)\n        ttl", "        latest = (pend.mark, pend.mark_at)\n        ttl"),
    ('arms', 'MARK_FILL uses a stale mark', "if latest is None or latest[0] <= 0 or not 0 <= now_f - latest[1] <= ttl:", "if latest is None or latest[0] <= 0:"),
    ('arms', 'the headline keeps the flagged fills', "        trades = [t for t in every if not (t['flags'] & excluded_flags)]", "        trades = list(every)"),
    ('arms', 'the entry price source is not recorded', "            t['entry_source'] = t['entry_source'] or source", "            pass"),
    ('arms', 'the control leads the main arm', "        if arm.mirror and not ev.main_acted:", "        if False:"),
    ('arms', 'forced main exits are compared', "        if t.get('forced'):\n            forced.setdefault(t['mint'], t['forced'])\n        else:",
     "        if False:\n            forced.setdefault(t['mint'], t['forced'])\n        else:"),
    ('arms', 'a rug trigger is not a forced exit', "        if state.get('rug_trigger'):", "        if False:"),
    ('arms', 'an L20 write-off is not a forced exit', " or payload.get('write_off_reason') == 'SELL_UNVERIFIED':", ":"),
    ('arms', 'the filter health counts nothing', "            self.filter_stats[verdict] += 1", "            pass"),
    ('arms', 'the pin is not written at an open', "{**(M.screen_pool_info(ev.screen) or {}), **self.runner.mark_model.pin()}", "(M.screen_pool_info(ev.screen) or {})"),
    ('arms', 'the pin is not used for the marks', "fee_lamports=arm.pcfg.fee_lamports, model=pinned,", "fee_lamports=arm.pcfg.fee_lamports, model=None,"),
    ('arms', 'one bad mark stops the others', "                except Exception:                                          # noqa: BLE001 - this position only\n",
     "                except ZeroDivisionError:                                  # noqa: BLE001 - this position only\n"),
    ('arms', 'one bad position stops the others', "            except Exception:                                                # noqa: BLE001 - this position only (W2a2 #7)",
     "            except ZeroDivisionError:                                        # noqa: BLE001 - this position only (W2a2 #7)"),
    ('arms', 'one bad layout stops the read', "            except Exception:                                              # noqa: BLE001\n                self.counts['marks_layout_errors'] += 1",
     "            except ZeroDivisionError:                                      # noqa: BLE001\n                self.counts['marks_layout_errors'] += 1"),
    ('arms', 'the reserve token is not kept', "        if available() >= need:\n            return True", "        if True:\n            return True"),
    ('arms', 'the reserve ignores the lane burst', "need = min(1.0 + reserve, float(getattr(limiter, 'burst', 1.0 + reserve)))", "need = 1.0"),
    ('arms', 'a marks read ignores the reserve', "            if not self._lane_open(self.low.helius):", "            if False:"),
    ('arms', 'an entry quote ignores the reserve', "        if not self._lane_open(self.low.jupiter):\n            self.counts['entry_shed'] += 1", "        if False:\n            self.counts['entry_shed'] += 1"),
    ('arms', 'a prefetched row is ignored before it is written', "                if ts <= upto_ts or observed_before(meta, upto_ts):", "                if ts <= upto_ts:"),
    ('arms', 'a late row counts as observed early', "return (late is not None and late <= 0 and collected", "return (late is not None and collected"),
    ('arms', 'a row of a later decision counts', " and decision <= upto_ts)", ")"),
    ('arms', 'the row wait is unbounded', "                        and now_f - ev.ts < self.cfg['row_grace_s']):", "                        ):"),
    ('arms', 'the row wait runs without a prefetch', "                if (self.runner.prefetch is not None and not deferred", "                if (True and not deferred"),
    ('arms', 'an exit quote ignores the reserve', "        if not (shared is not None and shared[1] >= int(pend.due_at)) and not self._lane_open(self.low.jupiter):", "        if False:"),
    ('main', 'the arms block is not validated', "        cfg['arms'] = arms.config(cfg['arms'], base_dir=path.parent)", "        pass"),
    ('notify', '/arms is not a command', ", '/arms': 'cmd_arms'}", "}"),
    ('report', 'the report has no arms section',
     "summary['arms'] = _section(_arms_section, arms_path, db, all_trades, arms_exclude_flags) if arms_path.is_file() else {'status': 'NOT_PROVIDED'}",
     "summary['arms'] = {'status': 'NOT_PROVIDED'}"),
    ('report', 'the arms are compared with nothing', "    main_trades = main_trades_for_arms(all_trades, forced)", "    main_trades = []"),
]


def run_tests():
    return subprocess.run([sys.executable, '-m', 'unittest', '-q'] + TESTS, cwd=ROOT, capture_output=True, text=True).returncode


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    files = {name: ROOT / path for name, path in FILES.items()}
    originals = {k: p.read_text() for k, p in files.items()}
    if run_tests() != 0:
        print('BASELINE FAILS: fix the tests first')
        return 2
    survived, ran = [], 0
    try:
        for target, name, old, new in MUTANTS:
            if only and only not in name:
                continue
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
            ran += 1
            print('%-8s %s' % ('killed' if rc != 0 else 'SURVIVED', name))
            if rc == 0:
                survived.append(name)
    finally:
        for k, p in files.items():
            p.write_text(originals[k])
    print('%d/%d killed' % (ran - len(survived), ran))
    return 1 if survived else 0


if __name__ == '__main__':
    sys.exit(main())
