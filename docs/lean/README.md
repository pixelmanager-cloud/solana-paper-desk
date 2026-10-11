# Lean paper trader (`lean/`)

A simple paper-only trader that runs beside the existing desk and never imports its gates, receipts, pins, monitoring
budget or pacing DB. No signing, no broadcasting, no real funds; every fill is quote-based and `EXECUTION_UNVERIFIED`.

## Run
```
python -m lean --config /etc/solana-paper/lean.json --state-dir /var/lib/solana-desk-lean \
  --discovery-db /var/lib/solana-desk/discovery/continuous.sqlite --keys-file <provider-keys.json>
```
`--once` does one candidate pass and one position pass. The unit is `deploy/lean/desk-lean.service` (user `solana-desk`,
`LoadCredential` keys, writes only `/var/lib/solana-desk-lean`, `Restart=on-failure`, MemoryMax 512M).
Config JSON: `strategy_config` (path), `strategy_version`, `entry_probe_sol`, `candidate_interval_s`, `position_interval_s` (capped at 10 s).

## Behaviour
* **Two concurrent loops** (threads): candidates (discovery → screen → entry quote → entry decision → paper BUY) and positions
  (every <=10 s: ONE batched `getMultipleAccounts` for all open positions → exit decisions → sell quote only when an exit triggers → paper SELL).
* **Isolation:** a provider error, malformed response or exception marks that candidate / position check as failed (an `error` row,
  counted by code) and the loop moves on. Only these stop entries: an `AccountingHalt` (invariant violation), a store write failure,
  or the kill-switch file `<state-dir>/KILL` (remove it to resume). Held-position checks keep running while entries are stopped.
* **Every row** carries `code_version` and `strategy_version` (`LEAN_CODE_VERSION` env or `git rev-parse HEAD`). No pins or migrations.
* **Health:** `<state-dir>/health.json`, written atomically every loop: last loop times, counts, errors by code, open positions, cash,
  cursor, halt reason, kill-switch state. SIGTERM/SIGINT stop both loops, join, and write a final health file.

## Interfaces the runner expects (duck-typed; integration points for L01-L04)
* store: `record(kind, payload, *, code_version, strategy_version)`; `positions()` (objects/dicts with `mint`, `qty_raw`, `vault_base`,
  `vault_quote`); `cash()`; `check_invariants()` raising `AccountingHalt`. Fills are recorded as kind `fill`.
* candidates: `iter_new_candidates(db, cursor)` -> list, each with a monotonic `cursor` (else `seq`/`id`); `screen(c, providers, cfg)` -> `.passed .reasons .features`.
* strategy: `entry_decision(features, quote, portfolio, cfg)` -> `.enter .size_sol .reason`; `exit_decision(position, mark, quote, now, cfg)` -> `.exit .fraction .reason`.
* paper: `buy(quote, size_sol, cfg)`, `sell(position, quote, fraction, cfg)` -> fill (dataclass/dict/`as_dict`).
* providers: `helius.get_multiple_accounts(pubkeys)`, `jupiter.quote(in, out, amount, taker)` -> `(parsed, raw_bytes, meta)` or `ProviderError(code, transient)`.
* Default mark = constant-product spot value from the two pool vault balances (`reserve_mark`); inject `mark_fn` to change it.
