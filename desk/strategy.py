"""Pure, versioned decision and sizing functions. Monetary inputs are SOL."""
from .model import D, ONE, ZERO, clip, decimal as dec


def scores(e):
    safety = clip(D(100) - 2 * max(ZERO, dec(e["top10_pct"]) - 15)
                  - 3 * max(ZERO, dec(e["dev_pct"]) - 3)
                  - D("2.5") * max(ZERO, dec(e["bundle_pct"]) - 5)
                  - 2 * max(ZERO, dec(e["cluster_pct"]) - 8)
                  - 40 * dec(e["fresh_wallet_ratio"]))
    momentum = 100 * (D(".35") * dec(e["net_buy_ratio"])
                     + D(".25") * min(ONE, dec(e["unique_buyers_5m"]) / 40)
                     + D(".20") * min(ONE, dec(e["volume_vs_liq"]) / D("1.5"))
                     + D(".20") * (ONE - dec(e["drawdown_from_high"]))) * (ONE - dec(e["wash_score"]))
    # Narrative disabled: original 35:30:25 weights renormalized to sum to 1.
    entry = (35 * safety + 30 * dec(e["flow"]) + 25 * momentum) / 90
    entry *= ONE - D(".6") * max(dec(e["manip_safety"]), dec(e["manip_flow"]))
    return {"safety": safety, "momentum": momentum, "flow": dec(e["flow"]), "entry": entry}


def gates(e, cfg, scored):
    reasons = []
    if e.get("venue") != "pumpswap" or not e["graduated"]:
        reasons.append("UNSUPPORTED_POOL")
    if not all(e[k] for k in ("mint_revoked", "freeze_revoked", "lp_verified", "extensions_safe")):
        reasons.append("UNVERIFIED_SAFETY")
    if not e["data_healthy"]:
        reasons.append("DATA_UNHEALTHY")
    if e["danger"]:
        reasons.append("DANGER")
    if not e["route_available"]:
        reasons.append("NO_ROUTE")
    age = e["ts"] - e["graduated_at"]
    if not cfg["min_age_seconds"] <= age <= cfg["max_age_seconds"]:
        reasons.append("AGE")
    if not dec(cfg["min_market_cap_usd"]) <= dec(e["market_cap_usd"]) <= dec(cfg["max_market_cap_usd"]):
        reasons.append("MARKET_CAP")
    if 2 * dec(e["reserve_sol"]) * dec(e["sol_usd"]) < dec(cfg["min_liquidity_usd"]):
        reasons.append("LIQUIDITY")
    if dec(e["dev_launches_7d"]) >= 3:
        reasons.append("REPEAT_DEPLOYER")
    for prefix in ("price", "holder", "flow", "momentum"):
        if e["ts"] - e[prefix + "_at"] > cfg[prefix + "_ttl_seconds"]:
            reasons.append("STALE_" + prefix.upper())
    if scored["safety"] < 55:
        reasons.append("SAFETY")
    if scored["momentum"] < 40 or dec(e["wash_score"]) > D(".3"):
        reasons.append("MOMENTUM_OR_WASH")
    threshold = 60 if e["flow_confirmed"] else 68
    if scored["entry"] < threshold:
        reasons.append("ENTRY_SCORE")
    return reasons


def size(e, cfg, score, equity, cash, exposure, daily_budget_left, reserved_risk):
    confidence = clip((score - 60) / 40, ZERO, ONE)
    stress = dec(cfg["stress_loss_fraction"])
    limits = {
        "capital": dec(cfg["max_position_fraction"]) * equity * confidence,
        "liquidity": dec(cfg["liquidity_fraction"]) * 2 * dec(e["reserve_sol"]),
        "trade_risk": dec(cfg["risk_per_trade_fraction"]) * equity / stress,
        "daily_risk": max(ZERO, daily_budget_left - reserved_risk) / stress,
        "cash": max(ZERO, cash - dec(cfg["fee_reserve_sol"]) - dec(cfg["fixed_fee_sol"])),
        "exposure": max(ZERO, dec(cfg["max_exposure_fraction"]) * equity - exposure),
    }
    result = max(ZERO, min(limits.values())).quantize(D(".000000001"), rounding="ROUND_DOWN")
    return result, limits


def swap_quote(e, amount, side, cfg):
    """Constant-product research fill; pool fees and impact already included.

    Additional adverse execution loss is a separate hypothesis, not measured data.
    Never use this as a substitute for a size-specific executable mainnet route.
    """
    if not e["route_available"]:
        return None
    r_sol, r_token = dec(e["reserve_sol"]), dec(e["reserve_tokens"])
    effective = amount * (ONE - dec(e["pool_fee_bps"]) / 10000)
    if side == "buy":
        out = r_token * effective / (r_sol + effective)
    else:
        out = r_sol * effective / (r_token + effective)
    return out * (ONE - dec(cfg["adverse_slippage_bps"]) / 10000)
