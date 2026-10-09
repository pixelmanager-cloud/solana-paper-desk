"""Read-only consumer checks; never repair, initialize or replace ledger records."""
from .token2022_paper import selected as selected_token_profile
import json
import sqlite3
from decimal import localcontext
from .model import decimal, digest


class RecoveryRequired(ValueError):
    """Persisted paper experiment cannot safely be projected or monitored."""


def validate_checkpoint(connection, payload):
    """Validate persisted engine state without rebuilding or changing it."""
    try:
        state = json.loads(payload)
        required = {'cash', 'realized_pnl', 'positions', 'cooldowns', 'mode',
                    'last_ts', 'day', 'day_start_equity', 'day_gross_losses',
                    'peak_equity', 'max_drawdown', 'last_entry_minute', 'loss_streak'}
        if not isinstance(state, dict) or not required <= state.keys():
            raise ValueError('missing state fields')
        if state['mode'] not in ('RUNNING', 'ENTRY_PAUSED', 'EXIT_ONLY', 'LIQUIDATING', 'STOPPED'):
            raise ValueError('invalid mode')
        for key in ('cash', 'realized_pnl', 'day_start_equity', 'day_gross_losses',
                    'peak_equity', 'max_drawdown'):
            if not isinstance(state[key], str):
                raise ValueError('invalid decimal field')
            decimal(state[key])
        for key in ('last_ts', 'last_entry_minute', 'loss_streak'):
            if type(state[key]) is not int:
                raise ValueError('invalid integer field')
        if state['last_ts'] < 0 or state['loss_streak'] < 0 or state['last_entry_minute'] < -1:
            raise ValueError('invalid counter')
        if state['day'] is not None and not isinstance(state['day'], str):
            raise ValueError('invalid day')
        if not isinstance(state['positions'], dict) or not isinstance(state['cooldowns'], dict):
            raise ValueError('invalid position/cooldown mapping')
        for mint, until in state['cooldowns'].items():
            if not mint or type(until) is not int or until < 0:
                raise ValueError('invalid cooldown')
        position_fields = {'qty', 'initial_qty', 'cost_left', 'initial_cost', 'trade_pnl',
                           'opened_at', 'exit_blocked', 'mark_status', 'stage', 'stop_ratio',
                           'peak_ratio', 'touched_15', 'mark_value', 'mark_at', 'pool',
                           'entry_scores', 'provenance', 'taker'}
        for mint, position in state['positions'].items():
            if not mint or not isinstance(position, dict) or not position_fields <= position.keys():
                raise ValueError('missing position fields')
            for key in ('qty', 'initial_qty', 'cost_left', 'initial_cost', 'trade_pnl',
                        'stop_ratio', 'peak_ratio', 'mark_value'):
                if not isinstance(position[key], str):
                    raise ValueError('invalid position decimal')
                decimal(position[key])
            if decimal(position['qty']) <= 0 or decimal(position['qty']) > decimal(position['initial_qty']):
                raise ValueError('invalid remaining quantity')
            for key in ('opened_at', 'mark_at', 'stage'):
                if type(position[key]) is not int or position[key] < 0:
                    raise ValueError('invalid position integer')
            if (type(position['touched_15']) is not bool or position['stage'] > 3
                    or position['mark_status'] not in ('MODEL_ESTIMATE', 'STALE', 'UNVERIFIED_EXIT')
                    or not isinstance(position['entry_scores'], dict)
                    or not isinstance(position['pool'], str) or not position['pool']
                    or not isinstance(position['provenance'], str) or not position['provenance']
                    or (position['taker'] is not None and not isinstance(position['taker'], str))
                    or (position['exit_blocked'] is not None and not isinstance(position['exit_blocked'], str))):
                raise ValueError('invalid position identity/shape')
        latest = connection.execute('SELECT MAX(ts) FROM events').fetchone()[0]
        if latest is None or state['last_ts'] != latest:
            raise ValueError('checkpoint journal timestamp mismatch')
        validate_entry_policies(connection, state)
        return state
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError) as exc:
        raise RecoveryRequired('CHECKPOINT_INVALID') from exc



def read_checkpoint(connection, *, _implementation=None):
    """Return a verified saved state, or None only for a genuinely fresh ledger.

    Caller supplies a read transaction so evidence and checkpoint are one snapshot.
    Raw observations and health alone do not imply committed paper accounting.
    """
    row = connection.execute('SELECT payload FROM state WHERE id=1').fetchone()
    metadata = dict(connection.execute(
        "SELECT key,value FROM metadata WHERE key IN ('config','config_hash','implementation_hash')"))
    events = connection.execute('SELECT 1 FROM events LIMIT 1').fetchone()
    outcomes = connection.execute('SELECT 1 FROM outcomes LIMIT 1').fetchone()
    sequence = connection.execute(
        "SELECT 1 FROM sqlite_sequence WHERE name IN ('events','outcomes') AND seq>0 LIMIT 1").fetchone()
    if not row:
        runtime_record = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name LIKE 'paper_runtime_%' LIMIT 1").fetchone()
        if events or outcomes or metadata or sequence or runtime_record:
            raise RecoveryRequired('CHECKPOINT_MISSING')
        return None
    if not events or connection.execute(
            'SELECT 1 FROM outcomes o LEFT JOIN events e ON e.event_id=o.event_id '
            'WHERE e.event_id IS NULL LIMIT 1').fetchone():
        raise RecoveryRequired('EVENT_JOURNAL_INCOMPLETE')
    if not all(metadata.get(key) for key in ('config','config_hash','implementation_hash')):
        raise RecoveryRequired('EXPERIMENT_IDENTITY_MISSING')
    try:
        cfg = json.loads(metadata['config'])
        if not isinstance(cfg, dict) or digest(cfg) != metadata['config_hash']:
            raise ValueError('Invalid saved config')
    except (ValueError, TypeError):
        raise RecoveryRequired('EXPERIMENT_IDENTITY_INVALID') from None
    try:
        from .runtime_compatibility import require_runtime
        # Internal callers may propagate the source freshly hashed in this
        # same validation invocation. Default readers always hash for themselves.
        require_runtime(connection,implementation=_implementation)
    except (ValueError, TypeError, KeyError, sqlite3.Error, OSError, RecursionError):
        raise RecoveryRequired('RUNTIME_IDENTITY_INVALID') from None
    return validate_checkpoint(connection, row[0])


def validate_entry_policies(connection, state):
    """Existing Ledger pre-duplicate hook: validate policy and quote journals."""
    try:
        validate_quote_positions(connection, state)
        _validate_entry_policies(connection, state)
    except (ArithmeticError, RecursionError, TypeError, KeyError) as exc:
        raise ValueError('Invalid persisted paper evidence') from exc


def policy_version(cfg):
    legacy = cfg.get('experimental_policy_version')
    signal = cfg.get('paper_signal_policy_version')
    if signal is not None:
        if (type(signal) is not int or signal != 3
                or (legacy is not None and (type(legacy) is not int or legacy != 3))
                or cfg.get('mode') != 'paper'
                or type(cfg.get('paper_quote_execution_version')) is not int
                or cfg['paper_quote_execution_version'] != 1):
            raise ValueError('Invalid explicit paper signal configuration')
        return signal
    if legacy is not None and (type(legacy) is not int or legacy not in (1, 2)
                               or cfg.get('mode') != 'paper'):
        raise ValueError('Invalid saved experimental policy')
    return legacy


def _validate_entry_policies(connection, state):
    """Rebind experimental risk metadata to the original hashed entry event.

    Local journal consistency only, never provider/source authentication.
    Missing or edited checkpoint metadata cannot promote a policy or lose risk
    labels on restart. No mutation, migration, or silent reconstruction occurs.
    """
    from .model import canonical, PAPER_EXPERIMENTAL
    from .strategy import experimental_scores
    row = connection.execute("SELECT value FROM metadata WHERE key='config'").fetchone()
    if row is None:
        raise ValueError('Missing saved experiment configuration')
    cfg = json.loads(row[0]); version = policy_version(cfg)
    if version is None:
        if any('entry_policy' in p or 'entry_event_id' in p for p in state['positions'].values()):
            raise ValueError('Experimental policy in strict checkpoint')
        return
    for mint, position in state['positions'].items():
        identity = position.get('entry_event_id')
        if type(identity) is not str or not identity or len(identity) > 4096:
            raise ValueError('Missing experimental entry identity')
        size = connection.execute('SELECT length(CAST(payload AS BLOB)) FROM events WHERE event_id=?', (identity,)).fetchone()
        if size is None or not 0 < size[0] <= 2 * 1024 * 1024:
            raise ValueError('Experimental entry unavailable')
        payload, saved_hash = connection.execute('SELECT payload,payload_hash FROM events WHERE event_id=?', (identity,)).fetchone()
        event = json.loads(payload)
        if (digest(event) != saved_hash or event.get('event_id') != identity
                or event.get('mint') != mint or event.get('pool') != position['pool']
                or event.get('provenance') != position['provenance'] or event.get('ts') != position['opened_at']):
            raise ValueError('Experimental entry binding mismatch')
        # Match the engine's persisted policy representation, independently of
        # a quote-accounting/report caller's Decimal context.
        with localcontext() as context:
            context.prec = 28
            expected = experimental_scores(event, mode=PAPER_EXPERIMENTAL, policy_version=version,token_profile_version=selected_token_profile(cfg))
        if canonical(position.get('entry_policy')) != canonical(expected):
            raise ValueError('Experimental risk metadata changed')
        if position['entry_scores'] != {key:expected[key] for key in ('safety','momentum','flow','entry')}:
            raise ValueError('Experimental entry scores changed')

        count, largest = connection.execute(
            'SELECT COUNT(*),MAX(length(CAST(payload AS BLOB))) FROM outcomes WHERE event_id=?',
            (identity,)).fetchone()
        if not 0 < count <= 64 or largest > 2 * 1024 * 1024:
            raise ValueError('Experimental entry outcomes unavailable')
        outcomes = [json.loads(row[0]) for row in connection.execute(
            'SELECT payload FROM outcomes WHERE event_id=?', (identity,))]
        if any(not isinstance(outcome, dict) for outcome in outcomes):
            raise ValueError('Invalid experimental entry outcome')
        buys = [o for o in outcomes if o.get('type') == 'fill' and o.get('side') == 'buy']
        if len(buys) != 1:
            raise ValueError('Experimental entry buy unavailable or ambiguous')
        buy = buys[0]
        if (buy.get('reason') != 'ENTRY' or buy.get('mint') != mint
                or buy.get('provenance') != position['provenance']
                or canonical(buy.get('entry_policy')) != canonical(expected)
                or buy.get('scores') != position['entry_scores']
                or decimal(buy['quantity']) != decimal(position['initial_qty'])
                or decimal(buy['amount_sol']) + decimal(buy['fee_sol']) != decimal(position['initial_cost'])):
            raise ValueError('Experimental entry buy binding mismatch')


def validate_quote_positions(connection, state):
    """Replay pinned quote entry sources and rebind the original journaled buy.

    Local read-only consistency; EXECUTION_UNVERIFIED is never promoted to
    provider authentication, transaction validity or an actual fill certificate.
    """
    from .model import canonical
    cfg = json.loads(connection.execute("SELECT value FROM metadata WHERE key='config'").fetchone()[0])
    version = cfg.get('paper_quote_execution_version')
    if version is None:
        if any('quote_execution' in p or 'last_quote_execution' in p for p in state['positions'].values()):
            raise ValueError('Quote execution in a default checkpoint')
        return
    from . import quote_execution as quote
    quote.config(cfg)
    if len(state['positions']) > 100:
        raise ValueError('Quote position ceiling')
    for mint, position in state['positions'].items():
        quote.validate_position(mint, position, cfg)
        from .live_observation import SourceRecord, ProviderObservation, ingest_mint, ingest_quote
        last = position.get('last_quote_execution')
        if type(last) is not dict or last.get('direction') != 'sell':
            raise ValueError('Missing last quote record')
        ms = SourceRecord(last['mint_source_id'],last['mint_observed_at'],last['mint_hash'],last['original_mint_json'])
        qs = SourceRecord(last['quote_source_id'],last['quote_observed_at'],last['quote_hash'],last['original_quote_json'])
        at = max(ms.observed_at,qs.observed_at,position['opened_at'])
        if at > state['last_ts']:
            raise ValueError('Future saved quote record')
        token = ingest_mint(lambda: ProviderObservation(ms.source_id,ms.observed_at,quote._original(ms)),
            mint=mint,now=at,max_age_seconds=cfg['price_ttl_seconds'],token_profile_version=quote.selected(cfg))
        observation = ingest_quote(lambda: ProviderObservation(qs.source_id,qs.observed_at,quote._original(qs)),
            mint=token,direction='sell',amount_raw=last['input_raw'],taker=position['taker'],
            expected_pool=position['pool'],now=at,max_age_seconds=cfg['price_ttl_seconds'])
        if quote.selected(cfg)==2:
            saved={'kind':'market','ts':at,'mint':mint,'pool':position['pool'],'taker':position['taker'],
                   'paper_pool_evidence':last['paper_pool_evidence']}
            last_book=quote._book(saved,(observation,),cfg)
        else:last_book=quote._Book('',(observation,),token.decimals)
        if (token.decimals != position['quote_execution']['mint_decimals']
                or canonical(last_book.record(observation,cfg)) != canonical(last)):
            raise ValueError('Last quote source binding changed')
        count, largest, total = connection.execute('''SELECT COUNT(*),
            MAX(length(CAST(o.payload AS BLOB))),SUM(length(CAST(o.payload AS BLOB)))
            FROM outcomes o JOIN events e ON e.event_id=o.event_id WHERE e.ts=?''',
            (position['opened_at'],)).fetchone()
        if not 0 < count <= 64 or largest > 2*1024*1024 or total > 2*1024*1024:
            raise ValueError('Quote entry outcomes unavailable')
        rows = [(identity, json.loads(payload)) for identity, payload in connection.execute(
            'SELECT o.event_id,o.payload FROM outcomes o JOIN events e ON e.event_id=o.event_id WHERE e.ts=?',
            (position['opened_at'],))]
        if any(type(outcome) is not dict for _, outcome in rows):
            raise ValueError('Invalid quote entry outcome')
        buys = [(identity, o) for identity, o in rows if o.get('type') == 'fill'
                and o.get('side') == 'buy' and o.get('mint') == mint]
        if len(buys) != 1:
            raise ValueError('Quote entry buy unavailable or ambiguous')
        identity, buy = buys[0]
        if 'entry_event_id' in position and position['entry_event_id'] != identity:
            raise ValueError('Quote entry identity changed')
        size = connection.execute('SELECT length(CAST(payload AS BLOB)) FROM events WHERE event_id=?', (identity,)).fetchone()
        if size is None or not 0 < size[0] <= 2*1024*1024:
            raise ValueError('Quote entry event unavailable')
        payload, key = connection.execute('SELECT payload,payload_hash FROM events WHERE event_id=?', (identity,)).fetchone()
        event = json.loads(payload)
        if (digest(event) != key or event.get('event_id') != identity
                or event.get('ts') != position['opened_at'] or event.get('kind') != 'market'
                or event.get('mint') != mint or event.get('pool') != position['pool']
                or event.get('provenance') != position['provenance'] or event.get('taker') != position['taker']):
            raise ValueError('Quote entry event binding changed')
        if (buy.get('reason') != 'ENTRY' or buy.get('provenance') != position['provenance']
                or buy.get('simulation') != 'quote_minimum_with_adverse_slippage'
                or buy.get('execution_status') != quote.STATUS
                or canonical(buy.get('quote_execution')) != canonical(position['quote_execution'])
                or decimal(buy['quantity']) != decimal(position['initial_qty'])
                or decimal(buy['amount_sol']) + decimal(buy['fee_sol']) != decimal(position['initial_cost'])
                or decimal(buy['fee_sol']) != decimal(cfg['fixed_fee_sol'])):
            raise ValueError('Quote entry buy binding changed')
    _validate_quote_journal(connection, state, cfg, quote)


def _validate_quote_journal(connection, state, cfg, quote):
    """Independently derive inventory/basis from original buys and net sells.

    Raw-zero closes inventory. The 1e-20 monetary tolerance accommodates the
    engine's persisted Decimal arithmetic, never token quantity mismatches.
    """
    from .model import canonical
    from .live_observation import SourceRecord, ProviderObservation, ingest_mint, ingest_quote
    count, largest, total = connection.execute('''SELECT COUNT(*),
        MAX(length(CAST(payload AS BLOB))),COALESCE(SUM(length(CAST(payload AS BLOB))),0)
        FROM outcomes''').fetchone()
    if count > 10000 or (largest is not None and largest > 2*1024*1024) or total > 16*1024*1024:
        raise ValueError('Quote journal ceiling')
    # Preflight event sizes before any joined body is materialized.
    event_count, largest, total = connection.execute('SELECT COUNT(*),MAX(length(CAST(payload AS BLOB))),COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM events').fetchone()
    if event_count > 10000 or total > 16*1024*1024 or (largest is not None and largest > 2*1024*1024):
        raise ValueError('Quote event ceiling')
    inventory = {}; cash = decimal(cfg['initial_equity_sol']); realized = decimal('0')
    tolerance = decimal('1e-20')
    with localcontext() as context:
        context.prec = 512
        for payload, event_payload, key, identity, timestamp in connection.execute('''SELECT o.payload,e.payload,e.payload_hash,e.event_id,e.ts
                FROM outcomes o LEFT JOIN events e ON e.event_id=o.event_id ORDER BY o.seq'''):
            outcome = json.loads(payload)
            if type(outcome) is not dict or event_payload is None:
                raise ValueError('Invalid quote journal outcome')
            if outcome.get('type') != 'fill':
                continue
            event = json.loads(event_payload); record = outcome.get('quote_execution')
            if (type(event) is not dict or digest(event) != key or event.get('event_id') != identity
                    or event.get('ts') != timestamp or event.get('kind') not in ('market', 'quote_exit')
                    or outcome.get('provenance') != event.get('provenance')
                    or outcome.get('mint') != event.get('mint') or type(record) is not dict
                    or outcome.get('execution_status') != quote.STATUS
                    or outcome.get('simulation') != 'quote_minimum_with_adverse_slippage'):
                raise ValueError('Quote fill identity changed')
            mint = event['mint']; side = outcome['side']
            if (side not in ('buy', 'sell') or record.get('direction') != side
                    or (side == 'buy' and event['kind'] != 'market')):
                raise ValueError('Quote fill direction changed')
            ms = SourceRecord(record['mint_source_id'], record['mint_observed_at'],
                              record['mint_hash'], record['original_mint_json'])
            qs = SourceRecord(record['quote_source_id'], record['quote_observed_at'],
                              record['quote_hash'], record['original_quote_json'])
            token = ingest_mint(lambda: ProviderObservation(ms.source_id, ms.observed_at,
                quote._original(ms)), mint=mint, now=event['ts'], max_age_seconds=cfg['price_ttl_seconds'],
                token_profile_version=quote.selected(cfg))
            observation = ingest_quote(lambda: ProviderObservation(qs.source_id, qs.observed_at,
                quote._original(qs)), mint=token, direction=side, amount_raw=record['input_raw'],
                taker=event['taker'], expected_pool=event['pool'], now=event['ts'],
                max_age_seconds=cfg['price_ttl_seconds'])
            if canonical((quote._book(event,(observation,),cfg) if quote.selected(cfg)==2 else quote._Book('', (observation,), token.decimals)).record(observation,cfg)) != canonical(record):
                raise ValueError('Quote fill source binding changed')
            if event['kind'] == 'quote_exit':
                from .model import validate_event
                validate_event(event,token_profile_version=selected_token_profile(cfg))  # exact strict exit grammar, never entry profile
                quote._book(event, (observation,), cfg)
                quote.validate_exit_valuation(event, outcome, cfg)
                trade = inventory.get(mint)
                if (trade is None or event['current_quantity_raw'] != trade['raw']
                        or event['mint_decimals'] != trade['decimals']
                        or token.slot != event['source_evidence']['mint_slot']
                        or token.source.observed_at != event['source_evidence']['mint_at']):
                    raise ValueError('Quote exit checkpoint/source binding changed')
            raw = quote.raw_quantity(outcome['quantity'], token.decimals)
            fee = decimal(outcome['fee_sol'])
            if fee != decimal(cfg['fixed_fee_sol']):
                raise ValueError('Quote fee changed')
            if side == 'buy':
                if quote.selected(cfg)==2:quote.validate_entry_roundtrip(event,outcome,cfg)
                if (mint in inventory or raw != quote.output_raw(observation,cfg)
                        or decimal(outcome['amount_sol']) != observation.input_units):
                    raise ValueError('Quote buy quantity/debit changed')
                cost = observation.input_units + fee; cash -= cost
                inventory[mint] = {'raw':raw,'initial_raw':raw,'basis':cost,'initial_cost':cost,
                    'pnl':decimal('0'),'decimals':token.decimals,'entry':record,
                    'pool':event['pool'],'taker':event['taker'],'provenance':event['provenance'],
                    'policy':outcome.get('entry_policy')}
            else:
                trade = inventory.get(mint)
                if (trade is None or token.decimals != trade['decimals']
                        or any(event[field] != trade[field] for field in ('pool','taker','provenance'))
                        or canonical(outcome.get('entry_policy')) != canonical(trade['policy'])
                        or raw > trade['raw']
                        or raw != observation.input_raw):
                    raise ValueError('Quote sell inventory changed')
                proceeds = max(decimal('0'),quote.units(quote.output_raw(observation,cfg),9)-fee)
                if decimal(outcome['proceeds_sol']) != proceeds:
                    raise ValueError('Quote sell proceeds changed')
                disposed = trade['basis'] * raw / trade['raw']; pnl = proceeds - disposed
                if abs(decimal(outcome['realized_pnl_sol'])-pnl) > tolerance:
                    raise ValueError('Quote journal cost basis changed')
                trade['basis'] -= disposed; trade['pnl'] += pnl; trade['raw'] -= raw
                cash += proceeds; realized += pnl
                if trade['raw'] == 0:
                    del inventory[mint]
        if set(inventory) != set(state['positions']):
            raise ValueError('Quote checkpoint inventory changed')
        for mint, trade in inventory.items():
            position = state['positions'][mint]
            if (quote.raw_quantity(position['qty'],trade['decimals']) != trade['raw']
                    or quote.raw_quantity(position['initial_qty'],trade['decimals']) != trade['initial_raw']
                    or canonical(position['quote_execution']) != canonical(trade['entry'])
                    or any(abs(decimal(position[field])-trade[value]) > tolerance for field,value in
                        (('cost_left','basis'),('initial_cost','initial_cost'),('trade_pnl','pnl')))):
                raise ValueError('Quote checkpoint cost basis changed')
        if (abs(decimal(state['cash'])-cash)>tolerance
                or abs(decimal(state['realized_pnl'])-realized)>tolerance):
            raise ValueError('Quote checkpoint cash/PnL changed')
