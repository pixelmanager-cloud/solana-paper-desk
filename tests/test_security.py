import base64
import unittest

from desk.security import TOKEN_2022, TOKEN_PROGRAM, base58, holding_policy, mint_policy, sellability_gate
from desk.model import D
from tests.helpers import T, event


class SecurityTests(unittest.TestCase):
    def account(self, data, owner=TOKEN_PROGRAM):
        return {"owner": owner, "executable": False, "data": [base64.b64encode(data).decode(), "base64"]}

    def mint(self):
        return event()["token_evidence"]["account"]

    def test_standard_revoked_mint_passes_only_token_policy(self):
        self.assertEqual(mint_policy(self.mint())["decision"], "PASS_TOKEN_POLICY")

    def test_freeze_and_mint_authorities_rejected(self):
        data = bytearray(base64.b64decode(self.mint()["data"][0]))
        data[0], data[46] = 1, 1
        result = mint_policy(self.account(data))
        self.assertIn("ACTIVE_FREEZE_AUTHORITY", result["reasons"])
        self.assertIn("ACTIVE_MINT_AUTHORITY", result["reasons"])

    def test_token2022_and_unknown_programs_rejected(self):
        for owner in (TOKEN_2022, "unexpected"):
            account = self.mint()
            account["owner"] = owner
            self.assertEqual(mint_policy(account)["decision"], "SKIP")

    def test_malformed_account_rejected(self):
        self.assertEqual(mint_policy(self.account(bytes(165)))["decision"], "SKIP")
        with self.assertRaises(ValueError):
            mint_policy({"owner": TOKEN_PROGRAM, "data": ["not-base64", "base64"]})

    def test_frozen_delegated_or_wrong_owner_holding_rejected(self):
        data = bytearray(165)
        data[:32] = bytes([1]) * 32
        data[32:64] = bytes([2]) * 32
        mint, wallet = base58(data[:32]), base58(data[32:64])
        data[108] = 1
        self.assertEqual(holding_policy(self.account(data), mint, wallet)["decision"], "PASS_HOLDING_POLICY")
        data[108], data[72] = 2, 1
        result = holding_policy(self.account(data), mint, wallet)
        self.assertIn("FROZEN_OR_UNINITIALIZED_HOLDING", result["reasons"])
        self.assertIn("TOKEN_ACCOUNT_DELEGATE", result["reasons"])
        self.assertIn("HOLDING_IDENTITY_MISMATCH", holding_policy(self.account(data), mint, "other")["reasons"])

    def test_quote_alone_is_not_sellability_proof(self):
        e = event(sellability={"kind": "quote"})
        self.assertEqual(sellability_gate(e, D(100)), ["SELL_SIMULATION_REQUIRED"])

    def test_synthetic_proof_cannot_be_used_on_live_data(self):
        self.assertEqual(sellability_gate(event(provenance="helius"), D(100)), ["SYNTHETIC_PROOF_ON_REAL_DATA"])

    def test_simulation_is_bound_to_mint_size_and_time(self):
        proof = {"kind": "sell_simulation", "mint": "SYNTHETIC_A", "wallet": "wallet",
                 "observed_at": T, "quantity_tokens": "100", "simulation_ok": True,
                 "transaction_policy_ok": True, "wallet_account_ok": True, "net_proceeds_sol": ".01",
                 "slot": 100, "transaction_hash": "a" * 64, "route_hash": "b" * 64}
        self.assertEqual(sellability_gate(event(sellability=proof, taker="wallet"), D(100)), [])
        self.assertIn("SELL_PROOF_SIZE_MISMATCH", sellability_gate(event(sellability=proof, taker="wallet"), D(101)))
        self.assertIn("SELL_IDENTITY_MISMATCH", sellability_gate(event(sellability=proof, taker="another_wallet"), D(100)))
        proof["observed_at"] = T - 11
        self.assertIn("SELL_PROOF_STALE", sellability_gate(event(sellability=proof, taker="wallet"), D(100)))


if __name__ == "__main__":
    unittest.main()
