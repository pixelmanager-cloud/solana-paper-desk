"""Exit-only original SELL observations; no entry windows, I/O or permission."""
from dataclasses import dataclass

from . import quote_execution as qe
from .model import digest, validate_event
from .paper_market_adapter import _replay_collected
from .paper_observation_collector import TargetObservation, ObservationTarget


@dataclass(frozen=True)
class ExitContext:
    now: int
    target: ObservationTarget
    rpc_source_id: str
    quote_source_id: str
    provenance: str
    known_hazards: tuple[str, ...] = ()
    token_profile_version: int = 0


def build_exit_event(collected, *, context, position, cfg, load_evidence):
    """Consume a public-reader-verified held position and exact full SELL read.

    Coordinator supplies source roster, hazard diagnostics and verified position;
    hashes bind local original records, never authenticate providers. Planner may
    request an additional exact partial-action quote, bound through its existing
    typed quote tuple. No history, entry profile, reserve price or USD fallback.
    """
    if (type(context) is not ExitContext or type(context.now) is not int or context.now<0
            or type(context.target) is not ObservationTarget or not callable(load_evidence)
            or type(context.known_hazards) is not tuple or len(context.known_hazards)>32
            or any(type(x) is not str or not 1<=len(x)<=128 for x in context.known_hazards)
            or context.provenance not in ('SYNTHETIC_TEST_ONLY','PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE')):
        raise ValueError('Trusted exit coordinator context required')
    out={'kind':'paper_exit_adapter_v1','event':None,'blockers':[],
         'entry_authorized':False,'execution_verified':False}
    try:
        from .token2022_paper import selected
        if context.token_profile_version!=selected(cfg):raise ValueError('Exit token profile mismatch')
        if (not qe.config(cfg) or type(collected) is not TargetObservation
                or collected.target!=context.target or collected.direction!='sell'):
            raise ValueError('Exit collection required')
        target,mint,pool,quote,_,_,records=_replay_collected(collected,context,load_evidence)
        qe.validate_position(target.mint,position,cfg)
        quantity=qe.raw_quantity(position['qty'],position['quote_execution']['mint_decimals'])
        if (position['pool']!=target.pool or position['taker']!=target.taker
                or position['provenance']!=context.provenance
                or mint.decimals!=position['quote_execution']['mint_decimals']
                or quote.direction!='sell' or quote.input_raw!=quantity or target.amount_raw!=quantity):
            raise ValueError('Held position binding mismatch')
        evidence={'collector_refs':sorted(records),'mint_hash':mint.source.raw_hash,
            'pool_hash':pool.source.raw_hash,'quote_hash':quote.source.raw_hash,
            'rpc_source_id':context.rpc_source_id,'quote_source_id':context.quote_source_id,
            'mint_at':mint.source.observed_at,'pool_at':pool.source.observed_at,
            'quote_at':quote.source.observed_at,'mint_slot':mint.slot,'pool_slot':pool.slot}
        e={'schema_version':1,'kind':'quote_exit','exit_contract_version':1,'ts':context.now,
            'mint':target.mint,'pool':target.pool,'taker':target.taker,'provenance':context.provenance,
            'price_at':quote.source.observed_at,'route_available':True,
            'danger':bool(context.known_hazards),'known_hazards':list(context.known_hazards),
            'current_quantity_raw':quantity,'mint_decimals':mint.decimals,'source_evidence':evidence,
            'execution_status':'EXECUTION_UNVERIFIED','entry_authorized':False}
        e['event_id']='paper-exit:'+digest(e)
        validate_event(e)
        qe._book(e,(quote,),cfg)  # exact original-byte quote caps/binding before publication
        out['event']=e
    except (ValueError,TypeError,KeyError,AttributeError,IndexError,OSError,RecursionError):
        out['blockers']=['EXIT_SOURCE_OR_POSITION_BINDING_INVALID']
    return out
