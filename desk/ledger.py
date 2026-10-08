"""SQLite WAL ledger for one-host paper research; transactionally saves decisions and state."""
import json
import sqlite3
from pathlib import Path

from .model import canonical, digest


class Ledger:
    def __init__(self, path, *, must_exist=False):
        if not must_exist:Path(path).parent.mkdir(parents=True, exist_ok=True)
        target=Path(path).resolve().as_uri()+"?mode=rw" if must_exist else path
        self.db = sqlite3.connect(target, uri=must_exist, timeout=20, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
            ts INTEGER NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outcomes(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS raw_events(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT UNIQUE NOT NULL,
            received_at INTEGER NOT NULL, slot INTEGER, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS health(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
            code TEXT NOT NULL, details TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def apply(self, event, cfg, transition, initial_state):
        """Idempotency, transition and checkpoint share one durable transaction."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            fingerprint = digest(cfg)
            implementation = digest({str(p.relative_to(Path(__file__).parent)): p.read_text()
                                     for p in sorted(Path(__file__).parent.rglob("*"))
                                     if p.is_file() and p.suffix in (".py", ".json")})
            old_code = self.db.execute("SELECT value FROM metadata WHERE key='implementation_hash'").fetchone()
            has_events = self.db.execute("SELECT 1 FROM events LIMIT 1").fetchone()
            checkpoint = self.db.execute("SELECT payload FROM state WHERE id=1").fetchone()
            # A previously committed ledger cannot be treated as a new experiment
            # when a damaged/partial restore has lost its checkpoint. Reject even
            # duplicate delivery rather than acknowledge an unrecoverable state.
            if has_events and not checkpoint:
                raise ValueError("ledger checkpoint missing: recovery required; original records preserved")
            if (old_code and old_code[0] != implementation) or (has_events and not old_code):
                raise ValueError("implementation changed or unversioned: use a new experiment database")
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('implementation_hash',?)", (implementation,))
            row = self.db.execute("SELECT value FROM metadata WHERE key='config_hash'").fetchone()
            if (row and row[0] != fingerprint) or (has_events and not row):
                raise ValueError("config changed or unversioned: use a new experiment database")
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('config_hash',?)", (fingerprint,))
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('config',?)", (canonical(cfg),))
            row = self.db.execute("SELECT payload_hash FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
            if row:
                if row[0] != digest(event):
                    raise ValueError("event_id collision with different payload")
                self.db.execute("COMMIT")
                return []
            state = json.loads(checkpoint[0]) if checkpoint else initial_state(cfg)
            new_state, outcomes = transition(state, event, cfg)
            self.db.execute("INSERT INTO events(event_id,ts,payload,payload_hash) VALUES(?,?,?,?)",
                            (event["event_id"], event["ts"], canonical(event), digest(event)))
            for outcome in outcomes:
                self.db.execute("INSERT INTO outcomes(event_id,payload) VALUES(?,?)",
                                (event["event_id"], canonical(outcome)))
            self.db.execute("INSERT OR REPLACE INTO state VALUES(1,?)", (canonical(new_state),))
            self.db.execute("COMMIT")
            return outcomes
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def record_raw(self, source_id, received_at, slot, payload):
        existing=self.db.execute("SELECT slot,payload FROM raw_events WHERE source_id=?",(source_id,)).fetchone()
        if existing:
            if existing[0]!=slot or existing[1]!=canonical(payload):
                raise ValueError("Conflicting raw observation for existing source identity")
            return False
        cursor = self.db.execute("INSERT OR IGNORE INTO raw_events(source_id,received_at,slot,payload) VALUES(?,?,?,?)",
                                 (source_id, received_at, slot, canonical(payload)))
        if cursor.rowcount==0:
            # Another connection may have inserted after our initial lookup.
            current=self.db.execute("SELECT slot,payload FROM raw_events WHERE source_id=?",(source_id,)).fetchone()
            if not current or current[0]!=slot or current[1]!=canonical(payload):
                raise ValueError("Conflicting concurrent raw observation for existing source identity")
        return cursor.rowcount == 1

    def health(self, ts, code, details):
        self.db.execute("INSERT INTO health(ts,code,details) VALUES(?,?,?)", (ts, code, canonical(details)))

    def report(self):
        row = self.db.execute("SELECT payload FROM state WHERE id=1").fetchone()
        state = json.loads(row[0]) if row else None
        outcomes = [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM outcomes ORDER BY seq")]
        counts = {}
        for outcome in outcomes:
            key = outcome.get("reason", outcome["type"])
            counts[key] = counts.get(key, 0) + 1
        cfg = self.db.execute("SELECT value FROM metadata WHERE key='config_hash'").fetchone()
        code = self.db.execute("SELECT value FROM metadata WHERE key='implementation_hash'").fetchone()
        result = {"mode": "PAPER_ONLY", "config_hash": cfg[0] if cfg else None,
                  "implementation_hash": code[0] if code else None,
                  "state": state, "outcomes": outcomes, "reason_counts": counts,
                  "raw_event_count": self.db.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0],
                  "health": [{"ts": r[0], "code": r[1], "details": json.loads(r[2])}
                             for r in self.db.execute("SELECT ts,code,details FROM health ORDER BY seq")]}
        result["replay_hash"] = digest({"state": state, "outcomes": outcomes})
        return result
