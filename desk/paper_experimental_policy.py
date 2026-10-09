"""Read-only paper policy projection; engine/event admission remains untouched.

PAPER_EXPERIMENTAL waives only unresolved ownership HISTORY as an explicit
risk, not source binding, observed hazards, controls, market data or routes.
Current persisted diagnostics cannot emit a validated market event; return
precise blockers rather than invented ownership percentages or approval flags.
"""
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
import zlib

from .control_obligations import ReplayView, read_guard
from .decision_runner import assess
from .live_features import MAX_SOURCE_BYTES, REQUIRED, _BoundedStore
from .model import digest

POLICY = 'paper_experimental_history_risk_v1'
PAPER_STRICT = 'PAPER_STRICT'
PAPER_EXPERIMENTAL = 'PAPER_EXPERIMENTAL'
HISTORY_GATES = ('launch_anchor', 'mint_history_coverage', 'historical_account_inventory',
                 'transfer_history', 'history_snapshot')
INCOMPLETE_HISTORY = frozenset(('LAUNCH_RAW_HISTORY_UNAVAILABLE', 'REQUEST_BOUND_HISTORY_UNAVAILABLE',
    'ACCOUNT_INVENTORY_RAW_HISTORY_UNAVAILABLE', 'TRANSFER_RAW_HISTORY_UNAVAILABLE',
    'HISTORY_TO_CURRENT_SNAPSHOT_NOT_RECONCILED', 'HISTORICAL_ACCOUNT_FRONTIER_NOT_CLOSED',
    'ACCOUNT_HISTORY_QUERY_MISSING_OR_AMBIGUOUS', 'LAUNCH_ANCHOR_UNVERIFIED'))
# Classification never uses suffix guessing or treats all BLOCKED as a hazard.
# Both known hazards and unrecognized/unknown control reasons still block.
KNOWN_HAZARDS = frozenset(('ACTIVE_MINT_AUTHORITY', 'ACTIVE_FREEZE_AUTHORITY',
    'TOKEN_2022_NOT_ALLOWED', 'UNKNOWN_TOKEN_PROGRAM', 'INVALID_MINT_LAYOUT',
    'INVALID_MINT_STATE', 'ZERO_SUPPLY', 'DELEGATED_HOLDER_ACCOUNT',
    'FROZEN_HOLDER_ACCOUNT', 'EXTERNAL_CLOSE_AUTHORITY', 'TOKEN_ACCOUNT_DELEGATE',
    'OUTSTANDING_WITHDRAWABLE_LP_SUPPLY', 'POOL_LAYOUT_HAS_UNKNOWN_EXTENSION',
    'ACCOUNT_HISTORY_CONFLICTING_TRANSACTION', 'LAUNCH_FREEZE_AUTHORITY',
    'LAUNCH_CREATOR_OR_USER_MISMATCH', 'LAUNCH_CURVE_PDA_MISMATCH',
    'LAUNCH_MINT_AUTHORITY_MISMATCH', 'LAUNCH_TOKEN_PROGRAM_MISMATCH'))


def _hash(value):
    return type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def ownership_history_rule(*, mode, history_reasons, nonhistory_controls_passed, known_hazards):
    """Pure policy intent, NEVER an event/source/control certificate.

    Coordinator integration must supply replayed controls; the public adapter
    below derives them itself. An accepted risk does not approve an entry.
    """
    if mode not in (PAPER_STRICT, PAPER_EXPERIMENTAL):raise ValueError('Unsupported paper policy mode')
    if type(nonhistory_controls_passed) is not bool or type(history_reasons) is not list or type(known_hazards) is not list:
        raise ValueError('Explicit policy observations required')
    if any(type(r) is not str or not r or len(r)>256 for r in history_reasons+known_hazards) or len(history_reasons)+len(known_hazards)>128:
        raise ValueError('Bounded policy reasons required')
    unresolved=bool(history_reasons)
    unsupported=sorted(set(history_reasons)-INCOMPLETE_HISTORY)
    accepted=(mode==PAPER_EXPERIMENTAL and unresolved and nonhistory_controls_passed and not known_hazards and not unsupported)
    return {'status':'UNKNOWN' if unresolved else 'NO_UNRESOLVED_HISTORY_REPORTED',
            'unknown_reasons':list(history_reasons), 'unwaived_reasons':unsupported, 'risk_accepted':accepted,
            'risk_flag':'UNRESOLVED_OWNERSHIP_HISTORY' if accepted else None,
            'ownership_verified':False, 'ownership_complete':False,
            'blocks_history':unresolved and not accepted}



def _validate_supplied_history(report, store):
    """Absent history may be a risk; supplied history must remain replayable."""
    if 'history_queries' not in report:return
    from .replay_history import replay_history,reconstruct_launch_history
    try:
        queries=report['history_queries']
        if type(queries) is not list or len(queries)>18:
            raise ValueError('Invalid supplied history queries')
        if any(type(q) is not dict or type(q.get('pages')) is not list for q in queries):
            raise ValueError('Invalid supplied history coverage')
        if sum(len(q['pages']) for q in queries)>18:
            raise ValueError('History request ceiling exceeded')
        if len({digest(q) for q in queries})!=len(queries):
            raise ValueError('Duplicate supplied history query')
        # Replay every supplied query, including account/funding queries that
        # the main evaluator may otherwise collapse into unavailable reasons.
        for query in queries:replay_history(query,store)
        mint_queries=[q for q in queries if q.get('address')==report['mint']
                      and q.get('token_accounts_filter')=='none']
        if mint_queries:
            if len(mint_queries)!=1:raise ValueError('Ambiguous mint discovery query')
            reconstruct_launch_history(report,store)
    except (ValueError,TypeError,KeyError,IndexError,AttributeError,zlib.error,UnicodeError,OverflowError,RecursionError) as exc:
        raise ValueError('PERSISTED_HISTORY_INTEGRITY_INVALID') from exc


def paper_candidate(research_db,evidence_db,scan_id,*,source_hash,now,
                    mode=PAPER_STRICT,revision_hash=None):
    """Read exact persisted scan/raw diagnostics, never accept caller events.

    source_hash/revision_hash are comparison pins, not authenticity admission.
    A missing history head stays UNKNOWN; an explicitly mismatching head rejects.
    No writes, provider calls, fill, migration or engine invocation occur.
    """
    if (type(scan_id) is not str or not 0<len(scan_id)<=256 or not _hash(source_hash)
            or type(now) is not int or not 0<=now<2**63
            or mode not in (PAPER_STRICT,PAPER_EXPERIMENTAL)
            or revision_hash is not None and not _hash(revision_hash)):
        raise ValueError('Invalid paper policy identity/mode/time')
    result={'kind':'paper_candidate_decision','policy':POLICY,'policy_version':1,
        'mode':mode,'paper_only':True,'policy_scope':'OWNERSHIP_HISTORY_RISK_ONLY',
        'component_scope':'historical_raw_replay_not_current_entry_attestation',
        'event_contract_schema_version':1,'scan_id':scan_id,'mint':None,'evaluated_at':now,
        'source_hash':None,'requested_source_hash':source_hash,'revision_hash':None,
        'evidence_hashes':[],'decision':'REJECT','eligible_for_trading':False,
        'source_authenticated':False,'ownership_verified':False,'ownership_complete':False,
        'ownership_history':None,'risk_flags':[],'known_hazards':[],
        'components':{},'fields':{name:{'status':'UNKNOWN','value':None,'units':units,
                    'unknown_reasons':[reason],'evidence_hashes':[]}
                    for name,(units,reason) in REQUIRED.items()},'blockers':[]}
    try:
        with ExitStack() as guards:
            guards.enter_context(read_guard(research_db));guards.enter_context(read_guard(evidence_db))
            with closing(sqlite3.connect(Path(research_db).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as c:
                c.row_factory=sqlite3.Row;c.execute('BEGIN')
                size=c.execute('SELECT length(CAST(result AS BLOB)) FROM scans WHERE id=?',(scan_id,)).fetchone()
                if size is None or type(size[0]) is not int or not 0<size[0]<=MAX_SOURCE_BYTES:
                    raise ValueError('SOURCE_MISSING_MALFORMED_OR_OVERSIZED')
                scan=dict(c.execute('SELECT id,mint,created,status,result FROM scans WHERE id=?',(scan_id,)).fetchone())
            result['source_hash']=digest(scan);result['mint']=scan['mint']
            if result['source_hash']!=source_hash:raise ValueError('SOURCE_HASH_MISMATCH')
            with closing(sqlite3.connect(Path(evidence_db).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as c:
                has_head=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ownership_heads'").fetchone()
                head=c.execute('SELECT evidence_hash FROM ownership_heads WHERE scan_id=?',(scan_id,)).fetchone() if has_head else None
            current=head[0] if head else None
            if current is not None and not _hash(current):raise ValueError('SOURCE_REVISION_MALFORMED')
            if revision_hash is not None and current!=revision_hash:raise ValueError('SOURCE_REVISION_MISMATCH')
            result['revision_hash']=current
            view=ReplayView(_BoundedStore(evidence_db))
            decision=assess(scan,now,view,progress={'evidence_hash':current} if current else None)
            gates=decision['entry_evidence']['gates'];result['components']=gates
            report=json.loads(scan['result'])
            all_reasons={r for gate in gates.values() for r in gate['reasons']}
            result['known_hazards']=sorted(KNOWN_HAZARDS & all_reasons)
            _validate_supplied_history(report,view)
            if current is not None:
                revision=view.load(current)
                if type(revision) is not dict or 'history_queries' not in revision:
                    raise ValueError('PERSISTED_HISTORY_INTEGRITY_INVALID')
                _validate_supplied_history({**report,'history_queries':revision['history_queries']},view)
            hashes=set(view.requested)
            for gate in gates.values():hashes.update(gate['evidence_hashes'])
            if len(hashes)>128 or any(not _hash(h) for h in hashes):raise ValueError('EVIDENCE_MANIFEST_INVALID')
            result['evidence_hashes']=sorted(hashes)
            all_reasons={r for gate in gates.values() for r in gate['reasons']}
            result['known_hazards']=sorted(KNOWN_HAZARDS & all_reasons)
            report=json.loads(scan['result'])
            # Arbitrary investigation findings are retained as blockers, not
            # silently reclassified into a waivable ownership-risk suffix.
            result['blockers']+=report.get('findings',[])
            if scan['status']!='COMPLETE':result['blockers'].append('INVESTIGATION_NOT_COMPLETE')
            observed=report.get('observed_at')
            fresh=type(observed) is int and 0<=now-observed<=10
            if not fresh:result['blockers'].append('INVESTIGATION_NOT_FRESH_FOR_ENTRY')
            if report.get('mint')!=scan['mint']:result['blockers'].append('INVESTIGATION_MINT_UNVERIFIED')
            controls=all(gates[name]['status']=='VERIFIED_COMPONENT' for name in
                         ('report_integrity','token_controls','holder_snapshot','pool_liquidity'))
            history_reasons=sorted({r for name in HISTORY_GATES for r in gates[name]['reasons']})
            result['blockers'] += [r for r in report.get('unknowns',[]) if r not in history_reasons]
            rule=ownership_history_rule(mode=mode,history_reasons=history_reasons,
                    nonhistory_controls_passed=controls and fresh and not result['blockers'],known_hazards=result['known_hazards'])
            result['ownership_history']=rule
            if rule['risk_flag']:result['risk_flags'].append(rule['risk_flag'])
            if rule['blocks_history']:result['blockers']+=['OWNERSHIP_HISTORY_UNRESOLVED']+rule['unwaived_reasons']
            for name in ('report_integrity','token_controls','holder_snapshot','pool_liquidity','bundle_exposure','sellability','strategy_inputs'):
                result['blockers']+=gates[name]['reasons']
            result['blockers']+=result['known_hazards']
            # Existing diagnostic schema contains no attested market event.
            # Do not cast UNKNOWN concentration to zero to satisfy validate_event.
            result['blockers']+=['PERSISTED_CURRENT_MARKET_EVENT_UNAVAILABLE',
                'EXACT_ENTRY_QUANTITY_AND_COST_UNAVAILABLE','CURRENT_MARKET_FRESHNESS_UNAVAILABLE',
                'UNKNOWN_OWNERSHIP_PERCENTAGES_INCOMPATIBLE_WITH_CURRENT_EVENT_CONTRACT']
    except (ValueError,TypeError,KeyError,sqlite3.Error,OverflowError,RecursionError,zlib.error,UnicodeError,OSError) as error:
        code=str(error)
        result['blockers'].append(code if code in ('SOURCE_MISSING_MALFORMED_OR_OVERSIZED','SOURCE_HASH_MISMATCH',
            'SOURCE_REVISION_MALFORMED','SOURCE_REVISION_MISMATCH','EVIDENCE_MANIFEST_INVALID',
            'PERSISTED_HISTORY_INTEGRITY_INVALID') else 'PERSISTED_DIAGNOSTICS_UNAVAILABLE')
        result['risk_flags']=[]
        result['ownership_history']=ownership_history_rule(mode=mode,history_reasons=['PERSISTED_OWNERSHIP_HISTORY_UNAVAILABLE'],
                    nonhistory_controls_passed=False,known_hazards=result['known_hazards'])
    result['blockers']=sorted(set(result['blockers']));result['decision_hash']=digest(result)
    return result
