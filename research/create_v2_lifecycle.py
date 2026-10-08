"""Offline create_v2 structural report; never authenticated lifecycle approval.

Reuses the reviewed trace/syntax foundations and exact PR48 state decoder.
Invocation preorder, supplied endpoint snapshots and event declarations are
untrusted witnesses, not intermediate state/effect-order/authorization proofs.
All observed instructions are retained; unmatched instructions block agreement.
No production consumer, network, optional-wrapper/EOF decoder or prestate inference.
"""
import copy

from solders.pubkey import Pubkey

from desk.programs import BorshReader, instruction, schemas, unbase58
from desk.security import TOKEN_PROGRAM
from research.execution_trace import reconstruct_execution_trace
from research.token2022_instructions import normalize_token2022_instruction, TOKEN_2022
from research.token2022_state import decode_token2022_state

PROFILE = 'create-v2-observed-structural-lifecycle-report-v1'
PUMP = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
SYSTEM = '11111111111111111111111111111111'
ATA = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'
WSOL = 'So11111111111111111111111111111111111111112'
CREATE = bytes.fromhex('d6904cec5f8b31b4')
SUPPLY = 10**15
REFERENCE_SHA256 = 'd80cf9876fdb3e9b465e4103fa14bf2baaf09483dad9a27a57ff7339624b3bf2'
PR48_HEAD = '3ffe18674a2162257e5fd641f1fafb92e5ef40c4'
INTERFACE_PROVENANCE = {
    'repository': 'pump-fun/pump-public-docs', 'commit': 'cb188ce08b5069196eef1f3e4a0c43b70099793b',
    'path': 'idl/pump.json', 'sha256': 'ffe966c42f1af41652ee753fe2f1e3f7cd4077d7e6f49faf3138959c8b56064b',
    'scope': 'current exact interface/prefix only, not wrapper EOF or deployed-slot proof'}
_SUPPLEMENTAL_PINS = (
    ('solana-labs/solana', 'd9f20e951a06b61e4505da0955228020b96a8915',
     'sdk/program/src/system_instruction.rs', 'bb66b4fbce6b67cc40849978b96027357637b609'),
    ('solana-labs/solana-program-library', 'ad2b81274075c45e6ef428e52479b7d3d8f0dd6a',
     'associated-token-account/program/src/instruction.rs', 'cdbe21909e4342414a53d960e5c68e4634d76011'),
    ('solana-labs/solana-program-library', 'ad2b81274075c45e6ef428e52479b7d3d8f0dd6a',
     'associated-token-account/program/src/processor.rs', '20767a247d15212c031d891d3e098e60e4c904b6'),
)
_ADVERSE_TAGS = {4, 5, 6, 10, 11, 13, 21, 22, 39}
GRADUATION_EVIDENCE = (
    ('REVIEWED_DEPENDENCIES', 'Independent review of this report and its exact trace, syntax and state dependencies.'),
    ('TRUSTED_FINALIZED_CAPTURE', 'Trusted finalized transaction/signature/slot binding, complete compiled outer/inner bytes, static/loaded keys and lookup-table slot proof.'),
    ('CREATE_SUFFIX_SEMANTICS', 'Slot-bound create_v2 wrapper deserializer and explicit presence/EOF behavior for cashback, creator fee and holder reward; no guessed defaults.'),
    ('PROGRAM_SLOT_SEMANTICS', 'Authenticated program binaries at the slot and exact applicable Pump/ATA/Token-2022/metadata semantics.'),
    ('FRESH_PREDECESSOR_STATE', 'Independent same-bank prestate/allocation/freshness/rent witnesses for mint, curve and every relevant token account; none inferred from create/init calls.'),
    ('SAME_BANK_ENDPOINT_STATE', 'Authenticated same-bank raw mint/account endpoints bound to this transaction and cutoff, with complete extensions/controls and supply reconciliation.'),
    ('CALLER_AND_SIGNER_PRIVILEGES', 'Actual direct CPI privileges and PDA authorization; message/header or stackHeight alone cannot prove them.'),
    ('INDIVIDUAL_CPI_OUTCOMES', 'Every individual invocation outcome including caught failures, not only err=null or success logs.'),
    ('VERIFIED_EFFECT_ORDER', 'Actual operation effects/intermediate state ordering across parent writes and nested calls, not invocation-start preorder.'),
    ('COMPLETE_CONTROL_HISTORY', 'Complete birth-to-cutoff inventory/coverage of approvals, owner/delegate/close/freeze/extension changes, including transient changes restored at endpoint.'),
    ('OPAQUE_MAYHEM_ACCOUNT_EFFECTS', 'Proved identity/prestate/poststate and effects of writable mayhem program/state/SOL/token vault accounts, even when a declaration says mode=false.'),
    ('QUOTE_RENT_AND_OTHER_EFFECTS', 'Complete native SOL/rent/quote/fee/other writable account effects and exact route semantics; no ignored instructions or complete-economic-route claim here.'),
    ('HOLDER_POOL_AND_EXIT_EVIDENCE', 'Separate coverage-aware holders, canonical pool, exact sellability/exit and coordinator runtime admission review within existing budgets.'),
)


def _raw(row):
    return bytes.fromhex(row['raw_hex']) if row['raw_hex'] is not None else None


def _pda(program, *seeds):
    return str(Pubkey.find_program_address(list(seeds), Pubkey.from_string(program))[0])


def _slot(value):
    return type(value) is int and 0 <= value < 2**64


def _signature(value):
    try:
        return isinstance(value, str) and len(unbase58(value)) == 64
    except ValueError:
        return False


def report_create_v2_lifecycle(record, *, endpoint_states=None, source_context=None):
    """Report a supplied RPC transaction object's observed structural agreement.

    Notification unwrapping belongs to the caller, never a provider adapter.
    endpoint_states is a list of {address, program_owner, executable, raw, slot,
    transaction_signature}; roles are recomputed from the raw create accounts.
    source_context optionally retains the supplied slot/signature/provenance.
    Neither envelope nor 'finalized'/success flags authenticate any evidence.
    supported_sequence_agreement does NOT mean complete semantics or approval:
    mandatory create prefix may agree while optional wrapper/EOF remains unknown.
    """
    trace = reconstruct_execution_trace(record)
    result = {'profile': PROFILE, 'trace': trace,
              'observed_pattern_reference': {'path': 'fixtures/mainnet-launch.json',
                  'sha256': REFERENCE_SHA256, 'authenticated_launch_or_state_evidence': False},
              'interface_provenance': copy.deepcopy(INTERFACE_PROVENANCE),
              'supplemental_format_provenance': _SUPPLEMENTAL_PINS,
              'source_context_witness': copy.deepcopy(source_context),
              'dependency': {'pr': 48, 'exact_head': PR48_HEAD,
                             'review_status': 'accepted_per_coordinator_handoff_not_runtime_admission'},
              'bindings': None, 'create_arguments': None, 'signer_witness': None,
              'inventory': [], 'birth_witnesses': {}, 'adverse_control_witnesses': [],
              'distributions': [], 'endpoint_states': [], 'endpoint_amount_comparison': None,
              'opaque_accounts': [], 'errors': list(trace['errors']),
              'unknowns': list(trace['reasons']),
              'graduation_evidence': [{'code': c, 'required_evidence': d, 'verified': False}
                                      for c, d in GRADUATION_EVIDENCE],
              'supported_sequence_agreement': False, 'observed_inventory_agreement': False,
              'authenticated_lifecycle_accepted': False, 'lifecycle_verified': False,
              'snapshot_authenticated': False, 'effect_order_verified': False,
              'individual_cpi_success_verified': False, 'signer_authorization_verified': False,
              'program_slot_semantics_verified': False, 'finality_verified': False,
              'ownership_approved': False, 'eligible_for_trading': False}
    result['unknowns'].extend(c for c, _ in GRADUATION_EVIDENCE if c != 'REVIEWED_DEPENDENCIES')
    rows = trace['instructions']
    inventory = {}
    for row in rows:
        item = {'instruction': copy.deepcopy(row), 'role': 'unresolved',
                'syntax': None, 'schema_witness': None, 'effect_verified': False,
                'cpi_success': 'unknown', 'adverse_control_candidate': False}
        if row['program'] == PUMP and row['raw_complete']:
            try:
                item['schema_witness'] = instruction({'programId': PUMP, 'accounts': row['accounts'],
                                                      'data': row['witness'].get('data', '')})
            except (ValueError, KeyError, TypeError, UnicodeError):
                item['schema_witness'] = {'status': 'PINNED_SCHEMA_UNRESOLVED'}
        if row['program'] == TOKEN_2022:
            kwargs = {'raw': _raw(row), 'accounts': row['accounts']}
            if row['parsed_supplied']:
                kwargs['parsed'] = row['witness']['parsed']
            item['syntax'] = normalize_token2022_instruction(row['program'], **kwargs)
            data = _raw(row)
            parsed = row['witness'].get('parsed', {}) if isinstance(row['witness'], dict) else {}
            item['adverse_control_candidate'] = bool(
                (data and not item['syntax']['normalization_complete'])
                or
                (data and (data[0] in _ADVERSE_TAGS or data[:8] == bytes.fromhex('d7e4a6e45464567b')))
                or (isinstance(parsed, dict) and parsed.get('type') in
                    {'approve', 'approveChecked', 'revoke', 'setAuthority', 'freezeAccount',
                     'thawAccount', 'initializeMetadataPointer', 'updateTokenMetadataAuthority',
                     'initializeImmutableOwner', 'getAccountDataSize'}))
        inventory[row['instruction_path']] = item
        result['inventory'].append(item)

    def error(code):
        result['errors'].append(code)

    def require(condition, code):
        if not condition:
            raise ValueError(code)

    def one(candidates, code):
        require(len(candidates) == 1, code)
        return candidates[0]

    def direct(row, parent):
        return (row['direct_parent_path'] == parent['instruction_path']
                and row['caller_program'] == parent['program'])

    def role(row, name):
        inventory[row['instruction_path']]['role'] = name

    def operation(row):
        syntax = inventory[row['instruction_path']]['syntax']
        return syntax['raw_operation'] if syntax and syntax['normalization_complete'] else None

    def op_rows(kind):
        return [row for row in rows if operation(row) and operation(row)['kind'] == kind]

    def op_one(kind):
        return one(op_rows(kind), kind.upper() + '_MISSING_OR_DUPLICATE')

    def allocation(row, target, size, owner, parent):
        data = _raw(row)
        require(row['program'] == SYSTEM and row['accounts'] == [payer, target]
                and direct(row, parent) and data is not None and len(data) == 52
                and data[:4] == bytes(4) and int.from_bytes(data[4:12], 'little') > 0
                and int.from_bytes(data[12:20], 'little') == size
                and data[20:] == unbase58(owner), 'ALLOCATION_ROLE_OR_LAYOUT_MISMATCH')
        inventory[row['instruction_path']]['allocation_witness'] = {
            'lamports': str(int.from_bytes(data[4:12], 'little')), 'space': size,
            'program_owner': owner, 'fresh_prestate_verified': False, 'rent_verified': False}

    b = text = create = payer = None
    try:
        create = one([row for row in rows if row['program'] == PUMP and
                      (_raw(row) or b'')[:8] == CREATE], 'CREATE_V2_MISSING_OR_DUPLICATE')
        require(create['inner_index'] is None, 'CREATE_V2_MUST_BE_OUTER')
        spec = schemas()[PUMP][CREATE]
        require(len(spec['accounts']) == 16 and create['accounts'] is not None
                and len(create['accounts']) == 16, 'CREATE_V2_ACCOUNT_COUNT_UNSUPPORTED')
        b = dict(zip((a['name'] for a in spec['accounts']), create['accounts']))
        require(len(set(create['accounts'])) == 16, 'CREATE_ACCOUNT_ALIAS')
        for account in spec['accounts']:
            name = account['name']
            if 'address' in account:
                require(b[name] == account['address'], 'CREATE_FIXED_ACCOUNT_MISMATCH:' + name)
            if 'pda' in account:
                seeds = []
                for seed in account['pda']['seeds']:
                    require(seed['kind'] in ('const', 'account'), 'CREATE_SEED_SCHEMA_UNSUPPORTED')
                    seeds.append(bytes(seed['value']) if seed['kind'] == 'const'
                                 else unbase58(b[seed['path']]))
                pda_program = account['pda'].get('program')
                require(pda_program is None or pda_program['kind'] == 'const',
                        'CREATE_PDA_PROGRAM_UNSUPPORTED')
                program = str(Pubkey.from_bytes(bytes(pda_program['value']))) if pda_program else PUMP
                require(b[name] == _pda(program, *seeds), 'CREATE_PDA_BINDING_MISMATCH:' + name)
        require(b['program'] == PUMP, 'CREATE_PROGRAM_ACCOUNT_MISMATCH')
        payer, mint, authority, curve, curve_ata = (b[n] for n in
            ('user', 'mint', 'mint_authority', 'bonding_curve', 'associated_bonding_curve'))
        holder_ata = _pda(ATA, unbase58(payer), unbase58(TOKEN_2022), unbase58(mint))
        require(holder_ata not in create['accounts'], 'HOLDER_ATA_ALIAS')
        result['bindings'] = dict(b, holder_ata=holder_ata, create_path=create['instruction_path'])
        result['opaque_accounts'] = [{'name': name, 'address': b[name],
            'reason': 'UNCONSTRAINED_WRITABLE_VAULT' if name == 'mayhem_token_vault'
                      else 'BOUND_IDENTITY_WITH_UNPROVED_WRITABLE_EFFECTS',
            'state_and_effects_verified': False}
            for name in ('mayhem_program_id', 'sol_vault', 'mayhem_state', 'mayhem_token_vault')]
        role(create, 'create_v2_raw_accounts_and_mandatory_prefix_only')
        raw = _raw(create)
        reader = BorshReader(raw[8:], {})
        require(spec['args'][:5] == [{'name': 'name', 'type': 'string'}, {'name': 'symbol', 'type': 'string'},
                {'name': 'uri', 'type': 'string'}, {'name': 'creator', 'type': 'pubkey'},
                {'name': 'is_mayhem_mode', 'type': 'bool'}], 'CREATE_PREFIX_SCHEMA_UNSUPPORTED')
        arguments = {}
        result['create_arguments'] = {'mandatory_prefix': arguments, 'schema_complete': False,
                                      'suffix_raw_hex': None, 'optional_fields': {}}
        for arg in spec['args'][:5]:
            arguments[arg['name']] = reader.read(arg['type'])
        suffix = raw[8 + reader.pos:]
        result['create_arguments'].update(suffix_raw_hex=suffix.hex(), suffix_length=len(suffix),
            observed_suffix_bytes_agree=suffix == bytes(9),
            prefix_end=8+reader.pos, optional_fields={arg['name']: {'presence': 'unresolved',
                'value': None, 'reason': 'WRAPPER_DESERIALIZER_AND_EOF_SEMANTICS_UNPROVED'}
                for arg in spec['args'][5:]})
        require(arguments['creator'] == payer, 'CREATE_CREATOR_PROFILE_MISMATCH')
        require(arguments['is_mayhem_mode'] is False, 'MAYHEM_MODE_OUTSIDE_PROFILE')
        text = {name: arguments[name] for name in ('name', 'symbol', 'uri')}
        result['unknowns'].append('CREATE_OPTIONAL_SUFFIX_AND_EOF_UNRESOLVED')
        if suffix != bytes(9):
            result['unknowns'].append('CREATE_SUFFIX_OUTSIDE_OBSERVED_RAW_PATTERN')

        # Header is only a declared privilege witness, never signature/PDA proof.
        message = record['transaction']['message']
        header = message.get('header')
        if header is None:
            result['unknowns'].append('MESSAGE_HEADER_MISSING')
        else:
            require(isinstance(header, dict) and set(header) == {'numRequiredSignatures',
                    'numReadonlySignedAccounts', 'numReadonlyUnsignedAccounts'}, 'MESSAGE_HEADER_INVALID')
            static = [key['pubkey'] for key in trace['key_witnesses'] if key['segment'] == 'static']
            require(all(type(value) is int and 0 <= value <= len(static) for value in header.values()),
                    'MESSAGE_HEADER_INVALID')
            require(header['numRequiredSignatures'] == 2 and header['numReadonlySignedAccounts'] == 0
                    and header['numReadonlyUnsignedAccounts'] <= len(static)-2
                    and static[:2] == [payer, mint], 'DECLARED_SIGNERS_PROFILE_MISMATCH')
            signatures = record['transaction'].get('signatures')
            require(isinstance(signatures, list) and len(signatures) == 2
                    and all(isinstance(s, str) and len(unbase58(s)) == 64 for s in signatures),
                    'SIGNATURE_ENVELOPE_INVALID')
            writable_end = len(static)-header['numReadonlyUnsignedAccounts']
            for key in trace['key_witnesses']:
                witness = key['witness']
                if isinstance(witness, dict):
                    require(witness['signer'] == (key['pubkey'] in static[:2]), 'PARSED_SIGNER_FLAGS_DISAGREE')
                writable = (static.index(key['pubkey']) < writable_end if key['segment'] == 'static'
                            else key['segment'] == 'loaded_writable')
                expected_writable = key['pubkey'] == holder_ata or any(
                    a.get('writable') and b[a['name']] == key['pubkey'] for a in spec['accounts'])
                if expected_writable:
                    require(writable and (not isinstance(witness, dict) or witness['writable']),
                            'DECLARED_WRITABLE_PROFILE_MISMATCH')
            result['signer_witness'] = {'header': copy.deepcopy(header), 'declared_signers': static[:2],
                                        'authenticated': False, 'cpi_privileges_verified': False}

        mint_alloc = one([row for row in rows if row['program'] == SYSTEM and
                          row['accounts'] == [payer, mint] and (_raw(row) or b'')[:4] == bytes(4)],
                         'MINT_ALLOCATION_MISSING_OR_DUPLICATE')
        allocation(mint_alloc, mint, 234, TOKEN_2022, create)
        role(mint_alloc, 'mint_allocation_234_observed_profile_not_freshness_proof')
        pointer, init = op_one('initializeMetadataPointer'), op_one('initializeMint2')
        require(operation(pointer) == {'kind': 'initializeMetadataPointer', 'mint': mint,
                'pointer_authority': {'presence': 'explicit_none', 'value': None},
                'metadata_address': {'presence': 'explicit_key', 'value': mint}}
                and direct(pointer, create), 'POINTER_ROLE_OR_CALLER_MISMATCH')
        role(pointer, 'birth_immutable_self_pointer')
        require(operation(init) == {'kind': 'initializeMint2', 'mint': mint, 'decimals': 6,
                'mint_authority': authority, 'freeze_authority': {'presence': 'explicit_none', 'value': None}}
                and direct(init, create), 'MINT_INIT_ROLE_OR_CALLER_MISMATCH')
        role(init, 'birth_mint_init_no_freeze')
        curve_alloc = one([row for row in rows if row['program'] == SYSTEM and
                           row['accounts'] == [payer, curve]], 'CURVE_ALLOCATION_MISSING_OR_DUPLICATE')
        allocation(curve_alloc, curve, 141, PUMP, create)
        role(curve_alloc, 'curve_allocation_141_observed_profile_opaque_state')
        require(mint_alloc['invocation_ordinal'] < pointer['invocation_ordinal'] < init['invocation_ordinal']
                < curve_alloc['invocation_ordinal'], 'INITIALIZATION_INVOCATION_PREORDER_MISMATCH')

        account_inits = []
        for token_account, owner in ((curve_ata, curve), (holder_ata, payer)):
            parent = one([row for row in rows if row['program'] == ATA and
                         row['accounts'] == [payer, token_account, owner, mint, SYSTEM, TOKEN_2022]],
                         'ATA_PARENT_MISSING_OR_DUPLICATE')
            if owner == curve:
                require(_raw(parent) in (b'', b'\0') and direct(parent, create), 'CURVE_ATA_CALLER_OR_VARIANT_MISMATCH')
            else:
                require(_raw(parent) == b'\1' and parent['inner_index'] is None
                        and parent['outer_index'] > create['outer_index'], 'HOLDER_ATA_CALLER_OR_VARIANT_MISMATCH')
            role(parent, 'curve_ata_create' if owner == curve else 'holder_ata_idempotent_observed_creation')
            children = [row for row in rows if row['direct_parent_path'] == parent['instruction_path']]
            require(len(children) == 4, 'ATA_CREATION_CHILDREN_MISSING_EXTRA_OR_EXISTING_CASE_UNPROVED')
            query, alloc, immutable, account_init = children
            require(operation(query) == {'kind': 'getAccountDataSize', 'mint': mint, 'requested_extension_types': [7]}
                    and direct(query, parent), 'ATA_SIZE_QUERY_ROLE_MISMATCH')
            allocation(alloc, token_account, 170, TOKEN_2022, parent)
            require(operation(immutable) == {'kind': 'initializeImmutableOwner', 'account': token_account,
                    'reference_semantics': 'token2022_immutable_owner_extension_initialization_not_legacy_noop'}
                    and direct(immutable, parent), 'ATA_IMMUTABLE_OWNER_ROLE_MISMATCH')
            require(operation(account_init) == {'kind': 'initializeAccount3', 'account': token_account,
                    'mint': mint, 'declared_owner': owner} and direct(account_init, parent), 'ATA_INIT_ROLE_MISMATCH')
            for child, name in ((query, 'ata_size_query_return_value_unproved'), (alloc, 'ata_allocation_170'),
                                (immutable, 'ata_immutable_owner_init'), (account_init, 'ata_account_init')):
                role(child, name)
            account_inits.append(account_init)
            result['birth_witnesses']['curve_ata' if owner == curve else 'holder_ata'] = parent['instruction_path']

        metadata_init, metadata_revoke = op_one('initializeTokenMetadata'), op_one('updateTokenMetadataAuthority')
        require(operation(metadata_init) == dict(kind='initializeTokenMetadata', metadata=mint,
                update_authority_account=authority, mint=mint, mint_authority_account=authority, **text)
                and direct(metadata_init, create), 'METADATA_INIT_ROLE_OR_CALLER_MISMATCH')
        role(metadata_init, 'self_metadata_initial_authority_witness')
        require(operation(metadata_revoke) == {'kind': 'updateTokenMetadataAuthority', 'metadata': mint,
                'current_metadata_authority_account': authority,
                'new_metadata_authority': {'presence': 'explicit_none', 'value': None}}
                and direct(metadata_revoke, create), 'METADATA_REVOCATION_ROLE_OR_CALLER_MISMATCH')
        role(metadata_revoke, 'birth_metadata_update_authority_revocation')
        rent = one([row for row in rows if row['program'] == SYSTEM and row['accounts'] == [payer, mint]
                    and (_raw(row) or b'')[:4] == b'\x02\0\0\0'], 'METADATA_RENT_TRANSFER_MISSING_OR_DUPLICATE')
        data = _raw(rent)
        require(len(data) == 12 and int.from_bytes(data[4:], 'little') > 0 and direct(rent, create),
                'METADATA_RENT_TRANSFER_ROLE_MISMATCH')
        role(rent, 'payer_to_mint_metadata_rent_witness_not_rent_approval')
        inventory[rent['instruction_path']]['native_lamports_witness'] = str(int.from_bytes(data[4:], 'little'))
        issuance, revoke = op_one('mintTo'), op_one('setAuthority')
        require(operation(issuance) == {'kind': 'mintTo', 'mint': mint, 'account': curve_ata,
                'mint_authority_account': authority, 'amount_raw': str(SUPPLY)} and direct(issuance, create),
                'ISSUANCE_ROLE_AMOUNT_OR_CALLER_MISMATCH')
        role(issuance, 'birth_exact_curve_issuance')
        require(operation(revoke) == {'kind': 'setAuthority', 'mint': mint, 'authority_role': 'mintTokens',
                'current_mint_authority_account': authority,
                'new_mint_authority': {'presence': 'explicit_none', 'value': None}} and direct(revoke, create),
                'MINT_REVOCATION_ROLE_OR_CALLER_MISMATCH')
        role(revoke, 'birth_mint_authority_revocation')
        ordered = [curve_alloc, account_inits[0], rent, metadata_init, metadata_revoke, issuance, revoke, account_inits[1]]
        require(all(a['invocation_ordinal'] < z['invocation_ordinal'] for a, z in zip(ordered, ordered[1:])),
                'BIRTH_INVOCATION_PREORDER_MISMATCH')
        result['birth_witnesses'].update({name: row['instruction_path'] for name, row in
            [('allocation', mint_alloc), ('pointer_init', pointer), ('mint_init', init),
             ('metadata_init', metadata_init), ('metadata_revocation', metadata_revoke),
             ('issuance', issuance), ('mint_revocation', revoke)]})

        # Current exact buy_v2 IDL has two ordinary u64 arguments. No wrapper
        # parser, quote-account safety, native SOL cost or complete trade claim.
        buy_spec = next(s for s in schemas()[PUMP].values() if s['name'] == 'buy_v2')
        buy_parents = []
        for row in rows:
            if row['program'] != PUMP or row is create or row['inner_index'] is not None:
                continue
            data = _raw(row)
            require(data is not None and data[:8] == bytes(buy_spec['discriminator']), 'PUMP_OUTER_OPERATION_UNSUPPORTED')
            require(len(data) == 24 and len(row['accounts']) == len(buy_spec['accounts']), 'BUY_V2_LAYOUT_OR_ACCOUNT_COUNT_MISMATCH')
            mapping = dict(zip((a['name'] for a in buy_spec['accounts']), row['accounts']))
            expected = {'global': b['global'], 'base_mint': mint, 'base_token_program': TOKEN_2022,
                        'bonding_curve': curve, 'associated_base_bonding_curve': curve_ata,
                        'user': payer, 'associated_base_user': holder_ata, 'system_program': SYSTEM,
                        'associated_token_program': ATA, 'event_authority': b['event_authority'],
                        'program': PUMP, 'quote_mint': WSOL, 'quote_token_program': TOKEN_PROGRAM}
            require(all(mapping[n] == value for n, value in expected.items()), 'BUY_V2_BASE_OR_REFERENCE_QUOTE_BINDING_MISMATCH')
            require(buy_spec['args'] == [{'name': 'amount', 'type': 'u64'}, {'name': 'max_sol_cost', 'type': 'u64'}],
                    'BUY_V2_ARGUMENT_SCHEMA_UNSUPPORTED')
            args_reader = BorshReader(data[8:], {})
            args = {arg['name']: args_reader.read(arg['type']) for arg in buy_spec['args']}
            require(args['amount'] > 0 and args['max_sol_cost'] > 0, 'BUY_V2_ARGUMENT_OUTSIDE_PROFILE')
            role(row, 'buy_v2_base_bindings_and_exact_args_only_quote_effects_unproved')
            inventory[row['instruction_path']]['schema_witness'] = {'accounts': mapping, 'args': args,
                'opaque_quote_fee_and_state_dependent_accounts': sorted(set(mapping)-set(expected)),
                'economic_effects_verified': False}
            result['unknowns'].append('BUY_V2_QUOTE_FEE_AND_STATE_DEPENDENT_ACCOUNT_EFFECTS_UNRESOLVED')
            buy_parents.append(row)
        transfers = op_rows('transferChecked')
        require(transfers, 'DISTRIBUTION_RAW_WITNESS_MISSING')
        for transfer in transfers:
            parent = one([row for row in buy_parents if direct(transfer, row)], 'DISTRIBUTION_DIRECT_BUY_PARENT_MISMATCH')
            op = operation(transfer)
            amount = int(op['amount_raw'])
            require(op == {'kind': 'transferChecked', 'source': curve_ata, 'mint': mint,
                    'destination': holder_ata, 'transfer_authority_account': curve,
                    'amount_raw': str(amount), 'decimals': 6} and 0 < amount <= SUPPLY,
                    'DISTRIBUTION_ROLE_AMOUNT_OR_DECIMALS_MISMATCH')
            require(parent['outer_index'] > create['outer_index']
                    and transfer['invocation_ordinal'] > account_inits[1]['invocation_ordinal']
                    and transfer['invocation_ordinal'] > revoke['invocation_ordinal'],
                    'DISTRIBUTION_BEFORE_SETUP_OR_REVOCATION')
            role(transfer, 'raw_curve_to_holder_distribution_witness')
            result['distributions'].append({'path': transfer['instruction_path'], 'amount_raw': str(amount),
                'direct_parent_path': parent['instruction_path'], 'effect_order_verified': False,
                'cpi_success_verified': False})
        for parent in buy_parents:
            associated = [d for d in result['distributions'] if d['direct_parent_path'] == parent['instruction_path']]
            require(len(associated) == 1 and int(associated[0]['amount_raw']) ==
                    inventory[parent['instruction_path']]['schema_witness']['args']['amount'],
                    'BUY_V2_DISTRIBUTION_AMOUNT_OR_COUNT_MISMATCH')
        require(sum(int(d['amount_raw']) for d in result['distributions']) <= SUPPLY,
                'DISTRIBUTION_EXCEEDS_ISSUANCE')

        # Events use existing exact decoding. Prefix/extra bytes never accepted.
        create_events = []
        for row in rows:
            if row['program'] == PUMP and row['inner_index'] is not None:
                decoded = instruction({'programId': PUMP, 'accounts': row['accounts'],
                                       'data': row['witness'].get('data', '')})
                inventory[row['instruction_path']]['schema_witness'] = decoded
                require(decoded and decoded['status'] == 'EVENT_DECODED' and decoded.get('schema_complete') is True
                        and decoded.get('schema_file') == 'pump.json'
                        and decoded['name'] == 'CreateEvent' and direct(row, create)
                        and row['accounts'] == [b['event_authority']], 'PUMP_INNER_EVENT_OR_OPERATION_UNRESOLVED')
                fields = decoded['fields']
                require(all(fields.get(name) == value for name, value in dict(text, mint=mint,
                        bonding_curve=curve, user=payer, creator=payer, token_program=TOKEN_2022,
                        token_total_supply=SUPPLY, quote_mint=SYSTEM, creator_fee_bps=0,
                        is_mayhem_mode=False, is_cashback_enabled=False, is_holder_reward=False).items()),
                        'CREATE_EVENT_BINDING_MISMATCH')
                require(row['invocation_ordinal'] > revoke['invocation_ordinal'], 'CREATE_EVENT_PREORDER_MISMATCH')
                role(row, 'complete_create_event_declaration_not_authenticated_state')
                create_events.append(row)
        require(len(create_events) == 1, 'CREATE_EVENT_MISSING_OR_DUPLICATE')
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        error(str(exc) if isinstance(exc, ValueError) else 'STRUCTURAL_CONTEXT_MALFORMED')

    # Always inventory leftovers, including after a fatal binding/order failure.
    for item in result['inventory']:
        row = item['instruction']
        if item['role'] == 'unresolved':
            if not row['raw_complete']:
                result['unknowns'].append('UNRESOLVED_RAW_WITNESS:' + row['instruction_path'])
            else:
                error('UNMATCHED_OBSERVED_INSTRUCTION:' + row['instruction_path'])
            if item['adverse_control_candidate'] or row['program'] == TOKEN_PROGRAM:
                result['adverse_control_witnesses'].append(copy.deepcopy(item))
                error('ADVERSE_OR_UNRESOLVED_CONTROL_WITNESS:' + row['instruction_path'])
        if row['parsed_supplied'] and row['raw_complete']:
            result['unknowns'].append('PARSED_RAW_AGREEMENT_UNPROVED:' + row['instruction_path'])

    # Independently supplied endpoints, never synthesized from balances/events.
    expected = {} if result['bindings'] is None else {
        b['mint']: ('mint', None), b['associated_bonding_curve']: ('account', b['bonding_curve']),
        result['bindings']['holder_ata']: ('account', b['user'])}
    transaction = record.get('transaction') if isinstance(record, dict) else None
    signatures = transaction.get('signatures') if isinstance(transaction, dict) else None
    first_signature = signatures[0] if isinstance(signatures, list) and signatures else None
    record_slot = record.get('slot') if isinstance(record, dict) else None
    context_slot = source_context.get('slot') if isinstance(source_context, dict) else None
    context_signature = source_context.get('signature') if isinstance(source_context, dict) else None
    if record_slot is None:
        result['unknowns'].append('RECORD_SLOT_MISSING')
    elif not _slot(record_slot):
        error('RECORD_SLOT_INVALID')
    if context_slot is None:
        result['unknowns'].append('SOURCE_SLOT_MISSING')
    elif not _slot(context_slot):
        error('SOURCE_SLOT_INVALID')
    if _slot(record_slot) and _slot(context_slot) and record_slot != context_slot:
        error('RECORD_SOURCE_SLOT_MISMATCH')
    if context_signature is None:
        result['unknowns'].append('SOURCE_SIGNATURE_MISSING')
    elif not _signature(context_signature):
        error('SOURCE_SIGNATURE_INVALID')
    elif context_signature != first_signature:
        error('SOURCE_SIGNATURE_DISAGREEMENT')
    if endpoint_states is None:
        result['unknowns'].append('RAW_STATE_MISSING')
    elif not isinstance(endpoint_states, list) or len(endpoint_states) > 64:
        error('ENDPOINT_ENVELOPE_SHAPE_OR_BUDGET')
    else:
        observed = set()
        endpoint_slots = set()
        for state in endpoint_states:
            if not isinstance(state, dict):
                error('ENDPOINT_ENVELOPE_MALFORMED')
                continue
            state_address = state.get('address')
            if not isinstance(state_address, str) or state_address not in expected:
                error('ENDPOINT_ADDRESS_UNBOUND')
                result['endpoint_states'].append({'address': copy.deepcopy(state_address), 'unbound': True,
                    'raw_hex': state['raw'].hex() if isinstance(state.get('raw'), (bytes, bytearray)) else None})
                continue
            if state_address in observed:
                error('DUPLICATE_ENDPOINT_ADDRESS')
            observed.add(state_address)
            kind, expected_owner = expected[state_address]
            kwargs = dict(kind=kind, address=state_address, program_owner=state.get('program_owner'),
                          executable=state.get('executable'))
            if kind == 'mint':
                kwargs['expected_metadata'] = text
            else:
                kwargs.update(expected_mint=b['mint'], expected_token_owner=expected_owner)
            decoded = decode_token2022_state(state.get('raw'), **kwargs)
            result['endpoint_states'].append({'address': state_address, 'slot': state.get('slot'),
                'transaction_signature': state.get('transaction_signature'), 'state': decoded,
                'capture_authenticated': False})
            if not decoded['structural_profile_match']:
                if 'RAW_STATE_MISSING' in {d['code'] for d in decoded['diagnostics']}:
                    result['unknowns'].append('RAW_STATE_MISSING:' + state_address)
                else:
                    error('ENDPOINT_STATE_PROFILE_MISMATCH:' + state_address)
            slot = state.get('slot')
            if slot is None:
                result['unknowns'].append('ENDPOINT_SLOT_MISSING:' + state_address)
            elif not _slot(slot):
                error('ENDPOINT_SLOT_INVALID')
            else:
                endpoint_slots.add(slot)
                if _slot(context_slot) and slot != context_slot:
                    error('ENDPOINT_SOURCE_SLOT_MISMATCH')
                if _slot(record_slot) and slot != record_slot:
                    error('ENDPOINT_RECORD_SLOT_MISMATCH')
            signature = state.get('transaction_signature')
            if signature is None or first_signature is None:
                result['unknowns'].append('ENDPOINT_TRANSACTION_BINDING_MISSING:' + state_address)
            elif not _signature(signature):
                error('ENDPOINT_TRANSACTION_BINDING_INVALID')
            elif signature != first_signature or (_signature(context_signature) and signature != context_signature):
                error('ENDPOINT_TRANSACTION_BINDING_MISMATCH')
        if len(endpoint_slots) > 1:
            error('ENDPOINT_SLOT_DISAGREEMENT')
        if set(expected) != observed or not expected:
            result['unknowns'].append('RAW_STATE_MISSING_REQUIRED_ENDPOINTS')
        decoded_by_address = {s['address']: s['state'] for s in result['endpoint_states'] if 'state' in s}
        if expected and set(expected) == set(decoded_by_address) and all(
                s['structural_profile_match'] for s in decoded_by_address.values()):
            distributed = sum(int(d['amount_raw']) for d in result['distributions'])
            amounts_match = (decoded_by_address[b['mint']]['base']['supply'] == SUPPLY
                            and decoded_by_address[b['associated_bonding_curve']]['base']['amount'] == SUPPLY-distributed
                            and decoded_by_address[result['bindings']['holder_ata']]['base']['amount'] == distributed)
            result['endpoint_amount_comparison'] = {'declared_issuance_raw': str(SUPPLY),
                'declared_distribution_raw': str(distributed), 'amounts_agree': amounts_match,
                'prestate_inferred': False, 'effects_authenticated': False, 'coverage_complete': False}
            if not amounts_match:
                error('ENDPOINT_AMOUNT_OR_SUPPLY_DISAGREEMENT')
    result['observed_inventory_agreement'] = (trace['syntax_complete'] and bool(result['inventory'])
                                            and all(i['role'] != 'unresolved' for i in result['inventory']))
    result['supported_sequence_agreement'] = (result['observed_inventory_agreement'] and not result['errors']
        and result['create_arguments'] is not None and result['create_arguments'].get('observed_suffix_bytes_agree') is True
        and result['endpoint_amount_comparison'] is not None and result['endpoint_amount_comparison']['amounts_agree']
        and result['signer_witness'] is not None and not any(
            u.startswith(('RAW_STATE_MISSING', 'ENDPOINT_SLOT_MISSING', 'ENDPOINT_TRANSACTION_BINDING_MISSING',
                          'SOURCE_SLOT_MISSING', 'RECORD_SLOT_MISSING', 'SOURCE_SIGNATURE_MISSING'))
            for u in result['unknowns']))
    result['errors'] = sorted(set(result['errors']))
    result['unknowns'] = sorted(set(result['unknowns']))
    return result
