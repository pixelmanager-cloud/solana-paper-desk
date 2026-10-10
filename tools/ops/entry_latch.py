"""First-BUY entry latch: stop new admissions once a paper BUY is persisted.

Read-only unless --apply. With --apply and at least one BUY fill while the
ledger mode is RUNNING, exactly one operator PAUSE_ENTRY control is delivered
through Ledger.apply + the engine/quote-execution transition (the same path as
paper_cycle control delivery). Held monitoring and exits are unaffected: the
engine manages open positions in every mode. No provider I/O, no RESUME, no
state writes outside that one control event.

Exit codes: 0 nothing to do / reported / latched; 2 unavailable or unsafe
(fail closed, e.g. config or runtime mismatch, clock behind ledger); 3 ledger
busy (a cycle holds the paper-cycle lock; retry on the next tick).
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from contextlib import closing

from desk import engine, paper_cycle as cycle, quote_execution as qe
from desk.ledger import Ledger
from desk.model import validate_event
from desk.paper_checkpoint import read_checkpoint
from desk.paper_cycle_cli import _config

LATCH_ID = 'entry-latch:v1:'
BASE = {'kind': 'paper_entry_latch_v1', 'execution_status': 'EXECUTION_UNVERIFIED',
        'live_readiness': False, 'provider_requests': 0}


def _buys(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as c:
        c.execute('BEGIN')
        rows = c.execute(
            "SELECT o.event_id,json_extract(o.payload,'$.execution_status') FROM outcomes o "
            "JOIN events e ON e.event_id=o.event_id WHERE json_extract(o.payload,'$.type')='fill' "
            "AND json_extract(o.payload,'$.side')='buy' ORDER BY o.seq").fetchall()
        latched = c.execute('SELECT 1 FROM events WHERE event_id=?',
                            (LATCH_ID + rows[0][0],)).fetchone() is not None if rows else False
    return rows, latched


def _report(path, cfg):
    state = cycle._state(path, cfg)  # config/marker/runtime/checkpoint identity, read-only
    rows, latched = _buys(path)
    return state, {**BASE, 'mode': state['mode'], 'buy_fills': len(rows),
                   'first_buy_event_id': rows[0][0] if rows else None,
                   'fill_labels': sorted({r[1] for r in rows}),
                   'open_positions': len(state['positions']), 'already_latched': latched}


def latch(ledger, cfg, *, apply=False, clock=time.time):
    """Return (exit_code, report). Raises only for programming defects."""
    if os.path.islink(ledger):
        raise ValueError('Symlinked ledger refused')
    path = cycle.canonical_job_path(ledger)
    if not path.is_file():
        raise ValueError('Existing ledger required')
    state, report = _report(path, cfg)
    if not report['buy_fills']:
        return 0, {**report, 'status': 'NO_BUY', 'action': 'NONE'}
    if state['mode'] != 'RUNNING':
        # Only RUNNING is paused: PAUSE_ENTRY must never relax EXIT_ONLY/LIQUIDATING.
        return 0, {**report, 'status': 'NOOP_ENTRY_ALREADY_BLOCKED', 'action': 'NONE'}
    if report['already_latched']:
        # Exactly one latch per ledger: an operator RESUME after it is deliberate.
        return 0, {**report, 'status': 'NOOP_ALREADY_LATCHED_ONCE', 'action': 'NONE'}
    if not apply:
        return 0, {**report, 'status': 'WOULD_PAUSE_ENTRY', 'action': 'NONE'}
    with cycle._lock(str(path) + '.paper-cycle.lock') as acquired:
        if not acquired:
            return 3, {**report, 'status': 'LEDGER_BUSY', 'action': 'NONE'}
        state, report = _report(path, cfg)  # re-prove under the cycle lock
        if state['mode'] != 'RUNNING' or report['already_latched'] or not report['buy_fills']:
            return 0, {**report, 'status': 'NOOP_CHANGED_BEFORE_LOCK', 'action': 'NONE'}
        ts = int(clock())
        if ts < state['last_ts']:
            return 2, {**report, 'status': 'CLOCK_BEHIND_LEDGER', 'action': 'NONE'}
        event = {'schema_version': 1, 'kind': 'control', 'event_id': LATCH_ID + report['first_buy_event_id'],
                 'ts': ts, 'actor': 'operator', 'command': 'PAUSE_ENTRY'}
        validate_event(event)
        db = Ledger(path, must_exist=True)
        try:
            bound = qe.bind_transition(event, ())
            def checked(saved, delivered, config):
                read_checkpoint(db.db)
                return bound(saved, delivered, config)
            db.apply(event, cfg, checked, engine.initial_state)
        finally:
            db.close()
        after = cycle._state(path, cfg)
        if after['mode'] != 'ENTRY_PAUSED':
            return 2, {**report, 'status': 'LATCH_NOT_EFFECTIVE', 'mode': after['mode'], 'action': 'ATTEMPTED'}
        return 0, {**report, 'status': 'LATCHED', 'mode': 'ENTRY_PAUSED', 'action': 'PAUSE_ENTRY'}


def main(argv=None, *, clock=time.time):
    parser = argparse.ArgumentParser(description='Pause new paper entries after the first BUY (EXECUTION_UNVERIFIED)')
    parser.add_argument('--ledger', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--apply', action='store_true', help='Deliver the single PAUSE_ENTRY control')
    args = parser.parse_args(argv)
    try:
        cfg = _config(args.config)
        cycle._config(cfg)
        code, report = latch(args.ledger, cfg, apply=args.apply, clock=clock)
    except cycle.CycleBlocked as error:
        code, report = 2, {**BASE, 'status': 'UNAVAILABLE', 'blockers': [error.code]}
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError, OverflowError, RecursionError):
        # No raw errors, paths or payloads: operator input/checkpoint/runtime unavailable.
        code, report = 2, {**BASE, 'status': 'UNAVAILABLE',
                           'blockers': ['OPERATOR_INPUT_CONFIG_OR_CHECKPOINT_UNAVAILABLE']}
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
