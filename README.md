# Solana trading desk - research prototype v0.2

**Solana only. Paper research and read-only provider access. No private keys, signing, transaction submission, or live trading.**

The project is **not yet an end-to-end market-connected paper trader**. Ownership evidence is the active priority: resumable history, exact snapshot reconciliation, funding/service classification and current-holder distribution exposure. Token/holder/pool checks, history replay, unsigned diagnostics and a deterministic offline paper engine are implemented. Automatic entries remain disabled.

The latest verified suite has 513 tests. See [readiness](docs/readiness.md) for implemented behavior and outstanding requirements, and [the agent task queue](docs/agent-task-queue.json) for dependencies and acceptance criteria. Test counts do not establish live paper readiness.

## Run locally

Requires Python 3.11+ (tested on Python 3.12). Install the pinned dependencies for the full suite.

```sh
python3 -m pip install -e . -r requirements-live.txt
python3 -m unittest discover -s tests -v
python3 -m desk replay --input fixtures/demo.jsonl --config config/paper.json --db var/demo.sqlite --report var/demo-report.json
python3 -m desk audit-bundles --input fixtures/bundled.json --as-of 1791417600
```

Re-running the same input/database is safe: previously committed event IDs are ignored; a reused ID with different contents is rejected. A config or implementation change requires a new experiment database. Do not delete an experiment to hide unfavorable results.

## Implemented

| Component | Current behavior |
|---|---|
| Durable ledger | SQLite WAL, full sync, atomic event + decision + state commit; restart and collision checks |
| Strategy | Deterministic safety/momentum/entry scoring, position limits, cost-aware sizing, cooldowns |
| Bundle screening | Shared verified private funding, early holdings, material transfers, hub exclusions, evidence completeness gates |
| Token policy | Binary legacy SPL mint/account inspection; reject mint/freeze authority, frozen/delegated holdings, Token-2022 and unknown programs |
| Sellability policy | Requires recent size/mint-specific simulation evidence; a quote alone fails; synthetic exemption limited to clearly labeled fixture data |
| Paper fills | Constant-product fees/impact plus explicit adverse-slippage hypothesis; initial-quantity profit ladder and net PnL |
| Risk states | RUNNING, ENTRY_PAUSED, EXIT_ONLY, LIQUIDATING, STOPPED; paused entries do not stop modeled exits |
| Helius adapters | Bounded confirmed stream recorder, raw archive, gap markers, resumable finalized address-history queries, on-chain mint reads |
| Jupiter adapter | Read-only Swap V2 `/build` request saved for inspection; no signing/submission |

## Important boundaries

- Normalized snapshots and evidence currently come from JSONL fixtures or a caller. Their schema is validated; their provenance is not cryptographically attested. Never expose this input interface to an untrusted service.
- Real raw records are not automatically turned into scores or trades. Historical query completion is not proof that a token's launch/funding/holder history is complete.
- `sellability` is an evidence contract, **not an implemented simulator**. The future trusted adapter must validate transaction instructions and actual simulated wallet deltas before setting success fields. An unsigned Jupiter quote cannot set them.
- The bundle detector implements first-hop funding and one-hop material transfers. Multi-hop funding, graph propagation to moved balances, recurring deployer clusters, actual bundle attribution, wallet labels and program decoding remain planned.
- All Token-2022 tokens are rejected in v0.1, including benign ones. This is a conservative scope choice, not a claim that all such tokens are scams.
- SQLite is for a single-host paper milestone. PostgreSQL/outbox/Redis and independent failover are prerequisites for a distributed live desk.
- The simulator omits rent, failed-attempt fees, market response to hypothetical trades, and real landing delays. No paper-profit gate can pass on this simulator alone.
- No automatic timer advances positions when input data stops. Live scheduling, quote refresh and independent recovery remain required. A server outage cannot be fixed by an in-process holding-time rule.
- The first eligible event per minute can enter. Ranking all candidates across a minute is deferred and must not use future arrivals during replay.
- This build cannot send money. Funding a wallet is unnecessary for this milestone.

## Helius setup and read-only trial

The user already has a VPS; Helius is not set up yet. Start by evaluating Helius Developer (published at $49/month on 2026-10-08), not a dedicated node. Check actual plan entitlements and set usage/overage limits before a program-wide capture.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-live.txt
```

Provide `HELIUS_API_KEY` through your local secret manager or VPS environment file; do not commit it or paste it into chat. The program never prints provider URLs or raw network exceptions. Then:

```sh
.venv/bin/python -m desk record --db var/raw.sqlite --seconds 60 --max-records 1000 --max-bytes 20000000
.venv/bin/python -m desk report --db var/raw.sqlite --output var/capture-report.json
```

Alternatively run `python3 tools/setup_credentials.py` in your own terminal. It prompts with hidden input and saves an owner-only `var/provider-keys.json`. Use `.venv/bin/python -m desk --secrets-file var/provider-keys.json doctor` or place the same global option before `record`. The setup helper stores API credentials only, never wallet keys.

Default subscriptions cover Pump and PumpSwap. Use `--address PUBLIC_POOL_ADDRESS` for a narrower trial. Record limits bound received data, not the provider's exact billable traffic. Reconnects are marked as unverified gaps; they never automatically clear missing-history gates.

Address backfill uses an inclusive start and exclusive end, in UTC epoch seconds. Replace placeholders with actual public addresses and times:

```sh
.venv/bin/python -m desk backfill --db var/raw.sqlite --address PUBLIC_ADDRESS --start START_EPOCH --end END_EPOCH --max-pages 10
.venv/bin/python -m desk inspect-mint --mint PUBLIC_MINT --output var/mint-audit.json
```

## Jupiter read-only route probe

Set `JUPITER_API_KEY` securely, then use a public taker address. Amount is integer input-token base units (lamports for SOL). This saves an unsigned route; it does not buy or prove future sellability.

```sh
python3 -m desk quote-probe --output-mint PUBLIC_MINT --amount 10000000 --taker PUBLIC_WALLET --output var/route.json
```

## VPS packaging

`deploy/desk-recorder.service` is a Linux systemd unit. It expects the code at `/opt/solana-desk`, a dedicated unprivileged `solana-desk` user, Python 3.11+, a virtualenv, and an owner-only `/etc/solana-desk/provider-keys.json` containing API keys. systemd LoadCredential supplies a read-only copy to the service. No timer is included; measure cost and verify gaps/coverage before continuous operation. No ports need to be opened for this collector.

Deployment status and verification are recorded separately in `docs/deployment.md`. Source files stay root-owned; the service writes only its data directory.

## Next implementation priorities

1. Verify a real provider capture; pin Pump/PumpSwap program IDLs with commit hashes and decoder fixtures.
2. Decode creation, bonding-curve trades, migrations, swaps, SPL transfers and failed transactions; reconcile replay against finalized chain state.
3. Build holder-owner snapshots and launch-to-current inventory graphs; expand funding evidence to multiple hops with exchange/service exclusions.
4. Build real buy/exit route simulation with transaction-policy validation and size-specific net proceeds; never convert missing results into a pass.
5. Connect those trusted adapters to continuous paper trading and outage/fault-injection tests.
6. Add PostgreSQL, signed-transaction reconciliation, an isolated policy-enforcing signer, a second RPC, and recovery fencing before any live pilot.

See `docs/architecture-v2.md` and `docs/threat-model.md` for the updated specification and scam defenses.

## Decode a captured sample

```sh
python3 -m desk decode-capture --db var/raw.sqlite --output var/observations.jsonl --limit 10000
```

Opens the source database read-only and refuses to overwrite an existing output. Supports Helius transaction notifications and full history transaction objects with jsonParsed account keys. Preserves signature, slot, payload hash and instruction index. Failed transactions produce no transfer edges; malformed records are quarantined. Exact integer balances avoid floating-point loss. Missing balances remain unknown. Token-account transfers are not automatically wallet transfers; SOL funding sources remain unclassified. Mint initialization observations are not assertions of a particular launchpad launch.

The report counts token activity without treating increases as buys or first-seen slots as launch slots. It always reports `eligible_for_trading: false`. It is an observation layer, not the trusted bundle evidence builder. Program-specific instructions remain explicitly undecoded. Schema reference: https://solana.com/docs/rpc/json-structures

## Live research dashboard

Open http://127.0.0.1:8765 on the configured Mac. If the tunnel is disconnected, run `Open Solana Desk.command`. The VPS dashboard binds only to loopback and is reached over SSH. Paste a mint or select an observed launch, then choose Investigate token. The UI keeps evidence gaps visible and has no trading buttons. Ten scans per rolling day keep initial usage bounded.

`python3 -m desk --secrets-file var/provider-keys.json screen --mint MINT --output report.json` runs the same scanner locally. `simulate-sell` performs an unsigned diagnostic for a specified public holder, never a broadcast. Requirements-live now includes solders 0.29.0. No wallet private key is used.

The public-holder simulation has been verified against mainnet. This does not approve the user's wallet or complete the transaction-effect policy. See [readiness](docs/readiness.md) for required work before end-to-end paper trading.


## Latest verified checkpoint — 8 October 2026

468 tests pass locally and on the VPS. Instruction inventory, pinned Jupiter V2 argument checks and outer setup/cleanup recipient checks are deployed. These are diagnostic components; full transaction policy remains false. Daily verified four-database snapshots retain seven sets. The dashboard remains a research prototype: continuous automatic paper entry/exit and complete live bundle evidence are still required. See docs/readiness.md for authoritative current status and docs/deployment.md for the backup and service runbook. Earlier test counts above describe historical milestones.
