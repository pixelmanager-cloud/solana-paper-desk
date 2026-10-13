"""W2b: measure the READINESS AT THE DECISION of the collectors' features, prefetch OFF vs ON, in the realistic-latency fake world
(tests/lean/w2bworld.py: the real runner and collectors, only HTTP faked). NOT a test module (no ``test_`` prefix). Run from the repo root:

    TMPDIR=/private/tmp/ .venv/bin/python tests/lean/w2b_measure.py [--candidates 60] [--seed 7] [--faults 0.02]

Prints one JSON object: per feature the share of hazard-free candidates (flow: PASS candidates) whose value was
  ready   OBSERVED at or before the screen decision (``collected_at`` <= ``decision_ts``, ``late_s`` <= 0): the strategy could use it;
  usable  accepted by the selection learner's lookahead guards (a window cut at the decision stays usable even when it was collected after);
and the low-lane / RugCheck calls each actor made (the credit estimate)."""
import argparse
import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lean import prefetch as PF                              # noqa: E402
from tests.lean import w2bworld as W                         # noqa: E402


def run_once(plan, *, prefetch, faults=0.0, latency=True, seed=3, hazard_sample=0.0, tail=240.0, shed_windows=(), prefetch_cfg=None):
    tmp = tempfile.TemporaryDirectory()
    try:
        root = Path(os.path.realpath(tmp.name))
        sim = W.Sim(root, plan, prefetch=prefetch, latency=latency, fault_p=faults, seed=seed, hazard_sample=hazard_sample,
                    shed_windows=[(W.T0 + a, W.T0 + b) for a, b in shed_windows], prefetch_cfg=prefetch_cfg)
        try:
            sim.run(plan[-1][1] + 300.0 + tail)
            r = sim.runner
            ready = PF.readiness(r.store.path)
            r.write_health()
            health = json.loads((Path(r.state_dir) / 'health.json').read_text())
            decisions = {}
            for d in r.store.rows('decisions', kind='screen', limit=100000):
                decisions.setdefault(d['mint'], []).append((d['ts'], d['action']))
            lag = [min(t for t, a in v if a in ('PASS', 'REJECT')) - sim.scene.arrival[m] for m, v in decisions.items()
                   if any(a in ('PASS', 'REJECT') for _, a in v)]
            calls = {}
            for (actor, method), n in sorted(sim.scene.net.calls.items()):
                calls.setdefault(actor, {})[method] = n
            return {'readiness': ready, 'prefetch_health': health.get('prefetch'), 'calls_by_actor': calls,
                    'decision_lag_s': {'min': round(min(lag), 2), 'p50': round(sorted(lag)[len(lag) // 2], 2), 'max': round(max(lag), 2)},
                    'fills': len(r.store.rows('fills', limit=100000)), 'entered': sum(1 for f in r.store.rows('fills', limit=100000) if f['side'] == 'buy')}
        finally:
            sim.close()
    finally:
        tmp.cleanup()


def table(off, on):
    rows = []
    for name in on['readiness']['features']:
        a, b = off['readiness']['features'][name], on['readiness']['features'][name]
        rows.append((name, b['n'], a['ready_rate'], b['ready_rate'], a['usable_rate'], b['usable_rate']))
    return rows


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--candidates', type=int, default=60)
    p.add_argument('--seed', type=int, default=7)
    p.add_argument('--faults', type=float, default=0.02)
    p.add_argument('--hazard-sample', type=float, default=0.0)
    p.add_argument('--out', help='write the full JSON here (default: print it before the table)')
    p.add_argument('--gap', default='30,70', help='lo,hi seconds between launches (bursts: 6,30)')
    p.add_argument('--latency', type=float, default=1.0, help='multiplier of the measured call latencies')
    p.add_argument('--shed', default='', help='from,to[;from,to]: offsets (s) at which auxiliary Helius calls get HTTP 429 (the low lane then sheds 30 s)')
    p.add_argument('--lead', default='', help='kind=seconds[,kind=seconds]: prefetch lead_s overrides')
    a = p.parse_args(argv)
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    lo, hi = (float(x) for x in a.gap.split(','))
    plan = W.scenario(a.candidates, seed=a.seed, gap=(lo, hi))
    shed = [tuple(float(x) for x in w.split(',')) for w in a.shed.split(';') if w]
    lead = {k: float(v) for k, v in (kv.split('=') for kv in a.lead.split(',') if kv)}
    kw = dict(faults=a.faults, hazard_sample=a.hazard_sample, latency=a.latency if a.latency != 1.0 else True, shed_windows=shed)
    off = run_once(plan, prefetch=False, **kw)
    on = run_once(plan, prefetch=True, prefetch_cfg={'lead_s': lead} if lead else None, **kw)
    text = json.dumps({'population': on['readiness']['population'], 'off': off, 'on': on}, indent=1, sort_keys=True, default=str)
    if a.out:
        Path(a.out).write_text(text)
    else:
        print(text)
    print('\n%-40s %5s %12s %12s %12s %12s' % ('feature', 'n', 'ready off', 'ready on', 'usable off', 'usable on'))
    for row in table(off, on):
        print('%-40s %5s %12s %12s %12s %12s' % row)
    return 0


if __name__ == '__main__':
    sys.exit(main())
