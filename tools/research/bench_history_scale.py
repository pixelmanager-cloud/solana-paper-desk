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
DAY_PASSES, DAY_RECEIPTS = 288, 20      # one pass every 5 minutes round the clock; about 20 retained rejections a day (planning figures)


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
    return calls, patch.object(owner, name, new=wrapper)   # a plain function: binds self on a class, stays a function on a module


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


def add_completed_passes(store, count, offset=0):
    """``count`` completed original passes whose outcome is NOT a receipt (what almost every real pass is: a closure or a result)."""
    rows = []
    for i in range(offset, offset + count):
        intent = store.save({'kind': 'paper_cycle_intent_v1', 'bench_pass': i, 'closure_v1': True})
        outcome = store.save({'kind': 'paper_pass_closure_v1', 'bench_pass': i, 'intent_hash': intent})
        rows.append(('%032x' % (0x10_0000_0000 + i), intent, outcome))
    with closing(store.connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        c.executemany('INSERT INTO paper_observation_passes VALUES(?,?,?)', rows)
        c.execute('COMMIT')


def add_clone_receipts(h, count):
    """``count`` more retained history-preparation rejections, structurally exactly like the real one (own pass, scan, intent page,
    outcome page, receipt row, verified-digest row and pass-inventory row). Their FULL replay is stubbed by ``measure_gate`` (it
    needs a real preparation history each); the binding checks the gate does for them are real."""
    from desk import history_preparation_rejection as rejection, pass_inventory, verified_index
    store = h.store
    with closing(store.connect()) as c:
        template = c.execute(f'SELECT pass_id,scan_id,intent_hash,outcome_hash FROM {rejection.TABLE}').fetchone()
    outcome = store.load(template[3])
    intent = store.load(template[2])
    clones = []
    for n in range(count):
        pass_id, scan = '%032x' % (0xA000_0000 + n), '%032x' % (0xB000_0000 + n)
        intent_key = store.save({**intent, 'bench_clone': n})
        page = {**outcome, 'pass_id': pass_id, 'scan_id': scan, 'intent_hash': intent_key}
        clones.append((pass_id, scan, intent_key, store.save(page)))
    with closing(store.connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        for pass_id, scan, intent_key, key in clones:
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,?)', (pass_id, intent_key, key))
            c.execute(f'INSERT INTO {rejection.TABLE} VALUES(?,?,?,?)', (pass_id, scan, intent_key, key))
            verified_index.record(c, rejection.KIND, pass_id, key, verified_index.proof_digest(rejection.KIND, key, pass_id, scan, intent_key))
            pass_inventory.record(c, pass_id, intent_key, key, rejection.PAGE_KIND, scan, intent_key)
        c.execute('COMMIT')
    return {c[0] for c in clones}


def measure_gate(pages, passes=0, receipts=0):
    from desk import history_preparation_rejection as rejection
    from desk import paper_terminal_reconciliation as terminal
    from tests import test_history_preparation_phase as fixture
    h = fixture.PreparationTests('test_global_gate_retirement_and_unrelated_scan')
    h.setUp()
    try:
        h.rejected()
        add_pages(h.store, pages)
        add_completed_passes(h.store, passes)
        clones = add_clone_receipts(h, receipts) if receipts else set()
        real_verify, replays, replaying = rejection.verify, [0], [False]
        real_load = terminal._load

        def counted_load(*args, **kwargs):                      # page loads of the GATE; the internals of a real replay are not counted
            if not replaying[0]:
                loads[0] += 1
            return real_load(*args, **kwargs)

        def verify(store, progress, value, **kwargs):          # clones have no preparation history to replay: count, do not prove
            replays[0] += 1
            if value.get('pass_id') in clones:
                return {'context': {'research_db': h.context['research_db']}}
            replaying[0] = True
            try:
                return real_verify(store, progress, value, **kwargs)
            finally:
                replaying[0] = False

        loads = [0]

        def gate_call():
            loads[0], replays[0] = 0, 0
            with patch.object(terminal, '_load', new=counted_load), patch.object(rejection, 'verify', new=verify):
                seconds, outcome = timed(lambda: terminal.gate(h.store, h.context['research_db'], ('b' * 32,)))
            return {'seconds': round(seconds, 4), 'loads': loads[0], 'replays': replays[0], 'outcome': 'OK' if outcome is None else outcome}

        cold = gate_call()                 # the first call after an upgrade also classifies every completed pass once
        warm = gate_call()                 # a later call, new connection: this is the steady state
        add_completed_passes(h.store, 1, offset=passes + 1)
        grown = gate_call()                # one more completed pass since the last call
        with closing(h.store.connect()) as c:
            total_pages = c.execute('SELECT count(*) FROM pages').fetchone()[0]
            total_passes = c.execute('SELECT count(*) FROM paper_observation_passes').fetchone()[0]
            total_receipts = c.execute(f'SELECT count(*) FROM {rejection.TABLE}').fetchone()[0]
        return {'pages': total_pages, 'passes': total_passes, 'receipts': total_receipts,
                'gate_seconds': cold['seconds'], 'gate_loads': cold['loads'], 'gate': cold['outcome'],
                'warm_gate_seconds': warm['seconds'], 'warm_gate_loads': warm['loads'], 'warm_replays': warm['replays'],
                'warm_gate': warm['outcome'], 'grown_gate_loads': grown['loads'], 'grown_gate': grown['outcome']}
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


def measure_scale(scale, pages_per_scale, reads_per_scale, passes_per_scale=0, receipts_per_scale=0):
    row = {'scale': scale}
    row.update(measure_gate(scale * pages_per_scale, scale * passes_per_scale, scale * receipts_per_scale))
    row.update(measure_monitoring(scale * reads_per_scale))
    return row


def run(scales, pages_per_scale, reads_per_scale, progress=None, passes_per_scale=0, receipts_per_scale=0, fresh=False):
    """Every scale in this process (default), or each in a FRESH interpreter (``fresh``: no warm caches, no shared module state)."""
    rows = []
    for scale in scales:
        if fresh:
            row = fresh_process(scale, pages_per_scale, reads_per_scale, passes_per_scale, receipts_per_scale)
        else:
            row = measure_scale(scale, pages_per_scale, reads_per_scale, passes_per_scale, receipts_per_scale)
        rows.append(row)
        if progress:
            progress(row)
    return rows


def fresh_process(scale, pages_per_scale, reads_per_scale, passes_per_scale, receipts_per_scale):
    import os
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    command = [sys.executable, '-m', 'tools.research.bench_history_scale', '--one-scale', str(scale),
               '--pages-per-scale', str(pages_per_scale), '--reads-per-scale', str(reads_per_scale),
               '--passes-per-scale', str(passes_per_scale), '--receipts-per-scale', str(receipts_per_scale)]
    done = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=3600, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    lines = [line for line in done.stdout.splitlines() if line.startswith('{')]
    if done.returncode != 0 or not lines:
        return {'scale': scale, 'ERROR': 'child exit %d: %s' % (done.returncode, done.stderr.strip()[-200:])}
    return json.loads(lines[-1])


# What must not grow with the history: page LOADS (exactly equal across scales) and a seconds ratio bounded by the constant fixture cost.
FLAT_LOADS = {'warm_gate_loads': 0, 'warm_replays': 0, 'snapshot_loads': 0, 'next_read_loads': 0,
              'grown_gate_loads': 2}      # value -> tolerance: the one new pass can coincide with a sampled one (+-2 loads)
FLAT_SECONDS = (('warm_gate_seconds', 2.5), ('snapshot_seconds', 3.0))


def check_flat(rows):
    """[] when gate and accounting cost are flat across ``rows``; otherwise one sentence per violation."""
    problems = []
    if len(rows) < 2:
        return ['at least two scales are needed to judge flatness']
    for row in rows:
        if 'ERROR' in row or any(str(row.get(k, '')).startswith('ERROR') for k in ('gate', 'warm_gate', 'grown_gate', 'snapshot')):
            problems.append('scale %s did not run cleanly: %s' % (row.get('scale'), {k: v for k, v in row.items() if 'ERROR' in str(v)}))
    ok = [r for r in rows if 'ERROR' not in r]
    for key, tolerance in FLAT_LOADS.items():
        values = {r['scale']: r[key] for r in ok if key in r}
        if values and max(values.values()) - min(values.values()) > tolerance:
            problems.append('%s grows with the history: %s' % (key, values))
    for key, ratio in FLAT_SECONDS:
        values = [r[key] for r in ok if key in r and type(r[key]) in (int, float)]
        if values and min(values) > 0 and max(values) / min(values) > ratio:
            problems.append('%s grew %.1fx (limit %.1fx): %s' % (key, max(values) / min(values), ratio, values))
    return problems


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    p.add_argument('--scales', default='1,10,30', help='comma separated multipliers of the base history')
    p.add_argument('--pages-per-scale', type=int, default=150)
    p.add_argument('--reads-per-scale', type=int, default=30)
    p.add_argument('--passes-per-scale', type=int, default=0, help='completed original passes added per scale unit (T24S)')
    p.add_argument('--receipts-per-scale', type=int, default=0, help='retained rejection receipts added per scale unit, at most 512 in total')
    p.add_argument('--days', type=int, help='7-day style profile: scales=[days], per-scale amounts from --pages-per-day/--reads-per-day')
    p.add_argument('--pages-per-day', type=int, default=DAY_PAGES)
    p.add_argument('--reads-per-day', type=int, default=DAY_READS)
    p.add_argument('--passes-per-day', type=int, default=DAY_PASSES)
    p.add_argument('--receipts-per-day', type=int, default=DAY_RECEIPTS)
    p.add_argument('--fresh', action='store_true', help='run every scale in a fresh interpreter (the reported numbers should use this)')
    p.add_argument('--one-scale', type=int, help=argparse.SUPPRESS)
    p.add_argument('--check', action='store_true', help='exit 1 unless gate and accounting cost are flat across the scales')
    p.add_argument('--json', help='also write the rows here (created exclusively)')
    args = p.parse_args(argv)
    if args.one_scale:
        print(json.dumps(measure_scale(args.one_scale, args.pages_per_scale, args.reads_per_scale, args.passes_per_scale,
                                       args.receipts_per_scale), sort_keys=True), flush=True)
        return 0
    if args.days:
        scales, pages, reads, passes, receipts = [args.days], args.pages_per_day, args.reads_per_day, args.passes_per_day, args.receipts_per_day
    else:
        scales, pages, reads = [int(x) for x in args.scales.split(',')], args.pages_per_scale, args.reads_per_scale
        passes, receipts = args.passes_per_scale, args.receipts_per_scale
    if not scales or min(scales) < 1 or pages < 0 or reads < 0 or passes < 0 or receipts < 0 or max(scales) * receipts > 512:
        raise SystemExit('scales must be positive, amounts non-negative and at most 512 receipts in total')
    if max(scales) * passes > 9000:
        raise SystemExit('more than 9000 passes would meet the 10,000 original-pass ceiling (rotate before ~34 days at 288 passes a day)')
    rows = run(scales, pages, reads, progress=lambda row: print(json.dumps(row, sort_keys=True), flush=True),
               passes_per_scale=passes, receipts_per_scale=receipts, fresh=args.fresh)
    if args.json:
        with open(args.json, 'x') as stream:
            json.dump(rows, stream, indent=1, sort_keys=True)
    problems = check_flat(rows) if args.check else []
    for problem in problems:
        print('NOT FLAT: ' + problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
