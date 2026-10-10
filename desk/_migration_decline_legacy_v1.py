"""Read-only retained Pump migration syntax witness, never chain authentication.

extract_graduation(raw_transactions, *, mint, pool, now, provenance) accepts
at most 256 original decoded JSON transaction payloads (128KiB each, 2MiB
aggregate). Callers may flatten retained page data; no completeness assertion
is accepted. Hashes identify canonical retained JSON, not original wire bytes.
Only outer migrate/migrate_v2 and direct stackHeight=2 event CPI are supported.
No provider calls, writes, capture, freshness substitution or entry promotion.
"""
from solders.pubkey import Pubkey
from .decode import decode
from .model import canonical, digest
from .programs import address, unbase58

PUMP = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
AMM = 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
SOL = 'So11111111111111111111111111111111111111112'
NATIVE_SOL_SENTINEL = '11111111111111111111111111111111'
PROVENANCES = {'SYNTHETIC_TEST_ONLY', 'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE'}


def _pda(seeds, program):
    return str(Pubkey.find_program_address(seeds, Pubkey.from_string(program))[0])


def extract_graduation(raw_transactions, *, mint, pool, now, provenance):
    """Return historical migration time or exact UNKNOWN; now only rejects future.

    Runtime must bind these retained hashes to its trusted evidence store/source.
    A returned witness does not attest provider authenticity or current safety.
    """
    address(mint); address(pool)
    if type(now) is not int or not 0 <= now < 2**63 or provenance not in PROVENANCES:
        raise ValueError('Trusted clock and explicit fixture/capture provenance required')
    if type(raw_transactions) not in (list, tuple) or len(raw_transactions) > 256:
        raise ValueError('Bounded original transaction sequence required')
    result = {'status':'UNKNOWN', 'graduated_at':None, 'blockers':[], 'witnesses':[],
              'source_hashes':[], 'provenance':provenance,
              'scope':'HISTORICAL_MIGRATION_SYNTAX_NOT_CURRENT_SAFETY',
              'entry_authorized':False}
    blockers = set(); seen = {}; slots = {}; total = 0
    curve = _pda([b'bonding-curve', bytes(Pubkey.from_string(mint))], PUMP)
    for raw in raw_transactions:
        try:
            if type(raw) is not dict:raise ValueError('shape')
            size = len(canonical(raw).encode()); total += size
            if size > 128*1024 or total > 2*1024*1024:
                blockers.add('RAW_TRANSACTION_BYTE_BOUND'); break
            source_hash = digest(raw); result['source_hashes'].append(source_hash)
            decoded = decode(raw)
            signature = decoded['signature']; slot = decoded['slot']; at = decoded['block_time']
            if type(signature) is not str or len(signature) > 128:raise ValueError('signature')
            if signature in seen:
                if seen[signature] != source_hash:blockers.add('CONFLICTING_TRANSACTION_IDENTITY')
                continue
            seen[signature] = source_hash
            if type(at) is not int or not 0 <= at <= now or not 0 <= slot < 2**63:raise ValueError('block time/slot')
            if slot in slots and slots[slot] != at:blockers.add('CONFLICTING_SLOT_TIME')
            slots[slot] = at
            if decoded['status'] != 'OBSERVED':continue
            # decode already performs compiled key resolution and pinned parsing.
            rows = decoded['program_observations']
            intents = {r['instruction']:r for r in rows if r['program']==PUMP
                       and r.get('name') in ('migrate','migrate_v2')
                       and r.get('status')=='IDENTIFIED' and '.' not in r['instruction']}
            envelope = raw['params']['result'] if raw.get('method')=='transactionNotification' else raw
            container = envelope['transaction'] if raw.get('method')=='transactionNotification' else envelope
            groups = container['meta'].get('innerInstructions') or []
            for event in rows:
                if event['program'] != PUMP or event.get('name') != 'CompletePumpAmmMigrationEvent':continue
                f = event.get('fields', {})
                path = event['instruction'].split('.')
                if len(path)!=2 or path[0] not in intents:
                    if f.get('mint')==mint:blockers.add('MIGRATION_INSTRUCTION_SCOPE_UNAVAILABLE')
                    continue
                intent = intents[path[0]]; accounts = intent['accounts']
                parent_mint = accounts.get('mint',accounts.get('base_mint'))
                # Scope first: a wrong-mint sibling under the target migration is
                # contradictory supplied evidence, not an unrelated observation.
                if parent_mint != mint and f.get('mint') != mint:continue
                if f.get('mint') != mint:
                    blockers.add('MIGRATION_ACCOUNT_BINDING_MISMATCH');continue
                groups_at = [g for g in groups if type(g.get('index')) is int and str(g['index'])==path[0]]
                if len(groups_at)!=1:raise ValueError('ambiguous CPI group')
                ix = groups_at[0]['instructions'][int(path[1])]
                if type(ix.get('stackHeight')) is not int or ix['stackHeight']!=2:
                    blockers.add('DIRECT_MIGRATION_EVENT_CPI_SCOPE_UNAVAILABLE');continue
                outer = container['transaction']['message']['instructions'][int(path[0])]
                if len(unbase58(outer.get('data',''))) != 8:
                    blockers.add('MIGRATION_INSTRUCTION_ARGUMENT_LAYOUT_UNKNOWN');continue
                pool_authority = _pda([b'pool-authority', bytes(Pubkey.from_string(mint))], PUMP)
                expected_pool = _pda([b'pool', b'\0\0', bytes(Pubkey.from_string(accounts['pool_authority'])),
                                      bytes(Pubkey.from_string(mint)), bytes(Pubkey.from_string(SOL))], AMM)
                # SOL curves store the zero key; migrate_v2 uses WSOL for its
                # pool/interface. This exception is event-only: outer WSOL,
                # canonical WSOL pool and every other binding stay mandatory.
                event_quote = f.get('quote_mint')
                event_quote_matches = (event_quote == SOL or
                    (intent['name'] == 'migrate_v2' and event_quote == NATIVE_SOL_SENTINEL))
                bindings = (accounts.get('pool_authority')==pool_authority, accounts.get('mint',accounts.get('base_mint'))==mint,
                            accounts.get('bonding_curve')==curve, accounts.get('pool')==pool==expected_pool,
                            accounts.get('quote_mint',accounts.get('wsol_mint'))==SOL,
                            accounts.get('program')==PUMP, f.get('bonding_curve')==curve,
                            f.get('pool')==pool, event_quote_matches,
                            f.get('user')==accounts.get('user'))
                if not all(bindings):blockers.add('MIGRATION_ACCOUNT_BINDING_MISMATCH');continue
                if event.get('status')!='EVENT_DECODED' or event.get('schema_complete') is not True:
                    blockers.add('MIGRATION_EVENT_LAYOUT_INCOMPLETE');continue
                if type(f.get('timestamp')) is not int or f['timestamp']!=at:
                    blockers.add('MIGRATION_TIMESTAMP_BLOCK_TIME_MISMATCH');continue
                result['witnesses'].append({'timestamp':at,'slot':slot,'signature':signature,
                    'payload_hash':source_hash,'instruction':intent['instruction'],
                    'event_instruction':event['instruction'],'schema_file':event['schema_file'],
                    'mint':mint,'bonding_curve':curve,'pool':pool,'quote_mint':SOL,
                    'event_quote_mint':event_quote})
        except (ValueError, KeyError, TypeError, IndexError, OverflowError):
            blockers.add('MALFORMED_RAW_TRANSACTION_OR_TIME')
    ordered = sorted(slots.items())
    if any(a[1]>b[1] for a,b in zip(ordered,ordered[1:])):blockers.add('REGRESSING_SLOT_TIME')
    times = {w['timestamp'] for w in result['witnesses']}
    if len(times)>1:blockers.add('CONFLICTING_MIGRATION_TIMESTAMPS')
    if not times:blockers.add('MIGRATION_WITNESS_ABSENT')
    result['blockers'] = sorted(blockers)
    if not blockers:
        result.update(status='OBSERVED_MIGRATION', graduated_at=next(iter(times)))
    return result
