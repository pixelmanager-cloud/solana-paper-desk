"""M1 mutation check: #7 (effective marks: lean/marks.py, the runner / recorder / labels wiring) and #13 (per-pass row spam:
lean/runner.py), plus #10 / #12. NOT a test module (no ``test_`` prefix).

Each mutant is ONE textual replacement in one file; the file is restored after every run (also on Ctrl-C). A mutant is KILLED
when tests/lean/test_m1.py fails with it. Run from the repository root:

    TMPDIR=/private/tmp/ .venv/bin/python tests/lean/m1_mutate.py

Exit code 0 only when every mutant is killed and the unmutated tree passes.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ['tests.lean.test_m1', 'tests.lean.test_m1b']

MUTANTS = [
    # --- #7 the mark model
    ('marks', 'virtual reserves ignored', '    quote = int(vault_quote) + int(virtual_raw)', '    quote = int(vault_quote)'),
    ('marks', 'accrued fees subtracted', '    quote = int(vault_quote) + int(virtual_raw)',
     "    quote = int(vault_quote) + int(virtual_raw) - 3 * 10 ** 9"),
    ('marks', 'fee rounded down', 'charged = sum((gross * int(bps) + MAX_BPS - 1) // MAX_BPS', 'charged = sum((gross * int(bps)) // MAX_BPS'),
    ('marks', 'lowest tier always', "        if tier['market_cap_lamports_threshold'] <= cap:\n            selected = tier",
     "        if False:\n            selected = tier"),
    ('marks', 'market cap from the vault only', '    cap = quote_effective * supply // base_reserve', '    cap = (quote_effective - 18 * 10 ** 9) * supply // base_reserve'),
    ('marks', 'creator fee always charged', "if has_creator or name != 'creator_fee_bps')", "if True)"),
    ('marks', 'creator override ignored', "            if creator_override:", "            if False:"),
    ('marks', 'stale schedule used', "if held is None or not -SCHEDULE_SKEW_S <= float(now) - held[1] <= self.cfg['schedule_ttl_s']:", "if held is None:"),
    ('marks', 'fallback not flagged', "'fee_fallback': fee_source == 'config'", "'fee_fallback': False"),
    ('marks', 'config fee ignored on fallback', "            fees = config_fees(self.pool_fee_bps)          # FALLBACK",
     "            fees = config_fees(25)          # FALLBACK"),
    ('marks', 'no screen fallback', "        elif 'virtual_quote_reserves_raw' in info:\n            virtual, accrued", "        elif False:\n            virtual, accrued"),
    ('marks', 'pool account not read', "        return [base_vault, quote_vault, pool] if self._model(model) == 'effective' else [base_vault, quote_vault]",
     "        return [base_vault, quote_vault, pool]"),
    ('marks', 'schedule never read', "        return [fee_config_address()] if any(wanted) and self.cfg['fee_source'] == 'dynamic' else []",
     "        return []"),
    ('marks', 'buy fee on the output', '    net = spend * MAX_BPS // (MAX_BPS + int(fee_bps))', '    net = spend'),
    # --- #7 wiring
    ('runner', 'runner ignores the model', "                    if pinned == 'effective':\n                        net, info = model.value_accounts(",
     "                    if False:\n                        net, info = model.value_accounts("),
    ('runner', 'schedule account not observed', '                model.observe_call(values[position_keys:], mark_at)', '                pass'),
    ('runner', 'no pool_info in the open state',
     "            features = {**features, '_pool_info': {**(_marks.screen_pool_info(result.features) or {}), **self.mark_model.pin()}}",
     "            pass"),
    ('runner', 'mark inputs not in the meta', "                    stored[mint] = {**info, 'net_lamports': int(mark * paper.LAMPORTS)}", "                    pass"),
    ('paths', 'recorder ignores the model', '            if not self.mark_model.effective:\n                liquidity',
     '            if True:\n                liquidity'),
    ('paths', 'mark columns not migrated', "                self.db.execute('ALTER TABLE path_marks ADD COLUMN %s %s' % (column, kind))", "                pass"),
    ('labels', 'labels ignore effective points', '    if point.effective:                                       # M1 #7',
     '    if False:                                       # M1 #7'),
    # --- #13 row spam
    ('runner', 'BLOCKED logged every pass', "        if final.action != 'BLOCKED' or self.blocked_log_s <= 0:",
     "        if True:"),
    ('runner', 'BLOCKED key ignores the reasons', "held[0] == open_id and held[1] == key and 0 <= now - held[2] < self.blocked_log_s:",
     "held[0] == open_id and 0 <= now - held[2] < 10 ** 9:"),
    ('runner', 'no exit back-off', "        if self._backoff_active(mint, open_id, decision.reason, mark):", "        if False:"),
    ('runner', 'back-off not exponential', "        delay = min(self.exit_backoff_max_s, EXIT_BACKOFF_BASE_S * 2 ** (failures - 1))",
     "        delay = self.exit_backoff_max_s"),
    ('runner', 'back-off not capped', "        delay = min(self.exit_backoff_max_s, EXIT_BACKOFF_BASE_S * 2 ** (failures - 1))",
     "        delay = EXIT_BACKOFF_BASE_S * 2 ** (failures - 1)"),
    ('runner', 'back-off never reset', "            self._exit_backoff.pop(mint, None)                         # M1 #13: the exit is no longer wanted",
     "            pass"),
    ('runner', 'back-off stops the D2 clock', "        if since is not None and now - int(since) >= self.unexitable_after_s:",
     "        if since is not None and now - int(since) >= self.unexitable_after_s and not (self._exit_backoff.get(mint) "
     "and self.clock() < self._exit_backoff[mint][2]):"),
    ('runner', 'peak written every pass', "        if not force and self.peak_persist_s > 0 and keys == {'peak_ratio'}:", "        if False:"),
    ('runner', 'peak deferred forever', "and 0 <= now - last[1] < self.peak_persist_s and peak < last[2] * PEAK_PERSIST_STEP):",
     "):"),
    ('runner', 'pending peak not used', "            if pending[0] == open_id:\n                state = pending[1]", "            if False:\n                state = pending[1]"),
    ('runner', 'pending peak not flushed', "        self.flush_pending_states()                                            # M1 #13", "        pass"),
    # --- #10 / #12
    ('runner', 'no outage event', "        self._record_outage()                                                  # M1 #10", "        pass"),
    ('runner', 'stale written every pass', "        if not feed['stale'] and silent > self.discovery_stale_s:", "        if silent > self.discovery_stale_s:"),
    ('store', 'errors_only keeps everything', "        if self.raw_policy == 'errors_only' and raw_droppable(kind) and full:", "        if False:"),
    ('store', 'errors dropped too', "    return (kind.startswith('quote:') or", "    return (kind.startswith(('quote:', 'error:')) or"),
    ('runner', 'route_check reads the store', "raw=self._raw_cache.get(ref))   # L16 (+M1 raw)", ")   # L16 (+M1 raw)"),
    # --- M1b review fixes
    ('runner', 'b1 priority exits backed off', "        if reason in PRIORITY_EXITS and not held[4] and not (mark is not None and mark <= 0):\n            return False",
     "        if False:\n            return False"),
    ('runner', 'b1 a new reason does not reset', "        if held[3] != reason:\n            self._exit_backoff.pop(mint, None)\n            return False",
     "        if False:\n            self._exit_backoff.pop(mint, None)\n            return False"),
    ('runner', 'b1 a worthless quote is not stuck', "stuck=final.reason == 'STUCK_POSITION')", "stuck=False)"),
    ('runner', 'b1 zero mark ignored', "        if reason in PRIORITY_EXITS and not held[4] and not (mark is not None and mark <= 0):",
     "        if reason in PRIORITY_EXITS and not held[4]:"),
    ('marks', 'b2 no skew tolerance', "        if held is None or not -SCHEDULE_SKEW_S <= float(now) - held[1]",
     "        if held is None or not 0 <= float(now) - held[1]"),
    ('marks', 'b3a stale not flagged', "            if self.schedule_stale():", "            if False:"),
    ('marks', 'b3a a failed read is not remembered', "                if self._schedule_bad_since is None:\n                    self._schedule_bad_since = float(at)",
     "                if False:\n                    self._schedule_bad_since = float(at)"),
    ('marks', 'b3a a good read does not clear it', "                self._schedule = (fields, float(at))\n                self._schedule_bad_since = None",
     "                self._schedule = (fields, float(at))"),
    ('marks', 'b3b supply never checked', "        if schedule is not None and supply > 0 and not supply_plausible(supply, base, info.get('decimals')):",
     "        if False:"),
    ('marks', 'b3b base above supply accepted', "    if supply <= 0 or base_reserve > supply:", "    if supply <= 0:"),
    ('marks', 'b3b any token count accepted', "        return low * 10 ** decimals <= supply <= high * 10 ** decimals", "        return True"),
    ('risk_external', 'b4 kept always true', "raw['kept'] = bool(keeps(RAW_KIND)) if callable(keeps) else True", "raw['kept'] = True"),
    ('labels', 'b5 vault rows at the lean.json fee', "pool_fee_bps=point.vault_fee_bps, pcfg=pcfg)", "pool_fee_bps=pool_fee_bps, pcfg=pcfg)"),
    ('labels', 'b5 vault quotes at the lean.json fee', "(paper.MAX_BPS - point.vault_fee_bps)", "(paper.MAX_BPS - pool_fee_bps)"),
    ('labels', 'b5 mixed models labelled', "    if len({p.model or 'vault' for p in points if not p.dead}) > 1:", "    if False:"),
    ('learn', 'b5 every run by default', "        config_hash = None if args.all_configs else (args.config_hash or latest_config_hash(args.lean_db))",
     "        config_hash = args.config_hash"),
    ('marks', 'b6 pin ignored', "            model = pool_info.get('model', 'effective')\n            if model in MODELS:",
     "            model = pool_info.get('model', 'effective')\n            if False:"),
    ('marks', 'b6 unpinned positions take the configured model',
     "        return 'vault', self.pool_fee_bps if self.model == 'vault' else LEGACY_VAULT_FEE_BPS", "        return self.model, self.pool_fee_bps"),
    ('marks', 'b6 pinned fee ignored', "                return model, int(fee) if type(fee) is int else self.pool_fee_bps",
     "                return model, self.pool_fee_bps"),
    ('runner', 'b6 a pinned vault position at the config fee', "pool_fee_bps=vault_fee, pcfg=self.pcfg)",
     "pool_fee_bps=self.pool_fee_bps, pcfg=self.pcfg)"),
]


def run_tests():
    return subprocess.run([sys.executable, '-m', 'unittest', '-q'] + TESTS, cwd=ROOT, capture_output=True, text=True).returncode


def main():
    files = {name: ROOT / 'lean' / (name + '.py') for name in ('marks', 'runner', 'paths', 'labels', 'store', 'risk_external', 'learn')}
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
