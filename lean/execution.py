"""Latency-aware paper fills and a realistic cost model (L10). PAPER ONLY: nothing here signs, builds or sends a transaction.

A real bot is a few seconds late: by the time its transaction lands, the price it was quoted has moved. This module
replays that on every paper fill, without changing the strategy or the accounting rules:

  BUY   q0 = the decision quote. After ``exec_delay_s`` the same spend is quoted again (q1). The fill is the WORSE of the
        two (fewer tokens). If q1 has no route, or the round trip on q1 breaks the cost cap (the strategy's own entry
        decision is re-run on q1), the entry is dropped and a decision ``ENTRY_ABORTED_LATENCY`` is recorded. Not an error.
  SELL  q0 = the decision quote. After ``exec_delay_s`` the same quantity is quoted again (q1) and the fill is ALWAYS q1
        (an exit is wanted, whatever the price). If no q1 can be had inside the mark's freshness window, the position is
        simply retried on the next position pass (it stays open; the exit lane is never slowed or blocked).

Both quotes, the fill quote and ``latency_tax_bps`` are stored with the fill (in the position_state row written in the SAME
transaction as the fill, key ``execution``), so an exit or an entry can always be audited.

Cost model (added to the paper fee, all in integer lamports):
  * priority fee + Jito tip per transaction: added to the fill's ``fee_lamports`` (also used by the net marks, so the
    strategy sees the same cost it pays);
  * token-account (ATA) rent: paid with the FIRST buy of a mint (added to that buy's fee, so cash is really out) and
    refunded in the sell proceeds when the position is fully closed. Tracked separately in the ``execution`` payload;
    the adapter keeps it out of the strategy's cost basis so stops and take-profits are not skewed;
  * Token-2022 transfer-fee extension: a buy delivers ``qty * (1 - bps)`` tokens and a sell is haircut by the same bps.
    ``bps`` is read from the mint account the screen already loaded (the larger of the older/newer fee, the cap is ignored,
    both conservative).

Everything is OFF unless lean.json has an ``execution`` object (so today's behaviour is unchanged when the key is absent).
"""
import base64
import dataclasses
import json
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN

from lean import adapters as A, paper, strategy as S
from lean.providers import ProviderError

LAMPORTS = paper.LAMPORTS
MAX_BPS = paper.MAX_BPS
TOKEN_2022 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
SOL_MINT = A.SOL_MINT
DEFAULT_SOL = {'priority_fee_sol': '0.0001', 'jito_tip_sol': '0.0001', 'ata_rent_sol': '0.00203928'}
KEYS = ('exec_delay_s', 'priority_fee_sol', 'jito_tip_sol', 'ata_rent_sol', 'exit_requote_attempts', 'exit_requote_gap_s')
ABORT = 'ENTRY_ABORTED_LATENCY'


class ExecutionError(ValueError):
    """Bad execution config, or evidence that cannot be converted (per-candidate/position; never a halt)."""


def _lamports(value, name):
    """Exact lamports for a SOL amount; zero is allowed (a cost that is switched off)."""
    if isinstance(value, bool):
        raise ExecutionError('%s must be a number' % name)
    try:
        number = Decimal(str(value) if isinstance(value, float) else value)
    except Exception:
        raise ExecutionError('%s must be a number' % name) from None
    scaled = number * LAMPORTS
    if not number.is_finite() or number < 0 or scaled != scaled.to_integral_value() or scaled >= 2 ** 63:
        raise ExecutionError('%s must be a non-negative whole number of lamports' % name)
    return int(scaled)


@dataclass(frozen=True)
class ExecConfig:
    exec_delay_s: float = 3.0
    priority_fee_lamports: int = 100_000
    jito_tip_lamports: int = 100_000
    ata_rent_lamports: int = 2_039_280
    exit_requote_attempts: int = 5
    exit_requote_gap_s: float = 1.0

    def __post_init__(self):
        for name in ('priority_fee_lamports', 'jito_tip_lamports', 'ata_rent_lamports', 'exit_requote_attempts'):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ExecutionError('%s must be a non-negative integer' % name)
        if self.exit_requote_attempts < 1:
            raise ExecutionError('exit_requote_attempts must be >= 1')
        for name in ('exec_delay_s', 'exit_requote_gap_s'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 60 or value != value:
                raise ExecutionError('%s must be between 0 and 60' % name)

    @classmethod
    def from_dict(cls, raw):
        """Strict: unknown keys are refused. Money is given in SOL (as in the task text) and stored as lamports."""
        if not isinstance(raw, dict):
            raise ExecutionError('execution config must be an object')
        unknown = set(raw) - set(KEYS) - {'_comment'}
        if unknown:
            raise ExecutionError('execution config: unknown keys %s' % sorted(unknown))
        values = {k: raw.get(k, v) for k, v in DEFAULT_SOL.items()}
        return cls(exec_delay_s=raw.get('exec_delay_s', 3.0), priority_fee_lamports=_lamports(values['priority_fee_sol'], 'priority_fee_sol'),
                   jito_tip_lamports=_lamports(values['jito_tip_sol'], 'jito_tip_sol'),
                   ata_rent_lamports=_lamports(values['ata_rent_sol'], 'ata_rent_sol'),
                   exit_requote_attempts=raw.get('exit_requote_attempts', 5), exit_requote_gap_s=raw.get('exit_requote_gap_s', 1.0))

    @property
    def tx_extra_lamports(self):
        return self.priority_fee_lamports + self.jito_tip_lamports


# ----------------------------------------------------------------------------------------------- Token-2022 transfer fee
def _tlv_transfer_fee_bps(data):
    """bps of the TransferFeeConfig extension (type 1) in a Token-2022 mint, or 0 when there is none. ValueError if unreadable."""
    if len(data) <= 82:
        return 0
    if len(data) < 167 or data[165] != 1:
        raise ValueError('mint extension padding')
    offset, best = 166, 0
    while offset + 4 <= len(data):
        kind = int.from_bytes(data[offset:offset + 2], 'little')
        size = int.from_bytes(data[offset + 2:offset + 4], 'little')
        offset += 4
        if kind == 0 and size == 0:
            break
        if offset + size > len(data):
            raise ValueError('truncated extension')
        if kind == 1:
            if size != 108:
                raise ValueError('transfer fee config length')
            older = int.from_bytes(data[offset + 88:offset + 90], 'little')      # epoch u64, max fee u64, bps u16
            newer = int.from_bytes(data[offset + 106:offset + 108], 'little')
            best = max(older, newer)
        offset += size
    if best > MAX_BPS:
        raise ValueError('transfer fee bps')
    return best


def transfer_fee_bps(account):
    """Transfer fee (bps) of a mint account in the RPC base64 shape; 0 for a legacy SPL mint. ExecutionError if unreadable."""
    try:
        if not isinstance(account, dict):
            raise ValueError('account')
        if account.get('owner') != TOKEN_2022:
            return 0
        data = account['data']
        if not isinstance(data, list) or len(data) != 2 or data[1] != 'base64':
            raise ValueError('encoding')
        return _tlv_transfer_fee_bps(base64.b64decode(data[0], validate=True))
    except (ValueError, KeyError, TypeError):
        raise ExecutionError('TRANSFER_FEE_UNREADABLE') from None


def transfer_fee_bps_from_screen(raw_items):
    """Read the mint account out of the raw response the screen kept (label ``accounts_mint_pool``, first value)."""
    for label, raw in raw_items:
        if label == 'accounts_mint_pool':
            try:
                parsed = json.loads(raw)
                value = (parsed.get('result', parsed) or {}).get('value')
                return transfer_fee_bps(value[0])
            except (ValueError, AttributeError, IndexError, TypeError):
                raise ExecutionError('TRANSFER_FEE_UNREADABLE') from None
    raise ExecutionError('TRANSFER_FEE_UNREADABLE')


# ----------------------------------------------------------------------------------------------------------- the model
def tax_bps(q0_out, fill_out):
    """(q0 - fill) / q0 in basis points, on the OUTPUT of the same input. Positive = the fill was worse than the decision."""
    return str((Decimal(q0_out - fill_out) * MAX_BPS / Decimal(q0_out)).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN))


class Execution:
    def __init__(self, cfg, *, sleep=time.sleep):
        if not isinstance(cfg, ExecConfig):
            raise ExecutionError('an ExecConfig is required')
        self.cfg, self.sleep = cfg, sleep

    def validate_against(self, strategy_cfg):
        """The re-quote must fit inside the strategy's price freshness window, or every exit would be BLOCKED as stale."""
        if self.cfg.exec_delay_s > strategy_cfg.price_ttl_seconds - 2:
            raise ExecutionError('exec_delay_s must be at most price_ttl_seconds - 2 (%d)' % (strategy_cfg.price_ttl_seconds - 2))

    def pcfg(self, base):
        """The paper config whose fee includes the priority fee and the Jito tip (one transaction per fill)."""
        return paper.PaperConfig(fee_lamports=base.fee_lamports + self.cfg.tx_extra_lamports, slippage_bps=base.slippage_bps)

    # -- BUY ---------------------------------------------------------------------------------------------------------
    def entry_requote(self, runner, features, mint, cid, buy, sell_leg, raw_items):
        """After the delay: re-quote and keep the worse. Returns ``(buy, sell_leg, meta)`` or None when the entry is aborted.

        ``buy`` = (quote, ref, observed_at) and ``sell_leg`` = (quote, observed_at), as in the runner."""
        buy_q, buy_ref, buy_at = buy
        base = {'delay_s': self.cfg.exec_delay_s, 'q0_ref': buy_ref, 'q0_out': buy_q.out_amount}
        try:
            fee_bps = transfer_fee_bps_from_screen(raw_items)
        except ExecutionError as error:
            return self._abort(runner, mint, cid, ['TRANSFER_FEE_UNREADABLE'], {**base, 'error': str(error)})
        if self.cfg.exec_delay_s:
            self.sleep(self.cfg.exec_delay_s)
        try:
            q1, q1_ref, q1_at = runner._quote(runner.providers, SOL_MINT, mint, buy_q.in_amount, 'quote:buy_requote', mint, cid)
        except ProviderError as error:                       # no route / provider down at fill time: cannot execute
            return self._abort(runner, mint, cid, ['Q1_UNAVAILABLE:%s' % error.code], base)
        meta = {**base, 'q1_ref': q1_ref, 'q1_out': q1.out_amount, 'transfer_fee_bps': fee_bps}
        if q1.out_amount >= buy_q.out_amount:                # the market did not move against us: fill at q0
            return buy, sell_leg, {**meta, 'fill': 'q0', 'fill_out': buy_q.out_amount, 'fill_ref': buy_ref}
        try:                                                  # worse: the sell leg for what we would now hold, and the cost cap
            sell_q, _ref, sell_at = runner._quote(runner.providers, mint, SOL_MINT, A.sell_leg_qty(q1.out_amount, runner.pcfg),
                                                  'quote:sell_leg_requote', mint, cid)
        except ProviderError as error:
            return self._abort(runner, mint, cid, ['Q1_SELL_LEG_UNAVAILABLE:%s' % error.code], meta)
        decision = S.entry_decision(features, A.roundtrip(q1, q1_at, sell_q, sell_at), runner.portfolio(runner._now()), runner.cfg)
        if decision.action != 'BUY':
            return self._abort(runner, mint, cid, ['LATENCY_%s' % r for r in (decision.reasons or (decision.action,))], meta)
        return (q1, q1_ref, q1_at), (sell_q, sell_at), {**meta, 'fill': 'q1', 'fill_out': q1.out_amount, 'fill_ref': q1_ref}

    def _abort(self, runner, mint, cid, reasons, detail):
        runner.store.add_decision('entry', ABORT, mint=mint, candidate_id=cid, reasons=list(reasons), features=_plain(detail), ts=runner.clock())
        with runner._counts_lock:
            runner.counts['entry_aborted_latency'] = runner.counts.get('entry_aborted_latency', 0) + 1
        return None

    def finish_buy(self, runner, fill, meta):
        """Apply the cost model to the paper BUY. Returns ``(fill, extra_position_state)``."""
        if meta is None:
            return fill, {}
        bps = meta['transfer_fee_bps']
        qty = fill.qty_raw * (MAX_BPS - bps) // MAX_BPS
        lost_tokens = fill.qty_raw - qty
        if qty <= 0:
            raise ExecutionError('nothing would be received after the transfer fee')
        base_fee = fill.fee_lamports - self.cfg.tx_extra_lamports
        first = fill.mint not in runner.store.positions()
        rent = self.cfg.ata_rent_lamports if first else 0
        fill = dataclasses.replace(fill, qty_raw=qty, fee_lamports=fill.fee_lamports + rent)
        payload = {'side': 'buy', **_plain(meta), 'latency_tax_bps': tax_bps(meta['q0_out'], meta['fill_out']),
                   'base_fee_lamports': base_fee, 'priority_fee_lamports': self.cfg.priority_fee_lamports,
                   'jito_tip_lamports': self.cfg.jito_tip_lamports, 'ata_rent_paid_lamports': rent, 'ata_refund_lamports': 0,
                   'transfer_fee_lost_tokens_raw': lost_tokens, 'transfer_fee_lost_lamports': 0}
        return fill, {'ata_rent_lamports': rent, 'transfer_fee_bps': bps, 'execution': payload}

    # -- SELL --------------------------------------------------------------------------------------------------------
    def requote_exit(self, runner, mint, qty, q0, ref0, at0, mark_at):
        """After the delay: the quote the exit really fills at. Retries inside the mark's freshness window, else raises."""
        deadline = (mark_at if isinstance(mark_at, int) else at0) + runner.cfg.price_ttl_seconds - 1
        if self.cfg.exec_delay_s:
            self.sleep(self.cfg.exec_delay_s)
        last = None
        for attempt in range(1, self.cfg.exit_requote_attempts + 1):
            if runner._now() >= deadline:
                break
            try:
                q1, ref1, at1 = runner._quote(runner.exit_providers, mint, SOL_MINT, qty, 'quote:exit_requote', mint)
                return q1, ref1, at1, {'delay_s': self.cfg.exec_delay_s, 'q0_ref': ref0, 'q0_out': q0.out_amount, 'q1_ref': ref1,
                                       'q1_out': q1.out_amount, 'fill': 'q1', 'fill_out': q1.out_amount, 'fill_ref': ref1, 'attempts': attempt}
            except ProviderError as error:
                last = error
                if attempt < self.cfg.exit_requote_attempts:
                    self.sleep(self.cfg.exit_requote_gap_s)
        if last is None:
            last = ProviderError('REQUOTE_WINDOW_CLOSED', True)
            last.kind = 'quote:exit_requote'
        raise last

    def finish_sell(self, fill, position, state, meta):
        """Apply the cost model to the paper SELL. Returns ``(fill, extra_position_state)``."""
        if meta is None:
            return fill, {}
        bps = int(state.get('transfer_fee_bps', 0))
        sol = fill.sol_lamports * (MAX_BPS - bps) // MAX_BPS
        haircut = fill.sol_lamports - sol
        closing = fill.qty_raw == position.qty_raw
        refund = int(state.get('ata_rent_lamports', 0)) if closing else 0
        sol += refund
        fill = dataclasses.replace(fill, sol_lamports=sol, realized_lamports=sol - fill.fee_lamports - fill.cost_sold_lamports)
        payload = {'side': 'sell', **_plain(meta), 'latency_tax_bps': tax_bps(meta['q0_out'], meta['fill_out']),
                   'base_fee_lamports': fill.fee_lamports - self.cfg.tx_extra_lamports, 'priority_fee_lamports': self.cfg.priority_fee_lamports,
                   'jito_tip_lamports': self.cfg.jito_tip_lamports, 'ata_rent_paid_lamports': 0, 'ata_refund_lamports': refund,
                   'transfer_fee_bps': bps, 'transfer_fee_lost_tokens_raw': 0, 'transfer_fee_lost_lamports': haircut}
        return fill, {'execution': payload}


def _plain(value):
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


# ----------------------------------------------------------------------------------------------------------- report
def report_section(connection, *, percentile, min_sample=30):
    """Latency-tax distribution (p50/p90 per side), aborted entries and the fee breakdown, from an open READ-ONLY
    connection to a lean store. Pure reads; ``percentile`` is lean.report's."""
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'position_state', 'fills', 'decisions'} <= tables:        # not a lean store: nothing to report
        rows, abort_rows = [], []
    else:
        rows = connection.execute("SELECT f.side, p.state FROM position_state p JOIN fills f ON f.id = p.fill_id "
                                  "WHERE p.fill_id IS NOT NULL ORDER BY p.id").fetchall()
        abort_rows = connection.execute("SELECT reasons FROM decisions WHERE action=?", (ABORT,)).fetchall()
    taxes = {'buy': [], 'sell': []}
    totals = dict.fromkeys(('base_fee', 'priority_fee', 'jito_tip', 'ata_rent_paid', 'ata_refund', 'transfer_fee_lost_lamports',
                            'transfer_fee_lost_tokens_raw'), 0)
    fills = 0
    for side, text in rows:
        payload = (json.loads(text) or {}).get('execution')
        if not isinstance(payload, dict) or payload.get('side') != side:
            continue
        fills += 1
        taxes[side].append(Decimal(payload['latency_tax_bps']))
        totals['base_fee'] += payload['base_fee_lamports']
        totals['priority_fee'] += payload['priority_fee_lamports']
        totals['jito_tip'] += payload['jito_tip_lamports']
        totals['ata_rent_paid'] += payload['ata_rent_paid_lamports']
        totals['ata_refund'] += payload['ata_refund_lamports']
        totals['transfer_fee_lost_lamports'] += payload['transfer_fee_lost_lamports']
        totals['transfer_fee_lost_tokens_raw'] += payload['transfer_fee_lost_tokens_raw']
    aborted = {}
    for (reasons,) in abort_rows:
        for reason in set(json.loads(reasons) or ['UNSPECIFIED']):
            aborted[reason.split(':')[0]] = aborted.get(reason.split(':')[0], 0) + 1

    def dist(values):
        if not values:
            return {'n': 0, 'p50': None, 'p90': None, 'mean': None, 'max': None}
        return {'n': len(values), 'p50': str(percentile(values, Decimal('0.5')).quantize(Decimal('0.01'))),
                'p90': str(percentile(values, Decimal('0.9')).quantize(Decimal('0.01'))),
                'mean': str((sum(values) / len(values)).quantize(Decimal('0.01'))), 'max': str(max(values).quantize(Decimal('0.01')))}
    sol = {k: str(Decimal(v) / LAMPORTS) for k, v in totals.items() if k != 'transfer_fee_lost_tokens_raw'}
    fees = Decimal(totals['base_fee'] + totals['priority_fee'] + totals['jito_tip']) / LAMPORTS
    return {'fills_with_execution': fills, 'latency_tax_bps': {side: dist(v) for side, v in taxes.items()},
            'sample': 'INSUFFICIENT_SAMPLE' if fills < min_sample else 'OK',
            'entries_aborted_latency': sum(aborted.values()), 'aborted_reasons': aborted,
            'fees_sol': {**sol, 'trading_fees_total': str(fees),
                         'ata_rent_outstanding': str(Decimal(totals['ata_rent_paid'] - totals['ata_refund']) / LAMPORTS)},
            'transfer_fee_lost_tokens_raw': totals['transfer_fee_lost_tokens_raw']}
