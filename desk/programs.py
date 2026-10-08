"""Pinned official IDL instruction identities; instruction intent is not a fill."""
import hashlib
import json
from functools import lru_cache
from pathlib import Path
from .security import ALPHABET


def unbase58(text):
    if not isinstance(text, str) or not text or len(text) > 20000:
        raise ValueError('invalid base58')
    n = 0
    for c in text:
        if c not in ALPHABET:
            raise ValueError('invalid base58 character')
        n = n * 58 + ALPHABET.index(c)
    return b'\0' * (len(text) - len(text.lstrip('1'))) + (n.to_bytes((n.bit_length()+7)//8, 'big') if n else b'')


def address(value):
    if not isinstance(value, str) or not 32 <= len(value) <= 44 or len(unbase58(value)) != 32:
        raise ValueError('Expected a Solana public mint address')
    return value


@lru_cache(maxsize=1)
def schemas():
    folder = Path(__file__).parent / 'schemas'
    manifest = json.loads((folder/'manifest.json').read_text())
    result = {}
    for name, record in manifest['files'].items():
        raw = (folder/(name+'.json')).read_bytes()
        if hashlib.sha256(raw).hexdigest() != record['sha256']:
            raise ValueError('Pinned program schema hash mismatch')
        data = json.loads(raw)
        if not record.get('historical') and not record.get('events_only'):
            result[data['address']] = {bytes(x['discriminator']): x for x in data['instructions']}
    return result


def instruction(ix):
    program = ix.get('programId')
    if program not in schemas():
        return None
    raw = unbase58(ix.get('data', ''))
    if raw[:8] == bytes.fromhex('e445a52e51cb9a1d'):
        return event_instruction(ix,raw)
    spec = schemas()[program].get(raw[:8])
    if not spec:
        return {'program': program, 'status': 'UNKNOWN_DISCRIMINATOR'}
    accounts = ix.get('accounts', [])
    if len(accounts) < len(spec['accounts']) or not all(isinstance(a, str) for a in accounts):
        return {'program': program, 'status': 'SCHEMA_ACCOUNT_MISMATCH', 'name': spec['name']}
    names = {a['name']: accounts[i] for i, a in enumerate(spec['accounts'])}
    for entry in spec['accounts']:
        if 'address' in entry and names[entry['name']] != entry['address']:
            return {'program': program, 'status': 'FIXED_ACCOUNT_MISMATCH', 'name': spec['name']}
    name = spec['name']
    kind = 'BUY_INTENT' if name in ('buy', 'buy_v2', 'buy_exact_sol_in', 'buy_exact_quote_in', 'buy_exact_quote_in_v2') else (
        'SELL_INTENT' if name in ('sell', 'sell_v2') else 'LAUNCH' if name in ('create', 'create_v2') else
        'POOL_CREATE' if name == 'create_pool' else 'OTHER')
    if kind in ('BUY_INTENT', 'SELL_INTENT') and len(raw) < 24:
        return {'program': program, 'status': 'TRUNCATED_ARGUMENTS', 'name': name}
    return {'program': program, 'status': 'IDENTIFIED', 'name': name, 'kind': kind,
            'mint': names.get('mint', names.get('base_mint')), 'quote_mint': names.get('quote_mint'),
            'wallet': names.get('user', names.get('creator')),
            'pool': names.get('pool', names.get('bonding_curve')),
            'accounts': names, 'notice': 'Pinned instruction identity, not verified fill or launch completeness.'}

@lru_cache(maxsize=1)
def event_schemas():
    schemas()  # Verify pinned digests before loading event layouts.
    result={}
    folder=Path(__file__).parent/'schemas'
    manifest=json.loads((folder/'manifest.json').read_text())
    for name,record in manifest['files'].items():
        path=folder/(name+'.json')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=record['sha256']:
            raise ValueError('Pinned event schema hash mismatch')
        data=json.loads(raw)
        events = data.get('events', [])
        scope = record.get('event_scope', 'all')
        if scope not in ('all', 'allowlist'):
            raise ValueError('Invalid event scope')
        if scope == 'allowlist' or 'event_allowlist' in record:
            allowed = record.get('event_allowlist')
            if (scope != 'allowlist' or not isinstance(allowed, list) or not allowed
                    or any(not isinstance(value, str) or not value for value in allowed)
                    or len(set(allowed)) != len(allowed)):
                raise ValueError('Invalid event allowlist')
            names = [event['name'] for event in events]
            if any(names.count(value) != 1 for value in allowed):
                raise ValueError('Unknown or ambiguous event allowlist name')
            events = [event for event in events if event['name'] in allowed]
        result.setdefault(data['address'],[]).append(({bytes(e['discriminator']):e['name'] for e in events},
                                  {t['name']:t['type'] for t in data.get('types',[])},path.name))
    return result


class BorshReader:
    def __init__(self, data, types):self.data,self.types,self.pos=data,types,0
    def take(self,n):
        if n<0 or self.pos+n>len(self.data):raise ValueError('Truncated event')
        r=self.data[self.pos:self.pos+n];self.pos+=n;return r
    def read(self,t,depth=0):
        from .security import base58
        if depth>12:raise ValueError('Event nesting limit')
        if isinstance(t,str):
            if t=='pubkey':return base58(self.take(32))
            if t=='bool':
                v=self.take(1)[0]
                if v>1:raise ValueError('Invalid Borsh boolean')
                return bool(v)
            if t=='string':
                n=int.from_bytes(self.take(4),'little')
                if n>4096:raise ValueError('Event string limit')
                return self.take(n).decode('utf-8')
            if t in ('u8','u16','u32','u64','u128','i8','i16','i32','i64','i128'):
                return int.from_bytes(self.take(int(t[1:])//8),'little',signed=t[0]=='i')
        if isinstance(t,dict):
            if 'defined' in t:
                spec=self.types[t['defined']['name']]
                if spec['kind']!='struct':raise ValueError('Unsupported Borsh definition')
                return {f['name']:self.read(f['type'],depth+1) for f in spec['fields']}
            if 'vec' in t:
                n=int.from_bytes(self.take(4),'little')
                if n>256:raise ValueError('Event vector limit')
                return [self.read(t['vec'],depth+1) for _ in range(n)]
            if 'array' in t:
                subtype,n=t['array']
                if n>256:raise ValueError('Event array limit')
                return [self.read(subtype,depth+1) for _ in range(n)]
            if 'option' in t:
                v=self.take(1)[0]
                if v>1:raise ValueError('Invalid option')
                return self.read(t['option'],depth+1) if v else None
        raise ValueError('Unsupported event field')


def event_instruction(ix,raw):
    from solders.pubkey import Pubkey
    program=ix['programId']
    authority=str(Pubkey.find_program_address([b'__event_authority'],Pubkey.from_string(program))[0])
    if not ix.get('accounts') or ix['accounts'][0]!=authority:
        return {'program':program,'status':'EVENT_AUTHORITY_MISMATCH'}
    partial = None
    for events,types,version in event_schemas()[program]:
        name=events.get(raw[8:16])
        if not name:continue
        reader=BorshReader(raw[16:],types)
        try:
            fields={field['name']:reader.read(field['type']) for field in types[name]['fields']}
            if reader.pos!=len(reader.data):
                candidate={'program':program,'status':'EVENT_PREFIX_DECODED','name':name,'kind':'PROGRAM_EVENT',
                    'fields':fields,'schema_complete':False,'schema_file':version,
                    'unknown_trailing_bytes':len(reader.data)-reader.pos}
                if partial is None or candidate['unknown_trailing_bytes']<partial['unknown_trailing_bytes']:partial=candidate
                continue
        except (ValueError,KeyError,UnicodeError):continue
        return {'program':program,'status':'EVENT_DECODED','name':name,'kind':'PROGRAM_EVENT',
                'fields':fields,'schema_complete':True,'schema_file':version}
    return partial or {'program':program,'status':'EVENT_SCHEMA_MISMATCH','kind':'PROGRAM_EVENT'}
