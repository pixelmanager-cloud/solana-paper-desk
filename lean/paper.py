"""Paper fills and accounting for the lean trader. Pure functions, integers only, no I/O.

Paper only: nothing here signs, builds or sends a transaction. Every fill is a QUOTE-based simulation with a fixed fee and an adverse
slippage haircut, and carries the label EXECUTION_UNVERIFIED.

Units: SOL amounts are integer lamports, token amounts are integer raw units. All divisions round DOWN (never in the trader's favour
twice), so every identity below is exact:

    cash            = initial - sum(buy sol + buy fee) + sum(sell sol - sell fee)
    open_cost       = sum of the cost basis still held (buy sol + buy fee, minus the cost of what was sold)
    cash + open_cost = initial + realized          (realized = sum over sells of: sell sol - sell fee - cost of the sold part)

The cost basis of a partial sell is the average cost: ``cost * qty_sold // qty_held`` (all of it for a full sell).
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

LAMPORTS = 10 ** 9
LABEL = 'EXECUTION_UNVERIFIED'
MAX_BPS = 10_000


class PaperError(ValueError):
    """A request that cannot be filled as asked (bad size, a quote that does not match, a bad fraction). Nothing was recorded."""


class AccountingHalt(RuntimeError):
    """An accounting invariant would be (or was) violated: cash / positions / PnL do not reconcile. The trader must stop entering."""


@dataclass(frozen=True)
class PaperConfig:
    fee_lamports: int = 50_000          # 0.00005 SOL per fill, like the current paper experiment
    slippage_bps: int = 50

    def __post_init__(self):
        for name in ('fee_lamports', 'slippage_bps'):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise PaperError(f'{name} must be a non-negative integer')
        if self.slippage_bps >= MAX_BPS:
            raise PaperError('slippage_bps must be below 10000')

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {'fee_lamports', 'slippage_bps'}:
            raise PaperError('unknown paper config field')
        return cls(**value)


@dataclass(frozen=True)
class Quote:
    """An executable-route quote: ``in_amount`` of the input asset for ``out_amount`` of the output asset (raw units).

    buy: input is SOL (lamports), output is the token. sell: input is the token, output is SOL (lamports)."""
    mint: str
    side: str
    in_amount: int
    out_amount: int
    decimals: int
    ts: float
    source: str = 'jupiter'
    ref: str = None                     # id of the retained raw response (an observation row), for audit

    def __post_init__(self):
        if self.side not in ('buy', 'sell'):
            raise PaperError('quote side must be buy or sell')
        for name in ('in_amount', 'out_amount'):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise PaperError(f'{name} must be a positive integer')
        if type(self.decimals) is not int or not 0 <= self.decimals <= 30:
            raise PaperError('decimals out of range')
        if not isinstance(self.mint, str) or not self.mint:
            raise PaperError('mint required')


@dataclass(frozen=True)
class Position:
    mint: str
    qty_raw: int
    cost_lamports: int                  # cost basis still held, buy fees included
    opened_at: float
    realized_lamports: int              # realized on the parts already sold (fees included)
    decimals: int


@dataclass(frozen=True)
class Fill:
    ts: float
    mint: str
    side: str
    qty_raw: int
    sol_lamports: int                   # after slippage, before the fee
    fee_lamports: int
    slippage_bps: int
    decimals: int
    cost_sold_lamports: int = 0         # sells: basis of the sold part
    realized_lamports: int = 0          # sells: sol - fee - cost_sold
    quote_ref: str = None
    label: str = LABEL

    @property
    def price_sol_per_token(self):
        return Decimal(self.sol_lamports) / Decimal(LAMPORTS) / (Decimal(self.qty_raw) / Decimal(10) ** self.decimals)


def to_lamports(size_sol):
    """Exact lamports for a SOL amount (int, str or Decimal; a float is taken via its repr). Sub-lamport sizes are refused."""
    if isinstance(size_sol, bool):
        raise PaperError('size must be a number')
    try:
        value = Decimal(str(size_sol)) if isinstance(size_sol, float) else Decimal(size_sol)
    except (InvalidOperation, TypeError, ValueError):
        raise PaperError('size must be a number') from None
    if not value.is_finite() or value <= 0:
        raise PaperError('size must be positive and finite')
    scaled = value * LAMPORTS
    if scaled != scaled.to_integral_value():
        raise PaperError('size is not a whole number of lamports')
    return int(scaled)


def _haircut(amount, bps):
    return amount * (MAX_BPS - bps) // MAX_BPS


def buy(quote, size_sol, cfg):
    """A paper BUY of ``size_sol`` SOL. The quote must be for exactly that spend."""
    if quote.side != 'buy':
        raise PaperError('a buy needs a buy quote')
    lamports = to_lamports(size_sol)
    if quote.in_amount != lamports:
        raise PaperError('the quote is not for this size')
    qty = _haircut(quote.out_amount, cfg.slippage_bps)
    if qty <= 0:
        raise PaperError('nothing would be received after slippage')
    return Fill(ts=quote.ts, mint=quote.mint, side='buy', qty_raw=qty, sol_lamports=lamports, fee_lamports=cfg.fee_lamports,
                slippage_bps=cfg.slippage_bps, decimals=quote.decimals, quote_ref=quote.ref)


def sell(position, quote, fraction, cfg):
    """A paper SELL of ``fraction`` (0 < f <= 1) of the position. The quote must be for exactly the quantity sold."""
    if quote.side != 'sell' or quote.mint != position.mint:
        raise PaperError('a sell needs a sell quote of the same mint')
    try:
        f = Decimal(str(fraction)) if isinstance(fraction, float) else Decimal(fraction)
    except (InvalidOperation, TypeError, ValueError):
        raise PaperError('fraction must be a number') from None
    if isinstance(fraction, bool) or not f.is_finite() or not 0 < f <= 1:
        raise PaperError('fraction must be in (0, 1]')
    qty = position.qty_raw if f == 1 else int(Decimal(position.qty_raw) * f)
    if qty <= 0:
        raise PaperError('fraction sells nothing')
    if quote.in_amount != qty:
        raise PaperError('the quote is not for this quantity')
    sol = _haircut(quote.out_amount, cfg.slippage_bps)
    cost_sold = position.cost_lamports if qty == position.qty_raw else position.cost_lamports * qty // position.qty_raw
    return Fill(ts=quote.ts, mint=quote.mint, side='sell', qty_raw=qty, sol_lamports=sol, fee_lamports=cfg.fee_lamports,
                slippage_bps=cfg.slippage_bps, decimals=position.decimals, cost_sold_lamports=cost_sold,
                realized_lamports=sol - cfg.fee_lamports - cost_sold, quote_ref=quote.ref)


def apply_fill(positions, cash, fill):
    """Pure transition: ``(positions, cash)`` after ``fill``. Raises AccountingHalt on anything that cannot be true.

    The single implementation of the accounting rules: the store replays stored fills through it and refuses a new fill that it rejects."""
    if fill.label != LABEL or fill.side not in ('buy', 'sell') or type(fill.qty_raw) is not int or fill.qty_raw <= 0:
        raise AccountingHalt('malformed fill')
    for name in ('sol_lamports', 'fee_lamports', 'cost_sold_lamports'):
        if type(getattr(fill, name)) is not int or getattr(fill, name) < 0:
            raise AccountingHalt(f'malformed fill: {name}')
    positions = dict(positions)
    held = positions.get(fill.mint)
    if fill.side == 'buy':
        spend = fill.sol_lamports + fill.fee_lamports
        if spend > cash:
            raise AccountingHalt('buy exceeds cash')
        if fill.cost_sold_lamports or fill.realized_lamports:
            raise AccountingHalt('a buy cannot carry realized pnl')
        if held is None:
            positions[fill.mint] = Position(fill.mint, fill.qty_raw, spend, fill.ts, 0, fill.decimals)
        else:
            if held.decimals != fill.decimals:
                raise AccountingHalt('token decimals changed')
            positions[fill.mint] = Position(fill.mint, held.qty_raw + fill.qty_raw, held.cost_lamports + spend, held.opened_at,
                                            held.realized_lamports, held.decimals)
        return positions, cash - spend
    if held is None:
        raise AccountingHalt('sell without a position')
    if fill.qty_raw > held.qty_raw:
        raise AccountingHalt('sell exceeds the position')
    expected_cost = held.cost_lamports if fill.qty_raw == held.qty_raw else held.cost_lamports * fill.qty_raw // held.qty_raw
    if fill.cost_sold_lamports != expected_cost or fill.realized_lamports != fill.sol_lamports - fill.fee_lamports - expected_cost:
        raise AccountingHalt('sell pnl does not reconcile with the cost basis')
    remaining = held.qty_raw - fill.qty_raw
    if remaining:
        positions[fill.mint] = Position(fill.mint, remaining, held.cost_lamports - expected_cost, held.opened_at,
                                        held.realized_lamports + fill.realized_lamports, held.decimals)
    else:
        del positions[fill.mint]
    after = cash + fill.sol_lamports - fill.fee_lamports
    if after < 0:
        raise AccountingHalt('sell would make cash negative')
    return positions, after
