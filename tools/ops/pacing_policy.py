"""Reviewed cadence upgrade of the shared provider-pacing database (paid Helius / Jupiter plans). Paper only.

    python -m tools.ops.pacing_policy plan  --db DB [--policy-id ID]
    python -m tools.ops.pacing_policy apply --db DB [--policy config/provider-pacing-policy.json] [--policy-id ID] [--execute]

Both are DRY RUNS unless ``apply --execute`` is given. The change is an explicit, reviewed, append-only record in
``pacing_policy_changes`` (old and new cadence/backoff, the reason, the sha256 of the policy file and the digest of the
reviewed entry): the original ``policy`` rows are never edited, the Kraken lane (row, state, migration receipt) is not
touched, and re-applying is a no-op. ``apply --execute`` needs every desk process that uses the shared database stopped
(the same ActiveState check as ``tools.ops.backup``) AND a database with no pending grant and no live waiter.

Mixed versions: every process that opens the shared database must run code that knows the new table BEFORE ``apply``;
older code raises PACING_DATABASE_INVALID on the extra table, that is, it fails closed instead of running at the old
cadence. Nothing here calls a provider, signs, broadcasts or touches any other store.
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from desk import provider_pacing as pacing  # noqa: E402
from tools.ops import backup  # noqa: E402

# Units that open the shared pacing database (entry, held, watcher path trigger, research samplers, dashboard).
DEFAULT_WRITERS = ('desk-paper-entry-dispatcher.service', 'desk-paper-entry-dispatcher.timer',
                   'desk-paper-held-cycle.service', 'desk-paper-held-cycle.timer', 'desk-paper-held-cycle.path',
                   'desk-counterfactual.service', 'desk-counterfactual.timer',
                   'desk-fill-realism-worker.service', 'desk-fill-realism-worker.timer',
                   'desk-dashboard.service')


class PolicyToolError(ValueError):
    pass


def _db(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or '..' in path.parts:
        raise PolicyToolError('--db must be a canonical absolute path')
    return path


def _policy_file(value):
    """The only acceptable file is this release's reviewed one: rows are validated against it forever after."""
    if value is None:
        return
    if Path(value).resolve() != Path(pacing.POLICY_FILE).resolve():
        raise PolicyToolError('--policy must be the release\'s reviewed config/provider-pacing-policy.json')


def unit_states(units, runner):
    states = {}
    for unit in units:
        try:
            states[unit] = runner(unit)
        except (OSError, ValueError, RuntimeError) as exc:
            states[unit] = 'UNKNOWN:' + type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - subprocess errors from the runner
            states[unit] = 'UNKNOWN:' + type(exc).__name__
    return states


def run(args, runner=backup.systemctl_state):
    db = _db(args.db)
    _policy_file(getattr(args, 'policy', None))
    units = tuple(dict.fromkeys(DEFAULT_WRITERS + tuple(args.require_quiesced or ())))
    plan = pacing.policy_plan(db, args.policy_id)
    plan['units'] = unit_states(units, runner)
    plan['all_units_quiet'] = all(state in backup.QUIET_STATES for state in plan['units'].values())
    if args.command == 'plan' or not args.execute:
        plan['status'] = 'PLAN' if args.command == 'plan' else 'DRY_RUN'
        plan['note'] = (None if args.command == 'plan' else 'nothing was changed; add --execute to apply')
        plan['ready_to_apply'] = plan['all_units_quiet'] and plan['quiescent_database'] and bool(plan['changes'])
        return plan
    # --execute: both quiesce proofs are checked again here, immediately before the write
    try:
        backup.check_quiesced(units, runner)
    except (backup.BackupError, OSError) as exc:
        raise PolicyToolError('writers not quiesced: ' + str(exc)[:200]) from None
    result = pacing.apply_policy(db, args.policy_id)
    result['units'] = plan['units']
    return result


def main(argv=None, runner=backup.systemctl_state):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     allow_abbrev=False)
    parser.add_argument('command', choices=('plan', 'apply'))
    parser.add_argument('--db', required=True, help='absolute path of the shared provider-pacing.sqlite')
    parser.add_argument('--policy', help='must be this release\'s config/provider-pacing-policy.json')
    parser.add_argument('--policy-id', help='reviewed entry to apply (default: the newest)')
    parser.add_argument('--execute', action='store_true', help='apply for real (default: dry run)')
    parser.add_argument('--require-quiesced', nargs='*', default=[], metavar='UNIT',
                        help='additional units that must be inactive/failed (added to the built-in writer list)')
    args = parser.parse_args(argv)
    try:
        report = run(args, runner)
    except (PolicyToolError, pacing.PacingError, OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        code = getattr(exc, 'code', None) or type(exc).__name__ + ': ' + str(exc)[:200]
        print(json.dumps({'status': 'REFUSED', 'reason': code, 'paper_only': True}, sort_keys=True))
        return 2
    report['paper_only'] = True
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
