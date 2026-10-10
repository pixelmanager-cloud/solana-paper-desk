# Live execution design (DESIGN ONLY — nothing here is implemented or authorised)

Status: proposal for the developer to approve or reject. Task T15, base `r1-base` (`c1e22c2`); amended by T17 (review findings 1-6).

This document adds **no executable trading code**. Interface sketches below are
prose/pseudo-signatures for discussion; they are not importable, and nothing in this
task adds signing, broadcasting, wallet or key handling. `desk/__init__.py:1` states
"No wallet signing or transaction sending is implemented"; `desk/cli.py:145` reports
`live_signing_available: False`. Both stay true until the developer explicitly
authorises a separate, reviewed implementation task.

Every `file:line` reference is against `r1-base`. Lines drift; re-check before relying on them.

## 0. Bottom line

The desk today measures **"would this quote, taken instantly and filled in full, have made
money"**. It does not measure "would a real transaction landed seconds later have made
money". The second number is the only one that matters for real funds, and the distance
between them on pump.fun/PumpSwap tokens is large (a previous project lost 5–8% per round
trip to ~4 s decision→fill latency that paper never showed). The codebase currently cannot
even *represent* a live fill, so going live is not a flag flip. Ranked blockers are in §6.

Recommended order: (1) T14 latency-drift measurement on paper, (2) shadow mode,
(3) tiny live phase. Do not skip (1): it is the cheapest evidence and it can kill the idea.

## 1. Executor abstraction

### 1.1 Where fills are decided today

The pipeline is fused: quote → fill → accounting happen in one pure function inside one
SQLite transaction.

1. `paper_cycle.fulfill_quotes` (`desk/paper_cycle.py:373`) asks `quote_execution.plan`
   for demands (`desk/quote_execution.py:290`), buys each demanded quote through the
   charged budget (`paper_cycle.py:403`, `budget.call(...)`, source
   `PaperReadSources.quote`, `desk/paper_read_sources.py:160`), and replays it through
   `ingest_quote`.
2. The caller binds the quotes to one exact event: `qe.bind_transition(event, quotes)`
   (`quote_execution.py:264`).
3. `Ledger.apply(event, cfg, transition, initial_state)` (`desk/ledger.py:108`, call site
   `paper_cycle.py:544`) runs `transition(state, event, cfg)` (`ledger.py:150`), which is
   `engine.transition` (`desk/engine.py:288`), and inserts event, outcomes and the new
   checkpoint atomically (`ledger.py:151-157`).
4. Inside `engine.transition` the **fill is computed from the quote**: BUY quantity is
   `units(output_raw(quote, cfg), decimals)` (`engine.py:429-433`), cost includes
   `fixed_fee_sol` (`engine.py:446`); SELL proceeds are
   `units(output_raw(quote, cfg), 9) - fixed_fee_sol` (`engine.py:184`).
   `output_raw` is the provider minimum-output times a flat `(10000 - adverse_slippage_bps)/10000`
   haircut (`quote_execution.py:60-66`).
5. Every paper fill is labelled `EXECUTION_UNVERIFIED` and `transaction_verified: False`,
   `actual_fill_verified: False` (`quote_execution.py:27-28,134-135`; `desk/model.py:29,209`).

### 1.2 Proposed interface

One interface, two implementations. The decision logic (`engine.transition` gates, sizing,
stops) stays the single authority; the executor only turns an *approved intent* into a
*fill record*.

```
Executor
  quote(intent)      -> QuoteBundle          # route + min-out + provenance + observed_at
  build(quote)       -> UnsignedTx           # Jupiter build; no keys involved
  sign(unsigned)     -> SignedTx             # LiveExecutor only; separate process (see §3)
  send(signed)       -> SendReceipt          # signature + send time; at-most-once
  confirm(receipt)   -> Confirmation         # finalized/confirmed status, slot, err
  reconcile(conf)    -> FillRecord           # from on-chain balance deltas, never from quote
```

* `PaperExecutor` = current behaviour. `quote` = `fulfill_quotes`; `build/sign/send/confirm`
  are no-ops; `reconcile` = the `engine.transition` arithmetic above. It keeps emitting
  `EXECUTION_UNVERIFIED`. Must remain byte-identical for existing config versions
  (the ledger refuses config changes, `ledger.py:129-134`, and implementation changes,
  `ledger.py:135-139`).
* `LiveExecutor` does all six steps. A `FillRecord` is produced **only** from the confirmed
  transaction: token-account and SOL balance deltas, actual fee paid, priority fee, tip,
  rent, landed slot and signature. The quote is retained as the *intent* and is never the
  source of the fill price. Label such fills `EXECUTION_VERIFIED_ONCHAIN` only after
  `reconcile` passes; anything else stays `EXECUTION_UNVERIFIED`. Never promote a label
  by default.
* `desk/effects.py:7 check_effects`, `desk/compile.py:16 compile_unsigned`,
  `desk/instructions.py:118` and `desk/router.py:36 check_sell_route` already decode and
  check Jupiter-built unsigned transactions (programs, signers, destination accounts,
  `max_fee_lamports=50000` default at `effects.py:7`). They are the natural pre-sign
  verifier; reuse them, do not write a second one.

### 1.3 Two-phase accounting (the structural change)

A live order is asynchronous (seconds), can fail, can partially fill, and can be unknown
(sent but unconfirmed). `engine.transition` has no such state. Required new event kinds,
in a **new config/ledger version** (never an in-place change to the existing one):

| Event | Effect on state |
|---|---|
| `order_intent` | Passes the *same* engine gates (`max_positions`, exposure, cooldown, daily pause). Reserves cash (BUY) or marks the position `sell_pending`. Records the exact quote/route and the signed-tx hash **before** `send`. |
| `order_confirmed` | Applies fill from on-chain deltas; releases/consumes the reservation. |
| `order_failed` | Releases the reservation; records error and fees actually burned (failed txs still pay fees). |
| `order_unknown` | Freezes entries (mode `ENTRY_PAUSED`/`EXIT_ONLY`) until reconciled by signature lookup. Never auto-retried blindly. |

Write-ahead rule: `order_intent` (with tx hash) is committed durably **before** broadcast;
on restart, any intent without a terminal event is reconciled by looking the signature
up, never re-sent. This is what makes `send` at-most-once.

Where it plugs in: `bind_transition` (`quote_execution.py:264`) is the current seam.
A `LiveExecutor` would replace `fulfill_quotes`+`bind_transition` for the live config
version, and `Ledger.apply` would be called three times per trade (intent / confirmation
or failure) instead of once.

## 2. Latency and MEV realities (pump.fun / PumpSwap via Jupiter)

What the current code assumes versus what happens live:

* **Decision→fill latency.** The paper event time is the quote time:
  `'ts': context.now` (`desk/paper_market_adapter.py:123`), applied immediately
  (`paper_cycle.py:544`). Live: quote → build → sign → send → leader → confirm is
  typically a few hundred ms to several seconds, during which a thin PumpSwap pool moves.
  The measured loss from this on a prior project (5–8%/round trip) is of the same order as
  this desk's whole cost budget (`max_roundtrip_cost_fraction` ≈ 0.08 in the experiment
  config). **This single effect can erase the paper edge.**
* **Provider pacing is a latency floor.** Shared Jupiter/Helius cadence is 2.0 s each
  (`desk/provider_pacing.py:48`, `initialize(... helius_seconds=2.0, jupiter_seconds=2.0)`)
  and Kraken is pinned at 2.0 s (`provider_pacing.py:142`). A live exit that needs a fresh
  quote then a rebuild can wait ≥2–4 s on pacing alone before any network time. Pacing
  exists to protect keys/quotas and must not be bypassed for live; the design must
  instead budget for it (pre-built exits, or a dedicated paid low-latency path).
* **Quote ≠ fill.** The probe is an unsigned build with a hard-coded `slippageBps="100"`
  (`desk/providers.py:208`). The paper model then applies its own separate flat haircut
  (`quote_execution.py:66`). Neither is a measured slippage distribution. Live needs a
  per-trade `minimumOut` derived from measured drift (T14 output), not a constant.
* **Fees.** Paper uses a fixed `fixed_fee_sol` (0.00005 SOL in the experiment config).
  Live cost = base fee + **priority fee** + optional **Jito tip** + first-time ATA rent
  (~0.00204 SOL for a standard 165-byte token account, 2% of a 0.1 SOL trade, 20% of a 0.01 SOL trade; Token-2022 accounts with extensions cost more) + pump.fun/PumpSwap protocol and creator fees
  already inside the quote. Priority fee and tip are market-priced and spike exactly when
  a token is hot. `check_effects` caps fee lamports at 50,000 by default (`effects.py:7`);
  that cap and a realistic landing fee will conflict and must be reconciled explicitly.
* **Landing.** Plain RPC `sendTransaction` on a congested leader can drop. Options:
  higher priority fee; Jito bundle or private submission (Jito docs are cited in
  `docs/threat-model.md` S9; `jupiter_sequence_probe` already requests
  `forJitoBundle=true` at `providers.py:213`). Bundles reduce sandwich exposure but cost a
  tip and add landing-rate and ordering semantics; neither is a guarantee. The threat model
  already lists "MEV and adverse execution" as "Cost model only; live executor pending"
  (`docs/threat-model.md`, MEV row).
* **Failure modes to design for, not discover:** dropped tx (blockhash expiry ≈ 60–90 s),
  landed-but-failed (fees burned, state unchanged), **partial fill** (AMM output below
  `minimumOut` reverts, but multi-hop or bundle partials exist), sandwiching, route
  change between quote and landing, token with freeze/transfer-hook behaviour only
  visible at sell time (`docs/threat-model.md`, sellability section: "A sell simulation
  against a wallet that does not yet own tokens is not evidence of sellability").
* **RPC choice.** The only RPC today is Helius via `mainnet.helius-rpc.com`
  (`providers.py:32,73`) with a 15 s per-call ceiling (`paper_read_sources.py:182`). Live
  needs separate read vs send endpoints, `processed`/`confirmed` commitment policy
  stated per use, and a second independent endpoint for confirmation cross-check.

### 2.1 Measure before moving funds

1. **T14 (paper, no signer):** re-quote the same route at +2/+5/+10 s after every
   simulated fill, persist drift append-only, and recompute PnL under delayed fills. This
   gives the latency penalty without any wallet.
2. **Shadow mode:** the full pipeline through `build` and a *simulate-only* check
   (`simulateTransaction` against a funded-state fork or a minimally funded wallet),
   never `send`. Record: build success rate, simulation error classes, simulated fee,
   simulated out-amount versus quote, and time-per-stage. Zero signing authority is needed
   if simulation uses `sigVerify=false`/`replaceRecentBlockhash`; if the developer later
   wants signed shadow transactions, that is a key-handling decision for §3.
3. **Tiny live canary** (§5) is the only way to measure true landing rate, priority-fee
   cost and real slippage. It will lose money by design; size it as tuition.

## 3. Key and fund safety (requirements, not code)

* **Dedicated hot wallet.** Never an existing personal wallet. Hard SOL cap on the wallet
  itself (the maximum you are willing to lose in total), funded manually in small tranches.
  Withdrawals of profit are manual and out-of-band.
* **Key storage.** Preferred: remote/hardware signer service that enforces policy on the
  transaction it signs (see below). Acceptable for a tiny canary: a systemd credential
  (`LoadCredential`, as already used for API keys, `deploy/desk-paper-entry-dispatcher.service`
  `LoadCredential=provider-keys.json:...`) readable only by a dedicated signer user. Never
  in Git (repo is PUBLIC), env files in the repo, logs, dashboard, SQLite stores, backups
  (`desk/backup.py`, T03 backups copy every `*.sqlite`), or artifacts. Add a secret-scan
  check to CI for base58/JSON-array keypair patterns before any key-adjacent code lands.
* **Process isolation.** Today every `desk-*` unit runs as the same `solana-desk` user with
  network access and read-write on all stores. The decision/monitor processes must **not**
  be able to read the key. Signer = separate user, separate unit, Unix socket only
  (`RestrictAddressFamilies=AF_UNIX`), no network, `MemoryMax`/`TasksMax` like the others.
* **Signer enforces policy, not just signs.** Before signing, the signer independently
  re-runs the unsigned-tx checks (`effects.check_effects`, program allowlist
  `ALLOWED_PROGRAMS` at `desk/instructions.py:12`, signer/destination checks) and refuses: any transfer to an unlisted
  address, any approval/authority change, any account close **except** `CloseAccount` on a
  token account that (a) is the desk's own ATA for the traded mint, (b) has a zero token
  balance in the simulated post-state, and (c) names the hot wallet as both close
  destination and owner; fee above cap, amount above per-trade cap, notional above
  remaining daily budget.

  *Why the close exception is needed (rent math).* A token account holds ~0.00204 SOL of
  rent. If the exit never closes it, each round trip strands that amount: on a 0.01 SOL
  canary trade that is 0.00204 / 0.01 = **20.4%** of the trade size per round trip, which
  would dominate every other cost and make the canary unable to measure anything else
  (30 trades strand ~0.061 SOL, 12% of the 0.5 SOL wallet cap). Closing returns the rent
  to the wallet, so the net rent cost of a round trip is only the extra transaction fee of
  the close instruction. The rent is therefore treated as a temporary lock-up, **not**
  as a cost, in the PnL model, while the *fee* of the close is a cost. The close-to-self
  rule keeps the drain surface closed: a close to any other destination, of any account
  not owned by the hot wallet, or of an account with a non-zero balance is still refused.
  If the developer prefers never to allow closes, the alternative is a canary of at least
  0.1 SOL per trade (rent 2%), which raises the wallet cap (see §4 item 8).
* **Limits (proposed defaults, developer to set):** per-trade cap 0.01 SOL for the canary
  (paper is 0.1 SOL: `--amount-raw 100000000` in the dispatcher unit; see §4 item 8 for the
  size-mismatch rule); max open
  exposure equal to the engine's existing fraction; **daily loss cap** (e.g. 10% of the
  wallet cap) that flips the ledger to `LIQUIDATING`→`STOPPED` (`engine.py:38-49,263`);
  wallet cap e.g. 0.5 SOL for the whole canary.
* **Kill switch.** (a) a file flag (e.g. `/var/lib/solana-desk/KILL`) checked by the signer
  before every signature and by the executor before every build; (b) a CLI that creates it;
  (c) both fail closed (file unreadable = killed). Killing blocks new signatures; open
  positions are then handled by an explicit manual runbook, not auto-sold silently.
* **Auto-halt triggers:** N consecutive failed/dropped sends (propose N=3); any
  reconciliation mismatch (SOL or token balance differs from ledger by more than dust);
  any `order_unknown` older than a deadline; fee or slippage beyond cap; signer or RPC
  disagreement; wallet balance above expected (unexplained inflow is also a mismatch).
  Halt = signer refuses, mode `ENTRY_PAUSED`, page the developer. Resume is manual.
* **Logging.** Log signatures and tx hashes, never keys, seed phrases, full signed bytes or
  API keys. The existing credential pattern (`provider-keys.json` via `LoadCredential`)
  and `tools/setup_credentials.py` show how provider keys are kept out of the repo today.

## 4. Go-live gate (PROPOSED — the developer must approve every number)

All of the following, in order, on **one frozen config version** (no mid-run tuning; the
ledger already pins config and implementation hashes, `ledger.py:129-139`):

1. **Sample size.** ≥ N forward paper round trips. A concrete illustration, not a measured
   value: if mean net return per trade were +3% with SD 25%, a 95% CI lower bound above 0
   needs roughly `(1.96·0.25/0.03)² ≈ 270` trades. Propose N = 200 minimum and treat the CI
   condition below as the real test. There are **zero** live-data paper fills so far
   (`docs/readiness.md`), so the trade rate and variance are unknown; recompute N from the
   first 30 trades.
2. **Latency-adjusted profit.** Net PnL after the **full live fee stack** (below) is
   positive **under T14's +5 s fills** and still non-negative under +10 s, not only the
   instantaneous quote. If the edge disappears at +5 s, stop.
3. **Statistical test.** Bootstrap 95% CI (10,000 resamples, T10's report) lower bound on
   mean per-trade return > 0, **computed on T14's +5 s latency-adjusted fills with the
   real fee stack applied per trade**: base fee + priority fee + Jito tip (if bundles are
   used) + the close-transaction fee, with ATA rent treated as a recoverable lock-up
   (§3). It must not be computed on the instantaneous quote and must not use the flat
   `fixed_fee_sol` (0.00005 SOL, `quote_execution.py:46-47`), which understates live cost.
   Until measured fees exist, use the shadow-mode simulated fee and the highest priority
   fee/tip the signer policy would accept (a conservative upper bound). The holdout split
   by time must also have a positive point estimate.
   *Risk-acceptance escape hatch (replaces the CI condition only, nothing else):* a
   written, dated acceptance by the developer naming a total loss figure **no larger
   than the canary wallet cap** (propose 0.5 SOL), valid for at most **30 days** and one
   frozen config version; it expires automatically and must be re-issued with the new data.
   It cannot waive items 2, 4, 5, 6 or 8, and cannot raise the wallet cap.
4. **Shadow period.** ≥ 7 days and ≥ 500 build+simulate attempts (never `send`), with
   all of: zero reconciliation errors (definition below); build success rate ≥ 95%;
   simulation success ≥ 90% of builds, with every failure classified; simulated fee within
   ±20% of the fee used in the item-3 model at the median and no single fee above the
   signer cap; simulated out-amount within the lower of 1.0% or the trade's `minimumOut`
   tolerance of the quote at the 90th percentile and never below `minimumOut`; build→simulate
   stage time p95 ≤ 3 s.
   *Definition of "reconciliation" in simulate-only shadow mode:* for each simulated
   transaction, take the simulated post-state account deltas (`simulateTransaction` with
   `accounts` requested for the wallet and the token account) and compare them with the
   expected deltas from the intent: wallet SOL delta must equal
   `-(input + simulated fee + any rent paid) + (rent refunded by a close)` within
   1,000 lamports + 0.5% of the input; token delta must be ≥ `minimumOut` and within
   the out-amount tolerance above. Any breach, any account touched outside the allowlist,
   or any simulated state the ledger projection cannot explain is one reconciliation
   error. Live-mode reconciliation uses the same rule on confirmed on-chain balance
   deltas with a dust tolerance of 1,000 lamports (§3 halts on a breach).
5. **Exit-path proof.** At least one paper position per exit class
   (stop / trailing / time / liquidate) has completed on live data, and the held path has
   survived cold restarts (T06 verifier). Live exits are the dangerous half.
6. **Security review.** Independent review of the signer, tx allowlist, kill switch and
   auto-halt, with adversarial tests (§6 items 8–9).
7. **Tiny live phase** (§5). Only then.
8. **Size-match rule.** Paper trades 0.1 SOL while the canary trades 0.01 SOL, and
   fixed costs (base fee, priority fee, tip, close fee) do not scale with size. Illustration
   with *assumed* values (replace with measured ones): priority fee + tip + base fee
   ≈ 0.0015 SOL per transaction, two transactions per round trip = 0.003 SOL, which is
   3% of a 0.1 SOL trade but **30%** of a 0.01 SOL trade. Therefore a paper edge at 0.1 SOL
   says nothing about the canary. The gate requires **either**
   (a) items 2 and 3 recomputed at the canary size: each paper trade's gross return is
   kept, but its cost is rebuilt as `fixed_costs_sol / canary_size + proportional_costs`
   (fixed costs must not be scaled down with size; price impact is smaller at the smaller
   size and may be reduced only using measured T14 drift, not assumed), and the CI and
   +5 s tests must still pass; **or** (b) the canary trades the same 0.1 SOL as paper
   (recommended if the developer accepts a larger wallet cap, e.g. ≥ 1.0 SOL, with the
   same 10% daily loss cap), so no rescaling is needed. In case (a) the canary's
   purpose is to measure fixed costs and landing, not to confirm profit.

Anything weaker (for example, "tests pass" or "the dashboard looks good") is not a gate.

## 5. Staged rollout

| Stage | Funds | What is proven | Exit criterion |
|---|---|---|---|
| 0. Paper (now) | none | decision loop, accounting, restart | gate §4 items 1–3, 5 |
| 1. T14 drift measurement | none | latency penalty on paper | PnL positive at +5 s/+10 s |
| 2. Shadow (build+simulate) | none / dust for rent | build rate, sim errors, stage timing | gate §4 item 4 |
| 3. Canary | wallet cap ≈ 0.5 SOL, 0.01 SOL/trade (or §4 item 8(b)) | landing rate, real fee, real slippage, sell works | ≥ 30 live trades; zero reconciliation errors; landing rate ≥ 90%; mean realised round-trip cost within +1.0 percentage point of the item-3 model, and no single trade more than 3 pp above it; all ATAs closed back to the wallet |
| 4. Scale | step up only after a full clean review | — | each step needs the developer's explicit approval |

**Failure handling.** Order states are explicit (§1.3). Dropped tx: confirm by signature
lookup, retire the blockhash, build a fresh tx only after the first is provably dead. Never
resend a signed tx that may still land. Sell failure: bounded escalation (higher priority
fee, then wider `minimumOut`), then `STUCK_POSITION` as the engine already records
(`engine.py:131`) — never a fabricated close. **Reconciliation** after every confirmed
trade and on every startup: wallet SOL, token balance, and token-account state versus the
ledger; any difference halts (§3).

## 6. What in the current codebase blocks live use (ranked, harshest first)

1. **No representation of an unconfirmed or on-chain fill.** Fill arithmetic lives inside
   the pure transition and is derived from the quote (`engine.py:184,429-478`;
   `quote_execution.py:116-135`). There is no pending/failed/unknown order state and no
   reconcile event. A live executor needs the §1.3 event kinds, i.e. a new versioned engine
   path. This is the largest piece of work.
2. **Position integrity is hard-bound to quote provenance.** `validate_position`
   (`quote_execution.py:233-261`) is run for every open position on every transition
   (`engine.py:293-295`) and requires `quote_execution` to replay exactly from the stored
   original quote JSON; on-chain fills would fail it by design. Needs a versioned
   alternative that binds to transaction signature + balance deltas.
3. **Zero-latency fill model is baked in.** Fill time = quote time (`paper_market_adapter.py:123`,
   `paper_cycle.py:544`), so every stored PnL is optimistic by the latency penalty. Until T14
   exists there is no estimate of the size of the error.
4. **Slippage and fees are flat hypotheses.** A single `adverse_slippage_bps` and a fixed
   `fixed_fee_sol` (`quote_execution.py:46-47,66`); probe slippage is a constant
   `"100"` (`providers.py:208`). Real priority fees, tips, ATA rent and price impact at size
   are not modelled; ATA rent is explicitly "UNVERIFIED" (`quote_execution.py:133`).
5. **Sellability is not proven live.** The threat model marks the honeypot check as
   "simulation producer pending" and notes a sell simulation without holdings is not
   evidence (`docs/threat-model.md`, honeypot row and sellability step 5). `desk/simulate.py`
   is "unsigned diagnostic simulation". Live needs the buy-then-sell canary logic.
6. **Provider pacing and budgets are exit-latency floors.** 2 s shared cadence per
   provider (`provider_pacing.py:48`), monitoring budget of 3600 requests/rolling hour,
   18 requests per investigation, and the held-cycle unit's `TimeoutStartSec=20`
   (`deploy/desk-paper-held-cycle.service`) with a 5 s monitor timer
   (`deploy/desk-paper-monitor.timer`) all sit on the exit path. Nothing here is wrong for
   paper; for live the exit must be able to run under stress without queueing behind
   discovery.
7. **Oneshot timer architecture.** Services are `Type=oneshot` with wall-clock timeouts
   (e.g. `TimeoutStartSec=180`, `MemoryMax=384M` in the dispatcher unit). A live order has a
   multi-step lifecycle that must survive a process exit between `send` and `confirm`;
   write-ahead intent plus a reconciler job is mandatory.
8. **No signer isolation.** All units run as one user with write access to all stores
   (`deploy/*.service`). Any code path that later touches a key would sit next to network
   code that parses untrusted provider JSON.
9. **Immutable-ledger rules cut both ways.** The ledger rejects config or implementation
   changes (`ledger.py:129-139`), so a live version = a new ledger by policy. That is good
   (no silent promotion of paper results), but live and paper results must never be summed
   in one ledger, and a fix to the live executor forces a rotation while flat.
10. **Secret-hygiene tooling for keys does not exist.** Provider-key handling is by
    credential file (`providers.py:21`, `paper_read_sources.py:230`); nothing scans for
    private keys, and the backup tool copies every `*.sqlite`. Add scanning and an
    explicit "no key material in stores" assertion before any signer code is merged.
11. **Known unknowns.** Jupiter Price V3 is blocked upstream (Cloudflare 1010, per
    `docs/readiness.md`) and swap-quote reliability is "still requires fresh verification";
    a live system that depends on these providers inherits that fragility. Zero live-data
    paper BUYs exist, so no slice of this design has been exercised on real fills.

## 7. Explicit non-goals of this document

No code, no key generation, no wallet addresses with funds, no paid subscriptions, no
change to existing gates, no change to `EXECUTION_UNVERIFIED` labelling of current paper
fills. Approval of this design is not approval to implement it; each stage in §5 needs its
own task, review and written go-ahead.
