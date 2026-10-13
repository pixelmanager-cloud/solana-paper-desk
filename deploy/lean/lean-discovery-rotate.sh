#!/usr/bin/env bash
# Rotate the discovery DB before its code-capped 5 GiB storage policy blocks the feed (sentinel alerts at 70%).
# Archives the old DB, initializes a fresh one with the same filter, and resets the lean desk cursor
# (sequence numbers restart at 1 in a new DB; without a cursor lean starts at now - max_age). Paper only.
set -euo pipefail
DB=/var/lib/solana-desk/discovery/continuous.sqlite
ARCH=/var/lib/solana-desk/discovery/archive
FILTER=$(sudo -u solana-desk /opt/solana-desk/.venv/bin/python -c "import sqlite3;print(sqlite3.connect('file:$DB?mode=ro',uri=True).execute('select filter from settings').fetchone()[0])")
STAMP=$(date -u +%Y%m%dT%H%M)
systemctl stop desk-lean
systemctl stop desk-continuous-discovery
install -d -o solana-desk -g solana-desk -m 0700 "$ARCH"
mv "$DB" "$ARCH/continuous-$STAMP.sqlite"
for s in -wal -shm -journal; do [ -e "$DB$s" ] && mv "$DB$s" "$ARCH/continuous-$STAMP.sqlite$s"; done
cd /opt/solana-desk-tools/discovery-dd09bb1
sudo -u solana-desk /opt/solana-desk/.venv/bin/python -m discovery.continuous init --db "$DB" --address "$FILTER"
mv /var/lib/solana-desk-lean/cursor.json "/var/lib/solana-desk-lean/cursor.json.pre-rotate-$STAMP"
systemctl start desk-continuous-discovery
systemctl start desk-lean
echo "rotated: archive=$ARCH/continuous-$STAMP.sqlite filter=$FILTER"
