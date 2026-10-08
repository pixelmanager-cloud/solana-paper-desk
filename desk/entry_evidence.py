"""Entry gates reconstructed from saved provider responses; summaries cannot approve."""
from collections import defaultdict
import zlib
from .model import digest, decimal
from .security import account_bytes, base58, mint_policy
from .holders import verify_holder_snapshot
from .pools import verify_pool

POLICY = 'persisted-entry-evidence-v4'


def continuation_snapshot(scan, report, store, progress=None):
    """Replay one canonical persisted revision, never caller approval fields.

    Capture mutable pointers/accounting in one SQLite read transaction. Raw
    objects are immutable content-addressed records, so a later published head
    cannot change this evaluation or its revision identity.
    """
    import json
    import sqlite3
    result = {'reasons': ['HISTORY_TO_CURRENT_SNAPSHOT_NOT_RECONCILED'],
              'evidence_hashes': [], 'scope': 'Historical finalized cutoff; not current entry freshness'}
    if store is None or scan is None:
        return result
    try:
        with store.connect() as c:
            c.execute('BEGIN')
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'ownership_heads' not in tables:
                if progress is not None: raise ValueError('Unpersisted progress')
                return result
            head = c.execute('SELECT evidence_hash FROM ownership_heads WHERE scan_id=?', (scan['id'],)).fetchone()
            if not head:
                if progress is not None: raise ValueError('Unpersisted progress')
                return result
            key = head[0]
            if not isinstance(key, str) or len(key) != 64 or any(ch not in '0123456789abcdef' for ch in key):
                raise ValueError('Invalid progress reference')
            # Rejected revisions also need a distinct journal identity. This is
            # the selected pointer, not a claim that its content verified.
            result['revision_hash'] = key
            if progress is not None and progress.get('evidence_hash') != key:
                raise ValueError('Progress revision mismatch')
            if not {'ownership_budgets', 'ownership_banks', 'ownership_history'} <= tables:
                raise ValueError('Continuation accounting missing')
            budget = c.execute('SELECT source_hash,used,ceiling FROM ownership_budgets WHERE id=?', (scan['id'],)).fetchone()
            bank = c.execute('SELECT snapshot_hash,clock_hash FROM ownership_banks WHERE budget=?', (scan['id'],)).fetchone()
            jobs = c.execute('SELECT id,query,coverage,attempts FROM ownership_history WHERE budget=?', (scan['id'],)).fetchall()
        source_hash = digest(dict(scan))
        summary = dict(report); claimed = summary.pop('report_hash', None)
        calls = report.get('calls')
        if (scan['status'] != 'COMPLETE' or report['mint'] != scan['mint']
                or claimed != digest(summary) or type(calls) is not int or not 0 <= calls <= 18
                or not budget or budget[0] != source_hash or type(budget[1]) is not int
                or type(budget[2]) is not int or not calls <= budget[1] <= budget[2] == 18):
            raise ValueError('Immutable source or budget mismatch')
        record = store.load(key)
        if (record['kind'] != 'ownership_progress_v1' or record['scan_id'] != scan['id']
                or record['source_hash'] != source_hash or record['source_report_hash'] != claimed
                or type(record['requests_used']) is not int
                or not calls <= record['requests_used'] <= budget[1]):
            raise ValueError('Progress source/accounting mismatch')
        # Partial heads remain diagnostic; they cannot supply a bank or clock.
        if not bank or not bank[1]:
            return {**result, 'progress_hash': key, 'requests_used': budget[1]}
        if record.get('snapshot_evidence') != {'snapshot_hash': bank[0], 'block_time_hash': bank[1]}:
            raise ValueError('Canonical bank mismatch')
        queries = record['history_queries']
        if not isinstance(queries, list) or len(queries) > 18:
            raise ValueError('Continuation query budget')
        attempts = 0
        persisted = {}
        for identity, query_json, coverage_json, count in jobs:
            query = json.loads(query_json)
            if identity != digest({'budget': scan['id'], 'query': query}) or type(count) is not int or count < 0:
                raise ValueError('Continuation job identity/accounting mismatch')
            attempts += count
            if coverage_json is not None:
                coverage = json.loads(coverage_json)
                bound = {k: coverage[k] for k in ('address', 'start', 'end', 'token_accounts_filter')}
                if 'slot_range' in coverage: bound['slot_range'] = coverage['slot_range']
                if bound != query:
                    raise ValueError('Continuation coverage/query mismatch')
                persisted[digest(coverage)] = coverage
        if budget[1] < calls + attempts + 2:
            raise ValueError('Snapshot/history requests not charged')
        # Each continued query must belong to this scan's persisted jobs. Funding
        # queries preserved verbatim from the source may be outside those jobs.
        originals = {digest(q) for q in report.get('history_queries', [])}
        for query in queries:
            if digest(query) not in persisted and digest(query) not in originals:
                raise ValueError('Unbound continuation query')
        page_count = sum(len(q['pages']) for q in queries)
        if page_count > 18 or page_count + 2 > record['requests_used'] or page_count + 2 > budget[1]:
            raise ValueError('Replay exceeds charged requests')
        updated = {**report, 'history_queries': queries}
        from .replay_history import reconstruct_launch_history, replay_history
        from .ownership_snapshot import validate_bank, reconcile_snapshot
        for query in queries:
            replay_history(query, store)
        history = reconstruct_launch_history(updated, store)
        snapshot, clock = store.load(bank[0]), store.load(bank[1])
        validate_bank(scan['mint'], [a['address'] for a in history['inventory']['accounts']], snapshot)
        replay = reconcile_snapshot(scan['mint'], history, snapshot, clock)
        # Include provenance for all queries, not just the mint query projection.
        hashes = [key, bank[0], bank[1]]
        for query in queries:
            for page in query['pages']:
                hashes.extend([page['request_evidence_hash'], page['payload_hash']])
        result.update(progress_hash=key, requests_used=budget[1],
                      evidence_hashes=list(dict.fromkeys(hashes)), history=history, replay=replay)
        if replay['reconciled']:
            result['reasons'] = []
        else:
            result['reasons'] += replay['reasons']
        return result
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, sqlite3.Error, zlib.error):
        result['reasons'].append('CONTINUATION_RAW_EVIDENCE_UNVERIFIED')
        return result


def evaluate(report, store, *, scan=None, progress=None):
    gates = {}
    metrics = {}
    def gate(name, reasons, hashes=(), **details):
        gates[name] = {'status': 'BLOCKED' if reasons else 'VERIFIED_COMPONENT',
                       'reasons': sorted(set(reasons)), 'evidence_hashes': list(hashes), **details}
    def load(key):
        if store is None or not isinstance(key, str) or len(key) != 64:
            raise ValueError('Persisted evidence required')
        return store.load(key)
    # A checksum detects summary changes; raw replay below remains necessary.
    summary = dict(report); claimed = summary.pop('report_hash', None)
    gate('report_integrity', [] if claimed == digest(summary) else ['REPORT_HASH_MISSING_OR_MISMATCH'])
    mint = report.get('mint')
    try:
        key = report['mint_evidence_hash']; saved = load(key)
        if saved['method'] != 'getAccountInfo' or saved['params'] != [mint, {'encoding':'base64','commitment':'confirmed'}]:
            raise ValueError('Mint request mismatch')
        policy = mint_policy(saved['result']['value'])
        gate('token_controls', policy['reasons'] if policy['decision'] == 'PASS_TOKEN_POLICY' else policy['reasons'] + ['TOKEN_POLICY_FAILED'], [key])
    except (ValueError, KeyError, TypeError, IndexError, zlib.error):
        gate('token_controls', ['TOKEN_RAW_EVIDENCE_UNAVAILABLE'])
    try:
        key = report['holder_snapshot']['evidence_hash']; saved = load(key)
        keys, options = saved['params']; values = saved['result']['value']
        if saved['method'] != 'getMultipleAccounts' or keys[0] != mint or len(keys) != len(values):
            raise ValueError('Holder request mismatch')
        rows = []
        for address, value in zip(keys[1:], values[1:]):
            raw = account_bytes(value)
            if len(raw) != 165: raise ValueError('Invalid holder layout')
            rows.append({'address':address,'wallet':base58(raw[32:64]),'amount_raw':str(int.from_bytes(raw[64:72],'little')),
                         'frozen':raw[108]==2,'delegated_raw':str(int.from_bytes(raw[121:129],'little'))})
        policy = mint_policy(values[0])
        # Positive balances summing to the same-bank supply establish snapshot
        # coverage without trusting an indexed completeness assertion.
        enumeration = {'mint':mint,'coverage_verified':True,'supply_raw':policy['supply_raw'],
                       'indexed_slot_max':options['minContextSlot'],'accounts':rows}
        def rpc(method, params):
            if method != saved['method'] or params != saved['params']:raise ValueError('Holder replay mismatch')
            return saved['result']
        replay = verify_holder_snapshot(enumeration, rpc, capture=digest)
        reasons = list(replay['reasons'])
        if replay.get('evidence_hash') != key: reasons.append('HOLDER_REPLAY_HASH_MISMATCH')
        if replay['delegated_accounts']: reasons.append('DELEGATED_HOLDER_ACCOUNT')
        if any(row['frozen'] for row in rows): reasons.append('FROZEN_HOLDER_ACCOUNT')
        if not reasons:
            totals = defaultdict(int)
            for row in rows:totals[row['wallet']] += int(row['amount_raw'])
            supply = int(policy['supply_raw'])
            metrics['gross_top10_supply_pct'] = str(sum(sorted(totals.values(),reverse=True)[:10])*decimal(100)/supply)
            metrics['holder_owner_count'] = len(totals)
            metrics['holder_slot'] = replay['slot']
        gate('holder_snapshot', reasons, [key], scope='Gross supply; pool/service wallets have not been excluded')
    except (ValueError, KeyError, TypeError, IndexError, ZeroDivisionError, zlib.error):
        gate('holder_snapshot', ['HOLDER_RAW_EVIDENCE_UNAVAILABLE'])
    try:
        pools = report['verified_pools']
        if len(pools) != 1:raise ValueError('Single pool required')
        key = pools[0]['evidence_hash']; saved = load(key)
        if saved['kind'] != 'pool_snapshot':raise ValueError('Pool snapshot required')
        pool = saved['params'][0][3]
        def rpc(method, params):
            if method == 'getAccountInfo' and params == [pool,{'encoding':'base64','commitment':'confirmed'}]:return saved['discovery']
            if method == saved['method'] and params == saved['params']:return saved['result']
            raise ValueError('Pool replay mismatch')
        replay = verify_pool(pool, mint, rpc, capture=digest)
        reasons = list(replay['reasons'])
        if replay.get('evidence_hash') != key: reasons.append('POOL_REPLAY_HASH_MISMATCH')
        if not replay['liquidity_control_verified']:reasons.append('LIQUIDITY_CONTROL_UNVERIFIED')
        gate('pool_liquidity', reasons, [key])
    except (ValueError, KeyError, TypeError, IndexError, zlib.error):
        gate('pool_liquidity', ['POOL_RAW_EVIDENCE_UNAVAILABLE'])
    history = None
    try:
        from .replay_history import reconstruct_launch_history
        history = reconstruct_launch_history(report, store)
        hashes = history['request_hashes'] + history['page_hashes']
        gate('launch_anchor', history['launch_reasons'], hashes)
        gate('mint_history_coverage', history['query_reasons'], hashes,
             scope='Mint-address query only; unchecked transfers require individual account histories')
        gate('historical_account_inventory', history['inventory']['reasons'], hashes)
        transfer_reasons=list(history['account_queries']['reasons'])+history['account_continuity']['reasons']+history['block_ordering']['reasons']
        if not history['observed_movements']['passed']:transfer_reasons.append('OBSERVED_MOVEMENTS_UNRECONCILED')
        transfer_reasons.append('HISTORY_TO_CURRENT_SNAPSHOT_NOT_RECONCILED')
        gate('transfer_history', transfer_reasons, hashes)
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, zlib.error):
        gate('launch_anchor', ['LAUNCH_RAW_HISTORY_UNAVAILABLE'])
        gate('mint_history_coverage', ['REQUEST_BOUND_HISTORY_UNAVAILABLE'])
        gate('historical_account_inventory', ['ACCOUNT_INVENTORY_RAW_HISTORY_UNAVAILABLE'])
        gate('transfer_history', ['TRANSFER_RAW_HISTORY_UNAVAILABLE'])
    continued = continuation_snapshot(scan, report, store, progress)
    gate('history_snapshot', continued['reasons'], continued['evidence_hashes'], scope=continued['scope'])
    if 'progress_hash' in continued:
        metrics['ownership_requests_used'] = continued['requests_used']
    if not continued['reasons']:
        gates['transfer_history']['reasons'] = [r for r in gates['transfer_history']['reasons']
                                               if r != 'HISTORY_TO_CURRENT_SNAPSHOT_NOT_RECONCILED']
        if not gates['transfer_history']['reasons']:
            gates['transfer_history']['reasons'] = ['TRANSFER_HISTORY_NOT_VERIFIED_FOR_ENTRY']
        # Keep the original history gates as historical diagnostics. Only this
        # specific reconciliation component receives continuation verification.
    # These data products do not yet support a complete live-entry attestation.
    # In particular, no observed links is NOT evidence of zero bundle exposure.
    gate('bundle_exposure', ['LAUNCH_TRANSFER_COVERAGE_NOT_VERIFIED_FOR_ENTRY',
         'FUNDING_SERVICE_CLASSIFICATION_NOT_VERIFIED_FOR_ENTRY', 'CURRENT_HOLDER_BUNDLE_EXPOSURE_UNVERIFIED'])
    gate('sellability', ['EXACT_ENTRY_EXIT_ROUTE_POLICY_NOT_VERIFIED'])
    gate('strategy_inputs', ['LIVE_FLOW_MOMENTUM_AND_COST_INPUTS_UNVERIFIED'])
    return {'policy':POLICY,'gates':gates,'metrics':metrics,'launch_history':history,
            'continuation_evidence':continued,'eligible_for_trading':False,
            'reasons':sorted({reason for value in gates.values() for reason in value['reasons']})}
