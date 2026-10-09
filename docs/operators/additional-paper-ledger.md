# Explicit additional paper ledger selection

The dashboard and daily backup accept one operator-selected additional ledger
through `DESK_PAPER_LEDGER_DB`. With the variable absent, existing behavior stays
unchanged: the dashboard reads `active-paper.sqlite`, and backup includes that
ledger when present. There is no directory discovery or database provisioning.

For a separately reviewed experiment, set the same variable in the dashboard
and backup service environments, for example:

```ini
Environment=DESK_PAPER_LEDGER_DB=/var/lib/solana-desk/token2022-paper.sqlite
```

The coordinator's planned new experiment uses this ledger and the separately
reviewed `/etc/solana-paper/paper-token2022-reviewed.json` config with
`paper_token_profile_version: 1`; this patch neither installs nor validates that
new profile. The original ledger is not renamed or copied as a continuation.
The selected file
must already exist directly in the original data folder, with an absolute
canonical path and a distinct simple `.sqlite` filename. Core research/evidence/
raw/launches databases, the decision journal and `active-paper.sqlite` cannot be
selected as the additional ledger. Symlinks, hard links, relative paths, missing
files and multiple paths are rejected. File ownership must be root or the process
user; no execute, group-write or world permissions are allowed (`0600` or `0640`
are typical). The data directory must not be group/world writable.

An empty or invalid explicit value never falls back to the original ledger.
Dashboard startup checks selection before research recovery or starting its
worker. `/api/paper` still uses the unchanged checkpoint/runtime/config/accounting
validation in `paper_status`. Replacement, removal, permission changes or changing
the selection while the dashboard runs make that endpoint unavailable; restart
with a reviewed selection. This is the existing protected, stable-path Linux
contract: do not rename or replace databases while services use them. It is not
a defense against a malicious administrator changing files during SQLite opens.
Loopback-only access and automatic-entry false flags are unchanged.

Backup requires the original `active-paper.sqlite` alongside an explicit
additional selection, includes both, and records the additional filename and
canonical source path in `additional_paper_ledger` in the manifest. It checks
source inode stability before publishing; failure publishes no partial archive
and does not prune prior backups. Every copied database is normalized to a
standalone DELETE-journal snapshot, with its final bytes/hash verified; source
database journal modes and records are unchanged. Retention recognizes verified
legacy manifests and manifests with at most one additional ledger, including
earlier selections in the same data directory. Malformed manifests, changed
hashes or unexpected archive members are preserved rather than pruned.

Quiesce writers for coordinated backups: snapshots remain consistent per database,
not one cross-database transaction. This change does not add discovery or provider
pacing databases to daily backup; their separate preservation procedures remain
required. Never restore older counters or pacing state to renew allowances.

Selection grants no Token-2022 or entry eligibility, runtime compatibility,
monitoring-budget handoff, signing or broadcast permission. A new experiment must
have its own separately approved ledger/config and matching implementation/source
bindings. The coordinator must review deployment identities after these source
changes; this patch does not rewrite original metadata or runtime allowlists.
