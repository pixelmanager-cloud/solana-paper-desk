"""M1 #7 calibration: how close is each mark model to the Jupiter quote a real sell got? READ-ONLY on the lean store.

    python tools/research/lean_mark_calibration.py --lean-db lean.sqlite [--fee-schedule fixtures/mainnet-dynamic-fees.json]
        [--max-dt 6] [--json out.json] [--extract fixtures/lean/m1-mark-calibration.json --extract-n 6]

SELL side: for every paper SELL with an L10 execution record, the decision quote q0 (Jupiter ``outAmount`` for exactly the
sold quantity, observation ``q0_ref``) is compared with the mark computed by ``lean.marks.MarkModel`` from the vault
balances the runner read in the nearest ``marks`` observation (|dt| <= max-dt s) and the pool account captured at the
screen (its virtual reserves; accrued fees as of the screen):
  vault/25                  today's marks (raw quote vault, pool_fee_bps 25)
  effective/dynamic         vault + virtual_quote_reserves, fee = the Pump fee program's market-cap tier (the FeeConfig
                            fixture; the live marks read the account itself) + the pool's creator_fee_bps override if any
  vault-fees+virtual/dyn    the analyst's variant: accrued fees subtracted from the vault (adjudicates the reserve definition)
  effective/<bps>           a flat fee (the ``pool_fee_bps`` fallback); the bps that centres the median is reported
BUY side: q0 tokens vs the screen reserves, read seconds before the quote with the accrued fees exact.
Ratios are q0 / model: 1.0 = the mark is the price a real sell got. Also broken down per fee tier. Writes nothing except
the optional outputs.
"""
import argparse
import base64
import bisect
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lean import marks as M          # noqa: E402


def ro(path):
    c = sqlite3.connect('file:%s?mode=ro' % path, uri=True)
    c.execute('PRAGMA query_only=1')
    return c


def quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return float('nan')
    pos = (len(xs) - 1) * q
    lo, hi = int(pos), min(len(xs) - 1, int(pos) + 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def summary(xs):
    xs = list(xs)
    return {'n': len(xs), 'p10': quantile(xs, .1), 'p25': quantile(xs, .25), 'median': quantile(xs, .5), 'p75': quantile(xs, .75),
            'p90': quantile(xs, .9)}


def vault(account):
    return int.from_bytes(base64.b64decode(account['data'][0])[64:72], 'little')


def model_with(cfg, schedule, pool_fee_bps=25):
    model = M.MarkModel(cfg, pool_fee_bps=pool_fee_bps)
    if schedule is not None:
        model._schedule = (schedule, 0.0)
        model.cfg['schedule_ttl_s'] = 1e18
    return model


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--lean-db', required=True)
    p.add_argument('--fee-schedule', default=str(ROOT / 'fixtures' / 'mainnet-dynamic-fees.json'))
    p.add_argument('--max-dt', type=float, default=6.0)
    p.add_argument('--json')
    p.add_argument('--extract')
    p.add_argument('--extract-n', type=int, default=6)
    a = p.parse_args(argv)
    schedule = M.decode_schedule(json.loads(Path(a.fee_schedule).read_text())['response']['value'])
    if schedule is None:
        raise SystemExit('fee schedule fixture does not decode')
    vault25 = model_with({'model': 'vault'}, None, 25)
    effective = model_with({'model': 'effective'}, schedule)
    c = ro(a.lean_db)

    screen = {}                      # mint -> pool fields + supply at the screen (screen:accounts_mint_pool, first capture)
    for mint, raw in c.execute("SELECT mint, raw FROM observations WHERE kind='screen:accounts_mint_pool' AND raw_truncated=0 ORDER BY id"):
        if mint in screen:
            continue
        try:
            values = json.loads(bytes(raw))['result']['value']
            supply = int.from_bytes(base64.b64decode(values[0]['data'][0])[36:44], 'little')
            screen[mint] = {'pool': M.pool_fields(values[1]), 'pool_account': values[1], 'supply': supply}
        except (ValueError, KeyError, TypeError, IndexError, M.MarkError):
            continue
    reads = {}                       # mint -> ([ts], [(base account, quote account)]) from the runner's marks observations
    for ts, meta, raw in c.execute("SELECT ts, meta, raw FROM observations WHERE kind='marks' AND raw_truncated=0 ORDER BY id"):
        meta = json.loads(meta)
        mints, per = meta.get('mints') or [], 3 if meta.get('layout') == 'base,quote,pool' else 2
        try:
            values = json.loads(bytes(raw))['result']['value']
        except (ValueError, KeyError, TypeError):
            continue
        for i, mint in enumerate(mints):
            if per * i + 1 < len(values):
                reads.setdefault(mint, ([], []))
                reads[mint][0].append(ts)
                reads[mint][1].append((values[per * i], values[per * i + 1]))
    fills = {r[0]: r for r in c.execute('SELECT id, ts, mint, side, qty_raw, sol_lamports FROM fills')}
    obs_ts, sells = {}, []
    for mint, fill_id, event, state in c.execute("SELECT mint, fill_id, event, state FROM position_state WHERE event IN ('rung','closed') ORDER BY id"):
        ex = (json.loads(state) or {}).get('execution') or {}
        if not ex.get('q0_out') or not ex.get('q0_ref') or fill_id not in fills or mint not in screen or mint not in reads:
            continue
        ref = ex['q0_ref']
        if ref not in obs_ts:
            got = c.execute('SELECT ts FROM observations WHERE id=?', (ref,)).fetchone()
            obs_ts[ref] = got[0] if got else None
        t = obs_ts[ref]
        if t is None:
            continue
        times, values = reads[mint]
        i = bisect.bisect_left(times, t)
        best = min((j for j in (i - 1, i) if 0 <= j < len(times)), key=lambda j: abs(times[j] - t), default=None)
        if best is None or abs(times[best] - t) > a.max_dt:
            continue
        sc, qty, q0 = screen[mint], fills[fill_id][4], ex['q0_out']
        try:
            base, quote = vault(values[best][0]), vault(values[best][1])
            info_in = {'supply_raw': sc['supply']}
            v25, _ = vault25.value(qty, base, quote, now=0, fee_lamports=0)
            veff, info = effective.value(qty, base, quote, pool=sc['pool'], pool_info=info_in, now=0, fee_lamports=0)
            accrued = info['accrued_fees_raw']
            vfee = M.sell_output(qty, base, M.effective_quote(quote - accrued, info['virtual_raw']), info['fees_bps'],
                                 has_creator=sc['pool']['coin_creator'] != M.DEFAULT_PUBKEY)
        except (M.MarkError, KeyError, TypeError, ValueError, IndexError):
            continue
        if min(v25, veff, vfee) <= 0:
            continue
        gross = info['quote_effective_raw'] * qty // (base + qty)
        sells.append({'mint': mint, 'ts': t, 'dt': times[best] - t, 'q0_out': q0, 'qty': qty, 'base': base, 'quote': quote,
                      'virtual': info['virtual_raw'], 'tier_bps': info['fee_bps'], 'cap_sol': info['market_cap_lamports'] / 1e9,
                      'implied_fee_bps': 10000 * (1 - q0 / gross), 'r:vault/25': q0 / v25, 'r:effective/dynamic': q0 / veff,
                      'r:vault-fees+virtual/dynamic': q0 / vfee,
                      '_accounts': (values[best][0], values[best][1], sc['pool_account']), '_supply': sc['supply']})
    # BUY side: q0 tokens vs the screen reserves
    decisions, buys = {}, []
    for mint, features in c.execute("SELECT mint, features FROM decisions WHERE kind='screen' AND action='PASS' ORDER BY id"):
        decisions[mint] = json.loads(features)
    for mint, fill_id, state in c.execute("SELECT mint, fill_id, state FROM position_state WHERE event='open' ORDER BY id"):
        ex = (json.loads(state) or {}).get('execution') or {}
        f = decisions.get(mint)
        if not ex.get('q0_out') or fill_id not in fills or not f:
            continue
        spend, q0 = fills[fill_id][5], ex['q0_out']
        base, gross, spendable = int(f['base_reserve_raw']), int(f['quote_gross_raw']), int(f['quote_spendable_raw'])
        virtual, supply = int(f.get('virtual_quote_reserves_raw') or 0), int(f['supply_raw'])
        fees, _cap = M.tier_fees(schedule, gross + virtual, base, supply)
        total = sum(fees.values())
        model = {'vault/25': M.buy_output(spend, base, gross, 25), 'effective/dynamic': M.buy_output(spend, base, gross + virtual, total),
                 'vault-fees+virtual/dynamic': M.buy_output(spend, base, spendable + virtual, total)}
        buys.append({'mint': mint, 'tier_bps': total, **{'r:' + k: q0 / v for k, v in model.items() if v > 0}})
    keys = ('vault/25', 'effective/dynamic', 'vault-fees+virtual/dynamic')
    out = {'sell': {k: summary(r['r:' + k] for r in sells) for k in keys},
           'buy': {k: summary(r['r:' + k] for r in buys if 'r:' + k in r) for k in keys},
           'buy_within_1e-5': {k: sum(1 for r in buys if abs(r.get('r:' + k, 0) - 1) < 1e-5) for k in keys},
           'sell_implied_fee_bps': summary(r['implied_fee_bps'] for r in sells)}
    out['sell_per_tier'] = {}
    for bps in sorted({r['tier_bps'] for r in sells}, reverse=True):
        rows = [r for r in sells if r['tier_bps'] == bps]
        out['sell_per_tier'][str(bps)] = {'n': len(rows), 'cap_sol_range': [min(r['cap_sol'] for r in rows), max(r['cap_sol'] for r in rows)],
                                          'effective/dynamic': summary(r['r:effective/dynamic'] for r in rows),
                                          'vault/25': summary(r['r:vault/25'] for r in rows),
                                          'implied_fee_bps': summary(r['implied_fee_bps'] for r in rows)}

    def flat(bps):
        model = model_with({'model': 'effective', 'fee_source': 'config'}, None, bps)
        return [r['q0_out'] / max(1, model.value(r['qty'], r['base'], r['quote'], pool_info={'virtual_quote_reserves_raw': r['virtual']},
                                                 now=0, fee_lamports=0)[0]) for r in sells]
    if sells:
        bps = min(range(25, 201, 5), key=lambda b: abs(quantile(flat(b), .5) - 1))
        out['fallback_pool_fee_bps'] = bps
        out['sell']['effective/flat_%d' % bps] = summary(flat(bps))
    print(json.dumps(out, indent=1, sort_keys=True))
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1, sort_keys=True) + '\n')
    if a.extract:
        chosen = sorted(sells, key=lambda r: r['ts'])
        step = max(1, len(chosen) // a.extract_n)
        picked = chosen[::step][:a.extract_n]
        Path(a.extract).write_text(json.dumps({
            'provenance': 'LEAN_PAPER_RUN_2026-10-11..13 (public mainnet accounts): Jupiter swap/v2/build outAmount (q0) of real paper '
                          'exit decisions, the base/quote vault accounts the runner read within %.0f s of it, and the pool account '
                          'captured at the screen (virtual reserves; accrued fees as of the screen). No keys, no wallet data.' % a.max_dt,
            'fee_schedule': 'fixtures/mainnet-dynamic-fees.json',
            'pairs': [{'mint': r['mint'], 'dt_s': round(r['dt'], 3), 'qty_raw': r['qty'], 'jupiter_out_lamports': r['q0_out'],
                       'supply_raw': r['_supply'], 'base_vault': r['_accounts'][0], 'quote_vault': r['_accounts'][1],
                       'pool': r['_accounts'][2]} for r in picked]}, indent=1, sort_keys=True) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
