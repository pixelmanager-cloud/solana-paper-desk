"""Cold-cost benchmark of the global terminal gate and of monitoring accounting against retained history (T24R). Fixtures only.

    python -m tools.research.bench_history_scale [--scales 1,10,30] [--pages-per-scale 150] [--reads-per-scale 30] [--json OUT]
    python -m tools.research.bench_history_scale --days 7        # the 7-day fresh-start fixture (slow on code before T24R)

A "scale" multiplies a base amount of retained history: evidence pages (history pages, attempts, intents, results ...) and
completed monitoring reads. The 7-day profile assumes 240 dispatched candidates a day at about 20 evidence pages each (4,800
pages a day) and 8 hours of held monitoring a day at the 72 reads/hour the allowance sees while a position is open (576 reads a
day). Those are planning assumptions, not measurements; change them with ``--pages-per-day`` / ``--reads-per-day``.

For every scale the harness builds a REAL retained history-preparation rejection (the production publisher, the test fixtures'
synthetic wire bytes) plus the requested number of unrelated evidence pages, then measures, on a connection that has not seen
the data (cold): one ``terminal.gate`` call (seconds and ``terminal._load`` calls), and, on a second fixture with that many
completed monitoring reads, one ``MonitoringBudget.snapshot`` (seconds and ``EvidenceStore.load`` calls) and the marginal cost of
one more read. It only uses interfaces that exist both before and after T24R, so the same file can be run on the base commit; a
measurement that raises (for example the former 4096-page inventory latch) is reported as ``ERROR:<message>``, not hidden.

No network, no provider key, nothing outside the temporary directories the fixtures create.
"""
import sys
sys.dont_write_bytecode = True
import argparse
import json
import time
import zlib
from contextlib import closing
from unittest.mock import patch

DAY_PAGES, DAY_READS = 4800, 576


def timed(function):
    started = time.perf_counter()
    try:
        value = function()
    except Exception as error:     # noqa: BLE001 - a failing measurement is data
        return time.perf_counter() - started, 'ERROR:%s:%s' % (type(error).__name__, str(error)[:80])
    return time.perf_counter() - started, value


def counting(owner, name):
    calls = [0]
    real = getattr(owner, name)

    def wrapper(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)
    return calls, patch.object(owner, name, side_effect=wrapper)


def add_pages(store, count, offset=0):
    """Unrelated, valid evidence pages in one transaction (about 200 bytes each, like a small attempt record)."""
    from desk.model import canonical, digest
    with closing(store.connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        for i in range(offset, offset + count):
            payload = {'bench_filler': i, 'pad': 'y' * 200}
            raw = canonical(payload).encode()
            c.execute('INSERT OR IGNORE INTO pages VALUES(?,?,?)', (digest(payload), zlib.compress(raw), len(raw)))
        c.execute('COMMIT')


def measure_gate(pages):
    from desk import paper_terminal_reconciliation as terminal
    from tests import test_history_preparation_phase as fixture
    h = fixture.PreparationTests('test_global_gate_retirement_and_unrelated_scan')
    h.setUp()
    try:
        h.rejected()
        add_pages(h.store, pages)
        loads, patcher = counting(terminal, '_load')
        with patcher:
            seconds, outcome = timed(lambda: terminal.gate(h.store, h.context['research_db'], ('b' * 32,)))
        with closing(h.store.connect()) as c:
            total_pages = c.execute('SELECT count(*) FROM pages').fetchone()[0]
        return {'pages': total_pages, 'gate_seconds': round(seconds, 4), 'gate_loads': loads[0],
                'gate': 'OK' if outcome is None else outcome}
    finally:
        h.doCleanups()


def measure_monitoring(reads):
    from desk.evidence import EvidenceStore
    from tests import test_monitoring_budget as fixture
    h = fixture.MonitoringBudgetTests('test_sixty_shared_original_receipts_then_zero_io_and_no_investigation_reset')
    h.setUp()
    try:
        started = time.perf_counter()
        for n in range(reads):
            with closing(h.store.connect()) as c:
                used = c.execute('SELECT count(*) FROM paper_monitoring_reservations WHERE at>?', (h.now - 3600,)).fetchone()[0]
            if used >= 55:
                h.now += 3601                       # the allowance is 60 reads per rolling hour
            h.read()
        build = time.perf_counter() - started
        loads, patcher = counting(EvidenceStore, 'load')
        with patcher:
            seconds, outcome = timed(h.budget.snapshot)
        snapshot_loads = loads[0]
        loads2, patcher2 = counting(EvidenceStore, 'load')
        with patcher2:
            read_seconds, _ = timed(h.read)
        return {'reads': reads, 'snapshot_seconds': round(seconds, 4), 'snapshot_loads': snapshot_loads,
                'snapshot': 'OK' if isinstance(outcome, dict) else outcome, 'next_read_seconds': round(read_seconds, 4),
                'next_read_loads': loads2[0], 'build_seconds': round(build, 2)}
    finally:
        h.doCleanups()


def run(scales, pages_per_scale, reads_per_scale, progress=None):
    rows = []
    for scale in scales:
        row = {'scale': scale}
        row.update(measure_gate(scale * pages_per_scale))
        row.update(measure_monitoring(scale * reads_per_scale))
        rows.append(row)
        if progress:
            progress(row)
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    p.add_argument('--scales', default='1,10,30', help='comma separated multipliers of the base history')
    p.add_argument('--pages-per-scale', type=int, default=150)
    p.add_argument('--reads-per-scale', type=int, default=30)
    p.add_argument('--days', type=int, help='7-day style profile: scales=[days], per-scale amounts from --pages-per-day/--reads-per-day')
    p.add_argument('--pages-per-day', type=int, default=DAY_PAGES)
    p.add_argument('--reads-per-day', type=int, default=DAY_READS)
    p.add_argument('--json', help='also write the rows here (created exclusively)')
    args = p.parse_args(argv)
    if args.days:
        scales, pages, reads = [args.days], args.pages_per_day, args.reads_per_day
    else:
        scales, pages, reads = [int(x) for x in args.scales.split(',')], args.pages_per_scale, args.reads_per_scale
    if not scales or min(scales) < 1 or pages < 0 or reads < 0:
        raise SystemExit('scales must be positive and amounts non-negative')
    rows = run(scales, pages, reads, progress=lambda row: print(json.dumps(row, sort_keys=True), flush=True))
    if args.json:
        with open(args.json, 'x') as stream:
            json.dump(rows, stream, indent=1, sort_keys=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
