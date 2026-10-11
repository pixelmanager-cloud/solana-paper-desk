"""python -m lean --config lean.json --state-dir DIR --discovery-db DB [--keys-file KEYS.json] [--once] [--clear-halt]

PAPER ONLY. The keys file defaults to the systemd credential ``$CREDENTIALS_DIRECTORY/provider-keys.json`` and is read
with ``lean.providers.load_keys`` (production names HELIUS_API_KEY / JUPITER_API_KEY). Keys are never printed or logged.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

CONFIG_KEYS = {
    'strategy_config': None,           # path to the versioned StrategyConfig JSON (relative to the lean.json file)
    'initial_cash_sol': None,          # paper cash of a NEW store (an existing store must match it)
    'candidate_interval_s': 5.0,
    'position_interval_s': 10.0,       # capped at 10 s
    'pool_fee_bps': 25,                # PumpSwap pool fee used by the net marks
    'scan_limit': 500,
    'max_candidate_retries': 3,
    'sol_usd_ttl_s': 30,
    'screen': {},                      # lean.candidates settings other than the strategy-owned bands
}
REQUIRED = ('strategy_config', 'initial_cash_sol')


class ConfigError(ValueError):
    pass


def load_config(path):
    """Strict lean.json: unknown keys are refused (a typo must not silently fall back to a default)."""
    path = Path(path)
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ConfigError('config must be an object')
    unknown = set(raw) - set(CONFIG_KEYS) - {'_comment'}
    missing = [k for k in REQUIRED if k not in raw]
    if unknown or missing:
        raise ConfigError('config keys: unknown=%s missing=%s' % (sorted(unknown), missing))
    cfg = {k: raw.get(k, v) for k, v in CONFIG_KEYS.items()}
    strategy_path = Path(cfg['strategy_config'])
    cfg['strategy_config'] = str(strategy_path if strategy_path.is_absolute() else path.parent / strategy_path)
    if not isinstance(cfg['screen'], dict):
        raise ConfigError('screen must be an object')
    return cfg


def _code_version():
    """LEAN_CODE_VERSION (set at install by the unit) or the git sha of this checkout."""
    if os.environ.get('LEAN_CODE_VERSION'):
        return os.environ['LEAN_CODE_VERSION']
    try:
        return subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=5, check=True,
                              cwd=Path(__file__).resolve().parent).stdout.strip() or 'unknown'
    except Exception:
        return 'unknown'


def build_runner(cfg, *, state_dir, discovery_db, keys, code_version, clock=None, transport_kwargs=None):
    """Everything wired: the store (created on first start with ``initial_cash_sol``), the main and exit provider lanes."""
    import time
    from lean import providers, runner, store, strategy
    strategy_cfg = strategy.StrategyConfig.load(cfg['strategy_config'])
    state = os.path.realpath(state_dir)
    os.makedirs(state, mode=0o700, exist_ok=True)
    clock = clock or time.time
    the_store = store.Store(os.path.join(state, 'lean.sqlite'), initial_cash_sol=str(cfg['initial_cash_sol']),
                            code_version=code_version, strategy_version=strategy_cfg.strategy_version, clock=clock)
    transport_kwargs = transport_kwargs or {}
    return runner.Runner(
        store=the_store, providers=providers.build_providers(keys, lane='main', **transport_kwargs),
        exit_providers=providers.build_providers(keys, lane='exit', **transport_kwargs), strategy_cfg=strategy_cfg,
        discovery_db=discovery_db, state_dir=state, code_version=code_version, screen_overrides=cfg['screen'],
        pool_fee_bps=cfg['pool_fee_bps'], clock=clock, scan_limit=cfg['scan_limit'],
        max_retries=cfg['max_candidate_retries'], sol_usd_ttl_s=cfg['sol_usd_ttl_s'])


def main(argv=None):
    p = argparse.ArgumentParser(description='Lean paper trader (paper only: no signing, no broadcasting, no real funds)')
    p.add_argument('--config', required=True)
    p.add_argument('--state-dir', required=True)
    p.add_argument('--discovery-db', required=True)
    p.add_argument('--keys-file', default=None, help='default $CREDENTIALS_DIRECTORY/provider-keys.json; never logged')
    p.add_argument('--once', action='store_true', help='one candidate pass and one position pass, then exit')
    p.add_argument('--clear-halt', action='store_true', help='operator: record that the persisted halt is cleared, then exit')
    a = p.parse_args(argv)
    from lean import providers
    cfg = load_config(a.config)
    keys_file = a.keys_file or os.path.join(os.environ.get('CREDENTIALS_DIRECTORY', ''), 'provider-keys.json')
    try:
        keys = providers.load_keys(keys_file)
    except providers.ProviderError as error:
        print(json.dumps({'status': 'ERROR', 'code': error.code}), file=sys.stderr)
        return 2
    r = build_runner(cfg, state_dir=a.state_dir, discovery_db=a.discovery_db, keys=keys, code_version=_code_version())
    if a.clear_halt:
        r.store.check_invariants()
        r.store.check_position_states()
        r.store.record('halt_cleared', {'previous': r.halted}, code_version=r.code_version, strategy_version=r.strategy_version)
        print(json.dumps({'status': 'HALT_CLEARED', 'previous': r.halted}))
        return 0
    if a.once:
        r.candidate_pass()
        r.position_pass()
        r.write_health()
        return 0 if r.halted is None else 3
    r.run(candidate_interval=float(cfg['candidate_interval_s']), position_interval=min(10.0, float(cfg['position_interval_s'])))
    return 0 if r.halted is None else 3


if __name__ == '__main__':
    sys.exit(main())
