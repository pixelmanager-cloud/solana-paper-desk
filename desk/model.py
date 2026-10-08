from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

D = Decimal
ZERO = D(0)
ONE = D(1)


def decimal(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    try:
        result = D(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if not result.is_finite():
        raise ValueError("non-finite number")
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def clip(value, low=ZERO, high=D(100)):
    return max(low, min(high, value))


def load_config(path):
    cfg = json.loads(Path(path).read_text())
    if cfg.get("mode") != "paper":
        raise ValueError("This build supports paper mode only")
    numeric = [k for k in cfg if k not in ("version", "mode")]
    for key in numeric:
        if decimal(cfg[key]) <= 0:
            raise ValueError(f"config {key} must be positive")
    if decimal(cfg["fee_reserve_sol"]) >= decimal(cfg["initial_equity_sol"]):
        raise ValueError("fee reserve must be below paper equity")
    for key in cfg:
        if key.endswith("fraction") and decimal(cfg[key]) >= 1:
            raise ValueError(f"config {key} must be below 1")
    if decimal(cfg["daily_pause_fraction"]) >= decimal(cfg["daily_liquidate_fraction"]):
        raise ValueError("pause threshold must precede liquidation")
    return cfg


def validate_event(event):
    if event.get("schema_version") != 1:
        raise ValueError("unsupported schema_version")
    if not isinstance(event.get("event_id"), str) or not event["event_id"]:
        raise ValueError("event_id is required")
    if type(event.get("ts")) is not int or event["ts"] < 0:
        raise ValueError("ts must be integer UTC epoch seconds")
    if event.get("kind") == "clock":
        if event.get("actor") != "paper_monitor":raise ValueError("paper monitor clock required")
        if set(event)!={"schema_version","event_id","ts","kind","actor"}:raise ValueError("Clock event cannot contain market assertions")
        return
    if event.get("kind") == "control":
        if event.get("command") not in ("PAUSE_ENTRY", "EXIT_ONLY", "LIQUIDATE", "RESUME"):
            raise ValueError("unknown control command")
        if event.get("actor") != "operator":
            raise ValueError("operator control required")
        return
    if event.get("kind") != "market":
        raise ValueError("unknown event kind")
    for key in ("mint", "pool", "provenance"):
        if not isinstance(event.get(key), str) or not event[key]:
            raise ValueError(f"{key} required")
    for key in ("graduated", "mint_revoked", "freeze_revoked", "lp_verified",
                "extensions_safe", "data_healthy", "flow_confirmed", "danger", "route_available"):
        if type(event.get(key)) is not bool:
            raise ValueError(f"{key} must be an explicit boolean; unknown is not safe")
    for key in ("price_at", "holder_at", "flow_at", "momentum_at", "graduated_at"):
        if type(event.get(key)) is not int or event[key] < 0 or event[key] > event["ts"]:
            raise ValueError(f"{key} invalid or in the future")
    for key in ("reserve_sol", "reserve_tokens", "sol_usd", "market_cap_usd"):
        if decimal(event.get(key)) <= 0:
            raise ValueError(f"{key} must be positive")
    for key in ("top10_pct", "dev_pct", "bundle_pct", "cluster_pct", "flow"):
        if not ZERO <= decimal(event.get(key)) <= 100:
            raise ValueError(f"{key} must be 0..100")
    for key in ("fresh_wallet_ratio", "manip_safety", "manip_flow", "net_buy_ratio",
                "drawdown_from_high", "wash_score"):
        if not ZERO <= decimal(event.get(key)) <= ONE:
            raise ValueError(f"{key} must be 0..1")
    for key in ("unique_buyers_5m", "volume_vs_liq", "dev_launches_7d"):
        if decimal(event.get(key)) < 0:
            raise ValueError(f"{key} must be nonnegative")
    if not ZERO <= decimal(event.get("pool_fee_bps")) < 10000:
        raise ValueError("pool fee must be 0..9999 bps")
