"""Pure, versioned decision and sizing functions. Monetary inputs are SOL."""
from .model import (D, ONE, ZERO, clip, decimal as dec, digest, validate_event,
    PAPER_EXPERIMENTAL, EXPERIMENTAL_POLICY_VERSION, OWNERSHIP_METRICS,
    OWNERSHIP_HISTORY_RISK, experimental_ownership_unknowns, EXPERIMENTAL_HISTORY_FIELDS,
    EXPERIMENTAL_OBSERVABLE_POLICY_VERSION, observable_signal_profile)

EXPERIMENTAL_SCORE_VERSION = 'paper-experimental-flow-momentum-v1'



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
    return _gates(e, cfg, scored)


def _gates(e, cfg, scored, *, omitted_ownership=False, developer_unknown=False, holder_unknown=False, observable_proxy=False):
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
    if not developer_unknown and dec(e["dev_launches_7d"]) >= 3:
        reasons.append("REPEAT_DEPLOYER")
    for prefix in ("price", "holder", "flow", "momentum"):
        if prefix == "holder" and holder_unknown:continue
        if e["ts"] - e[prefix + "_at"] > cfg[prefix + "_ttl_seconds"]:
            reasons.append("STALE_" + prefix.upper())
    if not omitted_ownership and scored["safety"] < 55:
        reasons.append("SAFETY")
    if omitted_ownership and _known_safety_ceiling(e) < 55:
        # Even the most favorable missing ownership values cannot rescue the
        # measured hazard. This is a rejection bound, never an asserted score.
        reasons.append("KNOWN_OWNERSHIP_HAZARD")
    if scored["momentum"] < 40 or dec(e["wash_score"]) > D(".3"):
        reasons.append("MOMENTUM_OR_OBSERVED_CHURN" if observable_proxy else "MOMENTUM_OR_WASH")
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


def _known_safety_ceiling(e):
    penalties = (('top10_pct', D(2), D(15)), ('dev_pct', D(3), D(3)),
                 ('bundle_pct', D('2.5'), D(5)), ('cluster_pct', D(2), D(8)))
    measured = sum((weight * max(ZERO, dec(e[key]) - threshold)
                    for key, weight, threshold in penalties if e[key] is not None), ZERO)
    if e['fresh_wallet_ratio'] is not None:
        measured += D(40) * dec(e['fresh_wallet_ratio'])
    return D(100) - measured


def experimental_scores(e, *, mode, policy_version,token_profile_version=0):
    """JSON-persistable measured-component scores, not entry authorization.

    Explicit opt-in and version required. No numeric ownership defaults and no
    safety score when any ownership metric is UNKNOWN. All remaining model
    fields still validate; history-risk metadata is copied, not cleared.
    """
    if mode != PAPER_EXPERIMENTAL:
        raise ValueError('Explicit PAPER_EXPERIMENTAL mode required')
    validate_event(e, mode=mode, policy_version=policy_version,token_profile_version=token_profile_version)
    unknowns = experimental_ownership_unknowns(e)
    if policy_version == EXPERIMENTAL_OBSERVABLE_POLICY_VERSION:
        numeric = _history_profile_scores(_observable_view(e,token_profile_version=token_profile_version))
    elif policy_version == 2:
        numeric = _history_profile_scores(e)
    elif unknowns:
        momentum = D(100) * (D('.35') * dec(e['net_buy_ratio'])
            + D('.25') * min(ONE, dec(e['unique_buyers_5m']) / 40)
            + D('.20') * min(ONE, dec(e['volume_vs_liq']) / D('1.5'))
            + D('.20') * (ONE - dec(e['drawdown_from_high']))) * (ONE - dec(e['wash_score']))
        entry = (30 * dec(e['flow']) + 25 * momentum) / 55
        entry *= ONE - D('.6') * max(dec(e['manip_safety']), dec(e['manip_flow']))
        numeric = {'safety': None, 'momentum': momentum, 'flow': dec(e['flow']), 'entry': entry}
    else:
        numeric = scores(e)
    omitted = numeric['safety'] is None
    fields = OWNERSHIP_METRICS if policy_version == 1 else EXPERIMENTAL_HISTORY_FIELDS
    result = {'mode': PAPER_EXPERIMENTAL, 'policy_version': policy_version,
        'score_version': EXPERIMENTAL_SCORE_VERSION if policy_version == 1 else 'paper-experimental-history-components-v2', 'event_hash': digest(e),
        'risk_flags': [OWNERSHIP_HISTORY_RISK],
        'ownership_component': {'status': 'OMITTED' if omitted else 'MEASURED',
            'unknown_fields': {key: {'status': row['status'], 'reasons': list(row['reasons'])}
                               for key, row in unknowns.items()},
            'measured_fields': {key: e[key] for key in fields if key not in unknowns}},
        'entry_basis': 'FLOW_MOMENTUM_30_25' if omitted else 'FULL_MEASURED_COMPONENTS',
        **{key: str(value) if value is not None else None for key, value in numeric.items()},
        'confidence': str(clip((numeric['entry'] - 60) / 40, ZERO, ONE)),
        'entry_authorized': False, 'ownership_verified': False,
        'ownership_complete': False, 'source_authenticated': False}
    if policy_version in (2, EXPERIMENTAL_OBSERVABLE_POLICY_VERSION):
        result['omitted_components'] = sorted(unknowns)
        result['manipulation_safety'] = {'status': 'UNKNOWN' if e['manip_safety'] is None else 'MEASURED',
            'value': e['manip_safety'], 'reasons': list(unknowns.get('manip_safety', {}).get('reasons', []))}
        result['manipulation_penalty_basis'] = ['manip_flow'] + (['manip_safety'] if e['manip_safety'] is not None else [])
    if policy_version == EXPERIMENTAL_OBSERVABLE_POLICY_VERSION:
        import json
        from .model import canonical
        result['score_version']='paper-observable-effective-pricing-momentum-v3-profile2' if token_profile_version==2 else 'paper-observable-flow-momentum-v3'
        if token_profile_version==2:result['volume_feature']='volume_vs_effective_pricing_reserves'
        result['signal_profile']=json.loads(canonical(observable_signal_profile(e,token_profile_version=token_profile_version)))
        result['manipulation_penalty_basis']=['buyer_volume_concentration_proxy_v1'] + (['manip_safety'] if e['manip_safety'] is not None else [])
        result['risk_flags']=sorted(set(result['risk_flags'])|{'EXECUTION_UNVERIFIED','OBSERVABLE_PROXIES_NOT_SAFETY_PROOF'})
    return result


def _observable_view(e,token_profile_version=0):
    # Private arithmetic view only. Persisted event retains UNKNOWN legacy fields.
    p=observable_signal_profile(e,token_profile_version=token_profile_version)['measurements']
    from .model import BOOST_VOLUME_FEATURE
    return {**e,**({'volume_vs_liq':e[BOOST_VOLUME_FEATURE]} if token_profile_version==2 else {}),'flow':p['directional_flow_proxy_v1']['value'],
            'wash_score':p['same_wallet_churn_proxy_v1']['value'],
            'manip_flow':p['buyer_volume_concentration_proxy_v1']['value']}


def experimental_gates(e, cfg, *, mode, policy_version):
    """Existing strategy constraints with only unavailable ownership omitted.

    Recompute scores rather than accept a caller-crafted score object. Portfolio,
    exact quantity/sellability and roundtrip cost checks remain in engine; this
    function never calls it or grants entry. Existing size/swap_quote unchanged.
    """
    if cfg.get('mode') != 'paper':
        raise ValueError('Experimental gates require paper configuration')
    from .token2022_paper import selected
    token_profile_version=selected(cfg)
    result = experimental_scores(e, mode=mode, policy_version=policy_version,token_profile_version=token_profile_version)
    numeric = {key: dec(result[key]) if result[key] is not None else None
               for key in ('safety','momentum','flow','entry')}
    view=_observable_view(e,token_profile_version=token_profile_version) if policy_version == EXPERIMENTAL_OBSERVABLE_POLICY_VERSION else e
    return _gates(view, cfg, numeric, omitted_ownership=result['ownership_component']['status'] == 'OMITTED',
                  developer_unknown=policy_version in (2, EXPERIMENTAL_OBSERVABLE_POLICY_VERSION) and e['dev_launches_7d'] is None,
                  holder_unknown=policy_version == EXPERIMENTAL_OBSERVABLE_POLICY_VERSION and e['holder_at'] is None,
                  observable_proxy=policy_version == EXPERIMENTAL_OBSERVABLE_POLICY_VERSION)


def _history_profile_scores(e):
    """V2: preserve each history-dependent omission, no safe replacements.

    Only measured manipulation inputs contribute a penalty. Unavailable safety
    manipulation is UNKNOWN, not zero; its omission is separately persisted.
    Current flow/momentum and manip_flow are still required measured inputs.
    """
    safety = None if any(e[key] is None for key in OWNERSHIP_METRICS + ('fresh_wallet_ratio', 'manip_safety')) else clip(_known_safety_ceiling(e))
    momentum = D(100) * (D('.35') * dec(e['net_buy_ratio'])
        + D('.25') * min(ONE, dec(e['unique_buyers_5m']) / 40)
        + D('.20') * min(ONE, dec(e['volume_vs_liq']) / D('1.5'))
        + D('.20') * (ONE - dec(e['drawdown_from_high']))) * (ONE - dec(e['wash_score']))
    entry = (30 * dec(e['flow']) + 25 * momentum) / 55 if safety is None else (35 * safety + 30 * dec(e['flow']) + 25 * momentum) / 90
    measured_penalties = [dec(e['manip_flow'])]
    if e['manip_safety'] is not None:measured_penalties.append(dec(e['manip_safety']))
    entry *= ONE - D('.6') * max(measured_penalties)
    return {'safety': safety, 'momentum': momentum, 'flow': dec(e['flow']), 'entry': entry}
