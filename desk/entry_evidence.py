"""Entry gates reconstructed from saved provider responses; summaries cannot approve."""
from collections import defaultdict
from .model import digest, decimal
from .security import account_bytes, base58, mint_policy
from .holders import verify_holder_snapshot
from .pools import verify_pool

POLICY = 'persisted-entry-evidence-v3'


def evaluate(report, store):
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
    except (ValueError, KeyError, TypeError, IndexError):
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
    except (ValueError, KeyError, TypeError, IndexError, ZeroDivisionError):
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
    except (ValueError, KeyError, TypeError, IndexError):
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
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        gate('launch_anchor', ['LAUNCH_RAW_HISTORY_UNAVAILABLE'])
        gate('mint_history_coverage', ['REQUEST_BOUND_HISTORY_UNAVAILABLE'])
        gate('historical_account_inventory', ['ACCOUNT_INVENTORY_RAW_HISTORY_UNAVAILABLE'])
        gate('transfer_history', ['TRANSFER_RAW_HISTORY_UNAVAILABLE'])
    # These data products do not yet support a complete live-entry attestation.
    # In particular, no observed links is NOT evidence of zero bundle exposure.
    gate('bundle_exposure', ['LAUNCH_TRANSFER_COVERAGE_NOT_VERIFIED_FOR_ENTRY',
         'FUNDING_SERVICE_CLASSIFICATION_NOT_VERIFIED_FOR_ENTRY', 'CURRENT_HOLDER_BUNDLE_EXPOSURE_UNVERIFIED'])
    gate('sellability', ['EXACT_ENTRY_EXIT_ROUTE_POLICY_NOT_VERIFIED'])
    gate('strategy_inputs', ['LIVE_FLOW_MOMENTUM_AND_COST_INPUTS_UNVERIFIED'])
    return {'policy':POLICY,'gates':gates,'metrics':metrics,'launch_history':history,'eligible_for_trading':False,
            'reasons':sorted({reason for value in gates.values() for reason in value['reasons']})}
