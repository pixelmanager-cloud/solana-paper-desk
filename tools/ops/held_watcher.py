"""Event-driven held-position watcher: nudges the normal held pass, never trades.

Memecoins can fall 30% in seconds while the held timer fires every couple of minutes. This process
watches the pool vault balances of every open paper position (Helius websocket ``accountSubscribe``,
with an HTTP ``getMultipleAccounts`` polling fallback), computes the implied mark, and when that mark
approaches an engine exit threshold it asks systemd to run the NORMAL held pass immediately.

The engine stays authoritative. This tool:

* never writes the ledger (opened ``mode=ro``) and never fills or decides anything;
* only reads thresholds from the frozen config and the persisted position state;
* uses its OWN request allowance and pacing in its OWN store (``watcher.sqlite``). It does not touch the
  shared provider pacing store, because a process killed mid-request would leave a pacing ticket pending
  and block the whole desk (T09 F6);
* is paper/research only: no signing, no broadcasting, no keys beyond the read-only Helius key.

    python -m tools.ops.held_watcher run     --ledger L --config C --state-dir D [options]
    python -m tools.ops.held_watcher latency --ledger L --state-dir D
"""
import argparse
import asyncio
import base64
import binascii
import contextlib
import json
import os
import random
import sqlite3
import stat
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from desk.model import load_config  # noqa: E402  (import only; the desk package is not modified)
from desk.programs import address  # noqa: E402
from desk.security import TOKEN_2022, TOKEN_PROGRAM  # noqa: E402

D = Decimal
ZERO = D(0)
ONE = D(1)
WSOL = 'So11111111111111111111111111111111111111112'
LADDER = (D('1.4'), D('2'), D('3'))      # engine.manage_position take-profit rungs, by position stage (parity-tested)
TOUCHED = D('1.15')                      # engine: touched_15
TRAILING_STAGE = 3
HELIUS_HTTP = 'https://mainnet.helius-rpc.com/'
HELIUS_WS = 'wss://mainnet.helius-rpc.com/'
POSITION_FIELDS = ('qty', 'cost_left', 'pool', 'stop_ratio', 'peak_ratio', 'stage', 'opened_at', 'touched_15')
REASON_ORDER = ('LIQUIDATE', 'STOP', 'TRAILING_STOP', 'MAX_HOLD', 'TIME_STOP', 'TAKE_PROFIT')
MAX_ACCOUNTS_PER_CALL = 100
CONNECTED = '__connected__'            # feed signal: every subscription acknowledged


class WatcherError(Exception):
    pass


class RpcError(WatcherError):
    """Message is a bounded error CLASS, never raw provider text (it can echo URLs with keys)."""


class AllowanceExhausted(WatcherError):
    pass


# ------------------------------------------------------------------ own store

class WatcherStore:
    """Append-only research store of the watcher itself; never one of the trading stores."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS request_results(request_id INTEGER PRIMARY KEY REFERENCES requests(id),
        ok INTEGER NOT NULL, detail TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS vaults(pool TEXT PRIMARY KEY, mint TEXT NOT NULL, base_vault TEXT NOT NULL,
        quote_vault TEXT NOT NULL, resolved_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS triggers(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mint TEXT NOT NULL,
        reason TEXT NOT NULL, reasons TEXT NOT NULL, ratio TEXT, slot INTEGER, observed_at REAL, source TEXT NOT NULL,
        method TEXT NOT NULL, ok INTEGER NOT NULL, detail TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS health(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, code TEXT NOT NULL,
        detail TEXT NOT NULL);
    """
    APPEND_ONLY = ('requests', 'request_results', 'vaults', 'triggers', 'health')

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise WatcherError('watcher store must not be a symlink')
        self.db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self.db.executescript(self.SCHEMA)
        for table in self.APPEND_ONLY:
            for op in ('UPDATE', 'DELETE'):
                self.db.execute("CREATE TRIGGER IF NOT EXISTS %s_no_%s BEFORE %s ON %s "
                                "BEGIN SELECT RAISE(ABORT,'watcher store is append-only'); END"
                                % (table, op.lower(), op, table))
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)

    def close(self):
        self.db.close()

    def used(self, now, window=3600.0):
        return self.db.execute('SELECT count(*) FROM requests WHERE ts > ?', (now - window,)).fetchone()[0]

    def last_request_ts(self):
        return self.db.execute('SELECT max(ts) FROM requests').fetchone()[0]

    def charge(self, kind, now):
        return self.db.execute('INSERT INTO requests(ts, kind) VALUES(?,?)', (now, kind)).lastrowid

    def finish(self, request_id, ok, detail=''):
        self.db.execute('INSERT INTO request_results(request_id, ok, detail) VALUES(?,?,?)',
                        (request_id, 1 if ok else 0, detail[:200]))

    def vault(self, pool):
        row = self.db.execute('SELECT mint, base_vault, quote_vault FROM vaults WHERE pool=?', (pool,)).fetchone()
        return row

    def save_vault(self, pool, mint, base_vault, quote_vault, now):
        self.db.execute('INSERT OR IGNORE INTO vaults VALUES(?,?,?,?,?)', (pool, mint, base_vault, quote_vault, now))

    def record_trigger(self, now, mint, reasons, ratio, slot, observed_at, source, method, ok, detail):
        self.db.execute('INSERT INTO triggers(ts,mint,reason,reasons,ratio,slot,observed_at,source,method,ok,detail) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (now, mint, reasons[0], json.dumps(reasons), None if ratio is None else str(ratio), slot,
                         observed_at, source, method, 1 if ok else 0, detail[:200]))

    def triggers_since(self, since, mint=None):
        if mint is None:
            return self.db.execute('SELECT count(*) FROM triggers WHERE ts > ?', (since,)).fetchone()[0]
        return self.db.execute('SELECT count(*) FROM triggers WHERE ts > ? AND mint=?', (since, mint)).fetchone()[0]

    def last_triggers(self):
        return {(mint, reason): ts for mint, reason, ts in self.db.execute(
            'SELECT mint, reason, max(ts) FROM triggers GROUP BY mint, reason')}

    def health(self, now, code, detail=''):
        self.db.execute('INSERT INTO health(ts, code, detail) VALUES(?,?,?)', (now, code, str(detail)[:300]))


# ------------------------------------------------------------------ pure logic

def implied_ratio(position, base_raw, quote_raw, cfg, pool_fee_bps):
    """mark / cost_left from vault balances, mirroring ``strategy.swap_quote`` plus the engine's fixed fee.
    Constant product on the PumpSwap vaults; an approximation of the executable quote the held pass will fetch."""
    decimals = position['quote_execution']['mint_decimals']
    qty, cost = D(position['qty']), D(position['cost_left'])
    if cost <= ZERO or qty <= ZERO:
        raise WatcherError('POSITION_NOT_PRICEABLE')
    token_reserve = D(base_raw) / (D(10) ** decimals)
    sol_reserve = D(quote_raw) / D(10 ** 9)
    if sol_reserve <= ZERO or token_reserve < ZERO:
        return ZERO                                   # drained pool: worth nothing, report the worst case
    effective = qty * (ONE - D(pool_fee_bps) / 10000)
    out = sol_reserve * effective / (token_reserve + effective)
    out *= ONE - D(cfg['adverse_slippage_bps']) / 10000
    return max(ZERO, out - D(cfg['fixed_fee_sol'])) / cost


def exit_reasons(position, ratio, now, cfg, mode, observed_peak=ZERO, margin=ZERO):
    """Engine exit conditions that are met (or within ``margin`` of being met) now, in engine priority order.
    ``ratio`` may be None (no fresh reserves): only time- and mode-based reasons are then possible."""
    found = set()
    if mode == 'LIQUIDATING':
        found.add('LIQUIDATE')
    age = now - position['opened_at']
    if age >= cfg['max_hold_seconds']:
        found.add('MAX_HOLD')
    if not position['touched_15'] and age >= cfg['time_stop_seconds']:
        found.add('TIME_STOP')
    if ratio is not None:
        if ratio <= D(position['stop_ratio']) + margin:
            found.add('STOP')
        peak = max(D(position['peak_ratio']), observed_peak, ratio)
        if position['stage'] >= TRAILING_STAGE and ratio <= peak * (ONE - D(cfg['trailing_fraction'])) + margin:
            found.add('TRAILING_STOP')
        if position['stage'] < len(LADDER) and ratio >= LADDER[position['stage']] - margin:
            found.add('TAKE_PROFIT')
    return [r for r in REASON_ORDER if r in found]


def decode_token_account(account, expected_mint):
    """Raw amount of an SPL / Token-2022 token account; fails closed on anything unexpected."""
    if not isinstance(account, dict) or account.get('owner') not in (TOKEN_PROGRAM, TOKEN_2022):
        raise WatcherError('VAULT_NOT_TOKEN_ACCOUNT')
    data = account.get('data')
    if not isinstance(data, list) or len(data) != 2 or data[1] != 'base64' or not isinstance(data[0], str):
        raise WatcherError('VAULT_DATA_ENCODING')
    try:
        raw = base64.b64decode(data[0], validate=True)
    except (ValueError, binascii.Error):
        raise WatcherError('VAULT_DATA_ENCODING') from None
    if len(raw) < 165 or raw[108] != 1:
        raise WatcherError('VAULT_NOT_INITIALIZED')
    from desk.security import base58
    if base58(raw[:32]) != expected_mint:
        raise WatcherError('VAULT_MINT_MISMATCH')
    return int.from_bytes(raw[64:72], 'little')


def resolve_vaults(position, mint, fetch_account):
    """(base_vault, quote_vault) of the position's PumpSwap pool, verified against the pool PDA."""
    from desk.pools import parse_pool
    from desk.providers import PUMPSWAP
    pool = position['pool']
    address(pool)
    account = fetch_account(pool)
    fields = parse_pool(account)
    if fields['base_mint'] != mint or fields['quote_mint'] != WSOL:
        raise WatcherError('POOL_MINT_MISMATCH')
    try:
        from solders.pubkey import Pubkey
    except ImportError:
        raise WatcherError('SOLDERS_REQUIRED_FOR_POOL_PDA_CHECK') from None
    program = Pubkey.from_string(PUMPSWAP)
    expected, bump = Pubkey.find_program_address([b'pool', fields['index'].to_bytes(2, 'little'),
                                                  bytes(Pubkey.from_string(fields['creator'])),
                                                  bytes(Pubkey.from_string(fields['base_mint'])),
                                                  bytes(Pubkey.from_string(fields['quote_mint']))], program)
    if str(expected) != pool or bump != fields['pool_bump']:
        raise WatcherError('POOL_PDA_MISMATCH')
    return fields['pool_base_token_account'], fields['pool_quote_token_account']


# ------------------------------------------------------------------ ledger (read-only)

def open_ledger_ro(path, connect=sqlite3.connect):
    """Read-only connection. A WAL database on a read-only mount cannot create its -shm when no writer is active;
    only then (WAL header, no -wal sidecar, so the main file is complete) fall back to ``immutable=1`` (T02F rule)."""
    uri = path.resolve().as_uri()
    try:
        db = connect(uri + '?mode=ro', uri=True, timeout=5)
        db.execute('SELECT 1 FROM sqlite_master LIMIT 1')
        return db
    except sqlite3.OperationalError:
        with path.open('rb') as stream:
            header = stream.read(100)
        sidecar = path.with_name(path.name + '-wal')
        if len(header) < 100 or header[18] != 2 or header[19] != 2 or os.path.lexists(sidecar):
            raise
        return connect(uri + '?mode=ro&immutable=1', uri=True, timeout=5)


def read_ledger_state(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise WatcherError('LEDGER_MISSING_OR_ALIASED')
    with closing(open_ledger_ro(path)) as db:
        row = db.execute('SELECT payload FROM state WHERE id=1').fetchone()
    if row is None:
        return {'positions': {}, 'mode': 'RUNNING'}
    state = json.loads(row[0])
    if not isinstance(state, dict) or not isinstance(state.get('positions'), dict) or not isinstance(state.get('mode'), str):
        raise WatcherError('LEDGER_STATE_INVALID')
    return state


# ------------------------------------------------------------------ provider access

class HeliusRpc:
    """Minimal read-only JSON-RPC over HTTPS. Errors are classes, never provider text or URLs."""

    def __init__(self, api_key, opener=None, timeout=5.0):
        self._url = HELIUS_HTTP + '?api-key=' + api_key
        self._opener = opener or urllib.request.build_opener(_NoRedirect()).open
        self._timeout = timeout

    def __call__(self, method, params):
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
        request = urllib.request.Request(self._url, data=body, headers={'Content-Type': 'application/json'})
        try:
            with self._opener(request, timeout=self._timeout) as response:
                if response.status != 200:
                    raise RpcError('HTTP_%s' % response.status)
                value = json.load(response)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise RpcError('HTTP_%s' % exc.code) from None
        except TimeoutError:
            raise RpcError('TIMEOUT') from None
        except (urllib.error.URLError, OSError):
            raise RpcError('NETWORK') from None
        except ValueError:
            raise RpcError('BAD_RESPONSE') from None
        if not isinstance(value, dict) or 'error' in value or 'result' not in value:
            raise RpcError('RPC_ERROR' if isinstance(value, dict) and 'error' in value else 'BAD_RESPONSE')
        return value['result']


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class StreamLost(WatcherError):
    pass


class WebsocketFeed:
    """``accountSubscribe`` for a set of accounts. ``updates`` yields (account, value, slot) and raises StreamLost."""

    def __init__(self, url, connect=None):
        self.url, self._connect = url, connect

    async def updates(self, accounts):
        connect = self._connect
        if connect is None:
            try:
                from websockets.asyncio.client import connect
            except ImportError:
                raise StreamLost('WEBSOCKETS_NOT_INSTALLED') from None
        try:
            async with connect(self.url, ping_interval=20, ping_timeout=20, open_timeout=10,
                               max_size=1 << 20, max_queue=64) as ws:
                pending, subscriptions = {}, {}
                for number, account in enumerate(accounts, 1):
                    pending[number] = account
                    await ws.send(json.dumps({'jsonrpc': '2.0', 'id': number, 'method': 'accountSubscribe',
                                              'params': [account, {'encoding': 'base64', 'commitment': 'confirmed'}]}))
                if not accounts:
                    raise StreamLost('NO_ACCOUNTS')
                async for raw in ws:
                    message = json.loads(raw)
                    if message.get('id') in pending:
                        if 'error' in message or type(message.get('result')) is not int:
                            raise StreamLost('SUBSCRIPTION_REJECTED')
                        subscriptions[message['result']] = pending.pop(message['id'])
                        if not pending:
                            yield CONNECTED, None, 0
                        continue
                    if message.get('method') != 'accountNotification':
                        continue
                    params = message['params']
                    account = subscriptions.get(params.get('subscription'))
                    slot = params['result']['context']['slot']
                    if account is None or type(slot) is not int:
                        continue
                    yield account, params['result']['value'], slot
        except StreamLost:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - every transport failure is a lost stream, class only
            raise StreamLost(type(exc).__name__) from None
        raise StreamLost('STREAM_CLOSED')


class Trigger:
    """How the normal held pass is started. The watcher never runs the pass itself."""

    def __init__(self, method, unit, request_path=None, runner=None, clock=time.time):
        if method not in ('path', 'systemctl', 'none'):
            raise WatcherError('unknown trigger method')
        self.method, self.unit, self.request_path, self.clock = method, unit, request_path, clock
        self.runner = runner or self._subprocess
        self._seq = 0

    @staticmethod
    def _subprocess(argv):
        return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)

    def fire(self, mint, reasons, ratio):
        if self.method == 'none':
            return True, 'DRY_RUN'
        if self.method == 'systemctl':
            try:
                result = self.runner(['systemctl', 'start', '--no-block', self.unit])
            except Exception as exc:  # noqa: BLE001
                return False, 'RUNNER_' + type(exc).__name__
            return result.returncode == 0, 'systemctl rc=%s' % result.returncode
        # path: atomic replace of a request file that a systemd .path unit watches; needs no privileges.
        self._seq += 1
        payload = json.dumps({'ts': self.clock(), 'seq': self._seq, 'pid': os.getpid(), 'mint': mint,
                              'reasons': reasons, 'ratio': None if ratio is None else str(ratio)}, sort_keys=True)
        path = Path(self.request_path)
        tmp = path.with_name('.' + path.name + '.tmp')
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            return False, 'PATH_' + type(exc).__name__
        return True, 'request file written'


# ------------------------------------------------------------------ the watcher

class Watcher:
    def __init__(self, *, ledger, cfg, store, trigger, rpc, feed=None, clock=time.time, sleep=asyncio.sleep,
                 rng=random.random, health_path=None, pool_fee_bps='25', allowance_per_hour=3600,
                 min_request_interval=1.0, poll_seconds=2.0, reconcile_seconds=30.0, refresh_seconds=5.0,
                 debounce_seconds=30.0, max_triggers_per_position_hour=12, max_triggers_hour=60, margin=D('0.02'),
                 max_reserve_age=20.0, vault_retry_seconds=60.0, tick_seconds=0.25, backoff_cap=60.0,
                 min_trigger_gap=5.0):
        self.ledger, self.cfg, self.store, self.trigger, self.rpc, self.feed = ledger, cfg, store, trigger, rpc, feed
        self.clock, self.sleep, self.rng, self.health_path = clock, sleep, rng, health_path
        self.pool_fee_bps, self.allowance, self.min_interval = pool_fee_bps, allowance_per_hour, min_request_interval
        self.poll_seconds, self.reconcile_seconds, self.refresh_seconds = poll_seconds, reconcile_seconds, refresh_seconds
        self.debounce, self.max_pos_hour, self.max_hour = debounce_seconds, max_triggers_per_position_hour, max_triggers_hour
        self.margin, self.max_reserve_age, self.vault_retry = D(margin), max_reserve_age, vault_retry_seconds
        self.tick_seconds, self.backoff_cap, self.min_gap = tick_seconds, backoff_cap, min_trigger_gap
        self.last_fire = None
        self.positions, self.mode = {}, 'RUNNING'
        self.vaults = {}                      # mint -> (pool, base_vault, quote_vault)
        self.accounts = {}                    # account -> (mint, side)
        self.reserves = {}                    # mint -> {'base': (raw, slot, at), 'quote': (...)}
        self.observed_peak = {}
        self.vault_failed_at = {}
        self.last_trigger = store.last_triggers()
        self.last_refresh = self.last_poll = None
        self.stream_up = False
        self.stream_task = None
        self.stream_accounts = frozenset()
        self.connect_failures = 0
        self.status = 'IDLE'
        self.warnings = {}
        self.last_error = None
        self.suppressed = 0
        self.stats = {'updates': 0, 'polls': 0, 'triggers': 0}
        self._health_written = None
        self._health_at = 0.0
        self._seeded = False

    # --- health
    def warn(self, code, detail=''):
        if self.warnings.get(code) != detail:
            self.warnings[code] = detail
            self.store.health(self.clock(), code, detail)

    def clear(self, code):
        self.warnings.pop(code, None)

    def write_health(self, force=False):
        now = self.clock()
        if self.health_path is None:
            return
        snapshot = {'status': self.status, 'positions_watched': len(self.positions), 'stream_connected': self.stream_up,
                    'warnings': dict(self.warnings), 'allowance_used': self.store.used(now), 'allowance_limit': self.allowance,
                    'updates': self.stats['updates'], 'polls': self.stats['polls'], 'triggers': self.stats['triggers'],
                    'suppressed_triggers': self.suppressed}
        key = json.dumps({k: v for k, v in snapshot.items() if k not in ('updates', 'polls', 'allowance_used')}, sort_keys=True)
        if not force and key == self._health_written and now - self._health_at < 30:
            return
        self._health_written, self._health_at = key, now
        snapshot['ts'] = now
        path = Path(self.health_path)
        tmp = path.with_name('.' + path.name + '.tmp')
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(json.dumps(snapshot, sort_keys=True))
            os.replace(tmp, path)
        except OSError:
            pass

    # --- own requests: charged before the call, failures included; spaced; bounded per rolling hour
    async def request(self, kind, method, params):
        now = self.clock()
        if self.store.used(now) >= self.allowance:
            self.warn('ALLOWANCE_EXHAUSTED', 'limit %s/hour' % self.allowance)
            raise AllowanceExhausted(kind)
        self.clear('ALLOWANCE_EXHAUSTED')
        last = self.store.last_request_ts()
        if last is not None and now - last < self.min_interval:
            await self.sleep(self.min_interval - (now - last))
            now = self.clock()
        request_id = self.store.charge(kind, now)
        try:
            result = await asyncio.get_running_loop().run_in_executor(None, self.rpc, method, params)
        except RpcError as exc:
            self.store.finish(request_id, False, str(exc))
            self.last_error = str(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - never leak provider text
            self.store.finish(request_id, False, type(exc).__name__)
            self.last_error = type(exc).__name__
            raise RpcError(type(exc).__name__) from None
        self.store.finish(request_id, True)
        return result

    async def fetch_account(self, address_):
        result = await self.request('getAccountInfo', 'getAccountInfo', [address_, {'encoding': 'base64', 'commitment': 'confirmed'}])
        if not isinstance(result, dict) or result.get('value') is None:
            raise WatcherError('ACCOUNT_NOT_FOUND')
        return result['value']

    # --- positions
    async def refresh_positions(self):
        now = self.clock()
        self.last_refresh = now
        try:
            state = read_ledger_state(self.ledger)
        except (OSError, sqlite3.Error, ValueError, WatcherError) as exc:
            self.warn('LEDGER_UNREADABLE', type(exc).__name__)
            return
        self.clear('LEDGER_UNREADABLE')
        self.mode = state['mode']
        positions = {}
        for mint, position in state['positions'].items():
            if isinstance(position, dict) and all(k in position for k in POSITION_FIELDS):
                positions[mint] = position
            else:
                self.warn('POSITION_FIELDS_MISSING', mint)
        for gone in set(self.positions) - set(positions):
            self.reserves.pop(gone, None)
            self.observed_peak.pop(gone, None)
        self.positions = positions
        for mint, position in positions.items():
            if mint not in self.vaults and now - self.vault_failed_at.get(mint, -1e18) >= self.vault_retry:
                await self.ensure_vaults(mint, position)
        self.accounts = {}
        for mint, (pool, base_vault, quote_vault) in self.vaults.items():
            if mint in positions:
                self.accounts[base_vault] = (mint, 'base')
                self.accounts[quote_vault] = (mint, 'quote')

    async def ensure_vaults(self, mint, position):
        cached = self.store.vault(position['pool'])
        if cached is not None:
            if cached[0] != mint:
                self.warn('VAULT_CACHE_MINT_MISMATCH', mint)
                self.vault_failed_at[mint] = self.clock()
                return
            self.vaults[mint] = (position['pool'], cached[1], cached[2])
            return
        try:
            account = await self.fetch_account(position['pool'])
            base_vault, quote_vault = resolve_vaults(position, mint, lambda _pool: account)
        except (WatcherError, ValueError, KeyError, TypeError) as exc:
            self.vault_failed_at[mint] = self.clock()
            self.warn('VAULT_RESOLUTION_FAILED', '%s: %s' % (mint, type(exc).__name__ + ':' + str(exc)[:60]))
            return
        self.clear('VAULT_RESOLUTION_FAILED')
        self.store.save_vault(position['pool'], mint, base_vault, quote_vault, self.clock())
        self.vaults[mint] = (position['pool'], base_vault, quote_vault)

    # --- reserves
    def apply_account(self, account_address, value, slot, source):
        if account_address not in self.accounts:
            return False
        mint, side = self.accounts[account_address]
        try:
            raw = decode_token_account(value, mint if side == 'base' else WSOL)
        except WatcherError as exc:
            self.warn('VAULT_DECODE_FAILED', '%s: %s' % (account_address[:8], exc))
            return False
        self.clear('VAULT_DECODE_FAILED')
        entry = self.reserves.setdefault(mint, {})
        previous = entry.get(side)
        if previous is not None and slot < previous[1]:
            return False                      # out-of-order notification
        entry[side] = (raw, slot, self.clock(), source)
        self.stats['updates'] += 1
        return True

    def handle_update(self, account_address, value, slot, source='stream'):
        if self.apply_account(account_address, value, slot, source):
            mint = self.accounts[account_address][0]
            self.evaluate(mint, self.clock(), source)

    def reserves_fresh(self, mint, now):
        entry = self.reserves.get(mint)
        if not entry or 'base' not in entry or 'quote' not in entry:
            return False
        oldest = min(entry['base'][2], entry['quote'][2])
        if self.stream_up:
            # Quiet pools send no notifications: while the stream is up the snapshot stays valid as long as the
            # periodic reconcile poll keeps refreshing it (or, with reconcile disabled, as long as the stream is up).
            return True if not self.reconcile_seconds else now - oldest <= self.max_reserve_age + self.reconcile_seconds
        return now - oldest <= self.max_reserve_age

    async def poll_once(self, source='poll'):
        accounts = sorted(self.accounts)
        if not accounts:
            return
        self.last_poll = self.clock()
        self.stats['polls'] += 1
        for start in range(0, len(accounts), MAX_ACCOUNTS_PER_CALL):
            chunk = accounts[start:start + MAX_ACCOUNTS_PER_CALL]
            try:
                result = await self.request('getMultipleAccounts', 'getMultipleAccounts',
                                            [chunk, {'encoding': 'base64', 'commitment': 'confirmed'}])
                values, slot = result['value'], result['context']['slot']
                if not isinstance(values, list) or len(values) != len(chunk) or type(slot) is not int:
                    raise WatcherError('POLL_SHAPE')
            except (RpcError, AllowanceExhausted, WatcherError, KeyError, TypeError) as exc:
                self.warn('POLL_FAILED', type(exc).__name__ + ':' + str(exc)[:40])
                return
            self.clear('POLL_FAILED')
            touched = set()
            for account_address, value in zip(chunk, values):
                if value is None:
                    self.warn('VAULT_MISSING', account_address[:8])
                    continue
                if self.apply_account(account_address, value, slot, source):
                    touched.add(self.accounts[account_address][0])
            for mint in touched:
                self.evaluate(mint, self.clock(), source)

    # --- evaluation and triggering
    def evaluate(self, mint, now, source='tick'):
        position = self.positions.get(mint)
        if position is None:
            return
        ratio, slot, observed_at = None, None, None
        if self.reserves_fresh(mint, now):
            entry = self.reserves[mint]
            try:
                ratio = implied_ratio(position, entry['base'][0], entry['quote'][0], self.cfg, self.pool_fee_bps)
            except (WatcherError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
                self.warn('RATIO_FAILED', '%s: %s' % (mint[:8], type(exc).__name__))
            else:
                self.clear('RATIO_FAILED')
                self.observed_peak[mint] = max(self.observed_peak.get(mint, ZERO), ratio)
                slot = max(entry['base'][1], entry['quote'][1])
                observed_at = max(entry['base'][2], entry['quote'][2])
        elif mint in self.vaults:
            self.warn('RESERVES_STALE', mint[:8])
        reasons = exit_reasons(position, ratio, now, self.cfg, self.mode, self.observed_peak.get(mint, ZERO), self.margin)
        if reasons:
            self.maybe_trigger(mint, reasons, ratio, slot, observed_at, now, source)
        return reasons

    def maybe_trigger(self, mint, reasons, ratio, slot, observed_at, now, source):
        key = (mint, reasons[0])
        last = self.last_trigger.get(key)
        if last is not None and now - last < self.debounce:
            self.suppressed += 1
            return False
        if self.last_fire is not None and now - self.last_fire < self.min_gap:
            self.suppressed += 1              # one held pass handles every position: a second request is redundant
            return False                      # (not remembered, so a still-crossing position fires after the gap)
        if self.store.triggers_since(now - 3600, mint) >= self.max_pos_hour \
                or self.store.triggers_since(now - 3600) >= self.max_hour:
            self.suppressed += 1
            self.warn('TRIGGER_RATE_LIMITED', mint[:8])
            return False
        ok, detail = self.trigger.fire(mint, reasons, ratio)
        self.last_fire = now
        self.store.record_trigger(now, mint, reasons, ratio, slot, observed_at, source, self.trigger.method, ok, detail)
        self.last_trigger[key] = now if ok else now - self.debounce + min(5.0, self.debounce)
        self.stats['triggers'] += 1
        if not ok:
            self.warn('TRIGGER_FAILED', detail)
        else:
            self.clear('TRIGGER_FAILED')
        return ok

    # --- stream supervision
    def watched_accounts(self):
        return frozenset(self.accounts)

    async def stream_loop(self, accounts):
        attempt = 0
        while True:
            try:
                async for account_address, value, slot in self.feed.updates(sorted(accounts)):
                    if account_address == CONNECTED:
                        attempt = 0
                        self.stream_up = True
                        self._seeded = False          # seed/reconcile now: subscriptions send no initial value
                        self.clear('STREAM_DOWN')
                        continue
                    self.handle_update(account_address, value, slot, 'stream')
            except asyncio.CancelledError:
                raise
            except StreamLost as exc:
                self.last_error = str(exc)
            except Exception as exc:  # noqa: BLE001
                self.last_error = type(exc).__name__
            self.stream_up = False
            self.connect_failures += 1
            self.warn('STREAM_DOWN', self.last_error or '')
            attempt += 1
            delay = min(self.backoff_cap, 2 ** min(attempt, 6)) * (0.5 + self.rng() / 2)
            await self.sleep(delay)

    def sync_stream(self):
        wanted = self.watched_accounts() if self.feed is not None else frozenset()
        if wanted == self.stream_accounts and (self.stream_task is None or not self.stream_task.done()):
            return
        if self.stream_task is not None:
            self.stream_task.cancel()
            self.stream_task = None
        self.stream_up = False
        self.stream_accounts = wanted
        if wanted:
            self.stream_task = asyncio.get_running_loop().create_task(self.stream_loop(wanted))

    async def tick(self):
        now = self.clock()
        if self.last_refresh is None or now - self.last_refresh >= self.refresh_seconds:
            before = self.watched_accounts()
            await self.refresh_positions()
            if self.watched_accounts() != before:
                self.last_poll, self._seeded = None, False
        self.sync_stream()
        if not self.positions:
            self.status = 'IDLE'
        else:
            streaming = self.feed is not None and self.stream_up
            self.status = 'STREAM' if streaming else 'POLLING'
            if streaming:
                interval = self.reconcile_seconds
                due = not self._seeded or (interval > 0 and (self.last_poll is None or now - self.last_poll >= interval))
            else:
                due = self.last_poll is None or now - self.last_poll >= self.poll_seconds
            if due and self.accounts:
                await self.poll_once('reconcile' if streaming else 'poll')
                self._seeded = self._seeded or streaming
            if self.warnings.get('POLL_FAILED') or self.warnings.get('ALLOWANCE_EXHAUSTED'):
                self.status = 'DEGRADED'
        for mint in list(self.positions):
            self.evaluate(mint, now, 'tick')
        self.write_health()

    async def run(self, stop):
        try:
            while not stop.is_set():
                await self.tick()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.tick_seconds)
        finally:
            if self.stream_task is not None:
                self.stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self.stream_task
            self.write_health(force=True)


# ------------------------------------------------------------------ latency report

def latency_report(ledger, store_path):
    """Trigger -> next sell fill, from the watcher store and a read-only ledger. Slot wall-time is not recorded."""
    path = Path(ledger)
    if path.is_symlink() or not path.is_file():
        raise WatcherError('LEDGER_MISSING_OR_ALIASED')
    with closing(open_ledger_ro(path)) as db:
        fills = []
        for payload, ts in db.execute('SELECT o.payload, e.ts FROM outcomes o JOIN events e ON e.event_id=o.event_id ORDER BY o.seq'):
            outcome = json.loads(payload)
            if outcome.get('type') == 'fill' and outcome.get('side') == 'sell':
                fills.append((outcome['mint'], ts, outcome.get('reason')))
    with closing(sqlite3.connect(Path(store_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        rows = db.execute('SELECT id, ts, mint, reason, slot, observed_at, source, method, ok FROM triggers ORDER BY id').fetchall()
    result, latencies, internal = [], [], []
    for tid, ts, mint, reason, slot, observed_at, source, method, ok in rows:
        fill = next(((fts, freason) for fmint, fts, freason in fills if fmint == mint and fts >= int(ts) - 1), None)
        entry = {'trigger_id': tid, 'mint': mint, 'trigger_reason': reason, 'trigger_ts': ts, 'slot': slot,
                 'source': source, 'method': method, 'fired': bool(ok),
                 'watch_to_trigger_seconds': None if observed_at is None else round(ts - observed_at, 3),
                 'trigger_to_fill_seconds': None, 'fill_reason': None, 'slot_wall_time': 'NOT_RECORDED'}
        if observed_at is not None:
            internal.append(ts - observed_at)
        if fill is not None:
            entry['trigger_to_fill_seconds'] = round(fill[0] - ts, 3)
            entry['fill_reason'] = fill[1]
            latencies.append(fill[0] - ts)
        result.append(entry)
    summary = {'triggers': len(rows), 'with_fill': len(latencies)}
    if latencies:
        ordered = sorted(latencies)
        summary.update({'trigger_to_fill_median_seconds': statistics.median(ordered),
                        'trigger_to_fill_p90_seconds': ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))]})
    if internal:
        summary['watch_to_trigger_median_seconds'] = round(statistics.median(internal), 3)
    return {'status': 'RESEARCH_ONLY_PAPER', 'summary': summary, 'triggers': result,
            'note': 'Ledger fill time is the quote time of the held pass event (whole seconds). Slot wall time is not recorded.'}


# ------------------------------------------------------------------ cli

def load_helius_key(directory):
    if not directory:
        raise WatcherError('systemd credential directory required (CREDENTIALS_DIRECTORY or --credentials-dir)')
    path = Path(directory) / 'provider-keys.json'
    with path.open('rb') as stream:
        if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) not in (0o400, 0o440, 0o600):
            raise WatcherError('Managed credential permissions required')
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise WatcherError('Credential size bound')
    value = json.loads(raw)
    key = value.get('HELIUS_API_KEY') if isinstance(value, dict) else None
    if type(key) is not str or not key.strip() or any(c in key for c in '&?/ \n\r'):
        raise WatcherError('HELIUS_API_KEY credential required')
    return key.strip()


def build_parser():
    p = argparse.ArgumentParser(prog='tools.ops.held_watcher', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)
    r = sub.add_parser('run')
    r.add_argument('--ledger', required=True)
    r.add_argument('--config', required=True)
    r.add_argument('--state-dir', required=True, help='private directory owned by the watcher (store, health, trigger file)')
    r.add_argument('--credentials-dir', default=os.environ.get('CREDENTIALS_DIRECTORY'))
    r.add_argument('--trigger', choices=('path', 'systemctl', 'none'), default='path',
                   help='path: write <state-dir>/trigger/request for a systemd .path unit (no privileges); '
                        'systemctl: `systemctl start --no-block` (needs privileges); none: log only')
    r.add_argument('--unit', default='desk-paper-held-cycle.service')
    r.add_argument('--pool-fee-bps', default='25')
    r.add_argument('--poll-seconds', type=float, default=2.0, help='HTTP fallback cadence (1-2 s)')
    r.add_argument('--reconcile-seconds', type=float, default=30.0, help='poll while streaming to catch missed notifications; 0 = seed once')
    r.add_argument('--refresh-seconds', type=float, default=5.0)
    r.add_argument('--allowance-per-hour', type=int, default=3600, help='own request allowance (never shared with entry/monitoring)')
    r.add_argument('--min-request-interval', type=float, default=1.0)
    r.add_argument('--debounce-seconds', type=float, default=30.0)
    r.add_argument('--min-trigger-gap-seconds', type=float, default=5.0,
                   help='minimum spacing between ANY two requests (one held pass handles all positions)')
    r.add_argument('--max-triggers-per-position-hour', type=int, default=12)
    r.add_argument('--max-triggers-hour', type=int, default=60)
    r.add_argument('--margin', default='0.02', help='trigger this far (ratio units) before a threshold')
    r.add_argument('--no-stream', action='store_true', help='polling only')
    r.add_argument('--once', action='store_true', help='one refresh/poll/evaluate cycle, then exit')
    r.add_argument('--dry-run', action='store_true', help='same as --trigger none')
    lat = sub.add_parser('latency')
    lat.add_argument('--ledger', required=True)
    lat.add_argument('--state-dir', required=True)
    return p


def validate_args(a):
    if not (0.5 <= a.poll_seconds <= 60) or not (0 <= a.reconcile_seconds <= 3600) or not (1 <= a.refresh_seconds <= 300):
        raise WatcherError('poll/reconcile/refresh seconds out of range')
    if not (1 <= a.allowance_per_hour <= 100_000) or not (0 <= a.min_request_interval <= 60):
        raise WatcherError('allowance/interval out of range')
    if not (0 <= a.min_trigger_gap_seconds <= 600):
        raise WatcherError('min trigger gap out of range')
    if not (0 <= a.debounce_seconds <= 3600) or a.max_triggers_per_position_hour < 1 or a.max_triggers_hour < 1:
        raise WatcherError('trigger limits out of range')
    if not (D(0) <= D(a.margin) < D('0.5')):
        raise WatcherError('margin out of range')
    D(a.pool_fee_bps)


async def run_command(a, *, rpc=None, feed=None, runner=None, clock=time.time, sleep=asyncio.sleep, stop=None):
    validate_args(a)
    state_dir = Path(a.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg = load_config(a.config)
    streaming = not (a.no_stream or a.once)
    key = load_helius_key(a.credentials_dir) if rpc is None or (feed is None and streaming) else None
    if rpc is None:
        rpc = HeliusRpc(key)
    if not streaming:
        feed = None
    elif feed is None:
        feed = WebsocketFeed(HELIUS_WS + '?api-key=' + key)
    method = 'none' if a.dry_run else a.trigger
    store = WatcherStore(state_dir / 'watcher.sqlite')
    watcher = Watcher(ledger=a.ledger, cfg=cfg, store=store, rpc=rpc, feed=feed, clock=clock, sleep=sleep,
                      trigger=Trigger(method, a.unit, state_dir / 'trigger' / 'request', runner, clock),
                      health_path=state_dir / 'health.json', pool_fee_bps=a.pool_fee_bps,
                      allowance_per_hour=a.allowance_per_hour, min_request_interval=a.min_request_interval,
                      poll_seconds=a.poll_seconds, reconcile_seconds=a.reconcile_seconds, refresh_seconds=a.refresh_seconds,
                      debounce_seconds=a.debounce_seconds, max_triggers_per_position_hour=a.max_triggers_per_position_hour,
                      max_triggers_hour=a.max_triggers_hour, margin=a.margin,
                      min_trigger_gap=a.min_trigger_gap_seconds)
    try:
        if a.once:
            await watcher.refresh_positions()
            await watcher.poll_once('poll')
            for mint in list(watcher.positions):
                watcher.evaluate(mint, clock(), 'once')
            watcher.write_health(force=True)
            return {'status': watcher.status, 'positions': sorted(watcher.positions), 'warnings': watcher.warnings,
                    'triggers': watcher.stats['triggers'], 'suppressed': watcher.suppressed}
        stop = stop or asyncio.Event()
        import signal
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        await watcher.run(stop)
        return {'status': 'STOPPED'}
    finally:
        store.close()


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        if a.command == 'latency':
            print(json.dumps(latency_report(a.ledger, Path(a.state_dir) / 'watcher.sqlite'), sort_keys=True, indent=2))
            return 0
        result = asyncio.run(run_command(a))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (WatcherError, ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(json.dumps({'status': 'BLOCKED', 'reason': type(exc).__name__ + ': ' + str(exc)[:160]}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
