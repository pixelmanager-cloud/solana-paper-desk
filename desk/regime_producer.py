"""Regime evidence producer for the opt-in ``paper_regime_version: 1`` entry gate. Paper only.

Pure, read-only and non-raising: it makes no provider call and charges no request. It reads
  * graduation rate  - scans created in the trailing hour in the research (scan) database, and
  * SOL/USD change   - retained Kraken attempt records already in the evidence store (the same
                       exact-shape parser the USD valuation uses), newest vs the oldest record
                       inside the window that is at least MIN_SPAN seconds older.
Any problem (missing store, no/too-short Kraken history, parse failure) returns None, which the gate
turns into REGIME_EVIDENCE_REQUIRED (entry rejected, fail closed). The result is written INTO the
market event, so the gate itself stays a pure function of the event and replays deterministically.
"""
import json, sqlite3, zlib
from contextlib import closing
from decimal import Decimal, localcontext
from pathlib import Path

GRAD_WINDOW = 3600
KRAKEN_WINDOW = 7200
MIN_SPAN = 1800
MAX_PAGES = 600
MAX_PAGE_BYTES = 262144


def _readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def graduations_per_hour(research_db, ts):
    with closing(_readonly(research_db)) as c:
        count = c.execute("SELECT COUNT(*) FROM scans WHERE created>? AND created<=?",
                          (ts - GRAD_WINDOW, ts)).fetchone()[0]
    return str(count)


def _kraken_prices(evidence_db, ts):
    from .kraken_usd_observation import (METHOD, SOURCE, TrustedTimeBounds, URL, KrakenTradesResponse,
                                         parse_kraken_usd)
    import base64
    out = {}
    with closing(_readonly(evidence_db)) as c:
        rows = c.execute("SELECT payload,raw_bytes FROM pages WHERE raw_bytes<=? ORDER BY rowid DESC LIMIT ?",
                         (MAX_PAGE_BYTES, MAX_PAGES)).fetchall()
    for blob, size in rows:
        try:
            inflater = zlib.decompressobj()
            raw = inflater.decompress(blob, size + 1)
            if not inflater.eof or len(raw) != size:
                continue
            record = json.loads(raw)
            if (type(record) is not dict or record.get("kind") != "paper_read_attempt_v1"
                    or record.get("source_id") != SOURCE or record.get("method") != METHOD
                    or record.get("failure_code") is not None or type(record.get("acquired_at_decimal")) is not str):
                continue
            acquired = record["acquired_at_decimal"]
            response = KrakenTradesResponse("GET", URL, base64.b64decode(record["response_bytes_base64"], validate=True),
                                            acquired, record["http_status"])
            parsed = parse_kraken_usd(response, bounds=TrustedTimeBounds(acquired))
            if parsed.status != "MEASURED":
                continue
            at = int(Decimal(acquired))
            if ts - KRAKEN_WINDOW <= at <= ts:
                out[at] = parsed.usd_price
        except (ValueError, TypeError, KeyError, UnicodeError, ArithmeticError, RecursionError):
            continue
    return out


def sol_usd_change_pct(evidence_db, ts, ttl):
    prices = _kraken_prices(evidence_db, ts)
    if not prices:
        return None, None
    newest = max(prices)
    if ts - newest > ttl:
        return None, None
    older = [t for t in prices if newest - t >= MIN_SPAN]
    if not older:
        return None, None
    base = prices[min(older)]
    with localcontext() as context:
        context.prec = 28
        change = (prices[newest] - base) / base * 100
        return format(change.quantize(Decimal("0.0001")), "f"), newest


def evidence(research_db, evidence_db, ts, ttl):
    """Regime evidence dict for decision second ``ts`` or None. Never raises."""
    try:
        if type(ts) is not int or ts < 0:
            return None
        change, as_of = sol_usd_change_pct(evidence_db, ts, ttl)
        if change is None:
            return None
        return {"version": 1, "as_of": as_of, "graduations_per_hour": graduations_per_hour(research_db, ts),
                "sol_usd_change_pct": change}
    except Exception:
        return None
