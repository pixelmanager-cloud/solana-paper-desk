"""Conservative Solana token policy. Unknown and Token-2022 programs are rejected in v0.1."""
import base64
import binascii

from .model import decimal as dec

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def base58(data):
    n = int.from_bytes(data, "big")
    result = ""
    while n:
        n, r = divmod(n, 58)
        result = ALPHABET[r] + result
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + result


def account_bytes(account):
    data = account.get("data")
    if not isinstance(data, list) or len(data) != 2 or data[1] != "base64":
        raise ValueError("base64 account data required")
    try:
        return base64.b64decode(data[0], validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("invalid account encoding") from None


def mint_policy(account):
    if not isinstance(account, dict):
        return {"decision": "SKIP", "reasons": ["MINT_ACCOUNT_MISSING"]}
    owner = account.get("owner")
    if owner != TOKEN_PROGRAM:
        reason = "TOKEN_2022_NOT_ALLOWED" if owner == TOKEN_2022 else "UNKNOWN_TOKEN_PROGRAM"
        return {"decision": "SKIP", "reasons": [reason], "owner": owner}
    data = account_bytes(account)
    reasons = []
    if len(data) != 82 or account.get("executable") is not False:
        return {"decision": "SKIP", "reasons": ["INVALID_MINT_LAYOUT"]}
    mint_option = int.from_bytes(data[0:4], "little")
    freeze_option = int.from_bytes(data[46:50], "little")
    if mint_option not in (0, 1) or freeze_option not in (0, 1) or data[45] != 1:
        reasons.append("INVALID_MINT_STATE")
    if mint_option:
        reasons.append("ACTIVE_MINT_AUTHORITY")
    if freeze_option:
        reasons.append("ACTIVE_FREEZE_AUTHORITY")
    supply = int.from_bytes(data[36:44], "little")
    if supply == 0:
        reasons.append("ZERO_SUPPLY")
    return {"decision": "SKIP" if reasons else "PASS_TOKEN_POLICY", "reasons": reasons,
            "program": owner, "decimals": data[44], "supply_raw": str(supply),
            "mint_authority": base58(data[4:36]) if mint_option else None,
            "freeze_authority": base58(data[50:82]) if freeze_option else None,
            "notice": "Token-policy pass does not prove sellability, liquidity safety or honest ownership."}


def holding_policy(account, mint, wallet):
    reasons = []
    if not isinstance(account, dict) or account.get("owner") != TOKEN_PROGRAM:
        return {"decision": "SKIP", "reasons": ["UNSUPPORTED_HOLDING_ACCOUNT"]}
    data = account_bytes(account)
    if len(data) != 165 or account.get("executable") is not False:
        return {"decision": "SKIP", "reasons": ["INVALID_HOLDING_LAYOUT"]}
    if base58(data[:32]) != mint or base58(data[32:64]) != wallet:
        reasons.append("HOLDING_IDENTITY_MISMATCH")
    if data[108] != 1:
        reasons.append("FROZEN_OR_UNINITIALIZED_HOLDING")
    if int.from_bytes(data[72:76], "little") != 0:
        reasons.append("TOKEN_ACCOUNT_DELEGATE")
    close_option = int.from_bytes(data[129:133], "little")
    if close_option not in (0, 1) or (close_option and base58(data[133:165]) != wallet):
        reasons.append("EXTERNAL_CLOSE_AUTHORITY")
    return {"decision": "SKIP" if reasons else "PASS_HOLDING_POLICY", "reasons": reasons,
            "amount_raw": str(int.from_bytes(data[64:72], "little"))}


def entry_token_policy(e):
    evidence = e.get("token_evidence")
    if not isinstance(evidence, dict) or evidence.get("mint") != e["mint"]:
        return ["TOKEN_EVIDENCE_MISSING_OR_MISMATCHED"]
    at = evidence.get("observed_at")
    if type(at) is not int or not 0 <= e["ts"] - at <= 10:
        return ["TOKEN_EVIDENCE_STALE"]
    return mint_policy(evidence.get("account"))["reasons"]


def sellability_gate(e, quantity):
    proof = e.get("sellability")
    if not isinstance(proof, dict):
        return ["SELLABILITY_UNKNOWN"]
    if proof.get("kind") == "synthetic_model":
        return [] if e["provenance"] == "SYNTHETIC_TEST_ONLY" else ["SYNTHETIC_PROOF_ON_REAL_DATA"]
    # Future trusted adapter must create this evidence only after validating the actual
    # transaction, exact input quantity, wallet identity and simulated net balance delta.
    if proof.get("kind") != "sell_simulation":
        return ["SELL_SIMULATION_REQUIRED"]
    reasons = []
    if (proof.get("mint") != e["mint"] or not e.get("taker")
            or proof.get("wallet") != e.get("taker")):
        reasons.append("SELL_IDENTITY_MISMATCH")
    for field in ("transaction_hash", "route_hash"):
        value = proof.get(field)
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            reasons.append("SELL_" + field.upper() + "_MISSING")
    if type(proof.get("slot")) is not int or proof["slot"] < 0:
        reasons.append("SELL_SLOT_MISSING")
    at = proof.get("observed_at")
    if type(at) is not int or not 0 <= e["ts"] - at <= 10:
        reasons.append("SELL_PROOF_STALE")
    if dec(proof.get("quantity_tokens", 0)) != quantity:
        reasons.append("SELL_PROOF_SIZE_MISMATCH")
    for field in ("simulation_ok", "transaction_policy_ok", "wallet_account_ok"):
        if proof.get(field) is not True:
            reasons.append("SELL_" + field.upper())
    if dec(proof.get("net_proceeds_sol", 0)) <= 0:
        reasons.append("SELL_NO_NET_PROCEEDS")
    return reasons
