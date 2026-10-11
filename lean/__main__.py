"""python -m lean --config lean.json --state-dir DIR --discovery-db DB [--keys-file KEYS.json] [--once] [--clear-halt]

PAPER ONLY. The keys file defaults to the systemd credential ``$CREDENTIALS_DIRECTORY/provider-keys.json`` and is read
with ``lean.providers.load_keys`` (production names HELIUS_API_KEY / JUPITER_API_KEY). Keys are never printed or logged.
"""
import argparse
import json
import os
import hashlib
import logging
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
    'stale_mark_s': 30,                # a mark older than this is not used; the position is quote-marked instead (3x interval)
    'unexitable_after_s': 7200,        # a wanted exit failing (no route / 4xx) this long is written off at zero proceeds
    'screen': {},                      # lean.candidates settings other than the strategy-owned bands
    # --- L07R ---
    'lanes': None,                     # {"main","exit","low"} shares of each provider rate (+ "low_shed_s"); None = 0.55/0.25/0.20
    'paths': None,                     # lean.paths settings ({"enabled", "path_hours", "interval_s", ...}); None = disabled
    # --- end L07R ---
    'route_check': {},                 # L16: {"enabled": true} records whether a real bot could build each BUY / full exit
    'execution': None,                 # L10: null = instant fills (today); an object turns on latency-aware fills + the cost model
    'held_risk': {},                   # lean.held_risk settings (L11): rug / unsellable handling of held positions
    'sources': [],                     # L14: extra candidate sources [{name, type, discovery_db}]; [] = pump discovery only
    'watchlist': {},                   # L14: {enabled (false), watch_interval_s 300, watch_hours 2, watch_max_per_pass 5, watch_max_rescreens_per_hour 120}
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
    # --- L07R ---
    from lean import paths, providers
    try:
        cfg['paths'] = paths.config(cfg['paths'])
        lanes = cfg['lanes']
        if lanes is not None:
            if not isinstance(lanes, dict):
                raise ValueError('lanes must be an object')
            shares = {k: v for k, v in lanes.items() if k not in ('low_shed_s', '_comment')}
            cfg['lanes'] = {'shares': providers.validate_lane_shares(shares), 'low_shed_s': lanes.get('low_shed_s')}
    except ValueError as error:
        raise ConfigError(str(error)) from None
    # --- end L07R ---
    rc = cfg['route_check']                                                 # --- L16 hook ---
    if not isinstance(rc, dict) or set(rc) - {'enabled'} or not isinstance(rc.get('enabled', False), bool):
        raise ConfigError('route_check must be an object like {"enabled": true}')
    # --- end L16 ---
    # --- L10 hook ---
    if cfg['execution'] is not None and not isinstance(cfg['execution'], dict):
        raise ConfigError('execution must be an object or null')
    # --- end L10 ---
    if not isinstance(cfg['held_risk'], dict):
        raise ConfigError('held_risk must be an object')
    from lean import held_risk                                              # L11: strict at load (unknown keys, bad types)
    try:
        held_risk.Config.from_dict(cfg['held_risk'])
    except held_risk.HeldRiskConfigError as error:
        raise ConfigError(str(error)) from None
    # --- L14 hook: validated at load, so a typo fails before the trader starts ---
    from lean import candidates, sources, watchlist
    try:
        sources.build_sources(cfg['sources'])
        watchlist.check_window(watchlist.WatchConfig.from_dict(cfg['watchlist']),
                               cfg['screen'].get('max_age_seconds', candidates.DEFAULTS['max_age_seconds']))
    except (sources.SourceConfigError, watchlist.WatchConfigError, TypeError) as error:
        raise ConfigError(str(error)) from None
    # --- end L14 ---
    return cfg


def code_version():
    """LEAN_CODE_VERSION (set at install by the unit), else ``h:`` + sha256 over the sorted ``lean/*.py`` files (name and
    contents). Never 'unknown' and no version-control dependency: the deployed tree is a root-owned copy."""
    value = os.environ.get('LEAN_CODE_VERSION', '').strip()
    if value:
        return value
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).resolve().parent.glob('*.py')):
        digest.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    return 'h:' + digest.hexdigest()


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
    # --- L07R: lane shares before any client exists ---
    from lean import paths
    lanes = cfg.get('lanes') or {}
    providers.configure_lanes(lanes.get('shares'), low_shed_s=lanes.get('low_shed_s'))
    # --- end L07R ---
    # --- L10 hook: the optional execution model (latency-aware fills + cost model) ---
    exec_model = None
    if cfg.get('execution') is not None:
        from lean import execution
        exec_model = execution.Execution(execution.ExecConfig.from_dict(cfg['execution']), sleep=transport_kwargs.get('sleep', time.sleep))
    # --- end L10 ---
    # --- L14 hook ---
    from lean import adapters, sources, watchlist
    screen_cfg = adapters.screen_config(strategy_cfg, cfg['screen'])
    extra = sources.build_sources(cfg['sources'], screen_cfg=screen_cfg, limit=cfg['scan_limit'], clock=clock)
    watch_cfg = watchlist.WatchConfig.from_dict(cfg['watchlist'])
    l14 = {'extra_sources': sources.SourceSet(extra, state) if extra else None,
           'watchlist': (watchlist.Watchlist(the_store, watch_cfg, clock=clock, code_version=code_version,
                                             strategy_version=strategy_cfg.strategy_version,
                                             max_age_seconds=screen_cfg['max_age_seconds']) if watch_cfg.enabled else None)}
    # --- end L14 ---
    r = runner.Runner(
        store=the_store, providers=providers.build_providers(keys, lane='main', **transport_kwargs),
        exit_providers=providers.build_providers(keys, lane='exit', **transport_kwargs), strategy_cfg=strategy_cfg,
        discovery_db=discovery_db, state_dir=state, code_version=code_version, screen_overrides=cfg['screen'],
        pool_fee_bps=cfg['pool_fee_bps'], clock=clock, scan_limit=cfg['scan_limit'],
        max_retries=cfg['max_candidate_retries'], sol_usd_ttl_s=cfg['sol_usd_ttl_s'], stale_mark_s=cfg['stale_mark_s'],
        unexitable_after_s=cfg['unexitable_after_s'], route_check=cfg['route_check'], execution=exec_model,
        held_risk=cfg['held_risk'], **l14)
    # --- L07R: the path recorder (low lane), resumed from the store before any loop runs ---
    r.paths = paths.build(paths.config(cfg.get('paths')), store=the_store, keys=keys, pcfg=r.pcfg,
                          code_version=code_version, strategy_version=strategy_cfg.strategy_version,
                          pool_fee_bps=cfg['pool_fee_bps'], clock=clock, transport_kwargs=transport_kwargs)
    if r.paths is not None:
        r.paths.resume()
    # --- end L07R ---
    # --- LINT1 hook: L11 probes / account checks on the shared non-blocking low lane, never on the exit lane ---
    if r.held_risk is not None:
        r.held_risk.low = providers.low(keys, **transport_kwargs)
    # --- end LINT1 ---
    return r


def main(argv=None):
    p = argparse.ArgumentParser(description='Lean paper trader (paper only: no signing, no broadcasting, no real funds)')
    p.add_argument('--config', required=True)
    p.add_argument('--state-dir', required=True)
    p.add_argument('--discovery-db', required=True)
    p.add_argument('--keys-file', default=None, help='default $CREDENTIALS_DIRECTORY/provider-keys.json; never logged')
    p.add_argument('--once', action='store_true', help='one candidate pass and one position pass, then exit')
    p.add_argument('--clear-halt', action='store_true', help='operator: record that the persisted halt is cleared, then exit')
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    from lean import providers
    cfg = load_config(a.config)
    keys_file = a.keys_file or os.path.join(os.environ.get('CREDENTIALS_DIRECTORY', ''), 'provider-keys.json')
    try:
        keys = providers.load_keys(keys_file)
    except providers.ProviderError as error:
        print(json.dumps({'status': 'ERROR', 'code': error.code}), file=sys.stderr)
        return 2
    r = build_runner(cfg, state_dir=a.state_dir, discovery_db=a.discovery_db, keys=keys, code_version=code_version())
    if a.clear_halt:
        r.store.check_invariants()
        r.store.check_position_states()
        r.store.record('halt_cleared', {'previous': r.halted}, code_version=r.code_version, strategy_version=r.strategy_version)
        print(json.dumps({'status': 'HALT_CLEARED', 'previous': r.halted}))
        return 0
    if a.once:
        r.position_pass()
        r.candidate_pass()
        r.write_health()
        return 0 if r.halted is None else 3
    # A halt does NOT end the process (exits keep running); only a dead loop does, non-zero, so systemd restarts it.
    clean = r.run(candidate_interval=float(cfg['candidate_interval_s']), position_interval=min(10.0, float(cfg['position_interval_s'])))
    return 0 if clean else 4


if __name__ == '__main__':
    sys.exit(main())
