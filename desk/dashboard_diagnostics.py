"""On-demand bounded local inspection; no evidence creation or entry adapter."""
from contextlib import closing
import errno
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from .control_obligations import ReadUnavailable, read_guard
from .evidence import EvidenceStore
from .live_features import candidate_snapshot

MAX_RESPONSE_BYTES = 128 * 1024
# Shared across handler factories in this process; no waiting or replay polling.
_REPLAY_LOCK = threading.Lock()


def valid_scan_id(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) is not None


def _envelope(scan_id, status, reason=None):
    return {'status': status, 'scan_id': scan_id, 'decision': 'REJECT',
            'eligible_for_trading': False, 'automatic_entry_enabled': False,
            'provider_calls': 0, 'diagnostics': None,
            'reasons': [reason] if reason else []}


def _bounded_json(value):
    body = bytearray()
    for chunk in json.JSONEncoder(allow_nan=False).iterencode(value):
        encoded = chunk.encode()
        if len(body) + len(encoded) > MAX_RESPONSE_BYTES:
            raise ValueError('Diagnostic output ceiling')
        body.extend(encoded)
    return bytes(body)


class DashboardDiagnostics:
    def __init__(self, research_db, evidence_db):
        # Only trusted server configuration supplies these paths.
        self.research_db = Path(research_db)
        self.evidence_db = Path(evidence_db)

    def inspect(self, scan_id):
        if not valid_scan_id(scan_id):
            return 400, _bounded_json(_envelope(None, 'UNAVAILABLE', 'INVALID_SCAN_ID'))
        if not _REPLAY_LOCK.acquire(blocking=False):
            return 409, _bounded_json(_envelope(scan_id, 'BUSY', 'DIAGNOSTIC_REPLAY_BUSY'))
        revision = None
        try:
            # Hold the approved guards from head lookup through replay. No WAL
            # fallback, repair, new store, synthetic head or caller revision.
            with read_guard(self.research_db), read_guard(self.evidence_db):
                store = EvidenceStore(self.evidence_db, read_only=True)
                with closing(store.connect()) as connection:
                    rows = connection.execute(
                        'SELECT evidence_hash FROM ownership_heads WHERE scan_id=? LIMIT 2',
                        (scan_id,)).fetchall()
                if not rows:
                    return 503, _bounded_json(_envelope(scan_id, 'UNAVAILABLE', 'OWNERSHIP_HEAD_MISSING'))
                if len(rows) != 1 or not isinstance(rows[0][0], str) or re.fullmatch('[0-9a-f]{64}', rows[0][0]) is None:
                    return 503, _bounded_json(_envelope(scan_id, 'UNAVAILABLE', 'OWNERSHIP_HEAD_INVALID'))
                revision = rows[0][0]
                projection = candidate_snapshot(self.research_db, self.evidence_db, scan_id,
                                                revision_hash=revision, now=int(time.time()))
                result = _envelope(scan_id, projection['read_status'])
                result['diagnostics'] = projection
                result['reasons'] = projection['reasons']
                return (200 if result['status'] == 'AVAILABLE' else 503), _bounded_json(result)
        except ReadUnavailable:
            reason = 'DIAGNOSTIC_READ_PLATFORM_UNAVAILABLE'
        except OSError as exc:
            reason = ('DIAGNOSTIC_READ_CONTENDED' if exc.errno in (errno.EAGAIN, errno.EACCES)
                      else 'DIAGNOSTIC_READ_UNAVAILABLE')
        except (ValueError, TypeError, KeyError, sqlite3.Error, OverflowError, RecursionError, UnicodeError):
            reason = 'DIAGNOSTIC_READ_LAYOUT_REPLAY_OR_OUTPUT_UNAVAILABLE'
        finally:
            _REPLAY_LOCK.release()
        result = _envelope(scan_id, 'UNAVAILABLE', reason)
        # An actual looked-up revision is safe to report; never invent one.
        if revision is not None:
            result['revision_hash'] = revision
        return 503, _bounded_json(result)
