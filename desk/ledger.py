"""SQLite WAL ledger for one-host paper research; transactionally saves decisions and state."""
import json
import sqlite3
from pathlib import Path

from .model import canonical, digest, decimal


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

    def _has_experiment_records(self):
        """Raw observations/health are fresh; any accounting identity is not."""
        return bool(self.db.execute('SELECT 1 FROM events LIMIT 1').fetchone()
                    or self.db.execute('SELECT 1 FROM outcomes LIMIT 1').fetchone()
                    or self.db.execute('SELECT 1 FROM state LIMIT 1').fetchone()
                    or self.db.execute("SELECT 1 FROM metadata WHERE key IN "
                                       "('implementation_hash','config_hash','config') LIMIT 1").fetchone()
                    or self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name LIKE 'paper_runtime_%' LIMIT 1").fetchone()
                    or self.db.execute("SELECT 1 FROM sqlite_sequence WHERE name IN "
                                       "('events','outcomes') AND seq>0 LIMIT 1").fetchone())

    def _checkpoint(self, payload):
        """Validate persisted engine state without rebuilding or changing it."""
        try:
            state = json.loads(payload)
            required = {'cash', 'realized_pnl', 'positions', 'cooldowns', 'mode',
                        'last_ts', 'day', 'day_start_equity', 'day_gross_losses',
                        'peak_equity', 'max_drawdown', 'last_entry_minute', 'loss_streak'}
            if not isinstance(state, dict) or not required <= state.keys():
                raise ValueError('missing state fields')
            if state['mode'] not in ('RUNNING', 'ENTRY_PAUSED', 'EXIT_ONLY', 'LIQUIDATING', 'STOPPED'):
                raise ValueError('invalid mode')
            for key in ('cash', 'realized_pnl', 'day_start_equity', 'day_gross_losses',
                        'peak_equity', 'max_drawdown'):
                if not isinstance(state[key], str):
                    raise ValueError('invalid decimal field')
                decimal(state[key])
            for key in ('last_ts', 'last_entry_minute', 'loss_streak'):
                if type(state[key]) is not int:
                    raise ValueError('invalid integer field')
            if state['last_ts'] < 0 or state['loss_streak'] < 0 or state['last_entry_minute'] < -1:
                raise ValueError('invalid counter')
            if state['day'] is not None and not isinstance(state['day'], str):
                raise ValueError('invalid day')
            if not isinstance(state['positions'], dict) or not isinstance(state['cooldowns'], dict):
                raise ValueError('invalid position/cooldown mapping')
            for mint, until in state['cooldowns'].items():
                if not mint or type(until) is not int or until < 0:
                    raise ValueError('invalid cooldown')
            position_fields = {'qty', 'initial_qty', 'cost_left', 'initial_cost', 'trade_pnl',
                               'opened_at', 'exit_blocked', 'mark_status', 'stage', 'stop_ratio',
                               'peak_ratio', 'touched_15', 'mark_value', 'mark_at', 'pool',
                               'entry_scores', 'provenance', 'taker'}
            for mint, position in state['positions'].items():
                if not mint or not isinstance(position, dict) or not position_fields <= position.keys():
                    raise ValueError('missing position fields')
                for key in ('qty', 'initial_qty', 'cost_left', 'initial_cost', 'trade_pnl',
                            'stop_ratio', 'peak_ratio', 'mark_value'):
                    if not isinstance(position[key], str):
                        raise ValueError('invalid position decimal')
                    decimal(position[key])
                if decimal(position['qty']) <= 0 or decimal(position['qty']) > decimal(position['initial_qty']):
                    raise ValueError('invalid remaining quantity')
                for key in ('opened_at', 'mark_at', 'stage'):
                    if type(position[key]) is not int or position[key] < 0:
                        raise ValueError('invalid position integer')
                if (type(position['touched_15']) is not bool or position['stage'] > 3
                        or position['mark_status'] not in ('MODEL_ESTIMATE', 'STALE', 'UNVERIFIED_EXIT')
                        or not isinstance(position['entry_scores'], dict)
                        or not isinstance(position['pool'], str) or not position['pool']
                        or not isinstance(position['provenance'], str) or not position['provenance']
                        or (position['taker'] is not None and not isinstance(position['taker'], str))
                        or (position['exit_blocked'] is not None and not isinstance(position['exit_blocked'], str))):
                    raise ValueError('invalid position identity/shape')
            latest = self.db.execute('SELECT MAX(ts) FROM events').fetchone()[0]
            if latest is None or state['last_ts'] != latest:
                raise ValueError('checkpoint journal timestamp mismatch')
            from .paper_checkpoint import validate_entry_policies
            validate_entry_policies(self.db, state)
            return state
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError('ledger checkpoint invalid: recovery required; original records preserved') from exc

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
            nonfresh = self._has_experiment_records()
            # A previously committed ledger cannot be treated as a new experiment
            # when a damaged/partial restore has lost its checkpoint. Reject even
            # duplicate delivery rather than acknowledge an unrecoverable state.
            if nonfresh and not checkpoint:
                raise ValueError("ledger checkpoint missing: recovery required; original records preserved")
            if nonfresh and (not has_events or self.db.execute(
                    "SELECT 1 FROM outcomes o LEFT JOIN events e ON e.event_id=o.event_id "
                    "WHERE e.event_id IS NULL LIMIT 1").fetchone()):
                raise ValueError("ledger event journal incomplete: recovery required; original records preserved")
            row = self.db.execute("SELECT value FROM metadata WHERE key='config_hash'").fetchone()
            if (row and row[0] != fingerprint) or (nonfresh and not row):
                raise ValueError("config changed or unversioned: use a new experiment database")
            saved_config = self.db.execute("SELECT value FROM metadata WHERE key='config'").fetchone()
            if (saved_config and saved_config[0] != canonical(cfg)) or (nonfresh and not saved_config):
                raise ValueError("config changed or unversioned: use a new experiment database")
            if nonfresh:
                from .runtime_compatibility import require_runtime
                try: require_runtime(self.db, implementation=implementation)
                except ValueError as error:
                    raise ValueError('implementation changed or unversioned: explicit reviewed transition required') from error
            state = self._checkpoint(checkpoint[0]) if checkpoint else initial_state(cfg)
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('implementation_hash',?)", (implementation,))
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('config_hash',?)", (fingerprint,))
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('config',?)", (canonical(cfg),))
            row = self.db.execute("SELECT payload_hash FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
            if row:
                if row[0] != digest(event):
                    raise ValueError("event_id collision with different payload")
                self.db.execute("COMMIT")
                return []
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
        if not row and self._has_experiment_records():
            raise ValueError("ledger checkpoint missing: recovery required; original records preserved")
        if row:
            from .runtime_compatibility import require_runtime
            require_runtime(self.db)
        state = self._checkpoint(row[0]) if row else None
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
