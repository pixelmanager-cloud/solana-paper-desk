"""SYNTHETIC_TEST_ONLY: LINT2b low-lane starvation simulation (no network, no threads, a fake clock).

The REAL ``lean.providers.LowLaneLimiter`` (Helius low lane: 10 req/s x 0.20 = 2 req/s, burst 2, 30 s shed window) is shared by
discrete-event models of its four users. Each model mirrors the decision logic of its module (not its I/O):

  path recorder  (lean.paths._poll/_call)  every 15 s: ``chunks`` getMultipleAccounts calls; a no_token shed paces 1/rate (0.5 s)
                 while inside 80 % of the interval, else the rest of the poll is a GAP; a shed window (429) gaps at once.
  L11 checks     (lean.held_risk)          a position pass every 10 s; the account check every 6th pass (1 call); a shed skips it
                 (r1) or retries ONCE on the next pass (LINT2b).
  L12F features  (lean.features.enrich)    per hazard-free candidate 2 calls (+1 for a soft reject); a no_token shed ends the
                 enrichment (r1) or is retried every 0.5 s for up to 10 s (LINT2b).
  L13 signals    (lean.wallet_signals)     per screened candidate ~125 s after graduation: 15 calls; a no_token shed paced every
                 ``pace_s`` up to 30 s per request; LINT2b also yields while fewer than 1 + ``reserve`` tokens are left.

Candidates arrive as a Poisson stream (70/h); a 429 sheds the whole low lane for 30 s about every 20 minutes. Everything is
seeded, so the scenarios see the SAME arrivals and 429s. Run ``python -m tests.lean.lint2b_starvation`` to print the table.
"""
import heapq
import random

from lean.providers import LANE_SHED, LowLaneLimiter, ProviderError

RATE, BURST, SHED_S = 2.0, 2, 30.0
CALL_S = 0.15                       # time one request takes (the caller is busy, the bucket is not)


class Sim:
    def __init__(self, seed, *, hours, l13, l13_pace, l13_reserve, l11_retry, l12_retry, chunks=9, positions=True,
                 rate_per_h=70, shed_every_s=1200):
        self.t, self.seq, self.queue = 0.0, 0, []
        self.rng = random.Random(seed)
        self.end = hours * 3600.0
        self.limiter = LowLaneLimiter(RATE, BURST, clock=lambda: self.t, shed_s=SHED_S)
        self.m = {'path_due': 0, 'path_gaps': 0, 'l11_due': 0, 'l11_sheds': 0, 'l11_missed': 0, 'l11_retries': 0,
                  'l12_rows': 0, 'l12_shed_rows': 0, 'l13_rows': 0, 'l13_shed_stops': 0, 'l13_calls': 0, 'calls': 0}
        self.l13, self.l13_pace, self.l13_reserve = l13, l13_pace, l13_reserve
        self.l11_retry, self.l12_retry, self.chunks = l11_retry, l12_retry, chunks
        self.l13_backlog = []
        arrivals, t = [], 0.0
        while True:
            t += self.rng.expovariate(rate_per_h / 3600.0)
            if t >= self.end:
                break
            arrivals.append((t, self.rng.random()))
        self.spawn(self.path_recorder(), self.rng.uniform(0, 15))
        if positions:
            self.spawn(self.l11(), self.rng.uniform(0, 10))
        for at, kind in arrivals:
            hazard, soft = kind < 0.4, 0.4 <= kind < 0.7
            if not hazard:
                self.spawn(self.l12(soft), at)
            if l13:
                self.spawn(self._enqueue_l13(), at + 125.0)
        self.spawn(self.l13_worker(), 0.5) if l13 else None
        t = self.rng.uniform(0, shed_every_s)
        while t < self.end:
            self.spawn(self._shed(), t)
            t += self.rng.expovariate(1.0 / shed_every_s)

    # -- the scheduler -------------------------------------------------------------------------------------------------
    def spawn(self, gen, at):
        self.seq += 1
        heapq.heappush(self.queue, (at, self.seq, gen))

    def run(self):
        while self.queue:
            at, _seq, gen = heapq.heappop(self.queue)
            if at > self.end:
                break
            self.t = at
            try:
                delay = next(gen)
            except StopIteration:
                continue
            self.spawn(gen, self.t + max(0.0, delay))
        return self.m

    def take(self):
        """Acquire on the shared low lane: None (sent) or the shed 'why'."""
        try:
            self.limiter.acquire()
        except ProviderError as error:
            assert error.code == LANE_SHED
            return error.meta.get('why')
        self.m['calls'] += 1
        return None

    def _shed(self):
        self.limiter.shed(SHED_S)
        yield 0.0

    # -- the users ---------------------------------------------------------------------------------------------------------
    def path_recorder(self):
        interval, pace = 15.0, 1.0 / RATE
        while True:
            start, done = self.t, 0
            deadline = start + interval * 0.8
            self.m['path_due'] += self.chunks
            while done < self.chunks:
                why = self.take()
                if why is None:
                    done += 1
                    yield CALL_S
                    continue
                if why != 'no_token' or self.t + pace > deadline:
                    break
                yield pace
            self.m['path_gaps'] += self.chunks - done
            yield max(0.0, start + interval - self.t)

    def l11(self):
        passes, retry_pending, last_ok_cycle, cycle = 0, False, 0, 0
        while True:
            passes += 1
            due = passes % 6 == 1
            if due:
                cycle += 1
                self.m['l11_due'] += 1
                if last_ok_cycle < cycle - 1:
                    self.m['l11_missed'] += 1                  # the previous cycle never got a successful check
            retry, retry_pending = retry_pending and not due, False
            if due or retry:
                self.m['l11_retries'] += 1 if retry else 0
                why = self.take()
                if why is None:
                    last_ok_cycle = cycle
                    yield CALL_S
                else:
                    self.m['l11_sheds'] += 1
                    retry_pending = self.l11_retry and not retry
            yield 10.0 + self.rng.uniform(0.2, 2.0)             # position_interval + the pass itself (drifts)

    def l12(self, soft):
        need, waited_total, shed = 2 + (1 if soft else 0), 0.0, False
        for _ in range(need):
            waited = 0.0
            while True:
                why = self.take()
                if why is None:
                    yield CALL_S
                    break
                if why == 'no_token' and self.l12_retry and waited < 10.0:
                    waited += 0.5
                    yield 0.5
                    continue
                shed = True
                break
            if shed:
                break
        self.m['l12_rows'] += 1
        self.m['l12_shed_rows'] += 1 if shed else 0

    def _enqueue_l13(self):
        self.l13_backlog.append(self.t)
        yield 0.0

    def l13_worker(self):
        while True:
            if not self.l13_backlog:
                yield 5.0                                       # signals pass interval
                continue
            self.l13_backlog.pop(0)
            stopped = False
            for _ in range(15):
                waited = 0.0
                while True:
                    yielding = self.l13_reserve and self.limiter.available() < 1 + self.l13_reserve
                    why = 'no_token' if yielding else self.take()
                    if why is None:
                        self.m['l13_calls'] += 1
                        yield CALL_S
                        break
                    if why != 'no_token' or waited >= 30.0:
                        stopped = True
                        break
                    waited += self.l13_pace
                    yield self.l13_pace
                if stopped:
                    break
            self.m['l13_rows'] += 1
            self.m['l13_shed_stops'] += 1 if stopped else 0


SCENARIOS = {
    'r1 without L13': dict(l13=False, l13_pace=0.25, l13_reserve=0, l11_retry=False, l12_retry=False),
    'r1 with L13 (pace 0.25, no reserve)': dict(l13=True, l13_pace=0.25, l13_reserve=0, l11_retry=False, l12_retry=False),
    'LINT2b without L13': dict(l13=False, l13_pace=0.5, l13_reserve=1, l11_retry=True, l12_retry=True),
    'LINT2b with L13 (pace 0.5, reserve 1)': dict(l13=True, l13_pace=0.5, l13_reserve=1, l11_retry=True, l12_retry=True),
}


def simulate(seeds=range(5), hours=24, **over):
    """{scenario: summed metrics over the seeds}."""
    out = {}
    for name, kwargs in SCENARIOS.items():
        total = {}
        for seed in seeds:
            for k, v in Sim(seed, hours=hours, **{**kwargs, **over}).run().items():
                total[k] = total.get(k, 0) + v
        out[name] = total
    return out


def table(results):
    lines = ['%-40s %10s %10s %9s %9s %10s %11s %9s' % ('scenario', 'path gap%', 'L11 sheds', 'L11 miss', 'L11 retr',
                                                         'L12 shed%', 'L13 stop%', 'calls/s')]
    for name, m in results.items():
        lines.append('%-40s %10.3f %10d %9d %9d %10.2f %11s %9.3f' % (
            name, 100.0 * m['path_gaps'] / max(1, m['path_due']), m['l11_sheds'], m['l11_missed'], m['l11_retries'],
            100.0 * m['l12_shed_rows'] / max(1, m['l12_rows']),
            '-' if not m['l13_rows'] else '%.2f' % (100.0 * m['l13_shed_stops'] / m['l13_rows']), m['calls'] / (5 * 24 * 3600.0)))
    return '\n'.join(lines)


if __name__ == '__main__':
    for chunks in (9, 14):
        print('path recorder: %d calls per 15 s poll (%.2f req/s), 5 seeds x 24 h' % (chunks, chunks / 15.0))
        print(table(simulate(chunks=chunks)))
        print()
