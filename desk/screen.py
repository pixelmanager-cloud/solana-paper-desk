"""Bounded live research. Unknown coverage never becomes a safety pass."""
import time
from collections import defaultdict
from decimal import Decimal
from .decode import decode, integer
from .programs import address
from .providers import helius_rpc, jupiter_probe, SOL
from .security import mint_policy, account_bytes, base58, TOKEN_PROGRAM, TOKEN_2022
from .model import digest
from .holders import enumerate_holders,verify_holder_snapshot
from .pools import verify_pool
from .providers import PUMPSWAP


def screen(mint, rpc=helius_rpc, quote=jupiter_probe, clock=time.time,history_capture=None):
    address(mint)
    now = int(clock())
    result = {'mint': mint, 'observed_at': now, 'mode': 'RESEARCH_ONLY', 'decision': 'SKIP',
              'eligible_for_trading': False, 'findings': [], 'unknowns': [], 'calls': 0,
              'notice': 'A route or absence of detected links does not prove sellability or safety.'}
    def call(method, params):
        if result['calls'] >= 18:
            raise ValueError('Scan request budget exhausted')
        result['calls'] += 1
        return rpc(method, params)
    def attempt(label, fn):
        try:
            return fn()
        except (ValueError, KeyError, TypeError, IndexError, OSError):
            result['unknowns'].append(label + '_UNAVAILABLE')
            return None
    account = call('getAccountInfo', [mint, {'encoding': 'base64', 'commitment': 'confirmed'}])
    if history_capture is not None:
        envelope={'method':'getAccountInfo','params':[mint,{'encoding':'base64','commitment':'confirmed'}],'result':account}
        key=history_capture(envelope)
        if key!=digest(envelope):raise ValueError('Mint evidence hash mismatch')
        result['mint_evidence_hash']=key
    policy = mint_policy(account.get('value'))
    result['token_policy'] = policy
    result['findings'].extend(policy['reasons'])
    raw_mint = account.get('value')
    supply = int(policy.get('supply_raw', 0))
    if raw_mint and raw_mint.get('owner') == TOKEN_2022:
        from .extensions import inspect_mint_extensions
        result['token_extensions']=inspect_mint_extensions(raw_mint)
        result['findings'].extend(result['token_extensions']['capabilities'])
        result['unknowns'].extend(result['token_extensions']['reasons'])
        data = account_bytes(raw_mint)
        if len(data) >= 82:
            supply = int.from_bytes(data[36:44], 'little')
    result['mint_slot'] = account.get('context', {}).get('slot')
    holders = defaultdict(int)
    holding_accounts = []
    largest = attempt('HOLDERS', lambda: call('getTokenLargestAccounts', [mint, {'commitment': 'confirmed'}]))
    if largest and largest.get('value'):
        addresses = [x['address'] for x in largest['value']]
        accounts = attempt('HOLDER_OWNERS', lambda: call('getMultipleAccounts', [addresses, {'encoding':'base64','commitment':'confirmed'}]))
        if accounts:
            for token_address, item in zip(addresses, accounts['value']):
                if not item or item.get('owner') not in (TOKEN_PROGRAM, TOKEN_2022):
                    result['unknowns'].append('HOLDER_ACCOUNT_UNSUPPORTED'); continue
                data = account_bytes(item)
                if len(data) < 165 or base58(data[:32]) != mint:
                    result['unknowns'].append('HOLDER_ACCOUNT_MISMATCH'); continue
                owner = base58(data[32:64])
                raw_amount = int.from_bytes(data[64:72], 'little')
                holders[owner] += raw_amount
                holding_accounts.append({'address':token_address,'wallet':owner,'amount':raw_amount})
                if data[108] == 2:
                    result['findings'].append('FROZEN_HOLDER_ACCOUNT')
                if int.from_bytes(data[72:76], 'little'):
                    result['findings'].append('DELEGATED_HOLDER_ACCOUNT')
    result['holders'] = [{'wallet': w, 'amount_raw': str(n),
        'gross_supply_pct': str(Decimal(n)*100/supply) if supply else None}
        for w,n in sorted(holders.items(), key=lambda x: (-x[1], x[0]))]
    result['holder_evidence']={'source':'LARGEST_ACCOUNTS_DIAGNOSTIC','verified':False,'slot':None}
    result['holder_scope'] = 'Largest 20 token accounts only; owners aggregated. Pool vaults not excluded.'
    result['holder_coverage_pct'] = str(Decimal(sum(holders.values()))*100/supply) if supply else None
    result['unknowns'].extend(['FULL_HOLDER_COVERAGE', 'POOL_VAULT_CLASSIFICATION', 'LIQUIDITY_LOCK_OR_CONTROL'])
    if supply and sum(holders.values()) > supply:
        result['unknowns'].append('INCONSISTENT_SUPPLY_SNAPSHOT')
    if supply and type(result['mint_slot']) is int:
        complete_holders=attempt('FULL_HOLDER_ENUMERATION',lambda:enumerate_holders(mint,supply,result['mint_slot'],call,max_pages=2))
        if complete_holders:
            result['holder_enumeration']={k:v for k,v in complete_holders.items() if k not in ('accounts','holders')}
            result['holders']=complete_holders['holders'][:100]
            result['holder_evidence']={'source':'INDEXED_ENUMERATION_DIAGNOSTIC','verified':False,'slot':complete_holders['indexed_slot_max']}
            result['holder_scope']='Indexed holder enumeration, up to 2,000 accounts. See reconciliation and slot coverage.'
            result['holder_coverage_pct']=complete_holders['coverage_pct']
            if complete_holders['coverage_verified']:
                snapshot=attempt('ATOMIC_HOLDER_SNAPSHOT',lambda:verify_holder_snapshot(complete_holders,call,capture=history_capture)) if policy['decision']=='PASS_TOKEN_POLICY' else None
                if policy['decision']!='PASS_TOKEN_POLICY':result['unknowns'].append('ATOMIC_HOLDER_SNAPSHOT_UNSUPPORTED_TOKEN')
                if snapshot:
                    result['holder_snapshot']=snapshot
                    result['unknowns'].extend(snapshot['reasons'])
                    if snapshot['verified']:
                        result['unknowns'].remove('FULL_HOLDER_COVERAGE')
                        result['holder_evidence']={'source':'PERSISTED_SINGLE_BANK_SNAPSHOT','verified':True,'slot':snapshot['slot'],'evidence_hash':snapshot['evidence_hash']}
                        result['holder_scope']='All positive legacy token holdings reconciled in one persisted bank snapshot. Wallet/service classification remains incomplete.'
                    if snapshot['delegated_accounts']:result['findings'].append('DELEGATED_HOLDER_ACCOUNT')
                holders=defaultdict(int,{h['wallet']:int(h['amount_raw']) for h in complete_holders['holders']})
                for h in complete_holders['accounts']:
                    if h['frozen']:result['findings'].append('FROZEN_HOLDER_ACCOUNT')
                    if int(h['delegated_raw']):result['findings'].append('DELEGATED_HOLDER_ACCOUNT')
            else:result['unknowns'].extend(complete_holders['reasons'])
    result['history_queries']=[]
    def history(owner,start,end,pages=1,token_accounts="balanceChanged"):
        from .history import collect_history
        observations,coverage=collect_history(owner,start,end,call,max_pages=pages,capture=history_capture,token_accounts=token_accounts)
        result['history_queries'].append(coverage)
        result['unknowns'].extend(coverage['reasons'])
        return observations,coverage['query_coverage_verified']
    chain = attempt('TOKEN_HISTORY', lambda: history(mint, max(0, now-21600), now, 2,token_accounts="none"))
    observations, range_complete = chain if chain else ([], False)
    result['history'] = {'window_seconds':21600,'transactions':len(observations),
                         'query_coverage_verified':range_complete,'launch_history_complete':False}
    intents, launches = [], []
    for obs in observations:
        for ix in obs.get('program_observations', []):
            if ix.get('status') != 'IDENTIFIED' or ix.get('mint') != mint: continue
            entry = {**ix, 'signature':obs['signature'],'slot':obs['slot'],'block_time':obs['block_time'],'commitment':obs.get('commitment')}
            if ix['kind'] == 'LAUNCH': launches.append(entry)
            if ix['kind'] in ('BUY_INTENT','SELL_INTENT'): intents.append(entry)
    pools=list(dict.fromkeys(ix['pool'] for ix in intents if ix['program']==PUMPSWAP and ix.get('pool')))
    result['verified_pools']=[]
    for pool in pools[:1]:
        verified=attempt('POOL_VERIFICATION',lambda:verify_pool(pool,mint,call,capture=history_capture))
        if verified:
            result['verified_pools'].append(verified)
            result['findings'].extend(verified['reasons'])
    if result['verified_pools']:
        result['pool_scope']='One observed PumpSwap pool verified; other venues/vaults may exist.'
        # Exclude exact vault accounts, not arbitrary owner wallets, from the indexed denominator.
        if 'complete_holders' in locals() and complete_holders and complete_holders['coverage_verified']:
            vault_keys={v['address'] for p in result['verified_pools'] if p.get('identity_evidence_verified') for v in p['vaults'] if v['mint']==mint}
            private_totals=defaultdict(int);excluded=0
            for row in complete_holders['accounts']:
                n=int(row['amount_raw'])
                if row['address'] in vault_keys:excluded+=n
                else:private_totals[row['wallet']]+=n
            denominator=supply-excluded
            result['holder_supply_excluding_verified_vaults_raw']=str(denominator)
            result['holders_excluding_verified_vaults']=[{'wallet':w,'amount_raw':str(n),
                'observed_holder_supply_pct':str(Decimal(n)*100/denominator) if denominator>0 else None}
                for w,n in sorted(private_totals.items(),key=lambda x:(-x[1],x[0]))][:100]
            result['unknowns'].append('OTHER_POOL_VAULT_COVERAGE')
    from .launch import launch_anchor
    result['launch_anchors']=[]
    for observation in observations:
        if any(x.get('kind')=='LAUNCH' and x.get('mint')==mint for x in observation.get('program_observations',[])):
            anchor=attempt('LAUNCH_ANCHOR',lambda:launch_anchor(observation,mint))
            if anchor:result['launch_anchors'].append(anchor)
    verified_anchors=[x for x in result['launch_anchors'] if x['verified']]
    from .account_history import account_inventory
    if result['history_queries']:
        inventory=attempt('HISTORICAL_ACCOUNT_INVENTORY',lambda:account_inventory(mint,observations,result['history_queries'][0]))
        if inventory:
            result['historical_token_accounts']=inventory
            result['unknowns'].extend(inventory['reasons'])
            if inventory['initialization_inventory_verified']:
                from .account_history import collect_account_histories
                account_history=attempt('ACCOUNT_HISTORY',lambda:collect_account_histories(inventory,observations,call,capture=history_capture,max_accounts=2))
                if account_history:
                    observations=account_history['observations']
                    result['account_history']={k:v for k,v in account_history.items() if k!='observations'}
                    result['history_queries'].extend(account_history['queries'])
                    result['unknowns'].extend(account_history['reasons'])
                    result['unknowns'].extend(account_history['account_continuity']['reasons'])
                    result['unknowns'].extend(account_history['block_ordering']['reasons'])
    result['history']['launch_anchor_verified']=len(verified_anchors)==1
    if not result['history']['launch_anchor_verified']:result['unknowns'].append('LAUNCH_ANCHOR_UNVERIFIED')
    if any(x.get('mayhem_mode') is True for x in result['launch_anchors']):result['findings'].append('LAUNCH_MAYHEM_MODE')
    result['launches'] = launches
    result['trade_intents'] = intents[:100]
    result['unknowns'].append('COMPLETE_LAUNCH_AND_TRANSFER_HISTORY')
    # Time/order are only taken from chain metadata, never receive-time substitutes.
    first_buys = {}
    for ix in sorted(intents,key=lambda x:x['slot']):
        if ix['kind'] == 'BUY_INTENT' and type(ix['block_time']) is int and ix.get('wallet'):
            first=first_buys.setdefault(ix['wallet'],dict(ix,same_slot_buy_signatures=[]))
            if ix['slot']==first['slot'] and ix['signature'] not in first['same_slot_buy_signatures']:
                first['same_slot_buy_signatures'].append(ix['signature'])
    targets = list(first_buys)[:6]
    result['funding_scope'] = {'early_buyers_observed':len(first_buys), 'wallets_selected':len(targets),'wallets_queried':0,
                               'max_history_transactions_per_wallet':100, 'lookback_seconds':3600}
    fundees, edges = defaultdict(set), []
    result['funding_ordering_ambiguities']=[]
    for wallet in targets:
        buy=first_buys[wallet];bought=buy['block_time']
        past = attempt('FUNDING_HISTORY', lambda: history(wallet,max(0,bought-3600),bought+1))
        if not past: continue
        result['funding_scope']['wallets_queried']+=1
        coverage=result['history_queries'][-1]
        from .funding import observed_funding
        funding=observed_funding(past[0],wallet,buy,ordering=result.get('account_history',{}).get('block_ordering'))
        result['unknowns'].extend(funding['reasons'])
        result['funding_ordering_ambiguities'].extend(funding['ambiguous_transfers'])
        for edge in funding['edges']:
            edge['query_evidence_hash']=coverage['evidence_hash']
            edge['raw_pages_persisted']=coverage['raw_pages_persisted']
            edge['query_coverage_verified']=coverage['query_coverage_verified']
            fundees[edge['source']].add(wallet);edges.append(edge)
    result['funding_edges'] = edges
    result['shared_funding_candidates'] = [{'source':source,'wallets':sorted(wallets),
        'classification':'UNVERIFIED_SOURCE_MAY_BE_EXCHANGE_OR_SERVICE','holder_evidence':result['holder_evidence'],
        'gross_supply_pct':str(sum((Decimal(holders.get(w,0))*100/supply for w in wallets),Decimal(0))) if supply else None}
        for source,wallets in sorted(fundees.items()) if len(wallets)>1]
    if result['shared_funding_candidates']:
        result['findings'].append('SHARED_FUNDING_REQUIRES_REVIEW')
    if len(verified_anchors)==1:
        launch_slot = verified_anchors[0]['slot']
        early = {x['wallet'] for x in intents if x['kind']=='BUY_INTENT' and 0<=x['slot']-launch_slot<=3}
        result['early_cohort'] = {'launch_slot':launch_slot,'holder_evidence':result['holder_evidence'],'wallets':sorted(early),
             'gross_supply_pct':str(sum((Decimal(holders.get(w,0))*100/supply for w in early),Decimal(0))) if supply else None}
    if supply and len(verified_anchors)==1:
        from .distribution import trace_distribution
        positions={}
        for ix in sorted(intents,key=lambda x:x['slot']):
            if ix['kind']=='BUY_INTENT' and 0<=ix['slot']-verified_anchors[0]['slot']<=3:
                positions.setdefault(ix['wallet'],{'slot':ix['slot'],'signature':ix['signature'],'instruction':ix['instruction']})
        rows=complete_holders['accounts'] if 'complete_holders' in locals() and complete_holders and complete_holders['coverage_verified'] else [
            {'address':h['address'],'wallet':h['wallet'],'amount_raw':str(h['amount'])} for h in holding_accounts]
        vault_keys={v['address'] for p in result['verified_pools'] if p.get('identity_evidence_verified') for v in p['vaults'] if v['mint']==mint}
        trace=attempt('DISTRIBUTION_TRACE',lambda:trace_distribution(mint,supply,observations,positions,rows,vault_keys,ordering=result.get('account_history',{}).get('block_ordering')))
        if trace:
            trace['holder_evidence']=result['holder_evidence']
            result['distribution_trace']=trace;result['unknowns'].extend(trace['reasons'])
            if trace['links']:result['findings'].append('EARLY_DISTRIBUTION_PATHS_REQUIRE_REVIEW')
    result['unknowns'].extend(['FUNDING_SOURCE_CLASSIFICATION','MULTIHOP_DISTRIBUTION_COVERAGE','EXACT_SIZE_SELL_SIMULATION'])
    # Route checks are quotes, never a sellability attestation. Disposable unfunded public address is a probe identity; its key was discarded.
    if mint != SOL:
        taker = 'AHV1J4AZroxCntiJBiJnChNCQqE8Fii7UmvxdQhwjct5'
        buy = attempt('BUY_ROUTE', lambda: quote(SOL,mint,10000000,taker))
        if buy:
            quantity = integer(buy['response']['outAmount'])
            sell = attempt('SELL_ROUTE', lambda: quote(mint,SOL,quantity,taker)) if quantity else None
            if sell:
                back = integer(sell['response']['outAmount'])
                result['roundtrip_quote'] = {'input_lamports':10000000,'token_raw':str(quantity),
                    'quoted_return_lamports':str(back),'quoted_loss_bps':str((Decimal(10000000-back)*10000)/10000000),
                    'simulation_performed':False, 'buy_route_hash':digest(buy),'sell_route_hash':digest(sell),
                    'notice':'Sequential quotes, not fills. Excludes network fees, rent, intervening price changes.'}
    if result.get('roundtrip_quote') and Decimal(result['roundtrip_quote']['quoted_loss_bps']) > 800:
        result['findings'].append('ROUNDTRIP_QUOTE_LOSS_GT_8PCT')
    if policy['decision']=='PASS_TOKEN_POLICY' and result.get('roundtrip_quote'):
        try:
            from solders.pubkey import Pubkey
            from .simulate import simulate_sell
            size=int(result['roundtrip_quote']['token_raw'])
            candidate=next((x for x in holding_accounts if x['amount']>=size and Pubkey.from_string(x['wallet']).is_on_curve()),None)
            if candidate:
                simulation=attempt('SELL_SIMULATION',lambda:simulate_sell(mint,candidate['wallet'],candidate['address'],size,rpc=call,quote=quote,capture=history_capture,pool_snapshot=next(iter(result['verified_pools']),None)))
                if simulation:
                    result['sell_simulation']=simulation
                    result['unknowns'].extend(simulation.get('instruction_inventory',{}).get('reasons',[]))
                    result['unknowns'].extend(simulation.get('router_checks',{}).get('reasons',[]))
                    result['unknowns'].extend(simulation.get('envelope_checks',{}).get('reasons',[]))
                    result['unknowns'].extend(simulation.get('wallet_debit_checks',{}).get('reasons',[]))
                    result['unknowns'].extend(simulation.get('amm_bindings',{}).get('reasons',[]))
                    result['unknowns'].extend(simulation.get('recipient_checks',{}).get('reasons',[]))
                    result['unknowns'].extend(simulation.get('fee_checks',{}).get('reasons',[]))
                    result['unknowns'].extend(simulation.get('fee_query',{}).get('reasons',[]))
                    result['unknowns'].extend(simulation.get('sell_event_checks',{}).get('reasons',[]))
                    result['unknowns'].extend(simulation.get('fee_split_checks',{}).get('reasons',[]))
                    result['unknowns'].extend(simulation.get('setup_checks',{}).get('reasons',[]))
                    result['unknowns'].extend(simulation.get('route_coverage',{}).get('reasons',[]))
                    effects_ok=simulation.get('balance_effects',{}).get('passed') is True
                    controls_ok=simulation.get('account_controls',{}).get('passed') is True
                    if simulation.get('raw_evidence_persisted') is not True:result['unknowns'].append('SELL_SIMULATION_EVIDENCE_NOT_PERSISTED')
                    if simulation['simulation_ok'] and effects_ok and controls_ok and simulation.get('fresh') is True and simulation.get('raw_evidence_persisted') is True:
                        result['unknowns'].remove('EXACT_SIZE_SELL_SIMULATION')
                        result['unknowns'].extend(['REPRESENTATIVE_HOLDER_ONLY','TRANSACTION_EFFECT_POLICY'])
                    else:
                        result['findings'].append('SELL_SIMULATION_OR_EFFECT_CHECK_FAILED')
                        if simulation.get('fresh') is not True:result['unknowns'].append('SELL_SIMULATION_STALE_OR_FRESHNESS_UNKNOWN')
                        result['findings'].extend(simulation.get('balance_effects',{}).get('reasons',[]))
                        result['findings'].extend(simulation.get('account_controls',{}).get('reasons',[]))
            else:result['unknowns'].append('NO_REPRESENTATIVE_HOLDER')
        except ImportError:result['unknowns'].append('SIMULATION_DEPENDENCY_MISSING')
    result['findings'] = sorted(set(result['findings']))
    result['unknowns'] = sorted(set(result['unknowns']))
    result['completed_at'] = int(clock())
    result['report_hash'] = digest(result)
    return result
