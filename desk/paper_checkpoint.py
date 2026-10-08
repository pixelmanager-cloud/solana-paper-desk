"""Read-only consumer checks; never repair, initialize or replace ledger records."""
import json
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
        return state
    except (ValueError, TypeError, KeyError) as exc:
        raise RecoveryRequired('CHECKPOINT_INVALID') from exc



def read_checkpoint(connection):
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
        if events or outcomes or metadata or sequence:
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
    return validate_checkpoint(connection, row[0])
