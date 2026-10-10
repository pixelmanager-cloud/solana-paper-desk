"""Deterministic market-regime entry gate (opt-in ``paper_regime_version: 1``). Paper only.

Memecoins move together, so NEW entries are reduced or blocked in bad regimes. Exits are never gated.

Replay determinism: the gate is a pure function of (market event, saved engine state, config). The
regime evidence travels INSIDE the event as ``event["regime"]``; nothing here reads a clock, store or
provider. Hysteresis uses the previous regime state saved in the engine state (``regime_state``, an
additive key, updated only from valid in-TTL evidence), so a restart or replay reproduces it exactly.

Evidence record (all numbers are decimal STRINGS, ``as_of`` an int epoch second <= event ts):
  {"version":1, "as_of":int, "graduations_per_hour":"12.5",   # discovery store, trailing window
   "sol_usd_change_pct":"-1.2",                                 # existing Kraken observations
   "median_forward_return":"-0.05"}                             # optional (T26 data); fraction
Score = MIN over present components, each in [0,1] (any single bad signal degrades the regime):
  graduation rate  = min(1, rate / grad_rate_normal)
  SOL/USD trend    = 1 if change >= 0 else max(0, 1 - drop / sol_drop_off_pct)
  forward return   = 1 if r >= 0 else max(0, 1 - drop / forward_floor)
States: NORMAL / CAUTION (size x caution_size_multiplier) / OFF (no entries), with hysteresis:
  NORMAL -> CAUTION below caution_enter, -> OFF below off_enter
  CAUTION -> NORMAL at/above caution_exit, -> OFF below off_enter
  OFF -> (CAUTION or NORMAL by caution_exit) only at/above off_exit
Missing, stale, future-dated or malformed evidence rejects the entry (fail closed) and leaves the
saved state unchanged.
"""
from decimal import Decimal, localcontext

NORMAL, CAUTION, OFF = "NORMAL", "CAUTION", "OFF"
DEFAULTS = {
    "ttl_seconds": 900,
    "grad_rate_normal": "6",          # graduations per hour regarded as a normal tape
    "sol_drop_off_pct": "5",          # SOL/USD fall (percent over the producer's window) scoring 0
    "forward_floor": "0.2",           # median forward return of -20% scores 0
    "caution_enter": "0.5", "caution_exit": "0.6",
    "off_enter": "0.25", "off_exit": "0.35",
    "caution_size_multiplier": "0.5",
}
_REQUIRED = {"version", "as_of", "graduations_per_hour", "sol_usd_change_pct"}
_OPTIONAL = {"median_forward_return"}
ZERO, ONE = Decimal(0), Decimal(1)


def enabled(cfg):
    version = cfg.get("paper_regime_version")
    if version is None:
        return False
    if type(version) is not int or version != 1:
        raise ValueError("Unsupported regime configuration")
    return True


def _dec(value, what):
    if type(value) is not str or not 1 <= len(value) <= 32:
        raise ValueError(f"{what} must be a decimal string")
    try:
        number = Decimal(value)
    except ArithmeticError:
        raise ValueError(f"{what} must be a decimal string") from None
    if not number.is_finite() or abs(number) > Decimal(10) ** 12:
        raise ValueError(f"{what} must be finite and bounded")
    return number


def policy(cfg):
    """Validated policy: DEFAULTS overridden by cfg['paper_regime_policy'] (unknown keys refused)."""
    override = cfg.get("paper_regime_policy", {})
    if type(override) is not dict or set(override) - set(DEFAULTS):
        raise ValueError("Invalid regime policy")
    merged = {**DEFAULTS, **override}
    ttl = merged["ttl_seconds"]
    if type(ttl) is not int or not 1 <= ttl <= 86400:
        raise ValueError("Invalid regime ttl")
    p = {k: _dec(v, k) for k, v in merged.items() if k != "ttl_seconds"}
    if not (ZERO <= p["off_enter"] < p["off_exit"] <= p["caution_enter"] < p["caution_exit"] <= ONE):
        raise ValueError("Regime thresholds must satisfy 0<=off_enter<off_exit<=caution_enter<caution_exit<=1")
    if not ZERO < p["caution_size_multiplier"] < ONE:
        raise ValueError("Invalid regime caution size multiplier")
    if p["grad_rate_normal"] <= ZERO or p["sol_drop_off_pct"] <= ZERO or p["forward_floor"] <= ZERO:
        raise ValueError("Invalid regime scale")
    return {"ttl_seconds": ttl, **p}


def validate_record(record, ts, pol):
    if type(record) is not dict or not _REQUIRED <= set(record) <= _REQUIRED | _OPTIONAL:
        raise ValueError("Regime evidence shape invalid")
    if type(record["version"]) is not int or record["version"] != 1:
        raise ValueError("Regime evidence version invalid")
    as_of = record["as_of"]
    if type(as_of) is not int or not 0 <= as_of <= ts:
        raise ValueError("Regime evidence time invalid or in the future")
    if ts - as_of > pol["ttl_seconds"]:
        raise ValueError("STALE")
    if _dec(record["graduations_per_hour"], "graduations_per_hour") < ZERO:
        raise ValueError("graduations_per_hour must be nonnegative")
    _dec(record["sol_usd_change_pct"], "sol_usd_change_pct")
    if "median_forward_return" in record:
        if _dec(record["median_forward_return"], "median_forward_return") < Decimal(-1):
            raise ValueError("median_forward_return below -100%")


def score(record, pol):
    with localcontext() as context:
        context.prec = 28
        components = [min(ONE, Decimal(record["graduations_per_hour"]) / pol["grad_rate_normal"])]
        change = Decimal(record["sol_usd_change_pct"])
        components.append(ONE if change >= ZERO else max(ZERO, ONE + change / pol["sol_drop_off_pct"]))
        if "median_forward_return" in record:
            forward = Decimal(record["median_forward_return"])
            components.append(ONE if forward >= ZERO else max(ZERO, ONE + forward / pol["forward_floor"]))
        return min(components)


def next_state(previous, value, pol):
    if previous not in (NORMAL, CAUTION, OFF):
        raise ValueError("Invalid saved regime state")
    if previous == OFF and value < pol["off_exit"]:
        return OFF
    if previous != OFF and value < pol["off_enter"]:
        return OFF
    if previous == NORMAL:
        return CAUTION if value < pol["caution_enter"] else NORMAL
    # CAUTION, or leaving OFF: climb to NORMAL only at/above caution_exit
    return NORMAL if value >= pol["caution_exit"] else CAUTION


def gate(event, cfg, state):
    """Entry-path gate. Returns (reasons, size_multiplier, info). Mutates state['regime_state'] only
    from valid evidence. Called for NEW entries only; exits never reach it."""
    pol = policy(cfg)
    record = event.get("regime")
    if record is None:
        return ["REGIME_EVIDENCE_REQUIRED"], ONE, None
    try:
        validate_record(record, event["ts"], pol)
    except ValueError as error:
        return ["REGIME_STALE" if str(error) == "STALE" else "REGIME_EVIDENCE_INVALID"], ONE, None
    try:
        value = score(record, pol)
    except ArithmeticError:
        return ["REGIME_EVIDENCE_INVALID"], ONE, None
    previous = state.get("regime_state", NORMAL)
    current = next_state(previous, value, pol)
    state["regime_state"] = current
    info = {"state": current, "previous": previous, "score": str(value), "as_of": record["as_of"]}
    if current == OFF:
        return ["REGIME_OFF"], ONE, info
    return [], pol["caution_size_multiplier"] if current == CAUTION else ONE, info
