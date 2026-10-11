"""L17: lean.notify (Telegram trade notifier). SYNTHETIC_TEST_ONLY: no network, no real token.

The store is a REAL lean store produced by the L09 end-to-end fake-world scenario (tests/lean/test_e2e_real.py: real
L01-L06 modules, only urllib faked). The notifier runs interleaved with the trader on the same state, as a separate
reader, against a fake Telegram Bot API + Helius DAS opener (FakeBot below).
"""
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from collections import Counter
from decimal import Decimal
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from lean import adapters as A, notify as N, providers
from lean.store import Store
from tests.lean import test_e2e_real as E2E
from tests.lean.fakeworld import FakeTime, Resp, T0

TOKEN = '123456789:AAFakeTokenForTestsOnly_abcdefghijklmn'
CHAT = '4242'
HELIUS_KEY = 'TEST-HELIUS-KEY-0000'
ALLOWED_TAGS = re.compile(r'^</?(b|code)>$|^<a href="https://(dexscreener\.com/solana|solscan\.io/token)/[1-9A-HJ-NP-Za-km-z]{32,44}">$|^</a>$')
HOSTILE_SYMBOL = '<b>LAD&DER</b><script>x</script>'
HOSTILE_BUY = '"><img src=x onerror=alert(1)>&amp;'


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    quiet = logging.NullHandler()                       # no lastResort stderr noise; assertLogs still captures
    logging.getLogger('lean').addHandler(quiet)
    unittest.addModuleCleanup(logging.getLogger('lean').removeHandler, quiet)


class FakeBot:
    """Telegram Bot API + Helius DAS behind the urllib opener. ``script`` scripts the next sendMessage answers."""

    def __init__(self, clock, names=None):
        self.clock, self.names = clock, dict(names or {})
        self.sent, self.script, self.updates, self.calls = [], [], [], []
        self.asset_calls = Counter()

    def __call__(self, request, timeout):
        url = urlsplit(request.full_url)
        body = json.loads(request.data) if request.data else {}
        if url.hostname == 'mainnet.helius-rpc.com':
            assert body['method'] == 'getAsset', body
            mint = body['params']['id']
            self.asset_calls[mint] += 1
            name = self.names.get(mint)
            if name is None:
                return Resp({'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32000, 'message': 'not found'}})
            return Resp({'jsonrpc': '2.0', 'id': 1, 'result': {'id': mint, 'content': {'metadata': name},
                                                              'token_info': {'symbol': name.get('symbol')}}})
        assert url.hostname == 'api.telegram.org', url.hostname
        assert url.path.startswith('/bot%s/' % TOKEN)
        method = url.path.rsplit('/', 1)[1]
        self.calls.append((method, self.clock.time()))
        if method == 'getUpdates':
            offset = body.get('offset')
            return Resp({'ok': True, 'result': [u for u in self.updates if offset is None or u['update_id'] >= offset]})
        assert method == 'sendMessage', method
        if self.script:
            action = self.script.pop(0)
            if isinstance(action, BaseException):
                raise action
            if action != 'ok':
                return action
        self.sent.append({'chat_id': str(body['chat_id']), 'text': body['text'], 'parse_mode': body.get('parse_mode'),
                          'at': self.clock.time()})
        return Resp({'ok': True, 'result': {'message_id': len(self.sent)}})

    def texts(self, chat=CHAT):
        return [m['text'] for m in self.sent if m['chat_id'] == chat]


def too_many(retry_after):
    return Resp({'ok': False, 'error_code': 429, 'description': 'Too Many Requests: retry after %d' % retry_after,
                 'parameters': {'retry_after': retry_after}}, 429)


def server_error():
    return Resp({'ok': False, 'error_code': 502, 'description': 'Bad Gateway'}, 502)


def make_notifier(db, state_dir, clock, bot, **cfg_overrides):
    cfg = N.load_config(None)
    cfg.update(cfg_overrides)
    helius = providers.Helius(HELIUS_KEY, providers.Transport('helius', lane='exit', opener=bot, clock=clock.time,
                                                              monotonic=clock.monotonic, sleep=clock.sleep, rng=lambda: 0.5))
    return N.Notifier(db=db, cfg=cfg, telegram=N.Telegram(TOKEN, opener=bot), chat_id=CHAT, state_dir=state_dir,
                      helius=helius, clock=clock.time, sleep=clock.sleep)


def view_of(path):
    return N.StoreView(N.open_store(path))


def parse_sol(text, label):
    match = re.search(re.escape(label) + r': ([+-]\d+\.\d+) SOL', text)
    return Decimal(match.group(1)) if match else None


# ---------------------------------------------------------------------------------------------------- the scenario
NOTIFIER_START = T0 + 600          # 17:10 KST: fills before this are history (never sent)
NOTIFIER_RESTART = T0 + 2900       # a fresh notifier process on the same state dir
QUIET = ['17:20', '17:35']         # T0+1200 .. T0+2100 local
TG_OUTAGE = (T0 + 2700, T0 + 3100)      # spans the notifier restart: the persisted outbox carries over
MARK_CHECK_AT = T0 + 1500


class Scenario:
    """The L09 e2e trader run (20 candidates, restart, outage) with the notifier polling after every tick."""

    def __init__(self):
        self.case = E2E.EndToEndRealModulesTest('test_twenty_candidates_end_to_end')
        self.case.setUp()
        self.clock = self.case.clock
        self.role = self.case.role
        self.mint = {role: mint for mint, role in self.role.items()}
        names = {m: {'name': 'Token %s' % role, 'symbol': role.upper()[:10]} for m, role in self.role.items()}
        names[self.mint['ladder']] = {'name': 'Ladder <i>coin</i>', 'symbol': HOSTILE_SYMBOL}
        names[self.mint['tp1_stop']] = {'name': 'x', 'symbol': HOSTILE_BUY}      # bought after the notifier started
        del names[self.mint['time']]                    # DAS has no record: the short CA is shown
        self.bot = FakeBot(self.clock, names)
        self.notify_state = self.case.root / 'notify-state'
        self.notify_state.mkdir()
        self.db = self.case.state / 'lean.sqlite'
        self.log = io.StringIO()

    def notifier(self):
        return make_notifier(self.db, self.notify_state, self.clock, self.bot, quiet_hours=QUIET, daily_summary_at='18:00')

    def run(self):
        handler = logging.StreamHandler(self.log)
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger('lean')
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            return self._run()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

    def _run(self):
        r, n, tick, restarted = self.case.runner(), None, T0, False
        self.first_cursor, self.mark_check = None, None
        while tick <= E2E.END:
            if self.clock.time() < tick:
                self.clock.t = tick
            if not restarted and tick >= E2E.RESTART_AT:
                r.store.close()
                r = self.case.runner()
                restarted = True
            r.position_pass()
            r.candidate_pass()
            assert r.halted is None, r.halted
            if tick == MARK_CHECK_AT:
                self.mark_check = self.check_marks(r, n)
            if TG_OUTAGE[0] <= tick < TG_OUTAGE[1]:
                self.bot.script = [server_error()] * 3
            elif tick == TG_OUTAGE[1]:
                self.bot.script = [too_many(30)]
            if tick == NOTIFIER_START:
                n = self.notifier()
            if tick == NOTIFIER_RESTART:
                n = self.notifier()                          # the process restarted: everything from the state file
            if n is not None:
                n.run_once()
                if tick == NOTIFIER_START:
                    self.first_cursor = n.state['fill_cursor']
            tick += 60
        self.final_notifier = n
        self.runner = r
        return self

    def check_marks(self, r, n):
        """The notifier's unrealized PnL at a tick with open positions, against marks recomputed from the world."""
        view = view_of(self.db)
        try:
            book = n.portfolio(view, self.clock.time())
        finally:
            view.close()
        positions = r.store.positions()
        expected = {}
        for mint, position in positions.items():
            token = self.case.world.tokens[mint]
            base, quote = token.reserves(self.clock.time())
            net = A.mark(position.qty_raw, base, quote, pool_fee_bps=25, pcfg=r.pcfg)
            expected[mint] = int(net * 10 ** 9) - position.cost_lamports
        return {p['mint']: p['unrealized'] for p in book['positions']}, expected, {m: r.marks[m][0] for m in r.marks}


class NotifierScenarioTest(unittest.TestCase):
    """One interleaved run, many assertions."""

    @classmethod
    def setUpClass(cls):
        cls.s = Scenario().run()
        cls.s.runner.store.close()
        cls.store = Store(cls.s.db)
        cls.view = view_of(cls.s.db)
        cls.fills = cls.view._fills()

    @classmethod
    def tearDownClass(cls):
        cls.view.close()
        cls.store.close()
        cls.s.case.doCleanups()

    def expected_bodies(self, after):
        n = make_notifier(self.s.db, tempfile.mkdtemp(dir=self.s.case.root), self.s.clock, FakeBot(self.s.clock))
        n.state['names'] = dict(self.s.final_notifier.state['names'])
        out = {}
        for row in self.fills:
            if row['id'] > after:
                out[row['id']] = N.render_fill(N.fill_event(self.view, row, sol_usd_max_age=3600), n.label(self.view, row['mint']))
        return out

    def test_every_new_fill_is_sent_exactly_once_and_history_never(self):
        self.assertIsNotNone(self.s.first_cursor)
        self.assertGreater(self.s.first_cursor, 0)                      # fills existed before the notifier started
        everything = '\n\x00\n'.join(self.s.bot.texts())
        for fill_id, body in self.expected_bodies(0).items():
            expected = 0 if fill_id <= self.s.first_cursor else 1
            self.assertEqual(everything.count(body), expected, 'fill %d sent %d times' % (fill_id, everything.count(body)))
        new = [f for f in self.fills if f['id'] > self.s.first_cursor]
        self.assertGreaterEqual(len(new), 10)
        self.assertEqual(self.s.final_notifier.state['fill_cursor'], self.fills[-1]['id'])
        self.assertEqual(self.s.final_notifier.state['outbox'], [])
        self.assertEqual(self.s.final_notifier.state['dropped'], 0)

    def test_every_message_starts_with_the_mode_tag_and_only_safe_tags(self):
        self.assertTrue(self.s.bot.sent)
        for message in self.s.bot.sent:
            self.assertTrue(message['text'].startswith('[PAPER] '), message['text'][:40])
            self.assertEqual(message['parse_mode'], 'HTML')
            for tag in re.findall(r'<[^>]*>', message['text']):
                self.assertRegex(tag, ALLOWED_TAGS)

    def test_hostile_token_name_is_escaped(self):
        ladder = [t for t in self.s.bot.texts() if self.s.mint['ladder'] in t]
        self.assertTrue(ladder)
        for text in ladder:
            self.assertIn('&lt;b&gt;LAD&amp;DER&lt;/b&gt;&lt;script&gt;x&lt;/script&gt;', text)
            self.assertNotIn('<script>', text)
            self.assertNotIn(HOSTILE_SYMBOL, text)
        tp1 = [t for t in self.s.bot.texts() if self.s.mint['tp1_stop'] in t]
        self.assertTrue(any('🟢 <b>매수 &quot;&gt;&lt;img src=x onerror=alert(1)&gt;&amp;amp;</b>' in t for t in tp1))
        self.assertTrue(any('매도 &quot;&gt;&lt;img' in t for t in tp1))
        self.assertFalse(any('<img' in t for t in self.s.bot.texts()))
        # the token DAS does not know is shown by its short CA
        time_mint = self.s.mint['time']
        self.assertTrue(any('%s…%s' % (time_mint[:4], time_mint[-4:]) in t for t in self.s.bot.texts()))

    def test_one_das_call_per_mint_across_restart(self):
        notified = {f['mint'] for f in self.fills if f['id'] > self.s.first_cursor}
        self.assertTrue(notified)
        for mint in notified:
            self.assertEqual(self.s.bot.asset_calls[mint], 1, self.s.role[mint])
        self.assertEqual(set(self.s.bot.asset_calls), notified)

    def test_buy_and_sell_numbers_match_the_store(self):
        closed = {c['fill_id']: c for c in self.store.closed_positions()}
        total = Decimal(0)
        for row in self.fills:
            e = N.fill_event(self.view, row, sol_usd_max_age=3600)
            fill = Store._fill([row[k] for k in N.FILL_COLUMNS[:13]] + [0, 0, 0])
            self.assertEqual(e['price_sol'], fill.price_sol_per_token)
            self.assertEqual(e['size_sol'], Decimal(row['sol_lamports']) / 10 ** 9)
            if row['side'] == 'buy':
                features = self.view.screen_features(row['mint'])
                sol_usd = Decimal(features['sol_usd'])
                self.assertEqual(e['price_usd'], fill.price_sol_per_token * sol_usd)
                self.assertEqual(e['market_cap_usd'], Decimal(features['supply_raw']) / 10 ** 6 * fill.price_sol_per_token * sol_usd)
                continue
            self.assertEqual(e['realized_sol'] * 10 ** 9, row['realized_lamports'])
            self.assertEqual(e['realized_pct'], Decimal(row['realized_lamports']) / Decimal(row['cost_sold_lamports']))
            total += e['realized_sol']
            if row['id'] in closed:
                self.assertTrue(e['full'])
                c = closed[row['id']]
                self.assertEqual(e['trade_pnl_sol'] * 10 ** 9, int(c['state']['trade_pnl_lamports']))
                lifecycle = [f for f in self.fills if f['mint'] == row['mint'] and c['open_fill_id'] <= f['id'] <= row['id']]
                buy = sum(f['sol_lamports'] + f['fee_lamports'] for f in lifecycle if f['side'] == 'buy')
                self.assertEqual(e['trade_pnl_pct'], Decimal(int(c['state']['trade_pnl_lamports'])) / buy)
                self.assertEqual(e['hold_seconds'], row['ts'] - lifecycle[0]['ts'])
            else:
                self.assertFalse(e['full'])
        self.assertEqual(total * 10 ** 9, self.store.realized())

    def test_sent_text_pnl_adds_up_to_store_realized(self):
        """Parse the SENT text of every SELL message after the first cursor (plain and digest): the shown PnL is the
        store's, to display precision."""
        texts = '\n\n'.join(t for t in self.s.bot.texts() if '일일 요약' not in t)
        shown = [Decimal(x) for x in re.findall(r'실현손익: ([+-]\d+\.\d+) SOL', texts)]
        sells = [f for f in self.fills if f['side'] == 'sell' and f['id'] > self.s.first_cursor]
        self.assertEqual(len(shown), len(sells))
        expected = sum(f['realized_lamports'] for f in sells)
        self.assertLessEqual(abs(sum(shown) * 10 ** 9 - expected), len(sells) * 500)
        trades = [Decimal(x) for x in re.findall(r'거래 총손익: ([+-]\d+\.\d+) SOL', texts)]
        closed = [c for c in self.store.closed_positions() if c['fill_id'] > self.s.first_cursor]
        self.assertEqual(len(trades), len(closed))
        for value, c in zip(trades, closed):
            self.assertLessEqual(abs(value * 10 ** 9 - int(c['state']['trade_pnl_lamports'])), 500)

    def test_exit_reasons_map_to_tp_stages(self):
        texts = '\n\n'.join(self.s.bot.texts())
        for label in ('익절 TP1', '익절 TP2', '익절 TP3', '트레일링 스탑', '손절 (STOP)', '시간 손절 (TIME)'):
            self.assertIn('사유: ' + label, texts)
        ladder = [N.fill_event(self.view, f, sol_usd_max_age=3600) for f in self.fills
                  if f['mint'] == self.s.mint['ladder'] and f['side'] == 'sell']
        self.assertEqual([e['reason_label'] for e in ladder], ['익절 TP1', '익절 TP2', '익절 TP3', '트레일링 스탑'])
        # fraction of the INITIAL quantity: 30% / 30% / 20% (floored to raw units), then the rest
        self.assertEqual([round(float(e['fraction_sold']), 6) for e in ladder], [0.3, 0.3, 0.2, 0.2])
        self.assertEqual([round(float(e['remaining_fraction']), 6) for e in ladder], [0.7, 0.4, 0.2, 0.0])

    def test_quiet_hours_batched_into_one_digest(self):
        quiet_start, quiet_end = T0 + 1200, T0 + 2100
        during = [m for m in self.s.bot.sent if quiet_start <= m['at'] < quiet_end]
        self.assertEqual([m for m in during if '매수' in m['text'] or '매도' in m['text']], [])
        digests = [m for m in self.s.bot.sent if '조용한 시간 요약' in m['text']]
        self.assertEqual(len(digests), 1)
        self.assertGreaterEqual(digests[0]['at'], quiet_end)
        quiet_fills = [f for f in self.fills if quiet_start <= f['ts'] < quiet_end]
        self.assertGreaterEqual(len(quiet_fills), 2)
        bodies = self.expected_bodies(0)
        for f in quiet_fills:
            self.assertIn(bodies[f['id']], digests[0]['text'])
        self.assertIn('(%d건)' % len(quiet_fills), digests[0]['text'])

    def test_telegram_outage_delays_but_never_loses_or_duplicates(self):
        during = [m for m in self.s.bot.sent if TG_OUTAGE[0] <= m['at'] < TG_OUTAGE[1]]
        self.assertEqual(during, [])
        self.assertGreaterEqual(len([f for f in self.fills if TG_OUTAGE[0] <= f['ts'] < TG_OUTAGE[1]]), 2)
        self.assertIn('telegram send failed: HTTP_502', self.s.log.getvalue())
        self.assertIn('getUpdates', {c[0] for c in self.s.bot.calls})

    def test_daily_summary_sent_once_across_restart(self):
        summaries = [m for m in self.s.bot.sent if '일일 요약' in m['text']]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]['at'], T0 + 3600)                 # 18:00 KST

    def test_unrealized_from_the_latest_stored_mark(self):
        shown, expected, marked = self.s.mark_check
        self.assertEqual(set(shown), set(expected))
        self.assertGreaterEqual(len(marked), 3)
        for mint in expected:                                  # a position opened after the last marks read has none
            self.assertEqual(shown[mint], expected[mint] if mint in marked else None)

    def test_the_token_never_appears_in_logs_or_state(self):
        self.assertNotIn(TOKEN, self.s.log.getvalue())
        self.assertNotIn(TOKEN.split(':')[1], self.s.log.getvalue())
        self.assertIn('telegram send failed: HTTP_502', self.s.log.getvalue())
        self.assertNotIn(TOKEN, (self.s.notify_state / N.STATE_FILE).read_text())
        self.assertNotIn(HELIUS_KEY, self.s.log.getvalue())

    def test_daily_summary_numbers(self):
        n = make_notifier(self.s.db, tempfile.mkdtemp(dir=self.s.case.root), self.s.clock, FakeBot(self.s.clock))
        end = self.s.clock.time()
        data = n.summary_data(self.view, end - 86400, end)
        closed = self.store.closed_positions()
        pnl = [int(c['state']['trade_pnl_lamports']) for c in closed]
        self.assertEqual(data['trades'], len(closed))
        self.assertEqual(data['trades'], 7)
        self.assertEqual(data['wins'], sum(1 for p in pnl if p > 0))
        self.assertEqual(data['realized'], self.store.realized())
        self.assertEqual(data['equity'], self.store.cash())                   # nothing open at the end
        self.assertEqual(data['initial'], self.store.initial_cash)
        self.assertEqual(data['best']['pnl'], max(pnl))
        self.assertEqual(data['worst']['pnl'], min(pnl))
        self.assertEqual(data['positions'], [])
        text = n.render_summary(self.view, data)
        self.assertIn('거래: 7건 · 승률 %s' % N.fmt_pct(Decimal(data['wins']) / 7, sign=False), text)
        self.assertLessEqual(abs(parse_sol(text, '실현손익') * 10 ** 9 - self.store.realized()), 500)
        self.assertIn(N.fmt_pct(Decimal(self.store.cash() - self.store.initial_cash) / self.store.initial_cash), text)
        best = re.search(r'최고: .* ([+-]\d+\.\d+) SOL', text)
        worst = re.search(r'최저: .* ([+-]\d+\.\d+) SOL', text)
        self.assertLessEqual(abs(Decimal(best.group(1)) * 10 ** 9 - max(pnl)), 500)
        self.assertLessEqual(abs(Decimal(worst.group(1)) * 10 ** 9 - min(pnl)), 500)

    def test_summary_window_excludes_older_trades(self):
        n = make_notifier(self.s.db, tempfile.mkdtemp(dir=self.s.case.root), self.s.clock, FakeBot(self.s.clock))
        cut = T0 + 2400
        data = n.summary_data(self.view, cut, cut + 86400)
        closed = [c for c in self.store.closed_positions() if c['ts'] >= cut]
        self.assertEqual(data['trades'], len(closed))
        self.assertEqual(data['realized'], sum(f['realized_lamports'] for f in self.fills if f['side'] == 'sell' and f['ts'] >= cut))

    def test_pnl_command_by_strategy(self):
        n = make_notifier(self.s.db, tempfile.mkdtemp(dir=self.s.case.root), self.s.clock, FakeBot(self.s.clock))
        text = n.cmd_pnl(self.view, self.s.clock.time())
        self.assertIn('lean-1: %s · 7건' % N.fmt_sol(N.sol_of(self.store.realized()), sign=True), text)

    def test_status_and_today_commands(self):
        n = make_notifier(self.s.db, tempfile.mkdtemp(dir=self.s.case.root), self.s.clock, FakeBot(self.s.clock))
        status = n.cmd_status(self.view, self.s.clock.time())
        self.assertIn('현금: %s' % N.fmt_sol(N.sol_of(self.store.cash())), status)
        self.assertIn('보유 포지션: 0개', status)
        # the run is 17:00-18:15 KST of one day: all of it is "today"
        self.assertIn('오늘 실현손익: %s' % N.fmt_sol(N.sol_of(self.store.realized()), sign=True), status)
        today = n.cmd_today(self.view, self.s.clock.time())
        self.assertIn('(%d건)' % len(self.fills), today)


# ---------------------------------------------------------------------------------------------------- small worlds
class _StoreCopy(unittest.TestCase):
    """A copy of a finished scenario store (quiet: no -wal) per test, plus a fresh state dir and bot."""

    @classmethod
    def setUpClass(cls):
        cls.s = Scenario()
        cls.s._run()
        cls.s.runner.store.close()
        cls.source = cls.s.db

    @classmethod
    def tearDownClass(cls):
        cls.s.case.doCleanups()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.db = self.root / 'lean.sqlite'
        shutil.copy(self.source, self.db)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.clock = FakeTime(T0 + 5000)                 # 18:23 KST
        self.bot = FakeBot(self.clock)

    def notifier(self, **cfg):
        return make_notifier(self.db, self.state, self.clock, self.bot, daily_summary_at=None, **cfg)

    def fresh_from(self, n, cursor):
        """Pretend the notifier had started when the fill cursor was ``cursor``."""
        n.state.update(fill_cursor=cursor, event_cursor=view_of(self.db).max_ids()[1], update_offset=0)
        return n


class OutboxAndRetryTest(_StoreCopy):
    def test_first_start_sends_nothing_and_restart_does_not_repeat(self):
        n = self.notifier()
        n.run_once()
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(n.state['fill_cursor'], view_of(self.db).max_ids()[0])
        again = self.notifier()
        again.run_once()
        self.assertEqual(self.bot.sent, [])

    def test_429_waits_exactly_retry_after(self):
        n = self.fresh_from(self.notifier(), view_of(self.db).max_ids()[0] - 1)
        self.bot.script = [too_many(7)]
        n.run_once()
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(n.backoff_until, self.clock.time() + 7)
        calls = len([c for c in self.bot.calls if c[0] == 'sendMessage'])
        self.clock.advance(6.5)
        n.run_once()
        self.assertEqual(len([c for c in self.bot.calls if c[0] == 'sendMessage']), calls)      # no request inside the wait
        self.assertEqual(self.bot.sent, [])
        self.clock.advance(0.5)
        n.run_once()
        self.assertEqual(len(self.bot.sent), 1)
        self.assertIn('매도', self.bot.sent[0]['text'])
        self.assertEqual(n.state['outbox'], [])

    def test_429_retry_after_header_and_exponential_backoff(self):
        n = self.fresh_from(self.notifier(), view_of(self.db).max_ids()[0] - 1)
        self.bot.script = [server_error(), server_error(), server_error()]
        n.run_once()
        self.assertEqual(n.backoff_until - self.clock.time(), 1.0)
        self.clock.advance(1)
        n.run_once()
        self.assertEqual(n.backoff_until - self.clock.time(), 2.0)
        self.clock.advance(2)
        n.run_once()
        self.assertEqual(n.backoff_until - self.clock.time(), 4.0)
        self.clock.advance(4)
        n.run_once()
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(n.failures, 0)

    def test_outbox_is_bounded_and_drops_are_announced(self):
        last = view_of(self.db).max_ids()[0]
        n = self.fresh_from(self.notifier(max_outbox=3), last - 6)
        self.bot.script = [server_error()] * 50
        n.run_once()
        self.assertEqual(len(n.state['outbox']), 3)
        self.assertEqual(n.state['dropped'], 3)
        view = view_of(self.db)
        newest = [n.tagged(N.render_fill(N.fill_event(view, view.fill(i), sol_usd_max_age=3600), n.label(view, view.fill(i)['mint'])))
                  for i in range(last - 2, last + 1)]                  # the OLDEST three were dropped
        view.close()
        self.assertEqual([m['text'] for m in n.state['outbox']], newest)
        self.bot.script = []
        self.clock.advance(60)
        restarted = self.notifier(max_outbox=3)             # the outbox survives a restart too
        self.assertEqual([m['text'] for m in restarted.state['outbox']], newest)
        restarted.run_once()
        texts = self.bot.texts()
        self.assertEqual(len(texts), 4)
        self.assertIn('3 messages dropped', texts[0])
        self.assertTrue(texts[0].startswith('[PAPER] ⚠️'))
        self.assertEqual(texts[1:], newest)
        self.assertEqual(restarted.state['dropped'], 0)

    def test_an_unreadable_fill_never_blocks_the_feed(self):
        last = view_of(self.db).max_ids()[0]
        n = self.fresh_from(self.notifier(), last - 2)
        real, calls = N.fill_event, iter([ValueError('bad')])

        def flaky(view, row, **kwargs):
            failure = next(calls, None)
            if failure is not None:
                raise failure
            return real(view, row, **kwargs)
        with mock.patch.object(N, 'fill_event', side_effect=flaky):
            n.run_once()
        self.assertEqual(n.state['fill_cursor'], last)
        self.assertEqual(len(self.bot.sent), 2)
        self.assertIn('세부 정보를 읽지 못했습니다', self.bot.sent[0]['text'])
        self.assertIn('사유:', self.bot.sent[1]['text'])

    def test_crash_mid_flush_repeats_nothing_already_sent(self):
        last = view_of(self.db).max_ids()[0]
        n = self.fresh_from(self.notifier(), last - 3)
        self.bot.script = ['ok', SystemExit('process killed')]        # dies while sending the 2nd message
        with self.assertRaises(SystemExit):
            n.run_once()
        self.assertEqual(len(self.bot.sent), 1)
        restarted = self.notifier()
        restarted.run_once()
        texts = self.bot.texts()
        self.assertEqual(len(texts), 3)
        self.assertEqual(len(set(texts)), 3)

    def test_permanent_400_retries_as_plain_text_then_moves_on(self):
        n = self.fresh_from(self.notifier(), view_of(self.db).max_ids()[0] - 2)
        bad = Resp({'ok': False, 'error_code': 400, 'description': "Bad Request: can't parse entities"}, 400)
        self.bot.script = [bad, bad]
        n.run_once()
        self.assertEqual(len(self.bot.sent), 2)
        self.assertIn('1 messages dropped', self.bot.sent[0]['text'])
        self.assertIn('매', self.bot.sent[1]['text'])
        self.assertEqual((n.state['dropped'], n.state['outbox']), (0, []))
        self.assertEqual(len([c for c in self.bot.calls if c[0] == 'sendMessage']), 4)

    def test_telegram_failure_never_touches_the_store(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        n = self.fresh_from(self.notifier(), 0)
        self.bot.script = [URLError('boom')] * 5 + [server_error()] * 5
        for _ in range(5):
            n.run_once()
            self.clock.advance(30)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        self.assertFalse(Path(str(self.db) + '-wal').exists())
        with self.assertRaises(sqlite3.Error):
            connection = N.open_store(self.db)
            connection.execute("INSERT INTO events(ts,kind,payload,code_version,strategy_version) VALUES(1,'x','{}','c','s')")

    def test_live_store_is_read_with_mode_ro_and_query_only(self):
        store = Store(self.db)                              # the trader holds the store open: -wal exists
        try:
            store.record('note', {}, code_version='c', strategy_version='s')
            self.assertTrue(Path(str(self.db) + '-wal').exists())
            connection = N.open_store(self.db)
            self.assertEqual(connection.execute('PRAGMA query_only').fetchone()[0], 1)
            with self.assertRaises(sqlite3.Error):
                connection.execute("INSERT INTO events(ts,kind,payload,code_version,strategy_version) VALUES(1,'x','{}','c','s')")
            connection.close()
        finally:
            store.close()


class HaltAndQuietTest(_StoreCopy):
    def test_halt_is_sent_immediately_even_in_quiet_hours(self):
        n = self.notifier(quiet_hours=['18:00', '19:00'])
        n.run_once()                                                       # first start
        store = Store(self.db)
        store.record('halt', {'reason': 'ACCOUNTING_INVARIANT: <cash> & more'}, code_version='c', strategy_version='s')
        store.close()
        n.run_once()
        self.assertEqual(len(self.bot.sent), 1)
        text = self.bot.sent[0]['text']
        self.assertTrue(text.startswith('[PAPER] ⛔'))
        self.assertIn('ACCOUNTING_INVARIANT: &lt;cash&gt; &amp; more', text)
        store = Store(self.db)
        store.record('halt_cleared', {'previous': 'ACCOUNTING_INVARIANT'}, code_version='c', strategy_version='s')
        store.record('start', {}, code_version='c', strategy_version='s')
        store.close()
        n.run_once()
        self.assertEqual(len(self.bot.sent), 2)
        self.assertIn('중단 해제', self.bot.sent[1]['text'])
        n.run_once()
        self.assertEqual(len(self.bot.sent), 2)

    def test_quiet_hours_wrap_midnight_and_digest_on_end(self):
        n = self.fresh_from(self.notifier(quiet_hours=['18:00', '08:00'], mode='LIVE'), view_of(self.db).max_ids()[0] - 3)
        n.run_once()
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(len(n.state['digest']), 3)
        self.clock.advance(3600 * 6)                                       # 00:23 KST: still quiet
        n.run_once()
        self.assertEqual(self.bot.sent, [])
        self.clock.advance(3600 * 8)                                       # 08:23 KST: quiet hours over
        n.run_once()
        self.assertEqual(len(self.bot.sent), 1)
        self.assertTrue(self.bot.sent[0]['text'].startswith('[LIVE] 🌙'))
        self.assertIn('(3건)', self.bot.sent[0]['text'])
        self.assertEqual(n.state['digest'], [])

    def test_daily_summary_schedule(self):
        n = make_notifier(self.db, self.state, self.clock, self.bot, daily_summary_at='18:30')
        n.run_once()                                                       # 18:23: not yet
        self.assertEqual([m for m in self.bot.sent if '일일 요약' in m['text']], [])
        self.clock.advance(7 * 60)                                         # 18:30
        n.run_once()
        n.run_once()
        self.assertEqual(len([m for m in self.bot.sent if '일일 요약' in m['text']]), 1)
        self.clock.advance(86400)
        n.run_once()
        self.assertEqual(len([m for m in self.bot.sent if '일일 요약' in m['text']]), 2)

    def test_first_start_long_after_summary_time_waits_for_tomorrow(self):
        n = make_notifier(self.db, self.state, self.clock, self.bot, daily_summary_at='09:00')
        n.run_once()
        self.assertEqual(self.bot.sent, [])


class CommandsTest(_StoreCopy):
    def update(self, uid, chat, text):
        return {'update_id': uid, 'message': {'message_id': uid, 'date': int(self.clock.time()), 'text': text,
                                              'chat': {'id': int(chat), 'type': 'private', 'first_name': 'X'}}}

    def test_pending_commands_at_first_start_are_skipped(self):
        self.bot.updates = [self.update(10, CHAT, '/status')]
        n = self.notifier()
        n.run_once()
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(n.state['update_offset'], 11)

    def test_only_allowed_chats_are_answered(self):
        n = self.notifier(allowed_chat_ids=[CHAT, '777'])
        n.run_once()
        self.bot.updates = [self.update(1, '999', '/status'), self.update(2, '999', '/pnl'),
                            self.update(3, CHAT, '/status'), self.update(4, '777', '/pnl@lean_bot'),
                            self.update(5, CHAT, 'hello'), self.update(6, CHAT, '/today')]
        n.run_once()
        self.assertEqual([m['chat_id'] for m in self.bot.sent], [CHAT, '777', CHAT])
        self.assertIn('현황', self.bot.sent[0]['text'])
        self.assertIn('누적 손익', self.bot.sent[1]['text'])
        self.assertIn('오늘 체결', self.bot.sent[2]['text'])
        self.assertNotIn('999', [m['chat_id'] for m in self.bot.sent])
        n.run_once()                                                       # the offset moved: nothing answered twice
        self.assertEqual(len(self.bot.sent), 3)

    def test_unauthorized_chat_gets_nothing_by_default(self):
        n = self.notifier()
        n.run_once()
        self.bot.updates = [self.update(1, '31337', '/status')]
        n.run_once()
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(n.state['update_offset'], 2)


class SecretsTest(_StoreCopy):
    def test_token_never_in_exceptions_or_logs(self):
        url = 'https://api.telegram.org/bot%s/sendMessage' % TOKEN
        n = self.fresh_from(self.notifier(), view_of(self.db).max_ids()[0] - 1)
        self.bot.script = [URLError(url), HTTPError(url, 502, 'bad %s' % url, {}, io.BytesIO(b'{"description":"%s"}' % url.encode())),
                           Resp({'ok': False, 'error_code': 401, 'description': 'Unauthorized ' + url}, 401),
                           OSError(url), TimeoutError(url)]
        with self.assertLogs('lean', level='DEBUG') as logs:
            for _ in range(5):
                n.run_once()
                self.clock.advance(400)
        output = '\n'.join(logs.output)
        self.assertNotIn(TOKEN, output)
        self.assertIn('NETWORK_URLError', output)
        self.assertIn('HTTP_502', output)
        self.assertIn('HTTP_401', output)
        tg = N.Telegram(TOKEN, opener=self.bot)
        for failure in (URLError(url), HTTPError(url, 500, url, {}, io.BytesIO(url.encode())), ValueError(url)):
            self.bot.script = [failure]
            try:
                tg.send(CHAT, 'x')
            except N.TelegramError as error:
                self.assertNotIn(TOKEN, str(error))
                self.assertNotIn(TOKEN, repr(error))
                self.assertNotIn(TOKEN, str(error.description))
                self.assertIsNone(error.__context__)
                self.assertIsNone(error.__cause__)
            else:
                self.fail('no error')
        self.assertNotIn(TOKEN, repr(tg))
        self.assertNotIn(TOKEN, N.redact('failed %s with token %s' % (url, TOKEN)))
        self.assertNotIn(TOKEN, N.redact('failed %s with token %s' % (url, TOKEN), TOKEN))
        self.assertNotIn(TOKEN, N.redact('bot%s' % TOKEN))

    def test_discover_chat_prints_ids_and_names_only(self):
        creds = self.root / 'creds'
        creds.mkdir()
        (creds / 'telegram.json').write_text(json.dumps({'bot_token': TOKEN}))
        os.chmod(creds / 'telegram.json', 0o600)
        self.bot.updates = [{'update_id': 1, 'message': {'chat': {'id': 4242, 'first_name': 'CK', 'type': 'private'}, 'text': '/start'}},
                            {'update_id': 2, 'message': {'chat': {'id': 4242, 'first_name': 'CK', 'type': 'private'}, 'text': 'hi'}},
                            {'update_id': 3, 'message': {'chat': {'id': -100, 'title': 'Desk group', 'type': 'group'}, 'text': 'x'}}]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = N.main(['--discover-chat', '--credentials', str(creds)], opener=self.bot)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()), {'status': 'OK', 'chats': [{'chat_id': '4242', 'first_name': 'CK'},
                                                                                 {'chat_id': '-100', 'first_name': 'Desk group'}]})
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        self.bot.updates = []
        bad = Resp({'ok': False, 'error_code': 401, 'description': 'Unauthorized for bot%s' % TOKEN}, 401)
        tg_bad = mock.Mock(side_effect=lambda request, timeout: bad)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = N.main(['--discover-chat', '--credentials', str(creds)], opener=tg_bad)
        self.assertEqual(rc, 3)
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        self.assertIn('HTTP_401', err.getvalue())

    def test_main_once_wires_config_credentials_and_state(self):
        creds = self.root / 'creds'
        creds.mkdir()
        (creds / 'telegram.json').write_text(json.dumps({'bot_token': TOKEN, 'chat_id': CHAT}))
        (creds / 'provider-keys.json').write_text(json.dumps({'HELIUS_API_KEY': HELIUS_KEY, 'JUPITER_API_KEY': 'J-KEY-0000'}))
        for name in ('telegram.json', 'provider-keys.json'):
            os.chmod(creds / name, 0o400)
        example = Path(E2E.ROOT) / 'config' / 'lean' / 'lean-notify.example.json'
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = N.main(['--db', str(self.db), '--config', str(example), '--credentials', str(creds),
                         '--state-dir', str(self.state), '--once'], opener=self.bot)
        self.assertEqual(rc, 0, err.getvalue())
        state = json.loads((self.state / N.STATE_FILE).read_text())
        self.assertEqual(state['fill_cursor'], view_of(self.db).max_ids()[0])
        self.assertEqual(self.bot.sent, [])
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        os.chmod(creds / 'telegram.json', 0o644)            # a world-readable token file is refused
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = N.main(['--db', str(self.db), '--credentials', str(creds), '--state-dir', str(self.state), '--once'],
                        opener=self.bot)
        self.assertEqual(rc, 2)
        self.assertIn('TELEGRAM_CREDENTIAL_INVALID', err.getvalue())
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())

    def test_credential_file_checks(self):
        creds = self.root / 'creds'
        creds.mkdir()
        path = creds / 'telegram.json'
        path.write_text(json.dumps({'bot_token': TOKEN, 'chat_id': CHAT}))
        os.chmod(path, 0o644)
        with self.assertRaises(N.NotifyError) as ctx:
            N.load_telegram(path)
        self.assertNotIn(TOKEN, str(ctx.exception))
        os.chmod(path, 0o600)
        self.assertEqual(N.load_telegram(path), {'bot_token': TOKEN, 'chat_id': CHAT})
        path.write_text(json.dumps({'bot_token': 'not a token', 'chat_id': CHAT}))
        with self.assertRaises(N.NotifyError):
            N.load_telegram(path)
        path.write_text(json.dumps({'bot_token': TOKEN}))
        with self.assertRaises(N.NotifyError):
            N.load_telegram(path)                       # the notifier needs a chat id (discover mode does not)
        self.assertEqual(N.load_telegram(path, need_chat=False)['chat_id'], None)

    def test_config_is_strict(self):
        cfg = self.root / 'notify.json'
        example = Path(E2E.ROOT) / 'config' / 'lean' / 'lean-notify.example.json'
        self.assertEqual(N.load_config(example), N.load_config(None))
        cfg.write_text(json.dumps({'quiet_hour': ['00:00', '08:00']}))
        with self.assertRaises(N.NotifyError):
            N.load_config(cfg)
        for bad in ({'mode': 'REAL'}, {'quiet_hours': ['25:00', '08:00']}, {'timezone': 'Mars/Base'},
                    {'allowed_chat_ids': ['abc']}, {'max_outbox': 0}):
            cfg.write_text(json.dumps(bad))
            with self.assertRaises(N.NotifyError, msg=bad):
                N.load_config(cfg)


class FormattingTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(N.fmt_price(Decimal('0.00000041234')), '0.0000004123')
        self.assertEqual(N.fmt_price(Decimal('123.456')), '123.5')
        self.assertEqual(N.fmt_sol(Decimal('0.2')), '0.20 SOL')
        self.assertEqual(N.fmt_sol(Decimal('-0.0123456'), sign=True), '-0.012346 SOL')
        self.assertEqual(N.fmt_sol(Decimal('0.01'), sign=True), '+0.01 SOL')
        self.assertEqual(N.fmt_usd_big(Decimal('123456')), '$123.46K')
        self.assertEqual(N.fmt_usd_big(Decimal('1500000')), '$1.50M')
        self.assertEqual(N.fmt_pct(Decimal('-0.18')), '-18.00%')
        self.assertEqual(N.fmt_duration(3900), '1시간 5분')
        self.assertEqual(N.fmt_duration(90061), '1일 1시간')
        self.assertEqual(N.short_ca('So11111111111111111111111111111111111111112'), 'So11…1112')

    def test_split_keeps_lines_whole(self):
        text = '\n'.join('<b>line %d</b>' % i for i in range(1000))
        parts = N.split_text(text, 500)
        self.assertTrue(all(len(p) <= 500 for p in parts))
        self.assertEqual('\n'.join(parts), text)


if __name__ == '__main__':
    unittest.main()
