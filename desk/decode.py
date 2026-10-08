"""Read-only transaction observations. Never promote partial capture to trading evidence."""
import json
import sqlite3
from collections import Counter
from pathlib import Path
from .model import digest
from .programs import instruction

SYSTEM = '11111111111111111111111111111111'
TOKENS = {'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
          'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'}


# Only these parsed operations have normalized accounting/lifetime witnesses.
# Recognition is not full instruction or authority-policy approval.
TOKEN_HISTORY_OPERATIONS = {
    'transfer', 'transferChecked', 'mintTo', 'mintToChecked', 'burn', 'burnChecked',
    'closeAccount', 'initializeAccount', 'initializeAccount2', 'initializeAccount3',
    'initializeMint', 'initializeMint2',
}
TOKEN_CONTROL_OPERATIONS = {
    'setAuthority', 'approve', 'approveChecked', 'revoke', 'freezeAccount',
    'thawAccount', 'initializeMultisig', 'initializeMultisig2', 'initializeImmutableOwner',
}


def integer(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError('invalid integer')
    if isinstance(value, str) and not value.isdigit():
        raise ValueError('invalid integer')
    result = int(value)
    if result < 0:
        raise ValueError('negative quantity')
    return result


def decode(payload):
    notification = payload.get('method') == 'transactionNotification'
    envelope = payload['params']['result'] if notification else payload
    container = envelope.get('transaction', {}) if notification else envelope
    tx, meta = container.get('transaction'), container.get('meta')
    if not isinstance(tx, dict) or not isinstance(meta, dict) or 'err' not in meta:
        raise ValueError('missing transaction metadata')
    signatures = tx.get('signatures', [])
    signature = envelope.get('signature') or (signatures[0] if signatures else None)
    if not signature or (signatures and signatures[0] != signature):
        raise ValueError('missing or conflicting signature')
    result = {'signature': signature, 'slot': integer(envelope['slot']),
              'block_time': envelope.get('blockTime'), 'payload_hash': digest(payload),
              'commitment': 'confirmed' if notification else 'unverified',
              'status': 'FAILED' if meta['err'] is not None else 'OBSERVED',
              'token_deltas': [], 'transfers': [], 'mint_initializations': [], 'token_account_initializations': [], 'token_supply_changes': [], 'token_account_closures': [], 'token_control_operations': [], 'program_observations': [],
              'limitations': ['HISTORY_INCOMPLETE', 'NOT_TRADE_EVIDENCE']}
    if meta['err'] is not None:
        return result  # Failed instructions must never create funding links.
    message = tx['message']
    keys = [k['pubkey'] if isinstance(k, dict) else k for k in message['accountKeys']]
    # Compiled messages need a different account-index resolver. Reject rather than guess.
    if not all(isinstance(k, dict) for k in message['accountKeys']):
        raise ValueError('requires jsonParsed account keys')
    balances = {}
    for side in ('pre', 'post'):
        entries = meta.get(side + 'TokenBalances')
        if not isinstance(entries, list):
            result['limitations'].append('TOKEN_BALANCES_MISSING')
            continue
        seen = set()
        for b in entries:
            idx = integer(b['accountIndex'])
            if idx >= len(keys) or idx in seen:
                raise ValueError('invalid or duplicate token account index')
            seen.add(idx)
            amount = integer(b['uiTokenAmount']['amount'])
            decimals = integer(b['uiTokenAmount']['decimals'])
            identity = (idx, b['mint'], b.get('owner'), b.get('programId'), decimals)
            balances.setdefault(identity, {})[side] = amount
    for (idx, mint, owner, program, decimals), values in sorted(balances.items(), key=lambda x: str(x[0])):
        result['token_deltas'].append({'account': keys[idx], 'mint': mint, 'owner': owner,
            'program': program, 'decimals': decimals, 'pre_raw': str(values['pre']) if 'pre' in values else None,
            'post_raw': str(values['post']) if 'post' in values else None,
            'delta_raw': str(values['post'] - values['pre']) if len(values) == 2 else None})
        if len(values) != 2:
            result['limitations'].append('ONE_SIDED_BALANCE')
    instructions = [(str(i), ix) for i, ix in enumerate(message.get('instructions', []))]
    inner = meta.get('innerInstructions')
    if inner is None:
        result['limitations'].append('INNER_INSTRUCTIONS_UNAVAILABLE')
    for group in inner or []:
        instructions.extend((f"{group['index']}.{i}", ix) for i, ix in enumerate(group['instructions']))
    for path, ix in instructions:
        parsed, program = ix.get('parsed'), ix.get('programId')
        if not isinstance(parsed, dict):
            if program in TOKENS:result['limitations'].append('UNDECODED_TOKEN_INSTRUCTION')
            known = instruction(ix)
            if known:
                result['program_observations'].append({'instruction': path, **known})
                if known['status'] in ('IDENTIFIED', 'EVENT_DECODED'):
                    continue
            result['limitations'].append('UNDECODED_PROGRAM_INSTRUCTIONS')
            continue
        kind, info = parsed.get('type'), parsed.get('info', {})
        event = {'instruction': path, 'program': program, 'type': kind}
        if program in TOKENS:
            if isinstance(kind, str) and kind in TOKEN_CONTROL_OPERATIONS:
                # Inventory only: do not infer authority semantics from parser info,
                # balances, or restored end-state. Raw payload remains hash-bound.
                result['token_control_operations'].append(event)
                result['limitations'].append('UNSUPPORTED_TOKEN_CONTROL_OPERATION')
            if (not isinstance(kind, str) or kind not in TOKEN_HISTORY_OPERATIONS
                    or not isinstance(info, dict)):
                result['limitations'].append('UNDECODED_TOKEN_INSTRUCTION')
                if not isinstance(kind, str) or not isinstance(info, dict):
                    result['limitations'].append('MALFORMED_TOKEN_INSTRUCTION')
                continue
        if program == SYSTEM and kind in ('transfer', 'transferWithSeed'):
            result['transfers'].append({**event, 'asset': 'SOL', 'source': info['source'],
                'destination': info['destination'], 'amount_raw': str(integer(info['lamports'])),
                'source_kind': 'unknown'})
        elif program in TOKENS and kind in ('transfer', 'transferChecked'):
            amount = info.get('amount') if kind == 'transfer' else info['tokenAmount']['amount']
            result['transfers'].append({**event, 'asset': 'TOKEN', 'source': info['source'],
                'destination': info['destination'], 'mint': info.get('mint'),
                'amount_raw': str(integer(amount)), 'endpoints_are_token_accounts': True})
        elif program in TOKENS and kind in ('mintTo','mintToChecked','burn','burnChecked'):
            from .programs import address
            amount=info.get('amount') if kind in ('mintTo','burn') else info['tokenAmount']['amount']
            result['token_supply_changes'].append({**event,'mint':address(info['mint']),
                'account':address(info['account']),'amount_raw':str(integer(amount)),
                'direction':'mint' if kind.startswith('mintTo') else 'burn'})
        elif program in TOKENS and kind=='closeAccount':
            from .programs import address
            result['token_account_closures'].append({**event,'account':address(info['account']),
                'destination':address(info['destination']),'owner':address(info['owner'])})
        elif program in TOKENS and kind in ('initializeAccount', 'initializeAccount2', 'initializeAccount3'):
            from .programs import address
            result['token_account_initializations'].append({**event,'account':address(info['account']),
                'mint':address(info['mint']),'owner':address(info['owner'])})
        elif program in TOKENS and kind in ('initializeMint', 'initializeMint2'):
            result['mint_initializations'].append({**event, 'mint': info['mint'],
                'mint_authority': info.get('mintAuthority'), 'freeze_authority': info.get('freezeAuthority'),
                'decimals': integer(info['decimals'])})
    result['limitations'] = sorted(set(result['limitations']))
    return result


def decode_capture(db, output, limit=10000):
    if not 1 <= limit <= 100000:
        raise ValueError('limit must be 1..100000')
    if Path(db).resolve() == Path(output).resolve():
        raise ValueError('output must not overwrite source database')
    counts, limitations = Counter(), Counter()
    seen = set()
    activity = {}
    connection = sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        with Path(output).open('x') as stream:
            for seq, source, received, slot, raw in connection.execute(
                    'SELECT seq,source_id,received_at,slot,payload FROM raw_events ORDER BY seq LIMIT ?', (limit,)):
                counts['raw_records'] += 1
                try:
                    observation = decode(json.loads(raw))
                    if observation['slot'] != slot:
                        raise ValueError('stored slot mismatch')
                    identity = (observation['signature'], observation['slot'])
                    if identity in seen:
                        counts['duplicates'] += 1
                        continue
                    seen.add(identity)
                    counts[observation['status'].lower()] += 1
                    for field in ('transfers', 'token_deltas', 'mint_initializations'):
                        counts[field] += len(observation[field])
                    limitations.update(observation['limitations'])
                    for balance in observation['token_deltas']:
                        mint = balance['mint']
                        item = activity.setdefault(mint, {'mint': mint, 'balance_records': 0,
                            'observed_increases': 0, 'observed_decreases': 0, 'unknown_deltas': 0,
                            'first_observed_slot': observation['slot'], 'last_observed_slot': observation['slot']})
                        item['balance_records'] += 1
                        item['first_observed_slot'] = min(item['first_observed_slot'], observation['slot'])
                        item['last_observed_slot'] = max(item['last_observed_slot'], observation['slot'])
                        delta = balance['delta_raw']
                        if delta is None:
                            item['unknown_deltas'] += 1
                        elif int(delta) > 0:
                            item['observed_increases'] += 1
                        elif int(delta) < 0:
                            item['observed_decreases'] += 1
                except (ValueError, KeyError, TypeError, IndexError) as exc:
                    observation = {'status': 'QUARANTINED', 'error_type': type(exc).__name__}
                    counts['quarantined'] += 1
                stream.write(json.dumps({'source_seq': seq, 'source_id': source,
                    'received_at': received, **observation}, sort_keys=True) + '\n')
        return {'counts': dict(counts), 'limitations': dict(limitations),
                'history_complete': False, 'eligible_for_trading': False,
                'unique_mints': len(activity),
                'token_activity': sorted(activity.values(), key=lambda x: (-x['balance_records'], x['mint']))[:100],
                'notice': 'Balance changes are not classified buys/sells. First observed slot is not launch slot.'}
    finally:
        connection.close()
