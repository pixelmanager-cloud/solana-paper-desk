"""Narrow historical legacy Pump birth-control structural analyzer (offline).

The profile is the PR39 three-string create / 6 decimals / 10**15 raw issuance
reference, NOT a universal Pump supply rule or a deployed-program proof. The
newer creator-argument/create_v2 variants, multisig and Token-2022 are excluded.
IDL pins corroborate account/PDA interfaces only; old argument bytes and issuance
come from the explicitly unauthenticated reference. No event-schema exception.

We compare raw targets, caller witnesses and invocation preorder, and describe
conditional legacy state transitions. Neither those comparisons nor err=null,
logs, signatures or postTokenBalances authenticate finality, actual CPI success,
state effect order, fresh account state, or code deployed at the slot. Lifecycle
acceptance is always false, even when structural_profile_match is true.
This module is outside the installed desk* runtime package and has no consumers.
"""
import copy

from solders.pubkey import Pubkey

from desk.legacy_controls import normalize_legacy_control
from desk.programs import unbase58
from desk.security import TOKEN_PROGRAM, TOKEN_2022
from research.execution_trace import reconstruct_execution_trace

PROFILE = 'historical-legacy-pump-birth-controls-v1'
PUMP = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
SYSTEM = '11111111111111111111111111111111'
ATA = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'
METADATA = 'metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s'
RENT = 'SysvarRent111111111111111111111111111111111'
CREATE = bytes.fromhex('181ec828051c0777')
SUPPLY = 10**15
REFERENCE_SHA256 = '89dcc14ecad1432598e4937da2dfa9955af8637a5ae373418e9631c174991749'
_CONTROLS = {4, 5, 6, 10, 11, 13, 21, 22}
_SPL_COMMIT = 'ad2b81274075c45e6ef428e52479b7d3d8f0dd6a'
_PINS = (
    ('solana-labs/solana-program-library', _SPL_COMMIT, 'token/program/src/instruction.rs',
     'e798abdea4dc930354b970dd3dc36f098d4bb4d3'),
    ('solana-labs/solana-program-library', _SPL_COMMIT, 'token/program/src/processor.rs',
     '7056f2e707ed93282d19ec56e9711d22a24b7498'),
    ('solana-labs/solana-program-library', _SPL_COMMIT, 'associated-token-account/program/src/processor.rs',
     '20767a247d15212c031d891d3e098e60e4c904b6'),
)


def _pda(program, *seeds):
    return str(Pubkey.find_program_address(list(seeds), Pubkey.from_string(program))[0])


def _bytes(row):
    return bytes.fromhex(row['raw_hex']) if row['raw_hex'] is not None else None


def _strings(data):
    position, strings = 8, []
    for _ in range(3):
        if position + 4 > len(data):
            raise ValueError('CREATE_ARGUMENT_LAYOUT_UNSUPPORTED')
        size = int.from_bytes(data[position:position + 4], 'little')
        position += 4
        if size > 4096 or position + size > len(data):
            raise ValueError('CREATE_ARGUMENT_LAYOUT_UNSUPPORTED')
        try:
            strings.append(data[position:position + size].decode('utf-8'))
        except UnicodeDecodeError:
            raise ValueError('CREATE_ARGUMENT_UTF8_INVALID') from None
        position += size
    if position != len(data):
        raise ValueError('CREATE_ARGUMENT_LAYOUT_UNSUPPORTED')
    return strings


def analyze_legacy_pump_birth(record):
    """Recompute trace and normalize controls; accept no caller-supplied verdicts.

    All adverse controls are retained individually, including restored/zero-value
    ones and controls outside create. Exact unknown targets remain blocking. A
    structural match is a reference-pattern comparison, never state/ownership
    approval. Conditional revocation irreversibility needs successful legacy
    execution and known predecessor state/code; it is not inferred from logs.
    """
    trace = reconstruct_execution_trace(record)
    result = {'profile': PROFILE, 'trace': trace, 'bindings': None,
              'create_arguments': None, 'declared_signer_witness': None,
              'balance_metadata_witness': None,
              'birth_witnesses': {}, 'control_witnesses': [], 'adverse_control_witnesses': [],
              'distribution_witnesses': [], 'uninterpreted_witnesses': [],
              'conditional_reference_transitions': [], 'errors': [], 'unknowns': [],
              'structural_profile_match': False, 'invocation_preorder_constraints_match': False,
              'authenticated_lifecycle_accepted': False, 'lifecycle_verified': False,
              'effect_order_verified': False, 'individual_cpi_success_verified': False,
              'signer_authorization_verified': False, 'program_slot_semantics_verified': False,
              'finality_verified': False, 'authenticity_verified': False,
              'ownership_approved': False, 'eligible_for_trading': False,
              'acceptance_blockers': ['TRUSTED_FINALIZED_CAPTURE_REQUIRED', 'PROGRAM_SLOT_SEMANTICS_REQUIRED',
                  'INDIVIDUAL_CPI_SUCCESS_UNVERIFIED', 'EFFECT_ORDER_UNVERIFIED',
                  'FRESH_PREDECESSOR_AND_FINAL_CONTROL_STATE_REQUIRED', 'COMPLETE_LIFETIME_COVERAGE_REQUIRED',
                  'OPAQUE_METADATA_AND_EVENT_SEMANTICS_UNVERIFIED', 'SIGNER_AND_PDA_PRIVILEGES_UNVERIFIED'],
              'reference_provenance': {'repository': 'DefaultPerson/solana-dex-parser-go',
                  'commit': '57ce4f643deec96b363d66778f220658df461497',
                  'path': 'testdata/example/4Cod1cNGv6RboJ7rSB79yeVCR4Lfd25rFgLY3eiPJfTJjTGyYP1r2i1upAYZHQsWDqUbGd1bhTRm1bpSQcpWMnEz.json',
                  'file_sha256': REFERENCE_SHA256, 'reference_only': True,
                  'acquisition_commitment': 'unknown', 'finality_status': 'CONFIRMED_SOURCE_NOT_FINALIZED'},
              'source_provenance': [{'repository': repo, 'commit': commit, 'path': path, 'git_blob': blob}
                                    for repo, commit, path, blob in _PINS],
              'interface_provenance': {'repository': 'pump-fun/pump-public-docs',
                  'commit': '91db6800e55bf341696564bd30a08ed4e3fc7491', 'path': 'idl/pump.json',
                  'git_blob': '1b4d4b5c7b9ffc740aa301ab402c4091c2f15bc7',
                  'sha256': '9c74bb906dbef3890082009e1fab2b80e26099385c697e6a1a87af817efadf7e',
                  'scope': 'account/PDA interface only; not three-string argument or supply/slot semantics'}}
    result['errors'].extend(trace['errors'])
    if not trace['syntax_complete']:
        result['unknowns'].extend(trace['reasons'])
    rows = trace['instructions']
    by_path = {row['instruction_path']: row for row in rows}
    controls = []
    # Inventory even when create/birth binding later fails. Never let an early
    # malformed create hide transient controls or erase original witnesses.
    for row in rows:
        raw = _bytes(row)
        if row['program'] in (TOKEN_PROGRAM, TOKEN_2022):
            parsed_kind = row['witness'].get('parsed', {}).get('type') if isinstance(row['witness'], dict) and isinstance(row['witness'].get('parsed', {}), dict) else None
            if (raw and raw[0] in _CONTROLS) or parsed_kind in {'setAuthority', 'approve', 'approveChecked',
                    'revoke', 'freezeAccount', 'thawAccount', 'getAccountDataSize', 'initializeImmutableOwner'}:
                kwargs = {'raw': raw, 'accounts': row['accounts']}
                if row['parsed_supplied']:
                    kwargs['parsed'] = row['witness']['parsed']
                normalized = normalize_legacy_control(row['program'], **kwargs)
                witness = {'instruction': copy.deepcopy(row), 'normalization': normalized,
                           'structural_role': 'unresolved'}
                controls.append((row, normalized, witness))
                result['control_witnesses'].append(witness)
            if row['program'] == TOKEN_2022:
                result['errors'].append('TOKEN_2022_UNSUPPORTED')
    def fail(reason):
        result['errors'].append(reason)
    def require(condition, reason):
        if not condition:
            raise ValueError(reason)
    def exactly(candidates, reason):
        require(len(candidates) == 1, reason)
        return candidates[0]
    def direct(row, parent):
        return row['direct_parent_path'] == parent['instruction_path'] and row['caller_program'] == parent['program']
    def tag(row, value):
        data = _bytes(row)
        return row['program'] == TOKEN_PROGRAM and data and data[0] == value
    try:
        create = exactly([row for row in rows if row['program'] == PUMP and (_bytes(row) or b'')[:8] == CREATE],
                         'CREATE_MISSING_OR_DUPLICATE')
        require(create['inner_index'] is None, 'CREATE_MUST_BE_OUTER')
        accounts = create['accounts']
        require(isinstance(accounts, list) and len(accounts) == 14, 'CREATE_ACCOUNT_COUNT_UNSUPPORTED')
        result['create_arguments'] = _strings(_bytes(create))
        mint, authority, curve, curve_ata, global_key, metadata_program, metadata, payer = accounts[:8]
        mint_bytes = unbase58(mint)
        require(authority == _pda(PUMP, b'mint-authority'), 'NONCANONICAL_MINT_AUTHORITY')
        require(curve == _pda(PUMP, b'bonding-curve', mint_bytes), 'NONCANONICAL_CURVE')
        require(curve_ata == _pda(ATA, unbase58(curve), unbase58(TOKEN_PROGRAM), mint_bytes), 'NONCANONICAL_CURVE_ATA')
        require(global_key == _pda(PUMP, b'global'), 'NONCANONICAL_GLOBAL')
        require(metadata_program == METADATA and metadata == _pda(METADATA, b'metadata', unbase58(METADATA), mint_bytes),
                'NONCANONICAL_METADATA_BINDING')
        require(accounts[8:] == [SYSTEM, TOKEN_PROGRAM, ATA, RENT, _pda(PUMP, b'__event_authority'), PUMP],
                'CREATE_FIXED_ACCOUNTS_MISMATCH')
        require(payer != mint and len(set(accounts)) == 14, 'CREATE_ACCOUNT_ALIAS')
        holder_ata = _pda(ATA, unbase58(payer), unbase58(TOKEN_PROGRAM), mint_bytes)
        result['bindings'] = dict(mint=mint, mint_authority=authority, curve=curve,
                                  curve_ata=curve_ata, payer=payer, holder_ata=holder_ata,
                                  create_path=create['instruction_path'], expected_supply_raw=str(SUPPLY), decimals=6)
        message = record['transaction']['message']
        header = message.get('header')
        require(isinstance(header, dict) and set(header) == {'numRequiredSignatures', 'numReadonlySignedAccounts',
                                                          'numReadonlyUnsignedAccounts'}, 'HEADER_MISSING_OR_INVALID')
        static = [row['pubkey'] for row in trace['key_witnesses'] if row['segment'] == 'static']
        for field in header:
            require(type(header[field]) is int and 0 <= header[field] <= len(static), 'HEADER_MISSING_OR_INVALID')
        require(header['numRequiredSignatures'] == 2 and header['numReadonlySignedAccounts'] == 0,
                'EXTRA_OR_MISSING_DECLARED_SIGNERS')
        require(header['numReadonlyUnsignedAccounts'] <= len(static) - 2, 'HEADER_MISSING_OR_INVALID')
        require(set(static[:2]) == {payer, mint} and static[0] == payer, 'DECLARED_SIGNER_BINDING_MISMATCH')
        signatures = record['transaction'].get('signatures')
        require(isinstance(signatures, list) and len(signatures) == 2, 'SIGNATURE_ENVELOPE_INVALID')
        require(all(isinstance(s, str) and len(unbase58(s)) == 64 for s in signatures), 'SIGNATURE_ENVELOPE_INVALID')
        result['declared_signer_witness'] = {'pubkeys': static[:2], 'header': copy.deepcopy(header),
                                           'signature_bytes_verified': False, 'cpi_privileges_verified': False}
        writable_end = len(static) - header['numReadonlyUnsignedAccounts']
        for key in (payer, mint, curve, curve_ata, metadata, holder_ata):
            require(key in static and static.index(key) < writable_end, 'DECLARED_WRITABLE_BINDING_MISMATCH')
        for key_row in trace['key_witnesses']:
            witness = key_row['witness']
            if isinstance(witness, dict):
                require(witness['signer'] == (key_row['pubkey'] in static[:2]), 'DECLARED_SIGNER_REPRESENTATIONS_DISAGREE')
                if key_row['pubkey'] in {payer, mint, curve, curve_ata, metadata, holder_ata}:
                    require(witness['writable'], 'DECLARED_WRITABLE_BINDING_MISMATCH')
        mint_alloc = exactly([row for row in rows if row['program'] == SYSTEM and row['accounts'] == [payer, mint]],
                             'MINT_ALLOCATION_MISSING_OR_DUPLICATE')
        allocation = _bytes(mint_alloc)
        require(direct(mint_alloc, create) and allocation is not None and len(allocation) == 52
                and allocation[:4] == bytes(4) and int.from_bytes(allocation[12:20], 'little') == 82
                and allocation[20:] == unbase58(TOKEN_PROGRAM), 'MINT_ALLOCATION_PROFILE_MISMATCH')
        init = exactly([row for row in rows if tag(row, 20) or tag(row, 0)], 'MINT_INIT_MISSING_OR_DUPLICATE')
        require(tag(init, 20) and _bytes(init) == bytes([20, 6]) + unbase58(authority) + b'\x00'
                and init['accounts'] == [mint] and direct(init, create), 'MINT_INIT_PROFILE_MISMATCH')
        issuance = exactly([row for row in rows if tag(row, 7) or tag(row, 14)], 'ISSUANCE_MISSING_OR_DUPLICATE')
        require(_bytes(issuance) == b'\x07' + SUPPLY.to_bytes(8, 'little') and issuance['accounts'] == [mint, curve_ata, authority]
                and direct(issuance, create), 'ISSUANCE_PROFILE_MISMATCH')
        revocations = [item for item in controls if item[1]['raw_operation'] and item[1]['raw_operation']['kind'] == 'setAuthority'
                       and item[1]['raw_operation']['authority_role'] == 'mintTokens'
                       and item[1]['raw_operation']['target_mint'] == mint
                       and item[1]['raw_operation']['new_authority_presence'] == 'explicit_none']
        revoke, normalized, revocation_witness = exactly(revocations, 'REVOCATION_MISSING_OR_DUPLICATE')
        require(normalized['normalization_complete'] and _bytes(revoke) == b'\x06\x00\x00'
                and revoke['accounts'] == [mint, authority] and direct(revoke, create), 'REVOCATION_PROFILE_MISMATCH')
        revocation_witness['structural_role'] = 'birth_mint_revocation_witness_not_authenticated'
        result['birth_witnesses'] = {name: copy.deepcopy(row) for name, row in
                                    [('mint_allocation', mint_alloc), ('initialize_mint', init),
                                     ('issuance', issuance), ('revocation', revoke)]}
        require(mint_alloc['invocation_ordinal'] < init['invocation_ordinal'] < issuance['invocation_ordinal']
                < revoke['invocation_ordinal'], 'BIRTH_INVOCATION_PREORDER_MISMATCH')
        # Match the exact observed standard ATA setup for curve and payer. This
        # is an invocation-pattern check, not account freshness or legacy noop approval.
        account_inits = []
        for token_account, owner in ((curve_ata, curve), (holder_ata, payer)):
            parent = exactly([row for row in rows if row['program'] == ATA and row['accounts'] ==
                              [payer, token_account, owner, mint, SYSTEM, TOKEN_PROGRAM]], 'ATA_SETUP_MISSING_OR_DUPLICATE')
            require(_bytes(parent) in (b'', b'\x00'), 'ATA_SETUP_VARIANT_UNSUPPORTED')
            require(direct(parent, create) if owner == curve else parent['inner_index'] is None,
                    'ATA_SETUP_CALLER_MISMATCH')
            children = [row for row in rows if row['direct_parent_path'] == parent['instruction_path']]
            require(len(children) == 4, 'ATA_SETUP_CHILD_COUNT_MISMATCH')
            query, alloc, immutable, account_init = children
            require(tag(query, 21) and _bytes(query) == b'\x15\x07\x00' and query['accounts'] == [mint]
                    and tag(immutable, 22) and _bytes(immutable) == b'\x16' and immutable['accounts'] == [token_account],
                    'ATA_CONTROL_LAYOUT_MISMATCH')
            raw_alloc = _bytes(alloc)
            require(alloc['program'] == SYSTEM and alloc['accounts'] == [payer, token_account]
                    and raw_alloc is not None and len(raw_alloc) == 52 and raw_alloc[:4] == bytes(4)
                    and int.from_bytes(raw_alloc[12:20], 'little') == 165
                    and raw_alloc[20:] == unbase58(TOKEN_PROGRAM), 'ATA_ALLOCATION_PROFILE_MISMATCH')
            require(tag(account_init, 18) and _bytes(account_init) == b'\x12' + unbase58(owner)
                    and account_init['accounts'] == [token_account, mint], 'ATA_INIT_PROFILE_MISMATCH')
            account_inits.append(account_init)
            for row, norm, witness in controls:
                if row['instruction_path'] in {query['instruction_path'], immutable['instruction_path']}:
                    require(norm['normalization_complete'], 'ATA_CONTROL_NORMALIZATION_INVALID')
                    witness['structural_role'] = 'standard_ata_setup_witness_not_authenticated'
            result['birth_witnesses']['curve_ata_init' if owner == curve else 'holder_ata_init'] = copy.deepcopy(account_init)
        require(account_inits[0]['invocation_ordinal'] < issuance['invocation_ordinal'], 'CURVE_ATA_INIT_AFTER_ISSUANCE')
        require(revoke['invocation_ordinal'] < account_inits[1]['invocation_ordinal'], 'HOLDER_SETUP_BEFORE_REVOCATION')
        result['conditional_reference_transitions'] = [
            {'condition': 'proved fresh mint + successful legacy initializeMint2 under slot-bound code',
             'supply_raw': '0', 'mint_authority': authority, 'freeze_authority': None},
            {'condition': 'successful authorized legacy mintTo after proved init/curve ATA birth',
             'issuance_raw': str(SUPPLY), 'destination': curve_ata},
            {'condition': 'successful authorized legacy MintTokens revocation after proved issuance',
             'mint_authority': None, 'reference_rule': 'None cannot authorize later mintTo or authority restoration',
             'actual_transition_verified': False}]
        allowed_token_paths = {row['instruction_path'] for row in (init, issuance, revoke, *account_inits)}
        allowed_token_paths.update(row['instruction_path'] for row, _, w in controls if w['structural_role'].startswith('standard_ata'))
        for row in rows:
            data = _bytes(row)
            if row['program'] == TOKEN_PROGRAM and data and data[0] in (3, 12):
                witness = copy.deepcopy(row)
                result['distribution_witnesses'].append(witness)
                expected_accounts = [curve_ata, holder_ata, curve] if data[0] == 3 else [curve_ata, mint, holder_ata, curve]
                expected_size = 9 if data[0] == 3 else 10
                parent = by_path.get(row['direct_parent_path'])
                require(len(data) == expected_size and row['accounts'] == expected_accounts
                        and (data[0] == 3 or data[9] == 6), 'DISTRIBUTION_LAYOUT_OR_BINDING_MISMATCH')
                require(parent is not None and parent['program'] == PUMP and parent['inner_index'] is None
                        and parent['outer_index'] > create['outer_index'], 'DISTRIBUTION_CALLER_OR_OUTER_ORDER_MISMATCH')
                require(row['invocation_ordinal'] > revoke['invocation_ordinal']
                        and row['invocation_ordinal'] > account_inits[1]['invocation_ordinal'], 'DISTRIBUTION_BEFORE_REVOCATION_OR_SETUP')
                amount = int.from_bytes(data[1:9], 'little')
                require(0 < amount <= SUPPLY, 'DISTRIBUTION_AMOUNT_INVALID')
                witness['amount_raw'] = str(amount)
                witness['effect_order_verified'] = False
                allowed_token_paths.add(row['instruction_path'])
            elif row['program'] not in (TOKEN_PROGRAM, TOKEN_2022):
                result['uninterpreted_witnesses'].append(copy.deepcopy(row))
        require(sum(int(row.get('amount_raw', '0')) for row in result['distribution_witnesses']) <= SUPPLY,
                'DISTRIBUTION_EXCEEDS_REFERENCE_ISSUANCE')
        result['invocation_preorder_constraints_match'] = True
        for row in rows:
            if row['program'] == TOKEN_PROGRAM and row['instruction_path'] not in allowed_token_paths:
                fail('UNSUPPORTED_OR_ADVERSE_TOKEN_OPERATION')
        meta = record['meta']
        result['balance_metadata_witness'] = {'pre': copy.deepcopy(meta.get('preTokenBalances')),
                                             'post': copy.deepcopy(meta.get('postTokenBalances')),
                                             'amounts_match_reference': None, 'authenticated': False,
                                             'mint_control_state_verified': False}
        pre, post = meta.get('preTokenBalances'), meta.get('postTokenBalances')
        if not isinstance(pre, list) or not isinstance(post, list):
            result['unknowns'].append('TOKEN_BALANCE_METADATA_UNAVAILABLE')
        else:
            require(all(isinstance(balance, dict) for balance in pre + post), 'TOKEN_BALANCE_METADATA_MALFORMED')
            require(not any(balance.get('mint') == mint for balance in pre), 'PREEXISTING_MINT_BALANCE_CONTRADICTION')
            observed, amounts = set(), {}
            keys = [row['pubkey'] for row in trace['key_witnesses']]
            for balance in post:
                require(type(balance.get('accountIndex')) is int and 0 <= balance['accountIndex'] < len(keys),
                        'TOKEN_BALANCE_METADATA_MALFORMED')
                account = keys[balance['accountIndex']]
                require(account not in observed, 'DUPLICATE_BALANCE_METADATA_INDEX')
                observed.add(account)
                require(balance.get('mint') == mint and account in {curve_ata, holder_ata}
                        and balance.get('programId') == TOKEN_PROGRAM, 'TOKEN_BALANCE_METADATA_BINDING_MISMATCH')
                require(balance.get('owner') == (curve if account == curve_ata else payer), 'TOKEN_BALANCE_METADATA_OWNER_MISMATCH')
                token_amount = balance.get('uiTokenAmount')
                require(isinstance(token_amount, dict) and type(token_amount.get('decimals')) is int
                        and token_amount['decimals'] == 6, 'TOKEN_BALANCE_METADATA_MALFORMED')
                amount = token_amount.get('amount')
                require(isinstance(amount, str) and amount.isascii() and amount.isdigit() and 1 <= len(amount) <= 20
                        and int(amount) < 2**64, 'TOKEN_BALANCE_METADATA_MALFORMED')
                amounts[account] = int(amount)
            distributed = sum(int(row.get('amount_raw', '0')) for row in result['distribution_witnesses'])
            require(set(amounts) == {curve_ata, holder_ata} and amounts[curve_ata] == SUPPLY - distributed
                    and amounts[holder_ata] == distributed, 'TOKEN_BALANCE_METADATA_AMOUNT_MISMATCH')
            result['balance_metadata_witness']['amounts_match_reference'] = True
    except (ValueError, KeyError, TypeError) as exc:
        fail(str(exc) if isinstance(exc, ValueError) else 'BIRTH_CONTEXT_MALFORMED')
    # Deliberately runs after failure too. Do not erase adverse controls when a
    # reordered/malformed/missing birth step prevents matching the profile.
    for row, norm, witness in controls:
        if witness['structural_role'] == 'unresolved':
            result['adverse_control_witnesses'].append(copy.deepcopy(witness))
            fail('ADVERSE_OR_UNRESOLVED_CONTROL_WITNESS')
        if not norm['normalization_complete']:
            fail('CONTROL_NORMALIZATION_INCOMPLETE')
    result['errors'] = sorted(set(result['errors']))
    result['unknowns'] = sorted(set(result['unknowns']))
    result['structural_profile_match'] = trace['syntax_complete'] and not result['errors']
    return result
