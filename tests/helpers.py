import copy
import base64
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
T = 1791417600


def config():
    return json.loads((ROOT / "config/paper.json").read_text())


def clean_evidence(ts=T):
    return {"as_of": ts, "launch_history_complete": True, "funding_history_complete": True,
            "holder_coverage_pct": "100", "launch_slot": 1000,
            "holders": [{"wallet": f"w{i:03}", "pct": "1"} for i in range(100)],
            "early_buys": [{"wallet": f"w{i:03}", "slot": 1010 + i, "ts": ts - 300 + i} for i in range(100)],
            "funding": [], "transfers": [], "known_bad_wallets": []}


def bundled_evidence(ts=T):
    evidence = clean_evidence(ts)
    for i in range(12):
        evidence["early_buys"][i]["slot"] = 1000 + i % 4
        evidence["funding"].append({"source": "private-funder", "destination": f"w{i:03}",
                                    "ts": ts - 400, "source_kind": "private_verified"})
    return evidence


def event(ts=T, mint="SYNTHETIC_A", **changes):
    data = bytearray(82)
    data[36:44] = (1000000000000000).to_bytes(8, "little")
    data[44], data[45] = 6, 1
    e = {"schema_version": 1, "event_id": f"{mint}:{ts}", "kind": "market", "ts": ts,
         "mint": mint, "pool": "SYNTHETIC_POOL", "venue": "pumpswap", "provenance": "SYNTHETIC_TEST_ONLY",
         "graduated": True, "mint_revoked": True, "freeze_revoked": True, "lp_verified": True,
         "extensions_safe": True, "data_healthy": True, "flow_confirmed": False, "danger": False,
         "route_available": True, "graduated_at": T - 600,
         "price_at": ts, "holder_at": ts, "flow_at": ts, "momentum_at": ts,
         "reserve_sol": "100", "reserve_tokens": "1000000", "sol_usd": "150", "market_cap_usd": "100000",
         "pool_fee_bps": "125", "top10_pct": "10", "dev_pct": "0", "bundle_pct": "0", "cluster_pct": "0",
         "fresh_wallet_ratio": "0", "flow": "90", "manip_safety": "0", "manip_flow": "0",
         "net_buy_ratio": ".8", "unique_buyers_5m": 50, "volume_vs_liq": "2",
         "drawdown_from_high": "0", "wash_score": "0", "dev_launches_7d": 1,
         "bundle_evidence": clean_evidence(ts), "sellability": {"kind": "synthetic_model"},
         "token_evidence": {"mint": mint, "observed_at": ts, "account": {
             "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "executable": False,
             "data": [base64.b64encode(data).decode(), "base64"]}}}
    e.update(copy.deepcopy(changes))
    return e


def control(ts, command):
    return {"schema_version": 1, "event_id": f"control:{ts}", "ts": ts, "kind": "control",
            "command": command, "actor": "operator"}
