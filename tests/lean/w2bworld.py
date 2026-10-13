"""SYNTHETIC_TEST_ONLY (W2b): a REALISTIC-LATENCY fake world around tests/lean/fakeworld.py, for the prefetch (#8) and hazard-path (#9) e2e.
Not a test module (no ``test_`` prefix). Only HTTP is faked; every lean module above it is the real one.

Time. One ``SimClock`` for everything, with PER-ACTOR time: the trader's own passes (``main``: candidate loop, position loop) advance the
shared ``base`` clock by the latency of their calls, exactly like the trader's own thread; every other actor (prefetch, flow, wallet signals,
features, RugCheck recorder, path recorder: each its own daemon thread in production) runs at ``base`` with a PRIVATE offset that its
latencies and sleeps add to. So an auxiliary thread being slow can never move the trader's timestamps (main fills are byte-identical with
the auxiliary features on and off), while the time an auxiliary value was OBSERVED at (``base + offset``) is real. The low lane's token
bucket sees a non-decreasing high-water clock: calls of different threads are ordered on it, as they are in a process.

Latency. Per call, by provider / RPC method, with a deterministic jitter (a seeded draw per actor and call number). The latencies are the
ones measured on the production VPS (TOOL_AUDIT: screen PASS to BUY p50 4.7 s; discovery to screen p50 62.6 s) rounded: getMultipleAccounts
120 ms, getTokenLargestAccounts 220 ms, getSignaturesForAddress 280 ms, getTransaction 180 ms, Jupiter quote 350 ms, Kraken 500 ms, RugCheck
1.4 s. Optionally 2 % of the auxiliary getTransaction / getSignaturesForAddress calls fail with HTTP 503.
"""
import copy
import json
import random
import sqlite3
from collections import Counter
from decimal import Decimal
from urllib.parse import urlsplit

from desk.security import base58
from tests.lean import tape as TP
from tests.lean.fakeworld import Resp, SUPPLY, T0, Token, World
from tests.lean.test_risk_external import report as rugcheck_report

SLOT0 = 300_000_000
BLOCK_BEFORE_RECEIPT = 25            # the graduation block is 25 s older than the frame's receive time (as in test_e2e_features)
LATENCY = {'getMultipleAccounts': 0.12, 'getTokenLargestAccounts': 0.22, 'getSignaturesForAddress': 0.28, 'getTransaction': 0.18,
           'getTokenAccountsByOwner': 0.20, 'getTokenSupply': 0.10, 'jupiter': 0.35, 'kraken': 0.5, 'rugcheck': 1.4}
MAIN_ACTORS = ('candidates', 'positions')
DEV_UNITS = 2 * 10 ** 13


def slot_at(t):
    return SLOT0 + int((t - T0) / 0.4)


class SimClock:
    """``time()`` = ``base`` + the current actor's private offset (the main actors move ``base`` itself)."""

    def __init__(self, start=T0):
        self.base, self.off, self.main, self.actor = float(start), 0.0, True, 'candidates'
        self._hw = 0.0

    def time(self):
        return self.base + self.off

    def monotonic(self):
        """The trader's own limiters (main / exit lanes: only the main actors use them) see the trader's time and nothing else; the
        low lane (only auxiliary threads use it) sees a high-water mark, so calls of different threads are ordered on its bucket."""
        now = self.time() - T0 + 1000.0
        if self.main:
            return now
        self._hw = max(self._hw, now)
        return self._hw

    def sleep(self, seconds):
        seconds = max(0.0, float(seconds))
        if self.main:
            self.base += seconds
        else:
            self.off += seconds

    def enter(self, name):
        self.actor, self.main, self.off = name, name in MAIN_ACTORS, 0.0

    @property
    def t(self):                                      # (FakeTime compatibility for helpers that read it)
        return self.time()


class Net:
    """The HTTP layer: latency + (optionally) auxiliary 503s in front of the fake world, plus ``api.rugcheck.xyz``."""

    def __init__(self, sim, world, *, latency=True, fault_p=0.0, seed=1, shed_windows=()):
        self.sim, self.world, self.latency, self.fault_p, self.seed = sim, world, latency, fault_p, seed
        self.shed_windows = list(shed_windows)         # [(from, to)] absolute: an auxiliary Helius call inside answers HTTP 429
        self.reports = {}                              # mint -> (status, body, headers) | dict
        self.calls = Counter()                         # (actor, method) -> n
        self.log = []                                  # (actor, method, sent_at)
        self.n = Counter()

    def _lat(self, method):
        if not self.latency:
            return 0.0
        actor = self.sim.actor
        self.n[actor] += 1
        jitter = random.Random('%s:%d:%d' % (actor, self.n[actor], self.seed)).uniform(0.7, 1.3)
        return LATENCY[method] * jitter * (1.0 if self.latency is True else float(self.latency))

    def _faulty(self, method):
        if not self.fault_p or self.sim.actor in MAIN_ACTORS or method not in ('getTransaction', 'getSignaturesForAddress'):
            return False
        return random.Random('fault:%s:%d:%d' % (self.sim.actor, self.n[self.sim.actor], self.seed)).random() < self.fault_p

    def opener(self, request, timeout):
        url = urlsplit(request.full_url)
        sim = self.sim
        if url.hostname == 'api.rugcheck.xyz':
            method = 'rugcheck'
        elif url.hostname == 'mainnet.helius-rpc.com':
            method = json.loads(request.data)['method']
        elif url.hostname == 'api.jup.ag':
            method = 'jupiter'
        else:
            method = 'kraken'
        lat = self._lat(method)
        self.calls[(sim.actor, method)] += 1
        self.log.append((sim.actor, method, sim.time()))
        sim.sleep(lat / 2)
        try:
            if self._faulty(method):
                return Resp(b'{"error":"service unavailable"}', 503)
            if (self.shed_windows and sim.actor not in MAIN_ACTORS and url.hostname == 'mainnet.helius-rpc.com'
                    and any(a <= sim.time() < b for a, b in self.shed_windows)):
                return Resp(b'{"error":"rate limited"}', 429)
            if method == 'rugcheck':
                mint = request.full_url.split('/v1/tokens/', 1)[1].rsplit('/report', 1)[0]
                answer = self.reports.get(mint, (400, b'{"error":"invalid token mint"}', {}))
                if isinstance(answer, tuple):
                    resp = Resp(answer[1], answer[0])
                    resp.headers = answer[2]
                    return resp
                return Resp(answer)
            return self.world.opener(request, timeout)
        finally:
            sim.sleep(lat / 2)


CANARY_OFFSETS = (14.5, 29.0, 37.0)       # seconds after the frame's receive time: in the gap between a prefetch's as-of and its first call


def canary_wallet(index, k):
    return TP.wallet(5000 + index * 3 + k)


def sparse_tape(token, born, index, *, seed, end=420.0):
    """Quiet at first (the creation, a same-slot buyer, one swap), then THREE CANARY BUYS timed to fall between the as-of of the default
    prefetch jobs and their first call: +14.5 s after the frame (features: start +15 s, as-of +12 s), +29.0 s (wallet signals: start +30 s,
    as-of +27 s; it is the 5th of the six transactions it examines) and +37.0 s (flow and clusters: start +38 s, as-of +35 s). A collector
    whose window were not cut at its as-of would count one. Busy after a minute."""
    trades = [(born, TP.wallet(900 + index), 'buy', 300_000_000, False), (born + 0.4, TP.wallet(901 + index), 'buy', 200_000_000, False),
              (born + 1.5, TP.wallet(index * 100), 'buy', 150_000_000, False)]
    received = born + BLOCK_BEFORE_RECEIPT
    trades += [(received + off, canary_wallet(index, k), 'buy', 500_000_000, False) for k, off in enumerate(CANARY_OFFSETS)]
    trades += TP.organic(born + 70, born + end, every=5.0, seed=seed, sell_share=0.3, wallets=50, base=index * 100 + 50)
    return trades


def wallet_tape(token, born, index, *, seed, end=420.0, every=5.0, sell_share=0.3):
    """The pool's swap tape: a creation transaction at the graduation block, a same-slot buyer, then organic trading by many wallets."""
    creator = TP.wallet(900 + index)
    trades = [(born, creator, 'buy', 300_000_000, False), (born + 0.4, TP.wallet(901 + index), 'buy', 200_000_000, False)]
    trades += TP.organic(born + 1.5, born + end, every=every, seed=seed, sell_share=sell_share, wallets=50, base=index * 100)
    return trades


ROLE_KW = {
    'ok': dict(), 'mcap_high': dict(quote_sol=3000), 'liquidity_low': dict(quote_sol=20, base_raw=2 * 10 ** 13),
    'mint_authority': dict(hazard='mint_authority'), 'concentrated': dict(hazard='concentrated'),
    'mayhem': dict(virtual_sol=1), 'holder_reward': dict(virtual_sol=1),
}
# appended pool fields (after the 243-byte base layout of fakeworld.Token): [0] is_mayhem_mode, [27] is_holder_reward
APPENDED = 243
FLAG_OFFSET = {'mayhem': 0, 'holder_reward': 27}


def steps(*points):
    """Piecewise-constant price multiplier by seconds since the token's anchor: ((from_age, m), ...)."""
    def path(age):
        value = Decimal(1)
        for start, m in points:
            if age >= start:
                value = Decimal(str(m))
        return value
    return path


# what each launch does to its price after the migration (a hazard token is never quoted, so its anchor is set to its arrival by the Scene):
# winners double, rugs lose 90 %, the entered ones fall 30 % (a stop) after two minutes
ROLE_PATH = {'ok': steps((120, 0.7)), 'mcap_high': steps((150, 1.6)), 'liquidity_low': steps((150, 1.6)), 'mayhem': steps((150, 2.0)),
             'holder_reward': steps((150, 2.0)), 'concentrated': steps((150, 0.1)), 'mint_authority': steps((150, 0.1))}


def holder_rows(tag, i):
    """Ten 1 % holders with real SPL token accounts (so the clusters collector can resolve their owners)."""
    return [(base58(bytes([tag, k + 1]) * 16), TP.wallet(300 + i * 20 + k), SUPPLY // 100) for k in range(10)]


def make_token(role, i, **extra):
    tag = 20 + 2 * i
    if role != 'concentrated':                      # (its holder list is the world's own "one wallet holds 70 %" hazard)
        extra.setdefault('holder_rows', holder_rows(tag, i))
    token = Token(tag, coin_creator_tag=110 + i, **{**ROLE_KW[role], 'path': ROLE_PATH.get(role), **extra})
    if role in FLAG_OFFSET:
        raw = bytearray(token.pool_raw)
        raw[APPENDED + FLAG_OFFSET[role]] = 1
        token.pool_raw = bytes(raw)
    return token


def scenario(n, *, seed=7, weights=None, gap=(30.0, 70.0), first=5.0):
    """``[(role, arrival offset)]``: ``n`` launches with random gaps and a realistic mix (~58 % hazard rejects)."""
    rng = random.Random(seed)
    weights = weights or {'ok': 38, 'mcap_high': 6, 'liquidity_low': 3, 'mayhem': 26, 'concentrated': 16, 'mint_authority': 4, 'holder_reward': 7}
    roles = list(weights)
    out, t = [], first
    for _ in range(n):
        out.append((rng.choices(roles, [weights[r] for r in roles])[0], t))
        t += rng.uniform(*gap)
    return out


class Scene:
    """tokens + tapes + the world, one candidate per scenario entry. ``arrival`` = the frame's receive time (offset from T0)."""

    def __init__(self, root, clock, plan, *, latency=True, fault_p=0.0, rugcheck_reports=True, seed=3, tape_end=420.0, shed_windows=(), sparse=False):
        self.plan = plan
        self.tokens = [make_token(role, i) for i, (role, _) in enumerate(plan)]
        self.role = {t.mint: role for t, (role, _) in zip(self.tokens, plan)}
        self.arrival = {t.mint: T0 + at for t, (_, at) in zip(self.tokens, plan)}
        for token, (role, at) in zip(self.tokens, plan):
            if role not in ('ok', 'mcap_high', 'liquidity_low'):
                token.anchor = T0 + at                                         # never quoted: its price path runs from the migration
        self.world = World(root, self.tokens, clock)
        self.world.add_frames([T0 + at + 300 for _, at in plan])        # fakeworld: received_at = ready - 300; the screen's min age is lowered
        self.tape = TP.Tape(None)
        self.tape.slot = slot_at
        with sqlite3.connect(self.world.discovery_db) as c:
            c.execute('DROP TRIGGER IF EXISTS raw_events_update')                  # (a TEST database: the slot column is re-keyed per token)
            for seq, (token, (_role, at)) in enumerate(zip(self.tokens, plan), start=1):
                born = T0 + at - BLOCK_BEFORE_RECEIPT
                c.execute('UPDATE raw_events SET slot=? WHERE seq=?', (slot_at(born), seq))
                make = sparse_tape if sparse else wallet_tape
                self.tape.add(token, make(token, born, seq, seed=seed + seq, end=tape_end + BLOCK_BEFORE_RECEIPT))
        self.born = {t.mint: T0 + at - BLOCK_BEFORE_RECEIPT for t, (_, at) in zip(self.tokens, plan)}
        self.net = Net(clock, self.world, latency=latency, fault_p=fault_p, seed=seed, shed_windows=shed_windows)
        handlers = self.tape.handlers()

        def owner_accounts(params, t):
            return {'context': {'slot': 1}, 'value': [{'account': {'data': {'parsed': {'info': {'tokenAmount': {'amount': str(DEV_UNITS)}}}}}}]}

        def token_supply(params, t):
            return {'context': {'slot': 1}, 'value': {'amount': str(SUPPLY), 'decimals': 6, 'uiAmount': 1e9, 'uiAmountString': '1000000000'}}
        self.world.extra_rpc = {**handlers, 'getTokenAccountsByOwner': owner_accounts, 'getTokenSupply': token_supply}
        if rugcheck_reports:
            for i, t in enumerate(self.tokens):
                self.net.reports[t.mint] = rugcheck_report(t.mint, score=100 + i, name='tok%d' % i, symbol='T%d' % i,
                                                           image='https://ipfs.io/ipfs/Qm%044d' % i, holders=1000 + i)


# ======================================================================================================== the driver
class Actor:
    def __init__(self, name, fn, interval):
        self.name, self.fn, self.interval = name, fn, interval
        self.next_at = self.free_at = T0


class Sim:
    """A run of the real runner on a ``Scene``. ``prefetch`` True turns the W2b prefetcher on (the shipped defaults); everything else
    is the production shape (tests/lean/lean.r1-prod.json: min age 60 s, helius 20 rps, low lane 30 %) with the collectors that this
    world can serve: features, flow, wallet signals, RugCheck and the path recorder."""
    DT = 0.5

    def __init__(self, root, plan, *, prefetch, latency=True, fault_p=0.0, hazard_sample=0.0, seed=3, tape_end=420.0,
                 patch=None, prefetch_cfg=None, rugcheck=True, shed_windows=(), clusters=True, sparse=False):
        from pathlib import Path
        from lean import flow, honeypot_sim, paths, prefetch as PF, providers, sell_evidence
        from lean.__main__ import build_runner, load_config
        root = Path(root)
        self.root = root
        providers.configure_lanes()
        self.clock = SimClock(T0)
        self.scene = Scene(root, self.clock, plan, latency=latency, fault_p=fault_p, seed=seed, tape_end=tape_end, rugcheck_reports=rugcheck,
                           shed_windows=shed_windows, sparse=sparse)
        cfg = load_config(Path(__file__).resolve().parents[2] / 'tests' / 'lean' / 'lean.r1-prod.json')
        cfg['held_risk'] = {'enabled': False}
        cfg['bot_exit'] = None
        if not clusters:
            cfg['clusters'] = None
        cfg['sell_evidence'], cfg['honeypot_sim'] = sell_evidence.config(None), honeypot_sim.config(None)
        cfg['route_check'] = {'enabled': False}
        cfg['watchlist'] = {**cfg['watchlist'], 'enabled': False}
        cfg['paths'] = paths.config({**cfg['paths'], 'hazard_path_sample': hazard_sample})
        cfg['prefetch'] = PF.config({'enabled': True, **(prefetch_cfg or {})}) if prefetch else PF.config(None)
        for key, value in (patch or {}).items():
            cfg[key] = value
        self.cfg = cfg
        kw = {'opener': self.scene.net.opener, 'clock': self.clock.time, 'monotonic': self.clock.monotonic, 'sleep': self.clock.sleep,
              'rng': lambda: 0.5}
        self.runner = r = build_runner(cfg, state_dir=root / 'state', discovery_db=self.scene.world.discovery_db,
                                       keys={'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'},
                                       code_version='w2b-e2e', clock=self.clock.time, transport_kwargs=kw)
        r.l07r_start_paths()
        self.actors = [Actor('positions', r.position_pass, 10.0), Actor('candidates', r.candidate_pass, 5.0)]
        if r.paths is not None:
            self.actors.append(Actor('paths', r.paths.poll, 15.0))
        if r.flow is not None:
            self.actors.append(Actor('flow', r.flow.flow_pass, 5.0))
        if r.wallet_signals is not None:
            self.actors.append(Actor('wallet', r.wallet_signals.signals_pass, 5.0))
        if r.clusters is not None:
            self.actors.append(Actor('clusters_collector', r.clusters.clusters_pass, 5.0))
        if r.features is not None:
            self.actors.append(Actor('features', lambda: self._drain(r.features), 0.5))
        if r.risk_external is not None:
            self.actors.append(Actor('risk', lambda: self._drain(r.risk_external), 0.5))
        if r.prefetch is not None:
            self.actors.append(Actor('prefetch', r.prefetch.prefetch_pass, 1.0))

    @staticmethod
    def _drain(worker):
        while worker.process_one():
            pass

    def run(self, until):
        """Step the world until ``T0 + until`` seconds."""
        end = T0 + until
        clock = self.clock
        while clock.base < end:
            clock.base += self.DT
            for a in self.actors:
                if clock.base < a.next_at:
                    continue
                main = a.name in MAIN_ACTORS
                if not main and clock.base < a.free_at:
                    continue
                clock.enter(a.name)
                a.fn()
                if main:
                    a.next_at = clock.base + a.interval
                else:
                    a.free_at = clock.base + clock.off
                    a.next_at = a.free_at + a.interval
            clock.enter('candidates')
            assert self.runner.halted is None, self.runner.halted
        return self

    def close(self):
        self.runner.store.close()
        if self.runner.paths is not None:
            self.runner.paths.db.close()
