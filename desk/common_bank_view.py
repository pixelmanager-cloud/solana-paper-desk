"""Disconnected original-parent view: local binding, never a bank certificate.

Trusted callers supply their existing guarded read-only ReplayView. This module
performs only persisted loads; no capture, reservation, receipt or runtime wiring.
"""
from dataclasses import dataclass
import json
import re
import sqlite3
import struct
import zlib

from .account_history import account_inventory
from .control_obligations import ReplayView
from .model import canonical, digest
from .pool_capture_bridge import canonical_accounts
from .pool_vault_admission import GENESIS, NETWORK, _mint, _vault, _raw_account, _Reject
from .pools import parse_pool
from .programs import address
from .providers import PUMP, PUMPSWAP, SOL
from .replay_history import replay_history
from .security import TOKEN_PROGRAM, holding_policy

MAX_RECORD_BYTES = 64 * 1024
MAX_DISCOVERY_BYTES = 2 * 1024 * 1024
FIELDS = {'kind','mint','discovery_hash','bank_response_hash','bank_request_hash',
          'slot_response_hash','genesis_response_hash','clock_response_hash',
          'clock_request_hash','keys','frontier','ownership_indices','pool_indices'}
REFS = FIELDS - {'kind','mint','keys','frontier','ownership_indices','pool_indices'}


class CommonBankError(ValueError):
    """Static refusal; no partial view is returned."""


def _need(condition, code):
    if not condition: raise CommonBankError(code)


def _integer(value): return type(value) is int and 0 <= value < 2**63


def _load(view, key, limit=None):
    if limit is None: limit=MAX_RECORD_BYTES
    _need(type(key) is str and re.fullmatch('[0-9a-f]{64}',key) is not None,'COMMON_BANK_REFERENCE_INVALID')
    value=view.load(key)
    _need(type(value) is dict and len(canonical(value).encode()) <= limit,'COMMON_BANK_RECORD_BOUND')
    _need(digest(value)==key,'COMMON_BANK_HASH_MISMATCH')
    return value


def _rpc(record, method, params):
    _need(set(record)=={'kind','method','params','result'} and record['kind']=='rpc_response_v1'
          and record['method']==method and canonical(record['params'])==canonical(params),
          'COMMON_BANK_RPC_BINDING_INVALID')
    return record['result']


def _request(record, method, params, response_hash):
    _need(canonical(record)==canonical({'kind':'common_bank_request_v1','network':NETWORK,'genesis_hash':GENESIS,
                  'method':method,'params':params,'response_hash':response_hash}),
          'COMMON_BANK_REQUEST_BINDING_INVALID')


@dataclass(frozen=True)
class CommonBankView:
    manifest_hash: str
    parent_hash: str
    parent_json: str
    keys: tuple
    frontier: tuple
    ownership_indices: tuple
    pool_indices: tuple
    slot: int
    request_floor: int
    block_time: int
    discovery_cutoff: int
    evidence_hashes: tuple

    def account(self, key):
        """Detached raw account or explicit None; never a sliced RPC response."""
        return json.loads(self.parent_json)['result']['value'][self.keys.index(key)]

    def diagnostic(self):
        return {'schema':'common_bank_index_view_v1','local_binding_valid':True,
                'manifest_hash':self.manifest_hash,'parent_hash':self.parent_hash,
                'snapshot_slot':self.slot,'request_floor':self.request_floor,
                'snapshot_time':self.block_time,'discovery_cutoff':self.discovery_cutoff,
                'keys':list(self.keys),'frontier':list(self.frontier),
                'ownership_indices':list(self.ownership_indices),'pool_indices':list(self.pool_indices),
                'holder_states':{k:'ABSENT_AT_BANK_UNVERIFIED_LIFETIME' if self.account(k) is None
                                 else 'OBSERVED_LEGACY_ACCOUNT' for k in self.frontier},
                'evidence_hashes':list(self.evidence_hashes),'provider_calls':0,
                'source_authenticated':False,'finality_authenticated':False,
                'frontier_at_bank_complete':False,'history_complete':False,
                'closed_lifetimes_verified':False,'cpi_success_verified':False,
                'historical_interval_exclusion_allowed':False,'ownership_approved':False,
                'common_control_verified':False,'private_control_proven':False,
                'lifecycle_verified':False,'historical_control_verified':False,
                'production_point_prerequisite':False,
                'eligible_for_trading':False,'decision':'REJECT'}


def validate_common_bank(manifest_hash, view):
    """Validate stored manifest/raw discovery and one original atomic parent.

    No scan/admission/journal/source trust is established here. Caller must hold
    the accepted read guard; future consumers must bind this result to their
    source/seal/budget and all competing observations before any use.
    """
    try:
        _need(type(view) is ReplayView and view.read_only is True,'COMMON_BANK_SHARED_VIEW_REQUIRED')
        m=_load(view,manifest_hash)
        _need(set(m)==FIELDS and m['kind']=='common_bank_manifest_v1','COMMON_BANK_MANIFEST_INVALID')
        for field in REFS:
            _need(type(m[field]) is str and re.fullmatch('[0-9a-f]{64}',m[field]) is not None,
                  'COMMON_BANK_REFERENCE_INVALID')
        mint=address(m['mint']); _need(mint!=SOL,'COMMON_BANK_MINT_UNSUPPORTED')
        genesis=_load(view,m['genesis_response_hash']); slot_record=_load(view,m['slot_response_hash'])
        _need(_rpc(genesis,'getGenesisHash',[])==GENESIS,'COMMON_BANK_GENESIS_MISMATCH')
        floor=_rpc(slot_record,'getSlot',[{'commitment':'finalized'}])
        _need(_integer(floor),'COMMON_BANK_FLOOR_INVALID')
        coverage=_load(view,m['discovery_hash'],MAX_DISCOVERY_BYTES)
        _need(type(coverage.get('pages')) is list and 1<=len(coverage['pages'])<=18,'COMMON_BANK_DISCOVERY_PAGE_CEILING')
        bounds=coverage.get('slot_range')
        _need(type(bounds) is dict and set(bounds)=={'gte','lt'} and type(bounds['gte']) is int
              and bounds['gte']==0 and type(bounds['lt']) is int and 0<bounds['lt']<=floor+1,
              'COMMON_BANK_DISCOVERY_RANGE_INVALID')
        observations,rebuilt=replay_history(coverage,view)
        inventory=account_inventory(mint,observations,rebuilt)
        _need(inventory['initialization_inventory_verified'],'COMMON_BANK_DISCOVERY_UNVERIFIED')
        frontier=[r['address'] for r in inventory['accounts']]
        _need(frontier and len(frontier)<=99 and mint not in frontier,'COMMON_BANK_FRONTIER_INVALID')
        owners={}
        for row in inventory['accounts']:
            initial=row['initializations']
            _need(initial and all(r['program']==TOKEN_PROGRAM for r in initial),'COMMON_BANK_TOKEN_PROGRAM_UNSUPPORTED')
            declared={address(r['owner_at_initialization']) for r in initial}
            _need(len(declared)==1,'COMMON_BANK_HOLDER_AUTHORITY_AMBIGUOUS')
            owners[row['address']]=next(iter(declared))
        pool=canonical_accounts(mint)
        _need(len(set(pool))==6 and pool[4] in frontier,'COMMON_BANK_BASE_VAULT_NOT_DISCOVERED')
        keys=pool+sorted(set(frontier)-set(pool))
        ownership=[keys.index(mint)]+[keys.index(k) for k in sorted(frontier)]
        _need(len(keys)<=100,'COMMON_BANK_ACCOUNT_CEILING')
        _need(type(m['keys']) is list and m['keys']==keys and type(m['frontier']) is list
              and m['frontier']==sorted(frontier),'COMMON_BANK_FRONTIER_OR_ORDER_MISMATCH')
        for field,expected in (('ownership_indices',ownership),('pool_indices',list(range(6)))):
            _need(type(m[field]) is list and all(type(i) is int for i in m[field]) and m[field]==expected,
                  'COMMON_BANK_ROLE_INDICES_INVALID')
        params=[keys,{'encoding':'base64','commitment':'finalized','minContextSlot':floor}]
        bank=_load(view,m['bank_response_hash']); result=_rpc(bank,'getMultipleAccounts',params)
        _request(_load(view,m['bank_request_hash']),'getMultipleAccounts',params,m['bank_response_hash'])
        _need(type(result) is dict and type(result.get('context')) is dict
              and _integer(result['context'].get('slot')),'COMMON_BANK_CONTEXT_INVALID')
        at_slot=result['context']['slot']; values=result.get('value')
        _need(at_slot>=floor and type(values) is list and len(values)==len(keys),'COMMON_BANK_ATOMIC_RESPONSE_INVALID')
        clock=_load(view,m['clock_response_hash']); at=_rpc(clock,'getBlockTime',[at_slot])
        _need(_integer(at),'COMMON_BANK_CLOCK_INVALID')
        _request(_load(view,m['clock_request_hash']),'getBlockTime',[at_slot],m['clock_response_hash'])
        _raw_account(values[0],PUMPSWAP,(243,287,300,301)); fields=parse_pool(values[0])
        from solders.pubkey import Pubkey
        creator,_=Pubkey.find_program_address([b'pool-authority',bytes(Pubkey.from_string(mint))],Pubkey.from_string(PUMP))
        _,bump=Pubkey.find_program_address([b'pool',bytes(2),bytes(creator),bytes(Pubkey.from_string(mint)),bytes(Pubkey.from_string(SOL))],Pubkey.from_string(PUMPSWAP))
        _need(fields['unknown_trailing_bytes']==0 and fields['pool_bump']==bump
              and fields['index']==0 and fields['creator']==str(creator)
              and [fields[k] for k in ('base_mint','quote_mint','lp_mint','pool_base_token_account','pool_quote_token_account')]==pool[1:],
              'COMMON_BANK_POOL_IDENTITY_INVALID')
        _need(not any(fields[k] for k in ('is_mayhem_mode','is_cashback_coin','is_holder_reward','can_edit_creator_fee')),
              'COMMON_BANK_POOL_PROFILE_UNSUPPORTED')
        supply=_mint(values[1]);_need(supply>0,'COMMON_BANK_MINT_SUPPLY_INVALID')
        _mint(values[2],native=True);_mint(values[3],lp_authority=pool[0])
        amount=_vault(values[4],mint,pool[0],native=False);_vault(values[5],SOL,pool[0],native=True)
        _need(amount<=supply,'COMMON_BANK_VAULT_SUPPLY_INVALID')
        for key in frontier:
            value=values[keys.index(key)]
            if value is None:continue  # Absence is not zero balance or proven closure.
            raw=_raw_account(value,TOKEN_PROGRAM,(165,))
            _need(int.from_bytes(raw[109:113],'little')==0,'COMMON_BANK_HOLDER_NATIVE_UNSUPPORTED')
            checked=holding_policy(value,mint,owners[key])
            _need(checked['decision']=='PASS_HOLDING_POLICY','COMMON_BANK_HOLDER_BINDING_INVALID')
        return CommonBankView(manifest_hash,m['bank_response_hash'],canonical(bank),tuple(keys),tuple(sorted(frontier)),
                              tuple(ownership),tuple(range(6)),at_slot,floor,at,bounds['lt']-1,tuple(sorted(view.requested)))
    except CommonBankError:
        raise
    except _Reject as exc:
        raise CommonBankError(str(exc)) from None
    except (ValueError,TypeError,KeyError,IndexError,AttributeError,RecursionError,OverflowError,
            UnicodeError,zlib.error,sqlite3.Error,OSError,struct.error,ImportError):
        raise CommonBankError('COMMON_BANK_RECORD_OR_DISCOVERY_INVALID') from None
