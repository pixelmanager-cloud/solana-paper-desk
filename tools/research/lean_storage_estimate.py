"""M1 #12/#13: estimated lean.sqlite growth per day before / after raw_policy errors_only and the row-spam throttles. READ-ONLY.

    python tools/research/lean_storage_estimate.py --lean-db lean.sqlite

Counts, over the store's own time span: the file size; the raw bytes errors_only would not keep (``lean.store.raw_droppable``);
the meta bytes M1 adds to quote / marks rows; the position_state ``update`` rows the 60 s / 1 % peak throttle would not write; the
exit BLOCKED rows and exit quotes the 300 s log throttle and the 10..60 s back-off would not write (simulated per position).
"""
import argparse
import json
import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lean.store import raw_droppable          # noqa: E402

MB = 1e6


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--lean-db', required=True)
    a = p.parse_args(argv)
    c = sqlite3.connect('file:%s?mode=ro' % a.lean_db, uri=True)
    size = os.path.getsize(a.lean_db) + (os.path.getsize(a.lean_db + '-wal') if os.path.exists(a.lean_db + '-wal') else 0)
    t0, t1 = c.execute('SELECT MIN(ts), MAX(ts) FROM observations').fetchone()
    days = (t1 - t0) / 86400
    by_kind = c.execute('SELECT kind, COUNT(*), SUM(length(raw)) FROM observations GROUP BY kind').fetchall()
    dropped = sum(b or 0 for k, n, b in by_kind if raw_droppable(k))
    rows_dropped = sum(n for k, n, b in by_kind if raw_droppable(k))
    quotes = sum(n for k, n, b in by_kind if k.startswith('quote:'))
    marks_rows, marks_mints = c.execute("SELECT COUNT(*), SUM(json_array_length(json_extract(meta,'$.mints'))) FROM observations "
                                        "WHERE kind='marks'").fetchone()
    added_meta = quotes * 45 + (marks_mints or 0) * 330            # in/out amounts; per-mint mark inputs (measured on the e2e rows)
    # peak throttle: replay the update rows per lifecycle
    kept = skipped = skipped_bytes = 0
    last = {}
    for oid, ts, event, state in c.execute('SELECT open_fill_id, ts, event, state FROM position_state ORDER BY id'):
        s = json.loads(state)
        peak = Decimal(str(s.get('peak_ratio', '0')))
        if event != 'update':
            last[oid] = (ts, peak, s.get('touched_15'), s.get('exit_failing_since'))
            continue
        prev = last.get(oid)
        peak_only = prev is not None and (s.get('touched_15'), s.get('exit_failing_since')) == prev[2:]
        if peak_only and ts - prev[0] < 60 and peak < prev[1] * Decimal('1.01'):
            skipped += 1
            skipped_bytes += len(state) + 120
            continue
        kept += 1
        last[oid] = (ts, peak, s.get('touched_15'), s.get('exit_failing_since'))
    # BLOCKED rows and exit quotes on a stuck position: one per 300 s / one per <= 60 s
    blocked = c.execute("SELECT mint, ts, length(features) + length(reasons) FROM decisions WHERE kind='exit' AND action='BLOCKED' "
                        'ORDER BY mint, ts').fetchall()
    b_skip_bytes, b_skipped, last_b = 0, 0, {}
    for mint, ts, n in blocked:
        if mint in last_b and ts - last_b[mint] < 300:
            b_skipped += 1
            b_skip_bytes += n + 150
        else:
            last_b[mint] = ts
    stuck = {m for m, _ts, _n in blocked}
    q_skip_rows = 0
    for mint in stuck:
        times = [t for (t,) in c.execute("SELECT ts FROM observations WHERE kind='quote:exit' AND mint=? ORDER BY ts", (mint,))]
        nxt, fails = None, 0
        for t in times:
            if nxt is not None and t < nxt:
                q_skip_rows += 1
                continue
            fails += 1
            nxt = t + min(60, 10 * 2 ** (fails - 1))
    row_overhead = 260                                              # an observation row without its body (meta, hashes, versions)
    after = size - dropped + added_meta - skipped_bytes - b_skip_bytes - q_skip_rows * row_overhead
    out = {'days': round(days, 3), 'file_mb': round(size / MB, 1), 'mb_per_day_now': round(size / MB / days, 1),
           'raw_dropped_mb': round(dropped / MB, 1), 'raw_dropped_rows': rows_dropped, 'meta_added_mb': round(added_meta / MB, 2),
           'peak_updates': {'kept': kept, 'skipped': skipped, 'mb': round(skipped_bytes / MB, 1)},
           'blocked_rows_skipped': b_skipped, 'blocked_mb': round(b_skip_bytes / MB, 2), 'stuck_exit_quotes_skipped': q_skip_rows,
           'mb_per_day_after': round(after / MB / days, 1),
           'raw_by_kind_mb': {k: round((b or 0) / MB, 1) for k, n, b in sorted(by_kind, key=lambda r: -(r[2] or 0))[:12]}}
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
