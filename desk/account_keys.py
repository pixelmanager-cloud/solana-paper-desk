"""Resolve compiled JSON keys without synthesizing jsonParsed instruction data.

Privileges are requested OUTER-message privileges, not runtime demotion state or
CPI signer authority. Loaded keys are never transaction signers.
"""
from .programs import address


def index(value, size):
    if type(value) is not int or not 0 <= value < min(size, 256):
        raise ValueError('Invalid compiled account index')
    return value


def compiled_keys(message, meta, version, signatures):
    if version != 'legacy' and not (type(version) is int and version == 0):
        raise ValueError('Unsupported transaction version')
    values = message.get('accountKeys')
    if not isinstance(values, list) or not 1 <= len(values) <= 256 or not all(isinstance(k, str) for k in values):
        raise ValueError('Invalid compiled static keys')
    static = [address(k) for k in values]
    header = message.get('header')
    fields = {'numRequiredSignatures', 'numReadonlySignedAccounts', 'numReadonlyUnsignedAccounts'}
    if not isinstance(header, dict) or set(header) != fields or any(type(header[k]) is not int or not 0 <= header[k] <= 255 for k in fields):
        raise ValueError('Invalid compiled message header')
    required, signed_ro, unsigned_ro = (header[k] for k in ('numRequiredSignatures', 'numReadonlySignedAccounts', 'numReadonlyUnsignedAccounts'))
    if not (1 <= required <= len(static) and 0 <= signed_ro < required and 0 <= unsigned_ro <= len(static)-required):
        raise ValueError('Invalid compiled header counts')
    if not isinstance(signatures, list) or len(signatures) != required or not all(isinstance(s, str) and s for s in signatures):
        raise ValueError('Compiled signature count mismatch')
    lookups = message.get('addressTableLookups', [] if version == 'legacy' else None)
    if not isinstance(lookups, list) or len(lookups) > 256 or (version == 'legacy' and lookups):
        raise ValueError('Invalid compiled lookup descriptors')
    counts = [0, 0]; tables = set()
    for lookup in lookups:
        if not isinstance(lookup, dict) or set(lookup) != {'accountKey', 'writableIndexes', 'readonlyIndexes'}:
            raise ValueError('Invalid compiled lookup descriptor')
        table = address(lookup['accountKey'])
        if table in tables:raise ValueError('Duplicate lookup table')
        tables.add(table); seen = set()
        for segment, field in enumerate(('writableIndexes', 'readonlyIndexes')):
            indices = lookup[field]
            if not isinstance(indices, list) or len(indices) > 256:raise ValueError('Invalid lookup indices')
            for value in indices:
                index(value, 256)
                if value in seen:raise ValueError('Duplicate lookup index')
                seen.add(value)
            counts[segment] += len(indices)
    loaded = meta.get('loadedAddresses')
    if loaded is None:
        if any(counts):raise ValueError('Loaded addresses unavailable')
        segments = [[], []]
    else:
        if not isinstance(loaded, dict) or set(loaded) != {'writable', 'readonly'}:raise ValueError('Invalid loaded addresses')
        segments = []
        for field in ('writable', 'readonly'):
            if not isinstance(loaded[field], list) or len(loaded[field]) > 256:raise ValueError('Invalid loaded addresses')
            segments.append([address(k) for k in loaded[field]])
        if list(map(len, segments)) != counts:raise ValueError('Loaded lookup count mismatch')
    keys = static + segments[0] + segments[1]
    if len(keys) > 256 or len(set(keys)) != len(keys):raise ValueError('Duplicate or excessive transaction keys')
    privileges = []
    for i, key in enumerate(static):
        signer = i < required
        writable = i < required-signed_ro if signer else i < len(static)-unsigned_ro
        privileges.append({'pubkey':key, 'segment':'static', 'signer':signer, 'writable':writable})
    for segment, writable, values in (('loaded_writable',True,segments[0]), ('loaded_readonly',False,segments[1])):
        privileges.extend({'pubkey':key,'segment':segment,'signer':False,'writable':writable} for key in values)
    return keys, privileges


def compiled_instruction(ix, keys):
    if not isinstance(ix, dict) or 'parsed' in ix or 'programIdIndex' not in ix:
        raise ValueError('Invalid compiled instruction shape')
    program = keys[index(ix['programIdIndex'], len(keys))]
    if 'programId' in ix and ix['programId'] != program:raise ValueError('Conflicting compiled program identity')
    accounts = ix.get('accounts')
    if not isinstance(accounts, list) or len(accounts) > 256:raise ValueError('Invalid compiled instruction accounts')
    # Local decoder view only; raw evidence and its payload hash remain unchanged.
    return {**ix, 'programId':program, 'accounts':[keys[index(i,len(keys))] for i in accounts]}
