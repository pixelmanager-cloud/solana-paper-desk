"""L21: external risk features from RugCheck, the creator's history and copycat detection. RECORDING ONLY: nothing here is read by
``lean.strategy``, nothing changes an entry or an exit, and every failure is a missing field, never an error of the trader. PAPER ONLY,
read-only GETs, no key (RugCheck's public report needs none). Default OFF: ``risk_external`` absent / null in lean.json = no client, no
thread, no row.

One ``observations(kind='risk_external')`` row per screened, HAZARD-FREE candidate (a PASS, or a reject only for the soft market bands:
``lean.features.hazard_free``). The runner hands the candidate over RIGHT AFTER its screen decision (a non-blocking ``submit``), so the
calls run while the trader quotes the entry and ``late_s`` stays a few seconds. The row is the canonical JSON of
``{version, mint, screen_passed, decision_ts, collected_at, late_s, calls, fields, missing, flags, identity, raw}``:

* ``decision_ts``  the trader's screen decision of this candidate (``decisions`` row, read from the store);
* ``collected_at`` when the LAST call of the row returned; ``late_s = collected_at - decision_ts``;
* ``flags``        every non-null field is ``AT_COLLECTION``: RugCheck answers as of the call, not as of the decision, so the learner
                   (``lean.learn``) uses a field only when ``late_s <= max_late_s`` (its lookahead guard);
* ``fields[name]`` is the value or None, and every None has a reason in ``missing[name]``.

Fields (#2 RugCheck report, #3 creator history, #8 copycat):
  rc_score / rc_score_norm            ``score`` / ``score_normalised``
  rc_risks                            ``[{name, level}]`` (at most 20); rc_danger_count / rc_warn_count count the levels
  rc_total_holders / rc_graph_insiders ``totalHolders`` / ``graphInsidersDetected``
  rc_insider_network_count            ``len(insiderNetworks)`` (null = none = 0)
  rc_insider_largest_pct              the largest network's ``tokenAmount`` as % of ``token.supply`` (0 without networks)
  rc_top10_pct                        the top 10 of ``topHolders`` by ``pct``, AMM / pool accounts excluded (owner or address of a
                                      ``markets`` entry or a ``knownAccounts`` AMM)
  rc_lp_locked_pct                    the largest ``markets[].lp.lpLockedPct``
  rc_rugged                           ``rugged``
  creator_tokens_7d / _30d            the creator's other tokens (``creatorTokens``) created in the 7 / 30 days BEFORE the decision
                                      (RugCheck's Go backend writes an empty list as null: null = 0, ``identity.creator_tokens='null'``)
  creator_dead_30d                    ... of those, ``marketCap`` below ``dead_mcap_usd`` (RugCheck's last figure)
  creator_sampled / creator_rugged_sampled  the newest ``creator_sample`` (<= 3) of the 30-day tokens, each one report call: how many
                                      were read and how many are ``rugged``
  copycat_of_recent                   distinct tokens this recorder collected in the ``copycat_window_s`` (24 h) BEFORE the decision
                                      whose normalized name OR symbol equals this one's (copycat_name_count / copycat_symbol_count apart)
  same_image_count                    ... whose image (IPFS CID, else the URL without query) equals this one's (null without an image)

Budget: one report call per candidate, plus up to ``creator_sample`` report calls only when the creator has tokens in the last 30 days.
RugCheck has its own bucket (``rate_per_s``, default 1/s) and is never on a Helius / Jupiter lane. A 429 opens a back-off window
(``backoff_s`` or its Retry-After, whichever is longer) in which nothing is sent (``RC_BACKOFF``). A candidate already later than
``max_late_s`` when its turn comes is not called at all (``COLLECTED_TOO_LATE``): its row would be excluded by the learner anyway.
The calls run on this recorder's own daemon thread (``run``), fed by a bounded queue; ``submit`` never blocks, never raises and holds
no lock while anything is sent. A full queue drops the candidate (counted, no row). On stop the queue is dropped (no row, no call).

Not done (see reports/L21.md): ``lpLockedPct`` of ``/report/summary`` (the full report's markets carry it); a Helius DAS fallback for
``creatorTokens`` (pump.fun metadata has no creators array and DAS assets carry no creation time, so it could not give 7 / 30 day counts);
hashing the image bytes (it would fetch an arbitrary URL; the URI / CID is compared instead).
"""
import calendar
import json
import logging
import queue
import re
import threading
import time
import unicodedata
from decimal import Decimal, InvalidOperation, localcontext

from lean.features import hazard_free
from lean.providers import ProviderError

log = logging.getLogger('lean.risk_external')

VERSION = 'risk_external_v1'
KIND = 'risk_external'
RAW_KIND = 'risk_external:rugcheck'
AT_COLLECTION = 'AT_COLLECTION'
DAY = 86400.0
FIELDS = ('rc_score', 'rc_score_norm', 'rc_risks', 'rc_danger_count', 'rc_warn_count', 'rc_total_holders', 'rc_graph_insiders',
          'rc_insider_network_count', 'rc_insider_largest_pct', 'rc_top10_pct', 'rc_lp_locked_pct', 'rc_rugged',
          'creator_tokens_7d', 'creator_tokens_30d', 'creator_dead_30d', 'creator_sampled', 'creator_rugged_sampled',
          'copycat_of_recent', 'copycat_name_count', 'copycat_symbol_count', 'same_image_count')
REPORT_FIELDS = FIELDS[:12]
CREATOR_FIELDS = ('creator_tokens_7d', 'creator_tokens_30d', 'creator_dead_30d', 'creator_sampled', 'creator_rugged_sampled')
COPYCAT_FIELDS = ('copycat_of_recent', 'copycat_name_count', 'copycat_symbol_count', 'same_image_count')
MAX_RISKS = 20
MAX_CREATOR_TOKENS = 1000
DEFAULTS = {'enabled': False, 'rate_per_s': 1.0, 'timeout_s': 10.0, 'max_attempts': 1, 'max_bytes': 4 * 1024 * 1024, 'queue_size': 200,
            'max_late_s': 30.0, 'creator_sample': 3, 'dead_mcap_usd': 10000, 'copycat_window_s': DAY, 'raw_max_bytes': 16384,
            'backoff_s': 60.0}
_RANGES = {'rate_per_s': (0.01, 1.0), 'timeout_s': (1, 60), 'max_late_s': (0, 3600), 'dead_mcap_usd': (0, 10 ** 9),
           'copycat_window_s': (0, 7 * DAY), 'backoff_s': (0, 3600)}
_INT_RANGES = {'max_attempts': (1, 3), 'max_bytes': (1024, 8 * 1024 * 1024), 'queue_size': (1, 10000), 'creator_sample': (0, 3),
               'raw_max_bytes': (0, 1024 * 1024)}


def config(raw):
    """lean.json ``risk_external`` -> validated settings. Absent / null = disabled. Unknown keys are refused."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError('risk_external must be an object')
    unknown = set(raw) - set(DEFAULTS) - {'_comment'}
    if unknown:
        raise ValueError('risk_external: unknown keys %s' % sorted(unknown))
    out = {k: raw.get(k, v) for k, v in DEFAULTS.items()}
    if type(out['enabled']) is not bool:
        raise ValueError('risk_external.enabled must be true or false')
    for key, (low, high) in _RANGES.items():
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError('risk_external.%s out of range' % key)
        out[key] = float(value)
    for key, (low, high) in _INT_RANGES.items():
        if type(out[key]) is not int or not low <= out[key] <= high:
            raise ValueError('risk_external.%s out of range' % key)
    return out


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, default=str).encode()


# ------------------------------------------------------------------------------------------------------------------ parsing
def _dec(value):
    """A finite Decimal from a JSON number (int or Decimal, never a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, Decimal, float)):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _fmt(value, places=4):
    with localcontext() as ctx:
        ctx.prec = 40
        return format(round(Decimal(value), places), 'f')


_ISO = re.compile(r'(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)')


def parse_time(value):
    """RFC 3339 (RugCheck writes Go nanosecond times, ``2026-09-08T15:26:59.363634079Z``) -> epoch seconds, or None."""
    if not isinstance(value, str):
        return None
    m = _ISO.fullmatch(value.strip())
    if m is None:
        return None
    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    try:
        epoch = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
        if not (1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60 and s < 61):
            raise ValueError
    except (ValueError, OverflowError):
        return None
    frac = float('0.' + m.group(7)) if m.group(7) else 0.0
    zone = m.group(8)
    offset = 0 if zone == 'Z' else (1 if zone[0] == '+' else -1) * (int(zone[1:3]) * 3600 + int(zone[4:6]) * 60)
    return epoch + frac - offset


def name_key(value):
    """Normalized name / symbol for the copycat match: NFKC, case-folded, letters and digits only (a leading ``$`` and all punctuation,
    spaces and emoji dropped). None when nothing is left."""
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize('NFKC', value).casefold()
    key = ''.join(c for c in text if c.isalnum())[:64]
    return key or None


_CID = re.compile(r'(?:/ipfs/|ipfs://)([A-Za-z0-9]{20,100})')


def image_key(value):
    """The image identity: ``ipfs:<CID>`` for any IPFS gateway / ipfs:// URI, else the URL without scheme, query and fragment,
    lower-cased. None when empty."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    m = _CID.search(text)
    if m:
        return 'ipfs:' + m.group(1)
    text = re.sub(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', '', text).split('#', 1)[0].split('?', 1)[0].rstrip('/').lower()
    return text[:256] or None


def _text(obj, key):
    value = obj.get(key) if isinstance(obj, dict) else None
    return value.strip()[:128] if isinstance(value, str) and value.strip() else None


def parse_report(report):
    """``(fields, missing, identity, creator_tokens)`` of one RugCheck report (the parsed dict). Pure; never raises on a bad field (the
    field is missing with a reason instead). ``creator_tokens`` is a list of ``(mint, created_ts | None, market_cap | None)`` or None."""
    fields, missing = {}, {}

    def put(name, value, why):
        if value is None:
            missing[name] = why
        else:
            fields[name] = value
    score, norm = _dec(report.get('score')), _dec(report.get('score_normalised'))
    put('rc_score', None if score is None else (int(score) if score == score.to_integral_value() else _fmt(score)), 'RC_SCORE_ABSENT')
    put('rc_score_norm', None if norm is None else (int(norm) if norm == norm.to_integral_value() else _fmt(norm)), 'RC_SCORE_ABSENT')
    risks = report.get('risks')
    if risks is None:
        risks = []                                     # Go: an empty list is written as null
    if isinstance(risks, list) and all(isinstance(r, dict) for r in risks):
        listed = [{'name': (_text(r, 'name') or '?')[:64], 'level': (_text(r, 'level') or '?')[:16]} for r in risks]
        fields['rc_risks'] = listed[:MAX_RISKS]
        fields['rc_danger_count'] = sum(1 for r in listed if r['level'].lower() == 'danger')
        fields['rc_warn_count'] = sum(1 for r in listed if r['level'].lower() == 'warn')
    else:
        for name in ('rc_risks', 'rc_danger_count', 'rc_warn_count'):
            missing[name] = 'RC_RISKS_MALFORMED'
    put('rc_total_holders', _count(report.get('totalHolders')), 'RC_HOLDERS_ABSENT')
    put('rc_graph_insiders', _count(report.get('graphInsidersDetected')), 'RC_INSIDERS_ABSENT')
    token = report.get('token') if isinstance(report.get('token'), dict) else {}
    supply = _dec(token.get('supply'))
    networks = report.get('insiderNetworks')
    if networks is None and 'insiderNetworks' in report:
        networks = []
    if isinstance(networks, list) and all(isinstance(n, dict) for n in networks):
        fields['rc_insider_network_count'] = len(networks)
        amounts = [_dec(n.get('tokenAmount')) for n in networks]
        if not networks:
            fields['rc_insider_largest_pct'] = '0.0000'
        elif supply is None or supply <= 0 or any(a is None or a < 0 for a in amounts):
            missing['rc_insider_largest_pct'] = 'RC_INSIDER_AMOUNTS_UNREADABLE'
        else:
            fields['rc_insider_largest_pct'] = _fmt(min(Decimal(100), max(amounts) * 100 / supply))
    else:
        missing['rc_insider_network_count'] = missing['rc_insider_largest_pct'] = 'RC_INSIDERS_ABSENT'
    markets = report.get('markets') if isinstance(report.get('markets'), list) else []
    pools = set()
    locked = []
    for market in markets:
        if not isinstance(market, dict):
            continue
        for key in ('pubkey', 'liquidityAAccount', 'liquidityBAccount'):
            if isinstance(market.get(key), str):
                pools.add(market[key])
        lp = market.get('lp') if isinstance(market.get('lp'), dict) else {}
        pct = _dec(lp.get('lpLockedPct'))
        if pct is not None:
            locked.append(pct)
    known = report.get('knownAccounts') if isinstance(report.get('knownAccounts'), dict) else {}
    pools |= {k for k, v in known.items() if isinstance(v, dict) and v.get('type') == 'AMM'}
    put('rc_lp_locked_pct', _fmt(max(locked)) if locked else None, 'RC_NO_MARKET_LP')
    holders = report.get('topHolders')
    if isinstance(holders, list) and all(isinstance(h, dict) for h in holders):
        pcts = []
        for h in holders:
            if h.get('owner') in pools or h.get('address') in pools:
                continue
            pct = _dec(h.get('pct'))
            if pct is None or pct < 0:
                pcts = None
                break
            pcts.append(pct)
        if pcts is None:
            missing['rc_top10_pct'] = 'RC_HOLDERS_UNREADABLE'
        else:
            fields['rc_top10_pct'] = _fmt(min(Decimal(100), sum(sorted(pcts, reverse=True)[:10])))
    else:
        missing['rc_top10_pct'] = 'RC_HOLDERS_ABSENT'
    rugged = report.get('rugged')
    put('rc_rugged', rugged if isinstance(rugged, bool) else None, 'RC_RUGGED_ABSENT')
    meta = report.get('tokenMeta') if isinstance(report.get('tokenMeta'), dict) else {}
    files = report.get('fileMeta') if isinstance(report.get('fileMeta'), dict) else {}
    identity = {'name_key': name_key(_text(meta, 'name') or _text(files, 'name')),
                'symbol_key': name_key(_text(meta, 'symbol') or _text(files, 'symbol')),
                'image_key': image_key(_text(files, 'image')),
                'creator': _text(report, 'creator')}
    tokens = report.get('creatorTokens')
    if tokens is None:
        identity['creator_tokens'] = 'null'
        creator_tokens = []
    elif isinstance(tokens, list) and all(isinstance(t, dict) for t in tokens):
        identity['creator_tokens'] = 'listed'
        creator_tokens = [(t.get('mint') if isinstance(t.get('mint'), str) else None, parse_time(t.get('createdAt')),
                           _dec(t.get('marketCap'))) for t in tokens[:MAX_CREATOR_TOKENS]]
    else:
        identity['creator_tokens'] = 'malformed'
        creator_tokens = None
    return fields, missing, identity, creator_tokens


def creator_history(creator_tokens, mint, decision_ts, *, dead_mcap_usd, sample):
    """``(fields, missing, sample_mints)``: the creator's OTHER tokens created before the decision, in the last 7 and 30 days; the dead
    ones (market cap below ``dead_mcap_usd``) among the 30-day ones; and the newest ``sample`` of those to read ``rugged`` from. A token
    created after the decision is never counted; one without a readable creation time is never counted either."""
    fields, missing = {}, {}
    if creator_tokens is None:
        for name in CREATOR_FIELDS:
            missing[name] = 'RC_CREATOR_TOKENS_MALFORMED'
        return fields, missing, []
    prior = [(m, ts, cap) for m, ts, cap in creator_tokens if m != mint and ts is not None and ts < decision_ts]
    recent = sorted((t for t in prior if t[1] >= decision_ts - 30 * DAY), key=lambda t: -t[1])
    fields['creator_tokens_7d'] = sum(1 for t in recent if t[1] >= decision_ts - 7 * DAY)
    fields['creator_tokens_30d'] = len(recent)
    caps = [cap for _, _, cap in recent]
    if any(cap is None for cap in caps):
        missing['creator_dead_30d'] = 'RC_MARKET_CAP_UNKNOWN'
    else:
        fields['creator_dead_30d'] = sum(1 for cap in caps if cap < Decimal(str(dead_mcap_usd)))
    seen, picks = set(), []
    for m, _, _ in recent:
        if isinstance(m, str) and m not in seen and len(picks) < sample:
            seen.add(m)
            picks.append(m)
    return fields, missing, picks


def copycat(identity, mint, decision_ts, recent, window_s):
    """``(fields, missing)`` against ``recent`` = ``[(collected_at, mint, name_key, symbol_key, image_key)]`` of tokens this recorder
    collected. Only entries collected AT OR BEFORE the decision and within ``window_s`` of it count; each other mint counts once."""
    fields, missing = {}, {}
    pool = {}
    for at, other, name, symbol, image in recent:
        if other != mint and decision_ts - window_s <= at <= decision_ts:
            pool[other] = (name, symbol, image)
    name, symbol, image = identity.get('name_key'), identity.get('symbol_key'), identity.get('image_key')
    if name is None and symbol is None:
        for field in ('copycat_of_recent', 'copycat_name_count', 'copycat_symbol_count'):
            missing[field] = 'NO_NAME_OR_SYMBOL'
    else:
        names = {m for m, v in pool.items() if name is not None and v[0] == name}
        symbols = {m for m, v in pool.items() if symbol is not None and v[1] == symbol}
        fields['copycat_name_count'], fields['copycat_symbol_count'] = len(names), len(symbols)
        fields['copycat_of_recent'] = len(names | symbols)
    if image is None:
        missing['same_image_count'] = 'NO_IMAGE'
    else:
        fields['same_image_count'] = sum(1 for v in pool.values() if v[2] == image)
    return fields, missing


def build_row(mint, *, screen_passed, decision_ts, collected_at, calls, fields, missing, identity=None, raw=None, candidate_id=None):
    """The row payload. Every field in ``FIELDS`` is present; None <=> a reason in ``missing``; every value is flagged AT_COLLECTION."""
    out_fields, out_missing = {name: None for name in FIELDS}, {}
    for name in FIELDS:
        value = fields.get(name)
        if value is not None:
            out_fields[name] = value
        else:
            out_missing[name] = missing.get(name) or 'UNKNOWN'
    late = None if decision_ts is None or collected_at is None else round(collected_at - decision_ts, 3)
    return {'version': VERSION, 'mint': mint, 'candidate_id': candidate_id, 'screen_passed': bool(screen_passed),
            'decision_ts': decision_ts, 'collected_at': collected_at, 'late_s': late, 'calls': calls, 'fields': out_fields,
            'missing': out_missing, 'flags': {name: AT_COLLECTION for name in FIELDS if out_fields[name] is not None},
            'identity': dict(identity or {}), 'raw': dict(raw or {}), 'source': 'rugcheck'}


# ------------------------------------------------------------------------------------------------------------------ recorder
class _Stopped(Exception):
    pass


def _why(error):
    code = str(getattr(error, 'code', type(error).__name__))[:40]
    return code if code.startswith('RC_') else 'RC_' + code


class RiskExternal:
    """Collects the rows on its own thread. ``submit`` (the runner hook) is non-blocking and never raises."""

    def __init__(self, *, store, client, cfg, code_version, strategy_version, clock=time.time, monotonic=time.monotonic):
        self.store, self.client, self.cfg = store, client, cfg
        self.clock, self.monotonic = clock, monotonic
        self.code_version, self.strategy_version = code_version, strategy_version
        self.queue = queue.Queue(maxsize=cfg['queue_size'])
        self.stats = {'submitted': 0, 'rows': 0, 'calls': 0, 'errors': 0, 'shed': 0, 'queue_full': 0, 'late': 0, 'backoff': 0,
                      'stopped': 0, 'unknown_mint': 0, 'http_429': 0, 'raw_kept': 0, 'dropped': 0}
        self.last_error = None
        self._stop = None
        self._seen = set()
        self._seen_lock = threading.Lock()                # only around the dedupe set; never held while anything is sent
        self._backoff_until = 0.0
        self._recent = []                                 # [(collected_at, mint, name_key, symbol_key, image_key)], worker thread only
        self._restored = False

    # -- the runner hook ------------------------------------------------------------------------------------------------
    def submit(self, candidate, candidate_id, screen):
        """Queue a screened candidate. Skips failed screens and hazard rejects; one row per mint while soft-rejected and one more once
        a screen passes (a watchlist re-screen does not buy a new row every 5 minutes). Never raises, never blocks."""
        try:
            if getattr(screen, 'error', None) or not hazard_free(screen):
                return False
            mint = getattr(candidate, 'mint', None)
            passed = bool(getattr(screen, 'passed', False))
            with self._seen_lock:
                key = (mint, passed)
                if key in self._seen:
                    return False
                self._seen.add(key)
                if len(self._seen) > 100000:
                    self._seen.clear()
            try:
                self.queue.put_nowait((mint, candidate_id, passed, self.clock()))
            except queue.Full:
                self.stats['queue_full'] += 1
                self.stats['shed'] += 1
                return False
            self.stats['submitted'] += 1
            return True
        except Exception as error:                        # noqa: BLE001 - a recorder failure never reaches the trader
            self._count_error(error)
            self.stats['dropped'] += 1
            return False

    def _count_error(self, error):
        self.stats['errors'] += 1
        self.last_error = str(getattr(error, 'code', type(error).__name__))[:40]

    # -- the worker -------------------------------------------------------------------------------------------------------
    def restore(self):
        """Reload the identities of the rows written in the last copycat window (after a restart). Never raises."""
        self._restored = True
        try:
            since, after = self.clock() - self.cfg['copycat_window_s'], 0
            while True:
                page = self.store.rows_after('observations', after, kind=KIND, since_ts=since, limit=1000)
                for row in page:
                    after = row['id']
                    try:
                        payload = json.loads(bytes(row['raw']).decode('utf-8'))
                    except (ValueError, TypeError):
                        continue
                    self._remember(payload)
                if len(page) < 1000:
                    break
        except Exception as error:                        # noqa: BLE001
            self._count_error(error)

    def _remember(self, payload):
        """Keep the identity of a written row (copycat memory), pruned to the copycat window."""
        ident = payload.get('identity') or {}
        at = payload.get('collected_at')
        keys = (ident.get('name_key'), ident.get('symbol_key'), ident.get('image_key'))
        if isinstance(at, (int, float)) and not isinstance(at, bool) and any(isinstance(k, str) for k in keys):
            self._recent.append((float(at), payload.get('mint'), *keys))
        horizon = self.clock() - self.cfg['copycat_window_s'] - 3600
        if self._recent and self._recent[0][0] < horizon:
            self._recent = [r for r in self._recent if r[0] >= horizon]

    def decision_ts(self, candidate_id, mint, submitted_at):
        """The trader's screen decision of this candidate: the last ``screen`` decision at or before the hand-over."""
        rows = self.store.rows('decisions', mint=mint, kind='screen', limit=1000)
        times = [d['ts'] for d in rows if (candidate_id is None or d['candidate_id'] == candidate_id) and d['ts'] <= submitted_at + 1e-6]
        return times[-1] if times else None

    def _check(self):
        if self._stop is not None and self._stop.is_set():
            raise _Stopped

    def _late(self, decision_ts):
        return self.clock() - decision_ts > self.cfg['max_late_s']

    def _report(self, mint, counter):
        """One report call; ``counter`` counts the requests actually sent. Opens the back-off window on a 429."""
        self._check()
        if self.monotonic() < self._backoff_until:
            self.stats['backoff'] += 1
            self.stats['shed'] += 1
            raise ProviderError('RC_BACKOFF', True)
        try:
            parsed, raw, meta = self.client.report(mint)
        except ProviderError as error:
            sent = error.meta.get('attempts', 0)
            counter[0] += sent if type(sent) is int else 0
            if error.code == 'HTTP_429':
                self.stats['http_429'] += 1
                retry_after = error.meta.get('retry_after') or 0.0
                self._backoff_until = self.monotonic() + max(self.cfg['backoff_s'], float(retry_after))
            if error.code == 'UNKNOWN_MINT':
                self.stats['unknown_mint'] += 1
            else:
                self._count_error(error)
            raise
        counter[0] += meta.get('attempts', 1) if isinstance(meta.get('attempts'), int) else 1
        return parsed, raw, meta

    def collect(self, mint, candidate_id, passed, submitted_at):
        """Build (and return) the row payload of one candidate. Raises only ``_Stopped``."""
        decision = self.decision_ts(candidate_id, mint, submitted_at)
        fields, missing, identity, raw = {}, {}, {}, {}
        counter = [0]

        def all_missing(why):
            for name in FIELDS:
                missing.setdefault(name, why)
        if decision is None:
            all_missing('DECISION_UNKNOWN')
        elif self._late(decision):
            self.stats['late'] += 1
            self.stats['shed'] += 1
            all_missing('COLLECTED_TOO_LATE')
        else:
            try:
                parsed, body, meta = self._report(mint, counter)
            except ProviderError as error:
                all_missing(_why(error))
            else:
                raw = {'bytes': len(body), 'sha256': meta.get('raw_sha256'), 'kept': False}
                if len(body) <= self.cfg['raw_max_bytes']:
                    self.store.add_observation(RAW_KIND, body, mint=mint, candidate_id=candidate_id,
                                               meta={'provider': 'rugcheck', 'endpoint': 'report', 'bytes': len(body)},
                                               code_version=self.code_version, strategy_version=self.strategy_version, ts=self.clock())
                    raw['kept'] = True
                    self.stats['raw_kept'] += 1
                try:
                    f, m, identity, tokens = parse_report(parsed)
                except Exception:                         # noqa: BLE001 - a shape we did not foresee is a gap, not a failure
                    f, m, identity, tokens = {}, {name: 'RC_REPORT_UNREADABLE' for name in FIELDS}, {}, None
                    self._count_error(ValueError('RC_REPORT_UNREADABLE'))
                fields.update(f)
                missing.update(m)
                cf, cm, picks = creator_history(tokens, mint, decision, dead_mcap_usd=self.cfg['dead_mcap_usd'],
                                                sample=self.cfg['creator_sample'])
                fields.update(cf)
                missing.update(cm)
                if tokens is not None and 'creator_tokens_30d' in fields:
                    self._sample(picks, decision, fields, missing, counter)
                kf, km = copycat(identity, mint, decision, self._recent, self.cfg['copycat_window_s'])
                fields.update(kf)
                missing.update(km)
        self.stats['calls'] += counter[0]
        return build_row(mint, screen_passed=passed, decision_ts=decision, collected_at=self.clock(), calls=counter[0], fields=fields,
                         missing=missing, identity=identity, raw=raw, candidate_id=candidate_id)

    def _sample(self, picks, decision, fields, missing, counter):
        """``rugged`` of the newest prior tokens of the creator, one report each, while the row is still in time. A partial sample would
        understate the rugs: ``creator_rugged_sampled`` is then missing (``creator_sampled`` still says how many were read)."""
        read = rugged = 0
        why = None
        for other in picks:
            if self._late(decision):
                why = 'COLLECTED_TOO_LATE'
                break
            try:
                parsed, _, _ = self._report(other, counter)
            except ProviderError as error:
                why = _why(error)
                if why in ('RC_BACKOFF', 'RC_HTTP_429'):
                    break
                continue
            value = parsed.get('rugged')
            if isinstance(value, bool):
                read += 1
                rugged += int(value)
            else:
                why = 'RC_RUGGED_ABSENT'
        if read == len(picks):
            fields['creator_sampled'], fields['creator_rugged_sampled'] = read, rugged
            return
        missing['creator_rugged_sampled'] = why or 'RC_RUGGED_ABSENT'
        if read:
            fields['creator_sampled'] = read
        else:
            missing['creator_sampled'] = why or 'RC_RUGGED_ABSENT'

    def process_one(self, timeout=0.0):
        """Take one queued candidate and write its row. True when one was taken."""
        if not self._restored:
            self.restore()
        try:
            item = self.queue.get(timeout=timeout) if timeout else self.queue.get_nowait()
        except queue.Empty:
            return False
        mint, candidate_id = item[0], item[1]
        try:
            payload = self.collect(*item)
            self.store.add_observation(KIND, canonical(payload), mint=mint, candidate_id=candidate_id, meta=payload,
                                       code_version=self.code_version, strategy_version=self.strategy_version, ts=payload['collected_at'])
            self.stats['rows'] += 1
            self._remember(payload)
        except _Stopped:
            self.stats['stopped'] += 1
            self.stats['shed'] += 1
        except Exception as error:                        # noqa: BLE001 - a recorder bug is a missing row, never a trader error
            self._count_error(error)
            self.stats['dropped'] += 1
            log.debug('risk_external row failed', exc_info=True)
        return True

    def run(self, stop, tick=0.5):
        self._stop = stop
        while not stop.is_set():
            self.process_one(timeout=tick)
        while True:                                        # on stop: the queued candidates are dropped (no call, no row)
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
            self.stats['stopped'] += 1
            self.stats['shed'] += 1

    def health(self):
        return {'enabled': True, 'queued': self.queue.qsize(), 'last_error': self.last_error,
                'backoff_s': round(max(0.0, self._backoff_until - self.monotonic()), 3), **self.stats}


def build(cfg, *, store, code_version, strategy_version, clock=time.time, transport_kwargs=None):
    """The recorder of a validated ``risk_external`` config with its own RugCheck client; None when disabled."""
    if not cfg['enabled']:
        return None
    from lean import providers
    kwargs = {k: v for k, v in dict(transport_kwargs or {}).items() if k in ('opener', 'clock', 'monotonic', 'sleep', 'rng')}
    client = providers.RugCheck(rate_per_s=cfg['rate_per_s'], max_bytes=cfg['max_bytes'], max_attempts=cfg['max_attempts'],
                                request_timeout=cfg['timeout_s'], deadline_seconds=cfg['timeout_s'] * cfg['max_attempts'] + 5, **kwargs)
    return RiskExternal(store=store, client=client, cfg=cfg, code_version=code_version, strategy_version=strategy_version, clock=clock,
                        monotonic=kwargs.get('monotonic', time.monotonic))
