from __future__ import annotations

from datetime import datetime, timezone

from .model import D, ONE, ZERO, decimal as dec, validate_event, digest
from .strategy import gates, scores, size, swap_quote
from .bundles import audit
from .security import entry_token_policy, sellability_gate


def initial_state(cfg):
    return {"cash": cfg["initial_equity_sol"], "realized_pnl": "0", "positions": {},
            "cooldowns": {}, "mode": "RUNNING", "last_ts": 0, "day": None,
            "day_start_equity": cfg["initial_equity_sol"], "day_gross_losses": "0",
            "peak_equity": cfg["initial_equity_sol"], "max_drawdown": "0",
            "last_entry_minute": -1, "loss_streak": 0}


def exposure(state):
    return sum((dec(p["cost_left"]) for p in state["positions"].values()), ZERO)


def equity(state):
    return dec(state["cash"]) + sum((dec(p["mark_value"]) for p in state["positions"].values()), ZERO)


def risk(state, cfg, output):
    eq = equity(state)
    peak = max(dec(state["peak_equity"]), eq)
    state["peak_equity"] = str(peak)
    state["max_drawdown"] = str(max(dec(state["max_drawdown"]), (peak - eq) / peak))
    daily_drawdown = max(ZERO, dec(state["day_start_equity"]) - eq)
    if daily_drawdown >= dec(cfg["daily_liquidate_fraction"]) * dec(state["day_start_equity"]):
        if state["mode"] not in ("LIQUIDATING", "STOPPED"):
            state["mode"] = "LIQUIDATING"
            output.append({"type": "control", "reason": "DAILY_LIQUIDATE"})
    elif any(p.get("exit_blocked") for p in state["positions"].values()):
        if state["mode"]=="RUNNING":
            state["mode"]="EXIT_ONLY"
            output.append({"type":"control","reason":"UNRESOLVED_EXIT_BLOCKS_ENTRY"})
    elif (daily_drawdown >= dec(cfg["daily_pause_fraction"]) * dec(state["day_start_equity"])
          or state["loss_streak"] >= 6):
        if state["mode"] == "RUNNING":
            state["mode"] = "EXIT_ONLY"
            output.append({"type": "control", "reason": "RISK_EXIT_ONLY"})


def _proof_hash(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _position_proof(p, e, qty, *, action=False):
    """Select exact evidence; never resize a valuation result into action proof.

    These records still require a future trusted adapter. Local source/hash
    binding does not authenticate provider data or promote diagnostic flags.
    """
    valuation = e.get("sellability")
    partial = action and qty != dec(p["qty"])
    synthetic = (p.get("provenance") == "SYNTHETIC_TEST_ONLY"
                 and e.get("provenance") == "SYNTHETIC_TEST_ONLY"
                 and isinstance(valuation, dict)
                 and valuation.get("kind") == "synthetic_model")
    paired = "action_sellability" in e
    proof = e.get("action_sellability") if partial and paired else valuation
    reasons = []
    if partial and not paired and not synthetic:
        reasons.append("EXIT_ACTION_PROOF_MISSING")
    try:
        reasons.extend(sellability_gate({**e, "sellability": proof}, qty))
    except (ValueError, TypeError, OverflowError):
        reasons.append("SELL_PROOF_MALFORMED")
    if p.get("provenance") != "SYNTHETIC_TEST_ONLY":
        if e["provenance"] == "SYNTHETIC_TEST_ONLY":
            reasons.append("SYNTHETIC_EXIT_ON_REAL_POSITION")
    if (p.get("provenance") != "SYNTHETIC_TEST_ONLY" or paired
            or "exit_source_hash" in e or "exit_revision_hash" in e):
        if not p.get("taker") or e.get("taker") != p["taker"]:
            reasons.append("POSITION_WALLET_MISMATCH")
    # An explicit pair has source identity and a hash of the full valuation
    # record. Source-bearing full exits also retain those checks.
    if paired or "exit_source_hash" in e or "exit_revision_hash" in e:
        for field, event_field in (("source_hash", "exit_source_hash"),
                                   ("revision_hash", "exit_revision_hash")):
            expected = e.get(event_field)
            if (not _proof_hash(expected) or not isinstance(proof, dict)
                    or proof.get(field) != expected):
                reasons.append("EXIT_" + field.upper() + "_MISMATCH")
        if (not isinstance(proof, dict) or proof.get("kind") != "sell_simulation"
                or proof.get("provenance") != e["provenance"]):
            reasons.append("EXIT_PROOF_PROVENANCE_MISMATCH")
        if partial:
            try:
                bound = isinstance(proof, dict) and proof.get("valuation_proof_hash") == digest(valuation)
            except (ValueError, TypeError, RecursionError):
                bound = False
            if not bound:
                reasons.append("EXIT_VALUATION_PROOF_BINDING_MISMATCH")
            if (isinstance(proof, dict) and isinstance(valuation, dict)
                    and proof.get("transaction_hash") == valuation.get("transaction_hash")):
                reasons.append("EXIT_ACTION_TRANSACTION_REUSED")
    return proof, reasons


def position_sellability(p, e, qty):
    return _position_proof(p, e, qty)[1]


def sell(state, e, cfg, fraction, reason, output):
    p = state["positions"][e["mint"]]
    qty = min(dec(p["qty"]), dec(p["initial_qty"]) * fraction)
    proof, evidence_reasons = _position_proof(p, e, qty, action=True)
    if qty != dec(p["qty"]):
        evidence_reasons.extend(_position_proof(p, e, dec(p["qty"]))[1])
    if evidence_reasons:
        p["exit_blocked"]="EXIT_SELLABILITY_UNVERIFIED"
        p["mark_status"]="UNVERIFIED_EXIT"
        output.append({"type":"blocked_exit","reason":"EXIT_SELLABILITY_UNVERIFIED",
                       "reasons":evidence_reasons,"mint":e["mint"]})
        return False
    gross = swap_quote(e, qty, "sell", cfg)
    fee = dec(cfg["fixed_fee_sol"])
    if gross is None or gross <= fee:
        p["exit_blocked"]="STUCK_POSITION"
        p["mark_status"]="UNVERIFIED_EXIT"
        output.append({"type": "blocked_exit", "reason": "STUCK_POSITION", "mint": e["mint"]})
        return False
    p["exit_blocked"]=None
    p["mark_status"]="MODEL_ESTIMATE"
    cost = dec(p["cost_left"]) * qty / dec(p["qty"])
    proceeds = gross - fee
    if proof.get("kind") == "sell_simulation":
        proceeds = min(proceeds, dec(proof["net_proceeds_sol"]))
    pnl = proceeds - cost
    p["qty"] = str(dec(p["qty"]) - qty)
    p["cost_left"] = str(dec(p["cost_left"]) - cost)
    p["trade_pnl"] = str(dec(p["trade_pnl"]) + pnl)
    state["cash"] = str(dec(state["cash"]) + proceeds)
    state["realized_pnl"] = str(dec(state["realized_pnl"]) + pnl)
    state["day_gross_losses"] = str(dec(state["day_gross_losses"]) + max(ZERO, -pnl))
    output.append({"type": "fill", "side": "sell", "mint": e["mint"], "reason": reason,
                   "quantity": str(qty), "proceeds_sol": str(proceeds), "fee_sol": str(fee),
                   "realized_pnl_sol": str(pnl), "simulation": "constant_product"})
    if dec(p["qty"]) <= D("1e-20"):
        state["loss_streak"] = state["loss_streak"] + 1 if dec(p["trade_pnl"]) < 0 else 0
        del state["positions"][e["mint"]]
        cooldown = cfg["stop_cooldown_seconds"] if reason == "STOP" else cfg["cooldown_seconds"]
        state["cooldowns"][e["mint"]] = e["ts"] + cooldown
    else:
        p["mark_value"] = str(max(ZERO, swap_quote(e, dec(p["qty"]), "sell", cfg) - fee))
        if proof.get("kind") == "sell_simulation":
            # The remaining model estimate is not an exact-size valuation proof.
            # Obtain new full-remaining evidence on the next observation.
            p["mark_status"] = "UNVERIFIED_EXIT"
            p["exit_blocked"] = "REMAINING_POSITION_VALUATION_REQUIRED"
    return True


def manage_position(state, e, cfg, output):
    p = state["positions"][e["mint"]]
    if e["ts"] - e["price_at"] > cfg["price_ttl_seconds"] or not e["route_available"]:
        p["exit_blocked"]="STALE_OR_UNAVAILABLE_EXIT"
        p["mark_status"]="STALE"
        output.append({"type": "blocked_exit", "reason": "STALE_OR_UNAVAILABLE_EXIT", "mint": e["mint"]})
        return
    valuation, evidence_reasons = _position_proof(p, e, dec(p["qty"]))
    if evidence_reasons:
        p["exit_blocked"]="EXIT_SELLABILITY_UNVERIFIED"
        p["mark_status"]="UNVERIFIED_EXIT"
        output.append({"type":"blocked_exit","reason":"EXIT_SELLABILITY_UNVERIFIED","mint":e["mint"],"reasons":evidence_reasons})
        return
    p["exit_blocked"]=None
    p["mark_status"]="MODEL_ESTIMATE"
    gross = swap_quote(e, dec(p["qty"]), "sell", cfg)
    mark = max(ZERO, gross - dec(cfg["fixed_fee_sol"]))
    if valuation.get("kind") == "sell_simulation":
        mark = min(mark, dec(valuation["net_proceeds_sol"]))
    p["mark_value"] = str(mark)
    p["mark_at"] = e["price_at"]
    ratio = dec(p["mark_value"]) / dec(p["cost_left"])
    p["peak_ratio"] = str(max(dec(p["peak_ratio"]), ratio))
    if ratio >= D("1.15"):
        p["touched_15"] = True
    risk(state, cfg, output)
    reason = None
    if state["mode"] == "LIQUIDATING":
        reason = "LIQUIDATE"
    elif e["danger"]:
        reason = "DANGER"
    elif ratio <= dec(p["stop_ratio"]):
        reason = "STOP"
    elif p["stage"] >= 3 and ratio <= dec(p["peak_ratio"]) * (ONE - dec(cfg["trailing_fraction"])):
        reason = "TRAILING_STOP"
    elif e["ts"] - p["opened_at"] >= cfg["max_hold_seconds"]:
        reason = "MAX_HOLD"
    elif not p["touched_15"] and e["ts"] - p["opened_at"] >= cfg["time_stop_seconds"]:
        reason = "TIME_STOP"
    if reason:
        sell(state, e, cfg, ONE, reason, output)
        return
    ladder = [(D("1.4"), D(".3"), ONE), (D("2"), D(".3"), D("1.4")),
              (D("3"), D(".2"), D("2"))]
    # One rung per observed quote. Requote on the next event; never reuse a quote for several fills.
    if p["stage"] < len(ladder):
        trigger, fraction, stop = ladder[p["stage"]]
        if ratio >= trigger and sell(state, e, cfg, fraction, "TAKE_PROFIT", output):
            p["stage"] += 1
            p["stop_ratio"] = str(stop)


def transition(state, e, cfg):
    validate_event(e)
    output = []
    if e["ts"] < state["last_ts"]:
        return state, [{"type": "reject", "reason": "OUT_OF_ORDER"}]
    state["last_ts"] = e["ts"]
    if e["kind"] == "clock":
        # A provider outage produces no market event. Expire marks without
        # synthesizing a price, sell fill, or new daily equity baseline.
        for mint,p in state["positions"].items():
            age=e["ts"]-p["mark_at"]
            if not 0<=age<=cfg["price_ttl_seconds"]:
                if p.get("mark_status")!="STALE" or not p.get("exit_blocked"):
                    output.append({"type":"blocked_exit","reason":"MARK_EXPIRED_WITHOUT_MARKET_DATA","mint":mint})
                p["mark_status"]="STALE"
                if not p.get("exit_blocked"):p["exit_blocked"]="STALE_OR_UNAVAILABLE_EXIT"
        if any(p.get("exit_blocked") for p in state["positions"].values()) and state["mode"]=="RUNNING":
            state["mode"]="EXIT_ONLY"
            output.append({"type":"control","reason":"UNRESOLVED_EXIT_BLOCKS_ENTRY"})
        return state,output
    day = datetime.fromtimestamp(e["ts"], timezone.utc).date().isoformat()
    if state["day"] != day:
        marks_current=all(not p.get("exit_blocked") and p.get("mark_status")=="MODEL_ESTIMATE"
            and 0<=e["ts"]-p["mark_at"]<=cfg["price_ttl_seconds"] for p in state["positions"].values())
        if marks_current:
            state["day"] = day
            state["day_start_equity"] = str(equity(state))
            state["day_gross_losses"] = "0"
        else:
            output.append({"type":"control","reason":"DAY_ROLLOVER_DEFERRED_UNVERIFIED_MARKS"})
    if e["kind"] == "control":
        mapping = {"PAUSE_ENTRY": "ENTRY_PAUSED", "EXIT_ONLY": "EXIT_ONLY",
                   "LIQUIDATE": "LIQUIDATING", "RESUME": "RUNNING"}
        state["mode"] = mapping[e["command"]]
        risk(state, cfg, output)
        return state, output + [{"type": "control", "reason": e["command"], "mode": state["mode"]}]
    mint = e["mint"]
    was_open = mint in state["positions"]
    if was_open:
        manage_position(state, e, cfg, output)
    risk(state, cfg, output)
    if state["mode"] == "LIQUIDATING" and not state["positions"]:
        state["mode"] = "STOPPED"
    if was_open:
        return state, output
    scored = scores(e)
    reasons = gates(e, cfg, scored)
    # Live adapters are unfinished. JSON assertions cannot authorize real-data fills.
    if e["provenance"] != "SYNTHETIC_TEST_ONLY":
        reasons.append("LIVE_FEATURE_ADAPTER_NOT_READY")
    reasons.extend(entry_token_policy(e))
    bundle_audit = audit(e.get("bundle_evidence"), e["ts"])
    reasons.extend(bundle_audit["reasons"])
    if state["mode"] != "RUNNING":
        reasons.append(state["mode"])
    if len(state["positions"]) >= cfg["max_positions"]:
        reasons.append("MAX_POSITIONS")
    if state["cooldowns"].get(mint, 0) > e["ts"]:
        reasons.append("COOLDOWN")
    if state["last_entry_minute"] == e["ts"] // 60:
        reasons.append("ENTRY_THROTTLE")
    if any(e["ts"] - p["mark_at"] > cfg["price_ttl_seconds"] for p in state["positions"].values()):
        reasons.append("STALE_PORTFOLIO")
    evidence = {k: str(v) for k, v in scored.items()}
    if reasons:
        return state, output + [{"type": "reject", "reason": reasons[0], "reasons": reasons,
                                 "mint": mint, "scores": evidence, "bundle_audit": bundle_audit}]
    budget = max(ZERO, dec(cfg["daily_pause_fraction"]) * dec(state["day_start_equity"])
                 - dec(state["day_gross_losses"]))
    allocated_risk = exposure(state) * dec(cfg["stress_loss_fraction"])
    amount, limits = size(e, cfg, scored["entry"], equity(state), dec(state["cash"]),
                          exposure(state), budget, allocated_risk)
    if state["loss_streak"] >= 4:
        amount /= 2
    if amount < dec(cfg["min_order_sol"]):
        return state, output + [{"type": "reject", "reason": "BELOW_MINIMUM", "mint": mint,
                                 "size_sol": str(amount), "limits": {k: str(v) for k, v in limits.items()}}]
    qty = swap_quote(e, amount, "buy", cfg)
    sale_reasons = sellability_gate(e, qty)
    if sale_reasons:
        return state, output + [{"type": "reject", "reason": sale_reasons[0], "reasons": sale_reasons, "mint": mint}]
    fee = dec(cfg["fixed_fee_sol"])
    expected_sell = swap_quote(e, qty, "sell", cfg)
    if e["sellability"].get("kind") == "sell_simulation":
        expected_sell = min(expected_sell, dec(e["sellability"]["net_proceeds_sol"]) + fee)
    # Reserve fees for entry plus all four ladder sells. The model excludes rent, which
    # must be provided by the mainnet simulator before any performance gate can pass.
    roundtrip = (amount - expected_sell + 5 * fee) / amount
    if roundtrip > dec(cfg["max_roundtrip_cost_fraction"]):
        return state, output + [{"type": "reject", "reason": "COST_BUDGET", "mint": mint,
                                 "estimated_cost_fraction": str(roundtrip)}]
    cost = amount + fee
    state["cash"] = str(dec(state["cash"]) - cost)
    state["positions"][mint] = {
        "qty": str(qty), "initial_qty": str(qty), "cost_left": str(cost),
        "initial_cost": str(cost), "trade_pnl": "0", "opened_at": e["ts"],
        "exit_blocked":None, "mark_status":"MODEL_ESTIMATE",
        "stage": 0, "stop_ratio": str(ONE - dec(cfg["stop_fraction"])),
        "peak_ratio": "1", "touched_15": False,
        "mark_value": str(max(ZERO, expected_sell - fee)), "mark_at": e["price_at"],
        "pool": e["pool"], "entry_scores": evidence, "provenance":e["provenance"], "taker":e.get("taker"),
    }
    state["last_entry_minute"] = e["ts"] // 60
    output.append({"type": "fill", "side": "buy", "mint": mint, "reason": "ENTRY",
                   "amount_sol": str(amount), "quantity": str(qty), "fee_sol": str(fee),
                   "scores": evidence, "estimated_cost_fraction": str(roundtrip),
                   "simulation": "constant_product", "provenance": e["provenance"],
                   "bundle_audit": bundle_audit})
    risk(state, cfg, output)
    return state, output
