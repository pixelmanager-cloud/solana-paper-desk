from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
from decimal import localcontext

from .model import D, ONE, ZERO, decimal as dec, validate_event, digest
from .strategy import gates, scores, size, swap_quote, experimental_scores, experimental_gates
from .model import PAPER_EXPERIMENTAL
from .bundles import audit
from .security import entry_token_policy, sellability_gate
from . import quote_execution as qe
from . import regime


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


def sell(state, e, cfg, fraction, reason, output, quote_book=None):
    p = state["positions"][e["mint"]]
    if quote_book is not None:
        return _quote_sell(state,e,cfg,fraction,reason,output,quote_book)
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
                   "realized_pnl_sol": str(pnl), "simulation": "constant_product",
                   **({"entry_policy": deepcopy(p["entry_policy"])} if "entry_policy" in p else {})})
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


def _quote_sell(state,e,cfg,fraction,reason,output,book):
    p=state['positions'][e['mint']]
    try:
        if (p.get('taker')!=e.get('taker') or p['pool']!=e['pool'] or 'quote_execution' not in p
                or p['provenance']!=e['provenance']):
            raise qe.QuoteExecutionError('QUOTE_POSITION_IDENTITY_MISMATCH')
        current=qe.raw_quantity(p['qty'],book.decimals)
        initial=qe.raw_quantity(p['initial_qty'],book.decimals)
        from decimal import localcontext, ROUND_DOWN
        with localcontext() as ctx:
            ctx.prec=400
            raw=min(current,int((D(initial)*fraction).to_integral_value(rounding=ROUND_DOWN)))
            if raw<=0:raise qe.QuoteExecutionError('QUOTE_QUANTITY_ZERO')
            quote=book.find('sell',raw)
            # The action record cannot witness the pre-action full-size mark.
            valuation_record=(book.record(book.find('sell',current),cfg)
                if e.get('kind')=='quote_exit' and raw<current else None)
            proceeds=qe.units(qe.output_raw(quote,cfg),9)-dec(cfg['fixed_fee_sol'])
            if proceeds<=ZERO:raise qe.QuoteExecutionError('QUOTE_NO_NET_PROCEEDS')
            qty=qe.units(raw,book.decimals)
            cost=dec(p['cost_left'])*D(raw)/D(current)
            pnl=proceeds-cost
            p['qty']=str(qe.units(current-raw,book.decimals))
            p['cost_left']=str(dec(p['cost_left'])-cost)
            p['trade_pnl']=str(dec(p['trade_pnl'])+pnl)
            state['cash']=str(dec(state['cash'])+proceeds)
            state['realized_pnl']=str(dec(state['realized_pnl'])+pnl)
            state['day_gross_losses']=str(dec(state['day_gross_losses'])+max(ZERO,-pnl))
    except (ValueError,TypeError,KeyError):
        p['exit_blocked']='EXACT_FRESH_SELL_QUOTE_REQUIRED';p['mark_status']='UNVERIFIED_EXIT'
        output.append({'type':'blocked_exit','reason':'EXACT_FRESH_SELL_QUOTE_REQUIRED','mint':e['mint'],
            'quote_demands':[{'direction':'sell','input_raw':raw}] if 'raw' in locals() and raw>0 else []})
        return False
    p['last_quote_execution']=book.record(quote,cfg)
    output.append({'type':'fill','side':'sell','mint':e['mint'],'reason':reason,
        'quantity':str(qty),'proceeds_sol':str(proceeds),'fee_sol':str(dec(cfg['fixed_fee_sol'])),
        'realized_pnl_sol':str(pnl),'simulation':'quote_minimum_with_adverse_slippage',
        'provenance':e['provenance'],
        'execution_status':qe.STATUS,'quote_execution':book.record(quote,cfg),
        **({'valuation_quote_execution':valuation_record} if valuation_record is not None else {}),
        **({'entry_policy':deepcopy(p['entry_policy'])} if 'entry_policy' in p else {})})
    if current==raw:
        state['loss_streak']=state['loss_streak']+1 if dec(p['trade_pnl'])<0 else 0
        del state['positions'][e['mint']]
        cooldown=cfg['stop_cooldown_seconds'] if reason=='STOP' else cfg['cooldown_seconds']
        state['cooldowns'][e['mint']]=e['ts']+cooldown
    else:
        # Exact action quote cannot be resized into a remaining-position mark.
        p['mark_status']='UNVERIFIED_EXIT';p['exit_blocked']='REMAINING_POSITION_VALUATION_REQUIRED'
        p['mark_value']='0'
    return True


def manage_position(state, e, cfg, output, quote_book=None):
    p = state["positions"][e["mint"]]
    if e["ts"] - e["price_at"] > cfg["price_ttl_seconds"] or not e["route_available"]:
        p["exit_blocked"]="STALE_OR_UNAVAILABLE_EXIT"
        p["mark_status"]="STALE"
        output.append({"type": "blocked_exit", "reason": "STALE_OR_UNAVAILABLE_EXIT", "mint": e["mint"]})
        return
    if quote_book is not None:
        try:
            if (p.get('taker') != e.get('taker') or p['pool'] != e['pool'] or 'quote_execution' not in p
                    or p['provenance']!=e['provenance']):
                raise qe.QuoteExecutionError('QUOTE_POSITION_IDENTITY_MISMATCH')
            valuation=quote_book.find('sell',qe.raw_quantity(p['qty'],quote_book.decimals))
            mark=qe.units(qe.output_raw(valuation,cfg),9)-dec(cfg['fixed_fee_sol'])
            p['last_quote_execution']=quote_book.record(valuation,cfg)
            evidence_reasons=[]
        except (ValueError,TypeError,KeyError):
            valuation=None;evidence_reasons=['EXACT_FRESH_SELL_QUOTE_REQUIRED']
    else:
        valuation, evidence_reasons = _position_proof(p, e, dec(p["qty"]))
    if evidence_reasons:
        p["exit_blocked"]="EXIT_SELLABILITY_UNVERIFIED"
        p["mark_status"]="UNVERIFIED_EXIT"
        output.append({"type":"blocked_exit","reason":"EXIT_SELLABILITY_UNVERIFIED","mint":e["mint"],"reasons":evidence_reasons})
        if quote_book is not None:
            output[-1]['quote_demands']=[{'direction':'sell','input_raw':qe.raw_quantity(
                p['qty'],p['quote_execution']['mint_decimals'])}]
        return
    p["exit_blocked"]=None
    p["mark_status"]="MODEL_ESTIMATE"
    if quote_book is None:
        gross = swap_quote(e, dec(p["qty"]), "sell", cfg)
        mark = max(ZERO, gross - dec(cfg["fixed_fee_sol"]))
        if valuation.get("kind") == "sell_simulation":
            mark = min(mark, dec(valuation["net_proceeds_sol"]))
    p["mark_value"] = str(max(ZERO,mark))
    p["mark_at"] = valuation.source.observed_at if quote_book is not None else e["price_at"]
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
        sell(state, e, cfg, ONE, reason, output,quote_book)
        return
    ladder = [(D("1.4"), D(".3"), ONE), (D("2"), D(".3"), D("1.4")),
              (D("3"), D(".2"), D("2"))]
    # One rung per observed quote. Requote on the next event; never reuse a quote for several fills.
    if p["stage"] < len(ladder):
        trigger, fraction, stop = ladder[p["stage"]]
        if ratio >= trigger and sell(state, e, cfg, fraction, "TAKE_PROFIT", output,quote_book):
            p["stage"] += 1
            p["stop_ratio"] = str(stop)


def transition(state, e, cfg, *, _quote_book=None):
    # Config is fingerprinted by Ledger: selecting this experimental strategy
    # requires a new experiment, never an event-provided permission flag.
    quote_mode=qe.config(cfg)
    from .kraken_usd_observation import validate_event as validate_usd
    validate_usd(e,cfg)
    if quote_mode:
        for mint,position in state['positions'].items():
            qe.validate_position(mint,position,cfg)
    if _quote_book is not None and (not quote_mode or type(_quote_book) is not qe._Book or _quote_book.event_hash!=digest(e)):
        raise qe.QuoteExecutionError('QUOTE_EVENT_BINDING_MISMATCH')
    if (quote_mode and _quote_book is not None and _quote_book.decimals is not None
            and e.get('mint') in state['positions']
            and _quote_book.decimals != state['positions'][e['mint']]['quote_execution']['mint_decimals']):
        raise qe.QuoteExecutionError('QUOTE_POSITION_DECIMALS_MISMATCH')
    if quote_mode and e.get('kind') in ('market','quote_exit') and _quote_book is None:
        # Opted-in experiments never fall back to model execution, even when
        # directly invoked without the trusted coordinator quote binder.
        _quote_book=qe._Book(digest(e),(),None)
    version = cfg.get("experimental_policy_version")
    signal_version=cfg.get('paper_signal_policy_version')
    if signal_version is not None:
        if (type(signal_version) is not int or signal_version!=3 or cfg.get('mode')!='paper'
                or (version is not None and (type(version) is not int or version!=3)) or not quote_mode):
            raise ValueError('Unsupported observable paper signal configuration')
        version=signal_version
    elif version is not None and (type(version) is not int or version not in (1, 2) or cfg.get("mode") != "paper"):
        raise ValueError("Unsupported experimental paper policy configuration")
    # V3 is dispatched to worker09's explicit observable proxy validator; this
    # engine never substitutes values for legacy null flow/wash/history fields.
    # Until that validator is installed, validate_event rejects V3 unchanged.
    experimental = version in (1, 2, 3) and e.get("kind") == "market"
    if experimental:
        validate_event(e, mode=PAPER_EXPERIMENTAL, policy_version=version,token_profile_version=qe.selected(cfg))
    else:
        validate_event(e,token_profile_version=qe.selected(cfg))
    if e.get('kind')=='quote_exit':
        if not quote_mode:raise qe.QuoteExecutionError('QUOTE_EXECUTION_CONFIG_REQUIRED')
        if e['mint'] not in state['positions']:
            return state,[{'type':'reject','reason':'EXIT_POSITION_REQUIRED','mint':e['mint']}]
        held=state['positions'][e['mint']]
        if (held['pool']!=e['pool'] or held['taker']!=e['taker'] or held['provenance']!=e['provenance']
                or held['quote_execution']['mint_decimals']!=e['mint_decimals']
                or qe.raw_quantity(held['qty'],e['mint_decimals'])!=e['current_quantity_raw']):
            raise qe.QuoteExecutionError('EXIT_POSITION_BINDING_MISMATCH')
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
        manage_position(state, e, cfg, output,_quote_book)
    risk(state, cfg, output)
    if state["mode"] == "LIQUIDATING" and not state["positions"]:
        state["mode"] = "STOPPED"
    if was_open:
        return state, output
    with localcontext() as policy_context:
        # Preserve the existing policy journal representation. High precision
        # raw-unit accounting must not alter checkpoint-recomputed score strings.
        if quote_mode:policy_context.prec=28
        policy = experimental_scores(e, mode=PAPER_EXPERIMENTAL, policy_version=version,token_profile_version=qe.selected(cfg)) if experimental else None
        scored = ({key: dec(policy[key]) if policy[key] is not None else None
                   for key in ("safety", "momentum", "flow", "entry")} if policy else scores(e))
        reasons = (experimental_gates(e, cfg, mode=PAPER_EXPERIMENTAL, policy_version=version)
                   if experimental else gates(e, cfg, scored))
    # Live adapters are unfinished. JSON assertions cannot authorize real-data fills.
    if e["provenance"] != "SYNTHETIC_TEST_ONLY" and not quote_mode:
        reasons.append("LIVE_FEATURE_ADAPTER_NOT_READY")
    reasons.extend(entry_token_policy(e,cfg))
    regime_multiplier, regime_info = ONE, None
    if regime.enabled(cfg):
        regime_reasons, regime_multiplier, regime_info = regime.gate(e, cfg, state)
        reasons.extend(regime_reasons)
    if signal_version==3 and 'bundle_evidence' not in e:
        # Explicit approved V3 omission only. Never manufacture complete bundle
        # history or safe percentages; any supplied evidence still runs audit.
        bundle_audit={'decision':'UNKNOWN','status':'UNKNOWN','reasons':['BUNDLE_HISTORY_UNAVAILABLE'],
            'risk_flags':['UNRESOLVED_OWNERSHIP_HISTORY'],'ownership_complete':False,
            'entry_authorized':False,'source_authenticated':False}
    else:
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
    evidence = {k: str(v) if v is not None else None for k, v in scored.items()}
    policy_record = {"entry_policy": deepcopy(policy)} if policy is not None else {}
    if reasons:
        return state, output + [{"type": "reject", "reason": reasons[0], "reasons": reasons,
                                 "mint": mint, "scores": evidence, "bundle_audit": bundle_audit, **policy_record,
                                 **({"regime": regime_info} if regime_info else {})}]
    budget = max(ZERO, dec(cfg["daily_pause_fraction"]) * dec(state["day_start_equity"])
                 - dec(state["day_gross_losses"]))
    allocated_risk = exposure(state) * dec(cfg["stress_loss_fraction"])
    amount, limits = size(e, cfg, scored["entry"], equity(state), dec(state["cash"]),
                          exposure(state), budget, allocated_risk)
    if state["loss_streak"] >= 4:
        amount /= 2
    if regime_multiplier != ONE:
        amount *= regime_multiplier
    if quote_mode:
        amount=qe.units(int((amount*10**9).to_integral_value(rounding='ROUND_DOWN')),9)
    if amount < dec(cfg["min_order_sol"]):
        return state, output + [{"type": "reject", "reason": "BELOW_MINIMUM", "mint": mint,
                                 "size_sol": str(amount), "limits": {k: str(v) for k, v in limits.items()}}]
    if quote_mode:
        demands=[{'direction':'buy','maximum_input_raw':qe.raw_quantity(amount,9),
                  'minimum_input_raw':qe.raw_quantity(cfg['min_order_sol'],9)}]
        try:
            quote=_quote_book.buy()
            if quote.input_units>amount or quote.input_units<dec(cfg['min_order_sol']):
                raise qe.QuoteExecutionError('QUOTE_EXCEEDS_RISK_ALLOCATION')
            amount=quote.input_units
            qty=qe.units(qe.output_raw(quote,cfg),_quote_book.decimals)
            demands=[{'direction':'sell','input_raw':qe.output_raw(quote,cfg)}]
            exit_quote=_quote_book.find('sell',qe.output_raw(quote,cfg))
            expected_sell=qe.units(qe.output_raw(exit_quote,cfg),9)
            if qty<=ZERO:raise qe.QuoteExecutionError('QUOTE_OUTPUT_ZERO')
        except (ValueError,TypeError):
            return state,output+[{'type':'reject','reason':'EXACT_FRESH_ROUNDTRIP_QUOTES_REQUIRED','mint':mint,
                                 'quote_demands':demands}]
    else:
        qty = swap_quote(e, amount, "buy", cfg)
    sale_reasons = [] if quote_mode else sellability_gate(e, qty)
    if sale_reasons:
        return state, output + [{"type": "reject", "reason": sale_reasons[0], "reasons": sale_reasons, "mint": mint}]
    fee = dec(cfg["fixed_fee_sol"])
    if not quote_mode:
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
        "pool": e["pool"], "entry_scores": evidence, "provenance":e["provenance"], "taker":e.get("taker"), **deepcopy(policy_record),
        **({"entry_event_id": e["event_id"]} if experimental else {}),
        **({'quote_execution':_quote_book.record(quote,cfg),
            'last_quote_execution':_quote_book.record(exit_quote,cfg)} if quote_mode else {}),
    }
    state["last_entry_minute"] = e["ts"] // 60
    output.append({"type": "fill", "side": "buy", "mint": mint, "reason": "ENTRY",
                   "amount_sol": str(amount), "quantity": str(qty), "fee_sol": str(fee),
                   "scores": evidence, "estimated_cost_fraction": str(roundtrip),
                   "simulation": "quote_minimum_with_adverse_slippage" if quote_mode else "constant_product", "provenance": e["provenance"],
                   **({"regime": regime_info} if regime_info else {}),
                   **({'quote_execution':_quote_book.record(quote,cfg),
                       **({'roundtrip_quote_execution':_quote_book.record(exit_quote,cfg)} if qe.selected(cfg)==2 else {}),
                       'execution_status':qe.STATUS} if quote_mode else {}),
                   "bundle_audit": bundle_audit, **policy_record})
    risk(state, cfg, output)
    return state, output
