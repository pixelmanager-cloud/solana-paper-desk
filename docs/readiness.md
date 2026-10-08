# Readiness and autonomous development checklist

Last verified: 8 October 2026, Korea time. The user's requested end state is a usable Solana paper-trading tool with bundle-first screening. Do not equate a usable research dashboard with that end state. Real-money execution is disabled.

## Working and deployed

- Helius capture, finalized history queries and account inspection; Jupiter routes.
- Generic parsed-transaction decoder and pinned official Pump/PumpSwap instruction schemas.
- Versioned event parsing: unknown appended bytes explicitly reported, never declared complete.
- Live bounded token investigations: revoked authorities, top-account owner aggregation, early trade intents, shared funding candidates, quote cost checks, and unsigned diagnostic sells against representative public holders.
- Normalized bundle policy traces classified material distributions up to three hops, aggregates split transfers, excludes service hubs, and rejects unknown paths. Live classification remains incomplete.
- Loopback dashboard, persistent scan queue, restart recovery, 10 investigations per rolling day, at most 18 RPC calls per investigation plus quote requests.
- Bounded launch samples every 15 minutes: maximum 20 seconds, 5 records or 200 KB target per sample. A received frame may exceed the target. This is not complete discovery coverage.
- Deterministic offline paper engine, transactional ledger and synthetic scenario coverage. Exits now require fresh, matching sellability evidence; model prices cannot fabricate real-data sells.
- History reports preserve page hashes and query coverage, flag duplicate/conflicting records, decoding gaps and cursor cycles. Dashboard scans persist replayable compressed pages in a separate 256 MB evidence store. A mainnet scan saved and replayed two pages using six RPC calls.
- Backfill pages and resume positions commit atomically; conflicting raw identities abort the page. Crash/restart regression tests cover rollback and nonadjacent cursor cycles.
- Consistent SQLite snapshots copied off-server and checksummed; a restore recovered all five saved investigations. Daily verified snapshots now retain seven complete daily sets; automatic off-server replication remains pending.
- 513 tests pass locally and on the VPS. Mainnet unsigned diagnostic verified exact debit, net SOL proceeds, and post-state account controls; no transaction signed/submitted.
- Pinned Token-2022 extension inventory identifies delegate/hook/fee/pause capabilities and rejects malformed or unknown layouts; all Token-2022 remains excluded.
- Bounded holder enumeration reconciles exact raw supply, duplicate pages and indexed slot freshness. One live legacy mint reconciled all 17 accounts to 100%.
- Pool verification checks official program/PDA/vault/LP identities. A live canonical pool had burned LP supply and a verified documented layout, but remains unapproved because Token-2022 vault extensions and virtual reserves need further handling. Same-bank supply/global/dynamic fee snapshots and standard aggregate sell-fee checks are implemented; exact split/full CPI policy remains incomplete.

## Required before calling the paper tool ready

1. Build trustworthy point-in-time holder/launch/funding evidence, with pagination watermarks, explicit service/pool labels and current-holder multi-hop exposure. Partial histories must fail closed. No caller-supplied completeness flag may bypass the live builder.
2. Verify canonical pool identity, vaults, reserves and liquidity control from chain state; avoid counting vaults as private whales. Address recent program schema extensions with pinned provenance and fixtures.
3. Complete full instruction policy. Atomic balance checks and post-state delegate/freeze/owner/close-authority checks are implemented, bounded to 64 accounts. A successful representative-holder simulation is diagnostic only. Sequential buy/partial-sell simulation is now verified against mainnet state; complete route policy and automatic-entry integration remain unfinished.
4. Connect validated features to the existing paper strategy and continuous exit monitoring with durable checkpoints, fee/rent/failure costs, freshness checks and provider outage handling.
5. Add replayable real-data regression fixtures, attack cases, operating controls, backup/restore and an end-to-end restart test. Reject unknown or stale inputs rather than inventing fills.
6. Expose paper positions, reasons, net outcomes, stale data and operational status in the dashboard. Validate UI workflow and bounded resource use.
7. Run a forward-paper observation period and report its limitations. Never treat synthetic PnL as profitability evidence.

Do not enable signing, broadcast transactions, ask for seed phrases, buy subscriptions, expand public network access, or claim that every rug can be detected. Continue with existing Helius/Jupiter access. Research collection and app service are permitted; maintain request budgets. Keep secrets out of outputs and logs.

## Paths and operation

Source: outputs/solana-desk in this chat workspace. VPS: /opt/solana-desk, root@158.247.196.20 using configured SSH. Data: /var/lib/solana-desk. Secrets: /etc/solana-desk/provider-keys.json (do not print). Dashboard: desk-dashboard.service on VPS loopback port 8765. Discovery: desk-discovery.timer. Read logs without exposing credentials. Historical experiment DBs must not be reused after implementation changes.

Latest regression: event schemas load only manifest entries and verify each digest. A committed public mainnet launch fixture exercises launch plus CPI event decoding; all 35 captured launches replayed successfully after the fix.

Simulation freshness additionally requires a ten-second request/quote window and at most 32 slots between starting accounts and simulation. Diagnostic network fees above 50,000 lamports are rejected.

Original Pump creation anchors now cross-check the pinned creation instruction, complete CreateEvent, mint initialization, derived curve/mint-authority PDAs, token program, payer/creator and chain time. Mainnet finalized history verified one original launch; the one-page query correctly remained incomplete at its page limit. Early cohorts now require exactly one verified anchor.

Unresolved exits now latch EXIT_ONLY; RESUME cannot bypass an unresolved position, and verified recovery does not automatically resume entries. Pool checks reject malformed legacy vault/LP layouts, executable vaults, and unsupported Token-2022 vault extensions. All 143 tests passed locally and on the VPS after deployment.


Instruction-policy checkpoint: outer and CPI instructions are inventoried across parsed and compiled response formats. Unknown programs, approvals, mint/burn/freeze and authority changes are blocked. Jupiter exact-input V2 schemas are pinned from its program-owned finalized Anchor IDL account (slot 454337229); router amounts, user bindings, slippage, fees and direct PumpSwap-sell route shape are checked. Outer setup is limited to the wallet's WSOL ATA, and cleanup must refund that wallet. Extra outer transfers, wrong recipients and excessive compute fees are rejected. Public mainnet replay validates decoding, but its multihop route is outside the narrow supported profile. Full inner AMM/recipient/rent policy remains incomplete, so transaction_policy_ok is still false.

Daily backup service deployed: desk-backup.timer runs around 03:30 UTC, retains seven complete sets, and verifies four databases before publishing. Missing/corrupt data or insufficient disk space aborts without pruning previous sets. SQLite snapshots have a 30-second deadline each. The first successful scheduled-service trial saved all four databases to /var/backups/solana-desk/daily/daily-20261007T211406Z. These are independent consistent database snapshots, not an atomic cross-database point in time. Off-server automation remains pending; the earlier manual off-server checkpoint is preserved.


Sequential simulation capability verified: the unsigned simulateBundle adapter accepts only null signatures, bounds transaction/account counts and sizes, requires every leg to succeed, compares each watched post-state to the next pre-state, and rejects stale or incomplete responses. A mainnet two-leg system-transfer diagnostic succeeded together, while its second leg failed alone. The temporary public account was absent before and after, confirming no submission. Public fixture: fixtures/mainnet-sequence-simulation.json, slot 454341016. This first probe established provider sequencing capability. The later token sequence checkpoint below verifies buy/sell effects; full instruction policy and paper-entry integration remain required. All 370 tests pass locally and remotely.


Actual token sequence diagnostic verified at slot 454342071: buy 10,000,000 lamports of legacy MET, receive 2,548,941 raw units, then sell the conservative buy minimum of 2,523,452 raw units. Atomic buy/sell effects and post-account controls passed. Net native wealth change was -143,010 lamports, with 25,489 raw tokens left in simulated inventory; this is not realized paper PnL or a profitability result. Both transactions used null signatures and were never submitted. Public capture: fixtures/mainnet-roundtrip-simulation.json. Unsupported program/route policy remains unapproved, and no wallet-specific entry permission follows from this public-holder diagnostic. The shared compiler verifies lookup-table contents, signer count and packet/account budgets. Sequence quotes request maxAccounts=32 and forJitoBundle=true. The original broad buy route exceeded the inspection budget and was rejected before simulation.

All four databases from the successful daily snapshot were copied off-server to work/backups/daily-20261007T211406Z and checksum/integrity verified. Automatic off-server replication remains pending.


Observed distribution integration: live research reports now resolve token-account endpoints using the same finalized transaction's token-balance identities, reject conflicting/missing identities, exclude exact verified vault accounts, and trace material paths from early-buy intents for up to three hops. Split sends reach the materiality floor only when cumulative transfer volume arrives; transfers before the seed buy and unordered same-slot transactions cannot advance a path. Wallet/service classification remains unknown and no completeness or eligibility pass is granted. Offline replay of two persisted pages (200 unique transactions across six mints) resolved 589 transfers without new provider calls. A public mainnet regression fixture preserves one exact owner-resolution case. The deployed suite has 222 passing tests.


Evidence-boundary checkpoint: real-data normalized JSON cannot create entries while the trusted live feature adapter is unfinished, even if it asserts successful bundle and simulation flags. The explicit LIVE_FEATURE_ADAPTER_NOT_READY entry guard preserves synthetic testing and existing exit handling. Concurrent raw observation writes recheck duplicate identities after INSERT OR IGNORE; conflicting payloads now raise instead of disappearing silently.

Historical token-account initialization inventory is now included in live research reports. It records all observed initializeAccount variants, including zero-balance and subsequently closed accounts. Mint discovery queries use tokenAccounts=none, separately from wallet funding queries. Verification requires persisted, exhausted finalized mint history including the verified launch, complete token-instruction decoding and initialization witnesses for observed token accounts. It never marks transfer history complete: every discovered account still needs bounded history collection and reconciliation. Current holders and wallet-related history alone cannot close that gap. All 370 tests pass locally and on the VPS.


Per-account history checkpoint: after a verified historical-account inventory, live investigations query at most two account histories (one page each), using tokenAccounts=none within the unchanged 18-RPC ceiling. Query hashes, exhausted ranges, unqueried accounts, conflicts and discovered missing endpoints remain explicit. Overlapping transactions are deduplicated; conflicting payloads are quarantined. No account-query result alone grants complete transfer history or entry eligibility.

The decoder now records mint, burn and token-account closure instructions. Per-transaction raw balance reconciliation accounts for transfers, supply changes and witnessed account creation/closure. Replaying the saved 200-transaction sample produced 201 passing non-native token/mint checks without new provider calls. Wrapped SOL requires separate lamport/rent accounting and is explicitly unsupported by this token-movement checker. The normalized bundle audit also waits until aggregated split transfers reach the materiality floor before advancing a path. All 370 tests pass locally and on the VPS.


Schema provenance check at finalized slot 454346803: program-owned Pump and PumpSwap Anchor IDL accounts were read and owner/address/checksum validated. Pump TradeEvent matches the pinned repository layout; PumpSwap's on-chain IDL has fewer instructions and omits the newer repository pool/fee fields. Neither explains the unknown bytes, so no layout restriction was relaxed and the pinned runtime schemas remain unchanged. Verification metadata is /var/lib/solana-desk/pump-idl-verification.json; local analysis copies are under work/. An on-chain IDL can lag the deployed interface and is not sufficient by itself to approve unknown account bytes.


Single-bank holder snapshot checkpoint: missing/null delegated amounts no longer become zero, and indexed coverage explicitly reports snapshot_atomic=false. For a verified legacy holder enumeration of at most 99 accounts, the scanner reads the mint plus every account together in one getMultipleAccounts request. It checks current supply, amounts, owners, initialized/frozen state, delegated amounts and delegate presence, with bounded slot drift. Missing or changed records block verification. Larger sets remain unverified rather than being split into falsely atomic pages. The unchanged 18-RPC ceiling applies; unsupported token policies do not spend an extra snapshot call.

Mainnet verification at slot 454347831 matched all 17 accounts for mint 7aN1pJGiMM93gjYgCqn9ReyexzzLLrFVotUcJG62JrbC and reconciled exactly 800017057543498 raw units to mint supply. No delegates were present. The public capture is fixtures/mainnet-holder-snapshot.json and /var/lib/solana-desk/holder-snapshot-verification.json. This verifies current holdings, not historical distribution coverage or token safety. All 370 tests pass locally and remotely.


Account-lifetime continuity checkpoint: account-history collection now checks each observed account from initialization through closure/recreation, requiring previous post-balances to match subsequent pre-balances and preserving owner/program identity while open. Individually reconciled transfers cannot hide a balance gap. Separate transactions touching an account in the same slot remain unordered; signatures are never used as chain-order evidence. These checks grant neither history completeness nor trading eligibility. Replaying the existing 200-transaction evidence sample used no new provider calls: the primary mint still has unresolved same-slot ordering, while other incomplete histories explicitly lack birth or identity evidence. Eleven new regression cases cover missing activity, closure/recreation, ownership changes, failed transactions and ambiguous ordering. All 370 tests pass locally and on the VPS. Full live entry remains blocked pending the remaining readiness gates.


Finalized transaction ordering: for ambiguous account histories, at most two getBlock requests retrieve signature-only finalized blocks within the existing 18-RPC investigation ceiling. History signatures and chain timestamps must match the block, and raw block evidence must be persisted before order is used. Missing blocks, conflicts and additional slots stay unknown. Mainnet probes verified ordering at slots 454310062 and 454310071; the broader saved history still exceeds the two-block budget and remains incomplete. A public finalized-block regression fixture is included. No entry gate was relaxed. Official RPC reference: https://solana.com/docs/rpc/http/getblock. All 370 tests pass locally and on the VPS.


Distribution ordering checkpoint: observed transfer paths now consume the persisted finalized-block positions already collected for account history, with no extra RPC calls. A same-slot relay advances only after its incoming material transfer; split sends use the transaction in which the cumulative threshold is reached. Without a verified order, the path remains unknown. The graph also revisits a wallet when a later-discovered indirect path arrives earlier than its direct path, preserving the three-hop limit and preventing that earlier relay from being missed. Four new attack/regression cases cover these behaviors. All 370 tests pass locally and remotely. These are observed paths, not proof of common ownership or complete bundle exposure.


Simulation consistency checkpoint: returned post-account lamports must agree with postBalances, and each postTokenBalances record must agree with account bytes for amount, mint, token owner, program and non-executable state. Wallet token accounts missing from metadata cannot pass control checks; closed accounts cannot retain token metadata. Both standalone sells and sequential buy/sell diagnostics now recheck the target mint policy after execution, including mint/freeze authority, program and layout. Saved mainnet roundtrip legs still pass these stricter checks. Eight new regressions reject altered state/metadata and a newly introduced mint authority. All 370 tests pass locally and on the VPS. This closes an evidence-consistency gap but does not complete inner instruction/recipient policy or enable entries.


Holder evidence persistence checkpoint: current holder coverage cannot be promoted to verified unless its full getMultipleAccounts request/response is saved and the returned content hash matches. The live scanner uses the existing bounded evidence store; failure or lack of persistence leaves FULL_HOLDER_COVERAGE unresolved. Reports label the source and slot of holder percentages, including shared-funding candidates, early cohorts and distribution paths. The dashboard distinguishes a persisted single-bank snapshot from provisional indexed/largest-account data. Five new tests cover missing persistence, hash mismatch, raw replay and full scanner integration with the public holder fixture. All 370 tests pass locally and on the VPS. No new provider calls or trading approvals were added.


Atomic pool snapshot checkpoint: the existing second pool RPC now reads the pool itself alongside both vaults and the LP mint. Changed discovery/second-read pool bytes, incomplete batches, duplicate identities and backward/excessively shifted slots cannot establish liquidity approval. The raw request/response and discovery record are persisted and checksum-linked; live vault exclusions require persisted identity evidence. This adds one account to the existing batch without increasing calls. A two-call mainnet probe at slot 454353707 verified the atomic pool identities but still rejected liquidity approval for unsupported vault extensions, unknown pool layout bytes and virtual reserves. Public replay fixture: fixtures/mainnet-pool-snapshot.json. Six new tests cover atomic membership, changes, missing persistence, slot regression, truncation and mainnet replay. All 370 tests pass locally and on the VPS. Full paper readiness remains incomplete.


Paper-ledger visibility checkpoint: the loopback dashboard now has a read-only Paper portfolio panel and /api/paper endpoint. It reads only /var/lib/solana-desk/active-paper.sqlite, never creates or seeds it, and reads positions, state, config identity and the latest 50 outcomes in one SQLite read transaction. Missing, empty and unreadable ledgers are distinct from zero balances. Stale/future marks or blocked exits withhold aggregate equity and unrealized PnL; synthetic provenance remains visible. Automatic entry stays disabled and runner status is explicitly NOT_CONNECTED until the trusted driver is implemented. Eight new tests cover persistence/reopen, blocked exits, stale/future marks, missing/corrupt ledgers and read-only behavior. All 370 tests pass locally and on the VPS; JavaScript syntax checks pass. The deployed browser panel was inspected and correctly shows no active paper experiment. This panel is supporting work, not end-to-end paper readiness.


Fragmented distribution checkpoint: both observed live tracing and normalized bundle screening now flag aggregate material outflows spread across multiple recipients whose individual transfers remain below the 0.5% floor. This prevents pair-wise thresholds alone from silently ignoring a material fanout. Only post-acquisition activity is counted; verified vault/service edges and aggregate dust are excluded. The finding is FRAGMENTED_DISTRIBUTION_REQUIRES_REVIEW, not a claim of common control. The normalized audit also revisits an earlier indirect arrival within its three-hop budget, matching the observed tracer's behavior. Six new regressions cover fanout, dust, timing, service exclusion and indirect arrival. All 370 tests pass locally and on the VPS. These heuristic flags can have false positives and do not establish complete rug detection.


Gross sell-debit checkpoint: normalized outer/CPI inventory now retains account bindings and instruction bytes for further policy checks. Standalone sell diagnostics inspect gross debits from pre-existing wallet token accounts, require exact input from the expected holding, and reject unrelated gross token withdrawals even when refunded. Wallet native debits are restricted to bounded creation of its WSOL ATA with the expected owner/layout; direct native transfers and external rent recipients fail. This remains a partial policy: exact rent, AMM recipients and full route approval are unfinished. Offline replay of the public mainnet sell fixture matched 2,652,954 raw input and 1,488,440 setup lamports but remains unapproved because its broader route inventory is unsupported. Nine new tests cover excess debit/refund, unrelated-token cycling, wrong authority, extra signer accounts, native diversion, rent destination/budget and mainnet replay. All 370 tests pass locally and on the VPS. No new provider calls were needed.


Exact sell-setup rent checkpoint: when a simulated sell creates the wallet's WSOL ATA, its creation debit must match getMinimumBalanceForRentExemption for 165 bytes, not merely fit the earlier maximum budget. Missing quotes, mismatches and repeated creations remain unapproved. The optional lookup is inside the existing 18-RPC scan ceiling and overall ten-second simulation freshness window; its response is included with captured simulation evidence. An independent mainnet lookup returned 1,488,440 lamports, consistent with the older public sell capture, but does not make that older simulation fresh. Metadata: /var/lib/solana-desk/rent-verification.json. Official RPC reference: https://solana.com/docs/rpc/http/getminimumbalanceforrentexemption. Three new regressions cover hidden extra rent, missing evidence and repeated creation. All 370 tests pass locally and on the VPS. Complete AMM recipient policy and paper integration remain unfinished.


PumpSwap sell binding checkpoint: a narrow decoder uses the pinned sell schema to bind one exact-layout instruction to the persisted pool identity, user, source/destination token accounts, pool vaults, token programs, fixed program IDs, creator vault, fee-config PDA and protocol-fee ATA. It enforces exact input, minimum output and pool freshness/control evidence. Fee-recipient membership in the global configuration and full CPI policy remain explicitly unverified; passing these component bindings cannot approve a route. Ten new tests cover substitutions, stale/unapproved pools, amounts, extra bytes, duplicate sells and simulation capture.

Live standalone sell investigations now persist raw simulation results, null-signature transaction bytes, instruction keys, quote minimum and rent lookup through the existing evidence store. Missing or mismatched persistence cannot resolve the sell-simulation evidence gate. Newly added gross-debit and AMM reasons are surfaced in the scan report. No extra RPC calls were introduced for these bindings. All 370 tests pass locally and on the VPS.


Global fee-recipient checkpoint: the pinned create_config PDA and GlobalConfig account schema now decode the program-owned configuration with exact-length/type checks. Mainnet finalized slot 454360272 matched all current pinned fields. The pool's existing atomic batch now includes this configuration as a fifth account, with no additional RPC per pool verification. Refreshed mainnet pool snapshot at slot 454360444 still rejects unsupported vault/pool extensions and virtual reserves.

Sell bindings require the named standard protocol-fee recipient to belong to the same-bank configuration, with sell enabled, complete schema and the non-mayhem profile. A correctly derived ATA alone cannot authorize an arbitrary fee recipient. Actual charged fee amounts, buyback/creator-tier behavior and complete CPI policy remain unfinished; full_route_policy_passed stays false. Nine new tests cover public configuration replay, malformed/unknown layouts, fee bounds, unlisted recipients, disabled sells, wrong context and unsupported mayhem mode. All 370 tests pass locally and on the VPS. Public fixtures include mainnet-fee-config.json and the refreshed mainnet-pool-snapshot.json.


CPI caller checkpoint: instruction inventory now retains stack height, parent instruction and parent program for each inner call. Missing/invalid heights and jumps over an unobserved caller prevent verification; returning to a shallower sibling correctly resets ancestry. The narrow PumpSwap sell binding requires a depth-two call directly beneath Jupiter. Four new regressions cover nested/sibling ancestry, absent metadata, impossible jumps and wrong AMM callers. The saved mainnet simulation retains valid stack metadata but remains outside the supported route profile. All 370 tests pass locally and on the VPS. This provides the call context needed for remaining per-transfer recipient/fee validation; it does not approve the full transaction.


AMM token-recipient checkpoint: after verified narrow sell bindings, token transfers must be direct children of that PumpSwap sell. Input moves only from the expected user holding to the base vault; quote transfers move only from the quote vault to the user's WSOL account or bound protocol/creator fee accounts, with the expected authorities and checked-transfer mints. Input sums and minimum user output are enforced. Recipient role aliases, external destinations and transfers under another caller are rejected. Protocol and creator fees are counted separately from proceeds, but fee_amounts_verified and full_route_policy_passed remain false until exact fee schedules and complete instruction policy are implemented. Eight new attack/regression tests pass. All 370 tests pass locally and on the VPS; no provider calls or entry permissions were added.


Dynamic fee research/parser checkpoint: the official fee-program IDL is pinned separately by commit and checksum, leaving trade-event schemas unchanged. The new parser validates owner, discriminator, PDA bump, tier ordering, rates and exact data consumption. Integer-only standard-SOL tier selection is implemented and tested, but is not connected to fee approval. At finalized slot 454363800, the live fee account decoded 25 tiers and retained unknown trailing bytes; configuration_complete remains false. Public capture: fixtures/mainnet-dynamic-fees.json. Seven new tests cover the live rejection, synthetic exact-layout decoding, tier boundaries, noncanonical flat fees, unsupported virtual reserves, truncation and large-number precision. All 370 tests pass locally and on the VPS.

Official sources reviewed: https://github.com/pump-fun/pump-public-docs/blob/cb188ce08b5069196eef1f3e4a0c43b70099793b/docs/FEE_PROGRAM_README.md and https://github.com/pump-fun/pump-public-docs/blob/cb188ce08b5069196eef1f3e4a0c43b70099793b/docs/BREAKING_FEE_RECIPIENT.md. The latter documents additional trailing fee-recipient accounts beyond the base sell IDL. The current narrow exact-account profile intentionally rejects those variants until their pool-v2/recipient roles and complete fee behavior are verified. Slippage limits were not increased.


## Versioned fee and pool layout checkpoint

The official @pump-fun/pump-swap-sdk 2.1.0 package (git 0bc59090bbd0b8d6c27b8df72d52e30bfc069c3b) documents allocation-length gates for FeeConfig: 2512, 4073 and 4097 bytes. Its registry SHA-512 integrity was checked before source inspection; no package installation or scripts were executed. Provenance and source/schema checksums are pinned in desk/schemas/fee_sdk_manifest.json. The parser now reads only the fields present in the selected version and recognizes zero unused capacity. Unknown allocations and nonzero reserved bytes still block approval, a deliberately narrower policy than the SDK. This supersedes the earlier unknown-suffix diagnosis: the public 4097-byte capture has 2097 active bytes and 2000 zero reserved bytes, and now decodes completely. Decoding does not establish exact charged fees.

The SDK Pool schema adds protocol_fees and creator_fees. Both are now decoded; nonzero accrued fees block liquidity approval until reserve availability and swap accounting explicitly handle them. The parser recognizes the observed 300/301-byte zero-capacity profiles, while unknown/nonzero suffixes remain blocked. Existing trade-event schemas and the instruction allowlist were not expanded.

The pool's atomic batch now reads seven accounts: both vaults, LP mint, pool, global configuration, dynamic fee configuration and base mint. Supply, token authorities and fee tiers therefore share the reserve snapshot slot. Invalid token policy, missing fee state or malformed schedules cannot approve liquidity. No additional RPC calls were added. A two-call mainnet probe at slot 454366117 persisted the complete snapshot and verified identity, while correctly rejecting Token-2022 vault extensions and virtual reserves. Public fixture: fixtures/mainnet-pool-fee-snapshot.json; earlier captures are preserved.

All 380 tests pass locally and on the VPS. New regressions cover allocation versions, stale/unknown capacity, vector overruns, new accrued pool fees, atomic fee/mint membership, authority changes and missing fee accounts. Dashboard, bounded discovery timer and backup timer remain active. Full fee/CPI policy, trusted live features and the continuous paper driver remain incomplete; no automatic entries or real transactions are enabled.


Standard sell trailing-account checkpoint: the pinned SDK's non-cashback sell profile now binds the optional creator pool-v2 PDA and mandatory buyback wallet/WSOL ATA. The buyback wallet must belong to the same-bank global configuration; substituted ATAs, unlisted recipients, unsupported cashback account lists and unknown reward profiles fail. Token-recipient checks separately account for buyback fees and reject aliases with user proceeds or other fee roles. Pool evidence now exposes reward flags and creator overrides, and unsupported nonzero settings explicitly block liquidity approval. Legacy base-account bindings remain diagnostics, never full transaction authorization.

All 387 tests pass locally and remotely. Full_route_policy_passed and fee_amounts_verified remain false: exact rates/rounding, full CPI behavior and live paper integration are still required. Services remain active and /api/paper continues to report NOT_CONFIGURED / NOT_CONNECTED with automatic_entry_enabled=false. No active paper ledger was created or synthetic result presented as a live result. SDK provenance: https://registry.npmjs.org/@pump-fun/pump-swap-sdk/2.1.0 (source archive and checksums recorded in fee_sdk_manifest.json).


## Standard sell aggregate-fee checkpoint

The standalone unsigned sell diagnostic now computes standard legacy-SOL output and separately rounded LP, creator and combined protocol fees with integer arithmetic, following SDK 2.1.0 sellBaseInput/util.fee. It compares actual recipient totals against that calculation. The simulated vault pre-balances must match the persisted atomic pool snapshot, and returned dynamic/global fee configurations and mint supply must still match. Missing states, duplicate balance indices, changed reserves/supply/configuration, failed simulations, special reward/creator-override profiles, accrued fee buckets and mismatched proceeds block the component. It introduces no provider calls.

The protocol/buyback subdivision is intentionally still unverified: matching combined fees does not prove correct split rounding or full CPI behavior. Both fee_amounts_verified and full_route_policy_passed remain false. These checks have synthetic adversarial coverage; no supported live PumpSwap sell has yet passed the complete policy. The older captured MET route remains outside the supported profile. Eleven tests cover exact rounding, dust, absent creator, large integers, diverted fees, underpayment, changed/missing/duplicate state and failed simulations. All 398 tests pass locally and on the VPS. Dashboard, discovery and backups remain active; continuous paper trading readiness is still incomplete.

Source provenance remains the integrity-verified SDK archive identified in desk/schemas/fee_sdk_manifest.json. Its sell source computes user proceeds after LP/protocol/creator fees; its v2 documentation describes buyback as a slice of protocol fees. No undocumented split formula was inferred for approval. Official trailing-account reference: https://github.com/pump-fun/pump-public-docs/blob/cb188ce08b5069196eef1f3e4a0c43b70099793b/docs/BREAKING_FEE_RECIPIENT.md.


## Paper outage watchdog checkpoint

A local clock event now expires stale paper-position marks even when no new market event arrives. It preserves quantity, cash, cost basis, PnL and the daily loss baseline; marks become STALE, exits remain unverified and RUNNING switches to EXIT_ONLY. Existing stronger exit-block reasons are retained. No price, quote or sell fill is synthesized. A RESUME command cannot bypass unresolved positions. Clock messages cannot carry market assertions.

The paper-monitor CLI opens only an existing ledger and preserves implementation/config fingerprint checks. Missing ledgers remain absent; empty/no-change ledgers do not get clock events. Seven tests cover TTL boundaries, outages across midnight, durable restart, repeat ticks, configuration-change rollback, clock regression, missing ledgers and invalid assertions. All 405 tests pass locally and on the VPS.

The unprivileged desk-paper-monitor.timer is installed at five-second intervals. Its service has no network access or provider credentials, writes only the data directory, is bounded to 25 seconds/128 MB and is conditioned on /var/lib/solana-desk/active-paper.sqlite existing. That ledger does not yet exist: systemd correctly skips the service, and no synthetic ledger was seeded. The timer is only a mark watchdog, not the unfinished automatic entry/exit driver. Dashboard runner_status remains NOT_CONNECTED. Before activating a real-data paper experiment, complete the live adapter, full transaction policy, cost accounting and inclusion of its ledger in backups. Future code/config changes require a separate experiment, preserving the old ledger.

Operations: inspect systemctl status desk-paper-monitor.timer and systemctl show desk-paper-monitor.service -p ConditionResult -p Result. Stop the timer with systemctl stop desk-paper-monitor.timer if needed. Repeated ticks make no provider requests and create no ledger events once all stale positions are already marked.


## Paper-ledger backup and restart checkpoint

Daily backups now include active-paper.sqlite whenever it exists. Its absence is explicitly recorded as not_configured in the manifest; no paper DB is created by backup. Broken symlinks are rejected, and paper-ledger appearance/disappearance during capture aborts publication. Retention recognizes both historical four-database sets and five-database sets with the paper ledger. Snapshot consistency remains per database, not atomic across all databases.

A synthetic paper entry followed by watchdog expiry was backed up, hash/integrity verified, restored to a separate file and reopened through the monitor/ledger. Cash, quantities, PnL, outcomes, replay hash and EXIT_ONLY state survived unchanged; restart created no sell fill. Five new tests also cover optional absence, symlink rejection, retention and membership races. All 410 tests pass locally and on the VPS.

The deployed backup service completed successfully at daily-20261007T232444Z. Its four current databases were copied off-server into work/backups/daily-20261007T232444Z and independently hash/SQLite-integrity checked. The manifest correctly records paper_ledger=not_configured. Automatic off-server replication remains unfinished; this was a manual verified checkpoint. No active paper experiment exists, and this restore test is synthetic operational validation, not live trading or profitability evidence.


## Pre-buy funding ordering and fragmentation checkpoint

Observed funding no longer uses second-resolution timestamps alone to establish pre-buy order. Only transfers in strictly earlier slots within the bounded one-hour window feed shared-source candidates. Later-slot transfers in the same second are excluded; same-slot transfers are explicitly retained as FUNDING_SAME_SLOT_ORDER_UNVERIFIED and cannot establish attribution. Same-source fragments are aggregated across the observed pre-buy window before applying the one-million-lamport diagnostic threshold. Identical transaction observations are deduplicated and conflicting signatures quarantined. Failed transactions, self-transfers, future timestamps and malformed amounts cannot create an edge.

Every aggregated edge retains individual transfer witnesses, the history-query evidence hash, persistence and query-coverage status. Source classification remains unknown; these links cannot assert a private controller or complete bundle exposure. Reports now distinguish wallets selected from wallets successfully queried. Eight regressions cover timing, ambiguity, fragments, duplicates/conflicts and invalid/failed transfers. All 418 tests pass locally and on the VPS; no additional provider calls were introduced. Full live launch/funding completeness and automatic paper readiness remain unfinished.


## Finalized funding-order checkpoint

Funding diagnostics now reuse persisted finalized block-position proofs already collected during account-history verification. A same-slot transfer can be considered pre-buy only when both signatures are present, the block timestamp matches, proof metadata is valid and the transfer precedes every observed buy for that wallet in its earliest slot. Later transfers are excluded; missing/conflicting proof, same-transaction execution order and incomplete earliest-buy positions remain ambiguous. Funding observations and buy anchors must carry finalized-provider provenance. No additional RPC calls are made, and unknown source/service classification remains unknown.

Six new regressions cover earlier/later positions, missing and conflicting proofs, same-transaction ambiguity, nonfinalized observations and multiple earliest-slot buys. All 424 tests pass locally and on the VPS. This improves ordering of bounded observed evidence; it does not establish complete funding history or a verified private controller. Automatic paper entry remains disabled.


## Offline sell-evidence reconstruction checkpoint

New standalone sell captures use evidence schema version 2 and retain the complete route, recent blockhash, compiler lookup-table response, holding address and linked pool-snapshot hash alongside null-signature bytes and simulation results. replay-sell reconstructs the unsigned transaction from the saved route and verified lookup accounts, requires exact transaction/key/instruction equality, reloads and re-verifies the raw pool snapshot when referenced, then reruns balance/control/debit/router/envelope/AMM/recipient/aggregate-fee checks. It does not trust a saved summary's passed flags. Missing or changed inputs and dangling pool references fail. Version-1 captures remain preserved but cannot claim complete reconstruction.

The command uses a read-only evidence database and makes no provider calls. Historical replay always returns fresh=false, transaction_policy_ok=false and eligible_for_trading=false. Three new tests cover live-function versus offline-check parity using synthetic RPC responses, altered identities/keys/missing references, and read-only/missing-store behavior. All 427 tests pass locally and on the VPS. The older public mainnet sell fixture lacks full reconstruction inputs and was not relabeled as newly verified. No new mainnet simulation was made for this checkpoint.

Usage: .venv/bin/python -m desk replay-sell --evidence-db /var/lib/solana-desk/evidence.sqlite --hash <saved-version-2-sell-evidence-hash>. This is diagnostic replay, not paper execution. Full live readiness remains incomplete.


## Stale-risk baseline and mark-refresh checkpoint

Daily rollover now waits until all open-position marks are current and have no unresolved exit block. A midnight control command cannot reset daily equity/loss baselines from stale or unverified inventory. Empty portfolios roll normally. Market updates now require matching full-position sellability evidence before refreshing an open position's mark, even when no stop/target currently fires; missing evidence preserves the prior mark timestamp/value, marks UNVERIFIED_EXIT and latches EXIT_ONLY. Fresh prices alone cannot conceal an unverified exit.

Three new regressions cover midnight RESUME, missing sellability without an exit trigger, and normal empty-portfolio rollover. All 430 tests pass locally and on the VPS. This is paper-engine safety behavior; the complete live evidence adapter and continuous entry/exit driver remain unfinished. No active paper ledger or real transaction was created.


## Fee-query CPI binding checkpoint

Offline inspection of the existing 1,000-record mainnet capture found 502 successful fee-program calls using the 57-byte get_fees_with_quote_mint interface, including 449 directly beneath PumpSwap. No provider requests were needed. One public call is preserved as fixtures/mainnet-fee-call.json at slot 454313568. Its original depth is two (direct AMM outer call), so it remains outside the narrow Jupiter-to-AMM sell profile; tests distinguish its real shape from a synthetic depth-three router context.

The new fee-query check requires exactly one current-layout call, direct parentage beneath the bound AMM sell, the pinned fee-config PDA and PumpSwap program accounts, legacy SOL quote mint and canonical/market-cap arguments matching independently calculated fee evidence. Wrong callers, account substitutions, multiple calls, unknown discriminators, invalid booleans and extra bytes fail. Live diagnostics and offline replay both run it. This does not add the entire fee program to the global instruction allowlist, verify protocol/buyback split rounding or enable full transaction approval.

Seven new regression tests pass. All 437 tests pass locally and on the VPS; services remain active. Full paper readiness is still incomplete.


## Sell-event callback checkpoint

The integrity-pinned SDK 2.1.0 schema is now an events-only manifest entry: generic instruction recognition still uses the previous pinned instruction set. Its appended SellEvent.creator_fee_unclaimed field explains the prior partial decodes. Offline replay of the existing 1,000-record mainnet capture decoded all 284 observed successful PumpSwap sell events completely. One is preserved in fixtures/mainnet-sell-event.json. This used no new provider requests.

A narrow callback check now requires one self-CPI event beneath the bound AMM sell, its exact event-authority account and a complete SellEvent layout. It compares pool/user/account identities, reserves, input, output, fees and minimum-output arguments against independently computed/bound data. Unknown bytes, missing current fields, extra callbacks/accounts, wrong callers and unsupported reward/boost/unclaimed-fee behavior fail. Event text cannot substitute for balance evidence or fee policy. Live diagnostics and saved replay both run the check; full_route_policy_passed stays false.

Six new tests distinguish the real event schema from synthetic supported-profile bindings and reject identity, amount, authority, caller and layout changes. The manifest test also verifies that events-only support does not enable sell_v2 instruction recognition. All 443 tests pass locally and on the VPS. Complete fee split/CPI approval and live paper readiness remain unfinished.


## Observed protocol/buyback split checkpoint

All 284 decoded sell events in the preserved raw capture matched floor(protocol_fee * buyback_basis_points / 10000). The observed rate was 5000 basis points; 144 fractional cases distinguished floor from ceiling. A narrow diagnostic now checks the observed 50% profile against the same-bank global configuration and exact recipient totals, requiring the standard trailing-buyback account profile. It rejects redistribution between the two recipients even when their combined fee matches. Different rates, stale configuration, absent trailing accounts and unknown amounts remain unsupported.

This is an empirical historical-event check, not independent verification of program internals. fee_amounts_verified and full_route_policy_passed remain false; no entry gate was relaxed. Five new tests cover rounding, same-total diversion, changed configuration, unsupported profiles and zero/even amounts. All 448 tests pass locally and on the VPS. Mainnet capture inspection made no provider requests. Complete CPI policy and trusted live paper integration remain unfinished.


## Inner account-setup checkpoint

Sell diagnostics and saved replay now bind non-transfer token/system setup operations to the wallet's single legacy WSOL ATA. Creation must have the wallet payer, expected address, 165-byte size and legacy token-program owner; initialization must have the correct mint/owner. Query-size and immutable-owner operations must reference the expected mint/account. Inner closes, unrelated accounts, wrong callers, repeated operations and incomplete creation/initialization pairs fail. Existing ATAs need no synthetic creation. Exact rent remains checked separately, and this component never grants full transaction approval.

Seven regression tests cover these cases. All 455 tests pass locally and on the VPS. Full instruction coverage, independent complete route validation and live paper integration remain unfinished; no entries were enabled.


## Full instruction-role coverage checkpoint

Live sell diagnostics and offline replay now require every outer/CPI instruction to have a checked role: envelope, bound AMM sell, exact event callback, exact fee query, validated token movement or wallet-account setup. Unknown inner router calls, duplicate paths, unresolved instruction flags and missing component checks fail coverage. A fee-program inventory warning can be accounted for only by the exact fee-query check, never by globally allowing that program. Role coverage remains distinct from independent full transaction approval, which is still false.

Five new tests and live/replay parity pass; all 460 tests pass locally and on the VPS. Four services/timers remain active. A bounded follow-up inspected three legacy sell candidates from saved capture, then found seven currently funded public holders for one legacy mint. Two unsigned build attempts for that asset failed with a sanitized provider HTTP error before transaction simulation (three RPCs each, including pool/account checks). A separate SOL/USDC control build succeeded, so there is no demonstrated general credential outage. No new successful mainnet combined-policy simulation or fresh replay was claimed, and no transaction was signed/submitted. The next independent route validation remains outstanding. Probe metadata/scripts are under /var/lib/solana-desk and /opt/solana-desk; secrets were neither printed nor copied into outputs.


## Persistent investigation-consumer checkpoint

A bounded reject-only decision consumer now reads terminal investigation rows from the research database through a read-only attachment and records source payload/hash, evaluation time and rejection reasons atomically in paper-decisions.sqlite. Restart does not duplicate decisions, and no rowid watermark skips an earlier job that finishes after later work. Batches are capped at 20 by default (50 maximum), each source report at 2 MB. Failed/interrupted and stale reports retain explicit rejection reasons. Caller-supplied eligible flags cannot authorize trading: LIVE_FEATURE_ADAPTER_NOT_READY remains mandatory. The consumer creates no positions, fills, equity or PnL.

The unprivileged, network-disabled desk-decisions.timer runs every 30 seconds without provider requests. Deployment verification caught and corrected use of the nonexecuting desk.cli module in this service and the paper watchdog service; both now use python -m desk. A subprocess regression executes the actual unit-file command entrypoints. The consumer recorded six real saved investigations, all rejected, and a second service run preserved the exact rows and source hashes. The paper watchdog remains conditional on an active paper ledger, which still does not exist.

Backups now include the decision journal when present, separately from the optional active paper ledger; retention accepts valid four-, five- and six-database sets. The deployed backup completed with five databases, decision_journal=included and paper_ledger=not_configured. Eight new tests cover consumer persistence, late completion, rollback, bounds, source hashes, optional journal backups and deployed entrypoints. All 468 tests pass locally and on the VPS. This establishes an investigation-to-decision stage, not a live buy-to-sell paper cycle. Trusted live features, complete route validation and automatic execution/exit integration remain outstanding.


## Persisted entry-evidence connection — 8 October 2026

The decision service now evaluates versioned entry gates against the read-only evidence store. It reconstructs token controls, same-bank positive holder supply coverage and pool/liquidity checks from content-addressed provider responses. Summary approval flags cannot pass these gates. Holder concentration is explicitly gross supply, not a private-wallet or bundle percentage. New investigations persist their initial mint response without adding provider requests.

The decision journal preserves the original decisions and adds evaluations keyed by scan and policy version. Six existing investigations were evaluated on the VPS; all remain REJECT. Historical scans lacking the newly saved mint response remain blocked rather than being upgraded. The loopback dashboard exposes /api/decisions and shows component results, entry blockers and evaluation times. These are historical decisions, never current approval.

Bundle/launch/transfer completeness, funding service classification, current-holder bundle exposure, full entry/exit route policy and live strategy/cost inputs are still explicit blockers. This connection does not enable entries or create positions. All 476 tests pass locally and remotely, including mainnet holder replay, missing/tampered/wrong-mint evidence, partial supply, versioned journal preservation and bounded dashboard projection. No new provider calls were made during deployment verification.

The decision unit now supplies --evidence-db /var/lib/solana-desk/evidence.sqlite. Restarting it reevaluates only scans not yet recorded for this policy version. The Codex automated follow-up remains paused at the user's request.


## Request-bound launch and transfer replay — 8 October 2026

New finalized history pages persist a separate content-addressed request manifest binding the address, query window, token-account filter and pagination cursor to the saved response hash. The entry evaluator reconstructs coverage and decoded observations from these records rather than accepting summary completeness flags. Missing manifests, changed ranges, missing/reordered pages and false coverage flags fail closed. Existing unbound captures remain preserved and cannot pass this new gate.

The evaluator now independently reports the creation anchor, mint-query coverage, historical account inventory and transfer-history gaps. Available individual account queries are replayed and merged with conflict quarantine; movement reconciliation, account continuity and saved finalized block ordering are recomputed. Same transaction observations across queries are deduplicated. Request/page replay is bounded to the scanner's 18-request ceiling and makes no provider calls itself. Transfer completeness remains false until historical ending balances are reconciled with current holdings and all other coverage requirements are met.

Policy persisted-entry-evidence-v2 preserves prior decisions and writes a separate policy evaluation. All 487 tests pass locally and on the VPS. A fresh live investigation (62b4e79f37f74c309aceed3b91e6f49c) used seven RPC calls and persisted two history queries. Its saved responses replayed into four observations and one verified creation anchor. The two-page mint query was not exhausted, both discovered account histories remained unverified, and its Token-2022 policy was rejected. The deployed dashboard shows the verified anchor separately from blocked history; the final decision remains REJECT. No position or transaction was created.

Remaining: resumable account-history collection and closure, reconciliation to the current holder snapshot, service/private funding classification and quantified current-holder bundle exposure. Automatic paper entries remain disabled. The user-requested Codex follow-up pause remains in effect.


## Ownership continuation and snapshot boundary — 8 October 2026

Ownership histories now have durable per-query cursors and shared per-investigation request accounting in evidence.sqlite. Continuation reserves each attempt before provider I/O; failures and interrupted processes cannot refund or reset usage. Each invocation performs at most two requests by default (four maximum), and original scans plus continuation remain capped at 18 RPC attempts. A process lock prevents concurrent cursor advancement. Content-addressed pages can survive a crash before the checkpoint; an orphan is harmless and its request remains charged. Exhaustion, corrupt coverage and cursor cycles remain explicit blockers.

The ownership-advance CLI requires an immutable completed scan, persisted initial mint data and request-bound histories. Unsupported tokens are rejected without provider calls. It finishes mint pagination before resuming the discovered historical accounts. Missing initialization witnesses, conflicting records and incomplete account coverage remain blocked. Progress is saved separately; original investigations and decisions are preserved. Decision policy persisted-entry-evidence-v3 records a new revision when ownership evidence changes, replays the continued histories and retains the original observation timestamp. The dashboard displays the continued historical account counts and spent request budget without labeling them fresh entry approval.

Funding replay reconstructs pre-buy native transfers for witnessed launch-cohort buyers from saved queries. Shared-source groups remain UNKNOWN_SERVICE_OR_PRIVATE_SOURCE, never verified controllers. Missing funding queries and ambiguous ordering remain explicit. Historical ending balances now have a separate finalized-snapshot reconciliation checker that verifies every historical account, closed accounts, account controls, total supply and an exact slot cutoff. The raw replay wrapper reconstructs history instead of accepting summary flags. Slot-filtered history queries and resume/replay preserve the cutoff in request hashes. Timestamp-only histories cannot pass snapshot-boundary reconciliation: Solana block timestamps are estimates.

Validation: 513 tests locally and on the VPS, including cursor continuation, failures, shared budgets, source rebinding, concurrent workers, snapshot mismatches, funding ambiguity, exact slot filters, decision revisions, and restoring progress from a database backup. The deployed CLI rejected the latest Token-2022 scan with zero provider calls; seven deployed entry decisions remain REJECT. The ownership continuation positive path and snapshot match currently have fixture/test coverage, not a successful fresh live ownership approval.

Ownership evidence is NOT complete. The finalized snapshot collection/requery stage still needs live integration, followed by service classification, current-holder distribution exposure and successful end-to-end forward verification. No periodic ownership continuation service was enabled; the bounded CLI is available for authorized development runs. Codex follow-up automation remains paused. Automatic paper entries remain disabled.

Command: .venv/bin/python -m desk --secrets-file <existing-systemd-credential-path> ownership-advance --db /var/lib/solana-desk/research.sqlite --evidence-db /var/lib/solana-desk/evidence.sqlite --scan-id <completed-scan-id>. Do not print credentials or source reports containing provider errors.

Sources checked: https://www.helius.dev/docs/api-reference/rpc/http/gettransactionsforaddress (slot gte/lt filtering); https://solana.com/docs/rpc/http/getblocktime (estimated block production time).


## Combined cloud integration — 8 October 2026

All ten initial worker branches were combined without conflicts on `integration/cloud-wave1`, not main. Python 3.12 with declared dependencies passed 662 tests at e056ec5b016782a06a20590b3ca4e498f238ab4c. The first local attempt used an interpreter missing solders (126 errors, 33 skips); a dedicated dependency-complete venv resolved that setup failure.

Independent review found a stale ownership_heads publication race in PR18. Worker01 is repairing it and adding an unmocked positive replay fixture. PR19/12 core contracts received independent review with required trust-adapter constraints: never trust candidate hashes/flags, distinguish token-program owner from token authority, preserve purpose/time/slot validity and exclusive expiry. Worker02 is preparing a separate conservative adapter. No live verification or deployment of this batch occurred; entries remain disabled. Passing combined tests does not satisfy these review or live gates.

The desktop 10-minute review/dispatch automation is now active and owns integration. The hourly cloud reviewer is GitHub-only; scheduled execution remains unverified.


Integration follow-up: PR18 repair 0e16a91 adds a distinct whole-invocation lock and unmocked positive snapshot/history reconciliation fixtures; 668 combined tests passed, with CI passing on integration head 18e0480. Desktop boundary inspection then found getBlockTime absent from the real provider allowlist. Added that read-only method and transport/write-rejection tests: 670 combined tests passed. Independent re-review and trusted adapter remain pending; no main merge, production replacement or live-provider calls. VPS read-only inspection confirmed desk-dashboard and all four timers active. Existing eligible raw-evidence scan is Token-2022; a suitable legacy evidence capture is still needed for a meaningful positive live ownership run. Worker06 now fixes token-control gaps; worker10 performs independent combined QA.


Combined follow-up at 7027033b0783851b84125c989731e014e1e7344c: 682 tests passed, zero skips. PR22 reject-only adapter and PR23 token-control corrections are staged on integration branch. Four old characterization tests failed against stronger external-close-authority rejection; their positive controls now explicitly use synthetic authority-normalized copies, preserving original public bytes and the intended freshness/hash/projection attacks. Production gates were not relaxed. Re-review identified an alias-path variant of the ownership publication race; worker01 is fixing canonical sidecar lock identity. Worker04 is building narrow raw pool-vault admission evidence. Continued snapshot-to-entry trust wiring and legacy live acceptance remain unfinished. No main merge/deployment/live calls.


Alias-path follow-up: integrated PR18 92e9c6104b411101e414c03774a1675cbbf1ac91. Symlink/relative evidence paths resolve before invocation/request/bank locking; multiple-hardlink databases are rejected. Regression suite at f4d51d4950b6f0736551d38c2e61c39d8b518d25 passes 687 tests. Operator rename/replacement while workers run remains unsupported. Independent re-review is pending; this is not acceptance. Worker05 is implementing a raw-evidence continuation adapter that can verify only the history/snapshot component while preserving age and all other entry blockers. Worker04 is preparing point-in-time pool-vault evidence admission. No live requests, main merge or deployment in this checkpoint.


Review checkpoint: independent reviews of PR18 (92e9c61), PR22 (c50b7aa), PR23 (11bf7d1), and combined head 22e1dae found no remaining blocking defect within their scoped contracts; these are not ownership/live acceptance. PR24 adds unmocked three-account, eight-page replay and lifetime cases; PR25 connects continued raw evidence to a separate historical snapshot gate; PR26 adds point-in-time legacy PumpSwap vault admission under an external trusted acquisition receipt. A local unpublished candidate combination 57a51245c85ba50d11fe782612a443131ffaa934 passes 750 tests, zero skips. PR24/25/26 exact-head CI passes and independent reviews are assigned immediately. No main merge or deployment occurred. PR24 exposed silently ignored parsed setAuthority operations; worker06 now owns a conservative decoder/history fix before historical control coverage can be accepted. The local dispatch recovery interval is five minutes; active work continues between checks. Live legacy acceptance, ownership exposure integration and later paper-readiness gates remain open.


Reviewed integration checkpoint f083a2860e5fe54455ed263d1e64ba51604e0648 includes PR24/25 after exact-head independent reviews and successful PR CI. The exact combined source passes 717 tests, zero skips. PR26 remains excluded pending its review; the 750-test result above is an unpublished candidate, not this branch. Historical snapshot gate now receives raw continuation evidence, while original observation age and all remaining entry blockers are preserved. Authority-operation completeness repair remains required; automatic entries remain disabled and main/VPS unchanged.


Live acquisition feasibility review found no committed public legacy launch that meets both the supported Pump creation-witness requirement and the 18-request full history budget. The saved legacy distribution mint uses an unsupported creation program and its 17-account fixture exceeds even an optimistic full-acquisition request lower bound. Current six-hour screen seeds cannot recover older births before initial inventory approval. Worker01 is preparing an explicit bounded birth-inclusive acquisition path, preserving original scans and limits. Availability of a suitable live candidate remains unverified. PR27 conservatively blocks ignored authority/control operations; exact-head independent review is active. This intentionally leaves legitimate but unsupported lifecycle operations rejected until their semantics are verified, not silently ignored.


Reviewed source checkpoint 40c6a25 integrates PR26 repaired pool receipt conflicts, PR27 strict control-operation rejection, PR28 operational probes/corrected bounds, PR30 bounded journal initialization and PR32 admission-bound budget/seal. Exact reviewed heads and successful individual CI were rechecked. Full combined macOS suite:806 tests,zero skips,21.376s. The merge retains PR27 strict authority regression unchanged. PR31 queue is deliberately excluded: independent review found a REPLACE descriptor bypass and malformed-JSON worker stoppage; repairs are active. No main merge,deployment,provider calls or ownership acceptance. New admission budgets still need acquisition executor/queue and read-only entry adapter wiring; original source-bound budgets remain supported.


Source checkpoint c61f088eff19c952e434df64a08a75215a3b5cd4 adds independently reviewed PR33 pure legacy control syntax normalization and PR34 read-only sealed admission validation. Combined suite:852 tests passed,zero skips. The normalizer does not remove runtime control blockers or prove lifecycle safety. Admission-derived histories now require exact sealed source/descriptor metadata before raw replay; missing/unsealed/rebound records remain blocked. Latest PR31 queue repair is still excluded pending independent review. Acquisition execution and live ownership acceptance remain unfinished; no main merge or deployment.


Queue foundation PR31 at5a97f42 independently passed re-review after replacement identity/migration repairs. Integrated source a24c88dd238d887b94ada69c4bd5be6de4265ce6 passes878 combined tests,zero skips. Existing rows migrate only once, acquisition descriptors cannot be reclassified by ordinary replacement SQL, and canonical worker locks/fenced publication preserve active scans. This enables the acquisition executor implementation; it does not yet supply that executor or positive live evidence. Before VPS upgrade, stop old unfenced dashboard workers; no production deployment occurred.


### Coordinator checkpoint: acquisition and historical display

PR35 at `ab41ed0a9b56bc0a1a96a9c10630b57425465f5d` is in independent review by worker10. It labels persisted evaluations as historical and displays original observation age; it does not enable entry. Acquisition executor remains active with worker01 on the reviewed queue/budget foundations.

A read-only check of 150 saved VPS launch notifications found 150 create_v2 launches and no legacy candidates. No provider requests were spent. This sample does not establish that supported legacy candidates do not exist; bounded live acceptance remains blocked pending a suitable candidate and reviewed acquisition path.

PR10 now records a hosted scheduler last-run time of 2026-10-08T02:01:01.779530Z. This is reviewer-recorded evidence, not a direct desktop scheduler query; same-chat binding remains unverified.


### Historical evidence display integrated

PR35 exact head `ab41ed0a9b56bc0a1a96a9c10630b57425465f5d` passed independent review and CI. Combined source `4f80ea330fde7f2105f8f4e19f2bc85f8fc1b4b3` passes 886 tests (23.397s, no skips). The display distinguishes original observation age from evaluation time and labels component verification as historical, without granting current approval. No main merge or deployment. Worker07 now implements raw execution-trace ordering/caller reconstruction, while acquisition and protected pool receipt storage remain active prerequisites.


### Acquisition executor review checkpoint

PR36 `d184189654ddbae327d23dbca8e308cc2b0b83eb` implements explicit birth-inclusive acquisition with queue fencing, shared budget and sealed-source publication. Scratch combined source `ec5d5104e5ceae3f9ef1c64135ccb9f23b0527d2` passes 909 tests in23.571s. Independent review by worker02 is pending, including setup identity immutability and archive/publication crash boundaries. This candidate is not integrated or deployed and establishes no live acceptance.


PR36 coordinator probe confirmed setup identity defect: default SQLite `UPDATE OR REPLACE` changing one setup rowid to another deletes the second setup despite current triggers. Author repair assigned; independent review continues. Candidate remains excluded from integration/deployment despite passing909 tests.


PR36 repair `1c87f2f2b37997bdb9291f0ae2184c5e5aad5f06` adds immutable setup rowid protection. PR37 `8220f8baf0d84fc9905f39d2762664095cd6bba1` adds offline invocation trace reconstruction, explicitly not instruction effect ordering or authority authentication. Both are in independent review. Scratch combination `10345dfa9d9ea1e685756a3151dff97a1719fb77` passes942 tests in23.575s, no skips; neither candidate is integrated/deployed.


### Reviewed acquisition and trace integration

Exact PR36 repair1c87f2f and PR37 trace8220f8b passed independent review and CI before integration. Combined source8553ae7d7ed8f83d7b9da0d933f49b2f5b8122ca passes942 tests in24.051s with no skips. Explicit birth-inclusive acquisition is implemented; mainnet acceptance remains unverified. Trace foundation is disconnected research and does not authorize controls. Protected pool receipts, lifecycle semantics and current-holder exposure remain unfinished. Worker03 is searching bounded public sources for an unchanged legacy launch fixture without RPC. No main merge or VPS deployment.


### Pool receipt candidate and historical reference

PR38 fabebccda9064cd8e69833ff7e9f588ab8e7d9e8 implements protected coordinator receipt storage, with no capture/runtime wiring. Scratch c65a4af1670bd24c8992f75f046f2d262e73c40c discovers975 tests:942 executed successfully,33 Linux-only tests skipped on macOS. Worker02 must execute the combined Linux suite and review before integration.

Bounded public search found no finalized legacy fixture. A confirmed-source historical legacy create+buy reference (mint B9Z9mKUoVy5k8KuL2HauUD1mhmfF3PPNnJoK83S1pump,slot282653703) has complete raw fields but current decoder key/event incompatibility and no finalized request binding. Worker03 will preserve it unchanged with explicit reference-only provenance and rejection tests. No provider calls or live acceptance.


### Protected pool ledger integrated

PR38 fabebccd passed independent Linux review (975 combined tests,zero skips) and CI. Integrated source1effeccecf3ed305d5f253fca1d77fa6d292fd9d has identical implementation/test tree to the reviewed combination; Mac skips remain disclosed. It supplies receipt persistence only. Worker04 now builds the guarded shared-budget capture bridge, preserving complete contradictory observations and protected source configuration. PR39 historical reference7651db1 is under independent review; no live finality/ownership acceptance. No main merge or deployment.


PR39 reference7651db1 passed independent provenance review, but final Linux combination with PR38 is pending. Local candidate8aa5938 has982 discovered tests:949 passed,33 Linux-only skips. Candidate is held locally until that final check. Worker07 begins narrow legacy birth-control structural semantics using reviewed unchanged reference and invocation trace; no runtime approval, finality or current-state claims. Pool capture bridge continues.
