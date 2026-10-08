# Finalized pool point evidence: request floor and actual bank

Verified from primary sources on 2026-10-08. This contract concerns offline
point admission only. It does not change history, lifecycle, entry decisions,
transport, source trust, or the shared 18-request ceiling.

The Solana Foundation's [getMultipleAccounts configuration documentation](https://solana-foundation.github.io/solana-web3.js/types/GetMultipleAccountsConfig.html)
defines `minContextSlot` as a minimum evaluation slot. The
[RPC method reference](https://solana.com/docs/rpc/http/getmultipleaccounts)
returns accounts in request order with one response context. The
[commitment reference](https://solana.com/docs/rpc#configuring-state-commitment)
describes finalized commitment. The
[getBlockTime reference](https://solana.com/docs/rpc/http/getblocktime)
accepts a particular slot and returns its estimated Unix production time;
missing time is unresolved, and this estimate is not the coordinator clock.

The method page links to the official Agave implementation at v3.1.8. Its
resolved commit is `2717084afeeb7baad4342468c27f528ef617a3cf`;
`rpc/src/rpc.rs` SHA256 is
`e2a8eb6c2079e766a8e1316ace9a37ca8aa0a51b2c3d149e4032675c8334809a`.
The [pinned bank-selection function](https://github.com/anza-xyz/agave/blob/2717084afeeb7baad4342468c27f528ef617a3cf/rpc/src/rpc.rs#L270-L286)
selects the commitment bank and rejects it when its slot is below the supplied
minimum. It does not select historical state at that minimum. The
[pinned multiple-account implementation](https://github.com/anza-xyz/agave/blob/2717084afeeb7baad4342468c27f528ef617a3cf/rpc/src/rpc.rs#L558-L592)
uses that one bank for every requested account and for the response context.
These source pins support the RPC interpretation; they are not proof that an
arbitrary provider ran that code or authenticated chain data.

The narrow application contract follows:

- Retain the exact original request: canonical six account keys in order,
  base64 encoding, explicit finalized commitment, and integer floor `S`.
  Independently trusted receipts bind both request manifests and responses.
- All six raw accounts come from that single response. Its exact integer
  context slot `T` must match the receipt and requested admission point, and
  satisfy `T >= S`. No upper drift bound is inferred from the floor; existing
  coordinator-clock freshness limits remain applicable.
- Preserve and hash the actual request using `S`; never rewrite it to `T`.
  `getBlockTime(T)` and its exact request/response references bind the time to
  that bank. There is no account-state observation at `S` when `T > S`.
- Ledger scope and contradiction checks use pool/mint/actual bank `T`, across
  every approved source and observation, including stale or malformed ones.
  Different valid request floors may evaluate to the same bank. Conflicting
  raw state or block times for that bank block both selected-ref directions.
- `T < S`, wrong commitment, malformed/missing context, incomplete accounts,
  mismatched bank/time/request/manifest/ref identities remain blocked.
  Point labels grant no historical interval, continuity, private control,
  ownership approval, or entry permission. Synthetic fixtures never authorize
  production exclusion. Provider-source trust remains an explicit coordinator
  prerequisite, separate from content integrity and protocol identity.

This is fixture-validated interpretation, not live acceptance. Independent
review and coordinator verification remain required. Existing raw records and
historical decisions are not rewritten or backfilled by these modules.
