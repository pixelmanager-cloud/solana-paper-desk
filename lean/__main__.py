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
    'lanes': None,                     # {"main","exit","low"} shares of each provider rate (+ "low_shed_s", LINT4 "helius_rps"); None = 0.55/0.25/0.20 of 10/s
    'paths': None,                     # lean.paths settings ({"enabled", "db", "path_hours", ...}); None = disabled
    # --- end L07R ---
    'route_check': {},                 # L16: {"enabled": true} records whether a real bot could build each BUY / full exit
    'execution': None,                 # L10: null = instant fills (today); an object turns on latency-aware fills + the cost model
    'held_risk': {},                   # lean.held_risk settings (L11): rug / unsellable handling of held positions
    'sources': [],                     # L14: extra candidate sources [{name, type, discovery_db}]; [] = pump discovery only
    'watchlist': {},                   # L14: {enabled (false), watch_interval_s 300, watch_hours 2, watch_max_per_pass 5, watch_max_rescreens_per_hour 120}
    'ops': None,                       # L15: credit tracker / watchdog / regime log (see lean.ops); absent = off
    # --- L12 ---
    'features': None,                  # lean.features settings ({"enabled", "enrich", "queue_size"}); None = disabled
    # --- end L12 ---
    # --- L13 ---
    'wallet_signals': None,            # lean.wallet_signals settings (an object, may be {}, turns it on); None = off
    # --- end L13 ---
    # --- L20 ---
    'sell_evidence': None,             # lean.sell_evidence settings ({"enabled", "min_organic_sells", ...}); None / absent = off. Needs execution.
    'flow': None,                      # lean.flow settings ({"enabled", "window_s", ...}); None / absent = off
    # --- end L20 ---
    # --- L22 ---
    'honeypot_sim': {},                # {enabled (false), max_per_hour 30, full_size true}: sell-simulation entry filter (main lane)
    # --- end L22 ---
    # --- L23 ---
    'bot_exit': None,                  # lean.bot_exit settings ({"enabled", "mode": "shadow"|"live", ...}); None/absent = off
    # --- end L23 ---
    # --- L24 ---
    'clusters': None,                  # lean.clusters settings (an object, may be {}, turns it on); None = off
    # --- end L24 ---
    # --- L21 ---
    'risk_external': None,             # lean.risk_external settings ({"enabled", ...}): RugCheck / creator / copycat rows; None = off
    # --- end L21 ---
    # --- M1 ---
    'marks': None,                     # #7 lean.marks: {"model": "vault"|"effective", "fee_source": "dynamic"|"config", "schedule_ttl_s"}; None = vault
    'raw_policy': 'all',               # #12 "all" (every provider body kept) | "errors_only" (bodies of failures only; sha256 + size + meta otherwise)
    'blocked_log_s': 300,              # #13 an unchanged exit BLOCKED decision is written at most once per this many seconds (0 = every pass)
    'exit_backoff_max_s': 60,          # #13 exit re-quotes of a position whose exit keeps failing back off 10 s, 20 s, ... up to this (0 = off)
    'peak_persist_s': 60,              # #13 a peak-only state change is persisted at most this often, or on a >= 1% rise (0 = every change)
    'discovery_stale_s': 600,          # #10 the discovery feed silent longer than this writes a discovery_stale event (and _recovered)
    # --- end M1 ---
    # --- W2b ---
    'prefetch': None,                  # #8 lean.prefetch settings ({"enabled", "kinds", "lead_s", ...}): collectors start during the min-age wait; None = off
    # --- end W2b ---
}
RAW_POLICIES = ('all', 'errors_only')
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
            shares = {k: v for k, v in lanes.items() if k not in ('low_shed_s', 'helius_rps', '_comment')}
            cfg['lanes'] = {'shares': providers.validate_lane_shares(shares), 'low_shed_s': lanes.get('low_shed_s'),
                            'helius_rps': providers.validate_helius_rps(lanes.get('helius_rps'))}          # LINT4
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
    from lean import ops as _ops           # L15: strict ops block (unknown keys refused); None when absent or disabled
    try:
        cfg['ops'] = _ops.load_ops_config(cfg['ops'])
    except _ops.OpsError as error:
        raise ConfigError(str(error)) from None
    # --- L12 ---
    from lean import features
    try:
        cfg['features'] = features.config(cfg['features'])
    except ValueError as error:
        raise ConfigError(str(error)) from None
    # --- end L12 ---
    # --- L13 ---
    if cfg['wallet_signals'] is not None:                                  # strict, validated at load
        from lean import wallet_signals
        try:
            wallet_signals.make_config(cfg['wallet_signals'])
        except wallet_signals.ConfigError as error:
            raise ConfigError(str(error)) from None
    # --- end L13 ---
    # --- L20: strict at load; the sell-evidence probe uses the L10 delay window, so it needs the execution model ---
    from lean import flow, sell_evidence
    try:
        cfg['sell_evidence'] = sell_evidence.config(cfg['sell_evidence'])
        cfg['flow'] = flow.config(cfg['flow'])
    except (sell_evidence.ConfigError, flow.ConfigError) as error:
        raise ConfigError(str(error)) from None
    if cfg['sell_evidence']['enabled'] and cfg['execution'] is None:
        raise ConfigError('sell_evidence needs execution (the probe runs inside the L10 exit delay)')
    # --- end L20 ---
    # --- L22 ---
    from lean import honeypot_sim
    try:
        cfg['honeypot_sim'] = honeypot_sim.config(cfg['honeypot_sim'])
    except honeypot_sim.HoneypotConfigError as error:
        raise ConfigError(str(error)) from None
    # --- end L22 ---
    # --- L23 --- (strict at load: unknown keys, bad rules and bad types fail before the trader starts)
    from lean import bot_exit
    try:
        bot_exit.Config.from_dict(cfg['bot_exit'])
    except bot_exit.BotExitConfigError as error:
        raise ConfigError(str(error)) from None
    # --- end L23 ---
    # --- L24 ---
    if cfg['clusters'] is not None:                                        # strict, validated at load
        from lean import clusters
        try:
            clusters.make_config(cfg['clusters'])
        except clusters.ConfigError as error:
            raise ConfigError(str(error)) from None
    # --- end L24 ---
    # --- L21 ---
    from lean import risk_external
    try:
        cfg['risk_external'] = risk_external.config(cfg['risk_external'])
    except ValueError as error:
        raise ConfigError(str(error)) from None
    # --- end L21 ---
    # --- M1 ---
    from lean import marks
    try:
        cfg['marks'] = marks.config(cfg['marks'])
    except marks.MarkConfigError as error:
        raise ConfigError(str(error)) from None
    if cfg['raw_policy'] not in RAW_POLICIES:
        raise ConfigError('raw_policy must be one of %s' % (RAW_POLICIES,))
    for key, high in (('blocked_log_s', 86400), ('exit_backoff_max_s', 3600), ('peak_persist_s', 3600), ('discovery_stale_s', 86400)):
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= high:
            raise ConfigError('%s must be a number between 0 and %d' % (key, high))
    if cfg['discovery_stale_s'] < 60:
        raise ConfigError('discovery_stale_s must be at least 60')
    # --- end M1 ---
    # --- W2b: strict at load (unknown keys, bad types); absent / null = off ---
    from lean import prefetch
    try:
        cfg['prefetch'] = prefetch.config(cfg['prefetch'])
    except prefetch.ConfigError as error:
        raise ConfigError(str(error)) from None
    # --- end W2b ---
    return cfg


def _strip_comments(value):
    if isinstance(value, dict):
        return {k: _strip_comments(v) for k, v in value.items() if k != '_comment'}
    if isinstance(value, list):
        return [_strip_comments(v) for v in value]
    return value


def lean_config_hash(cfg):
    """M1 #10: sha256 of the WHOLE loaded lean.json (``load_config`` output: every key, defaults filled in, so an added key at
    its default is not a new run; canonical JSON; ``_comment`` keys left out so a comment edit is not one either). The strategy
    file has its own ``StrategyConfig.config_hash``; both go into the start event and health."""
    text = json.dumps(_strip_neutral(_strip_comments(cfg)), sort_keys=True, separators=(',', ':'), ensure_ascii=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _strip_neutral(cfg):
    """W2b: the keys this code added are left out of the hash while they are OFF (``prefetch`` disabled, ``paths.hazard_path_sample`` 0), so a
    production config that does not use them keeps the hash it had before they existed (the learner / report ``--config-hash`` windows do
    not split at the deploy). Turning one on is a new run, as it should be."""
    out = dict(cfg)
    prefetch = out.get('prefetch')
    if prefetch is None or (isinstance(prefetch, dict) and not prefetch.get('enabled')):
        out.pop('prefetch', None)
    paths = out.get('paths')
    if isinstance(paths, dict) and not paths.get('hazard_path_sample'):
        out['paths'] = {k: v for k, v in paths.items() if k != 'hazard_path_sample'}
    return out


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
                            code_version=code_version, strategy_version=strategy_cfg.strategy_version, clock=clock,
                            raw_policy=cfg.get('raw_policy', 'all'))                                     # M1 #12
    transport_kwargs = transport_kwargs or {}
    # --- L07R: lane shares before any client exists ---
    from lean import paths
    lanes = cfg.get('lanes') or {}
    providers.configure_lanes(lanes.get('shares'), low_shed_s=lanes.get('low_shed_s'), helius_rps=lanes.get('helius_rps'))   # LINT4: + rate
    # --- end L07R ---
    # --- L10 hook: the optional execution model (latency-aware fills + cost model) ---
    exec_model = None
    if cfg.get('execution') is not None:
        from lean import execution
        exec_model = execution.Execution(execution.ExecConfig.from_dict(cfg['execution']), sleep=transport_kwargs.get('sleep', time.sleep))
    # --- end L10 ---
    from lean import marks                                                  # M1 #7
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
    # --- L15 hook: credit tracker / watchdog / regime log ---
    main_providers = providers.build_providers(keys, lane='main', **transport_kwargs)
    exit_providers = providers.build_providers(keys, lane='exit', **transport_kwargs)
    ops = None
    from lean import ops as lean_ops
    ops_cfg = cfg.get('ops')
    if ops_cfg is None and lean_ops.watchdog_interval() is not None:
        ops_cfg = lean_ops.watchdog_only_config()      # WatchdogSec= is set: keep pinging even without an ops block
    if ops_cfg is not None:
        ops = lean_ops.Ops(ops_cfg, clock=clock)
        main_providers, exit_providers = ops.meter(main_providers), ops.meter(exit_providers)
        ops.lane_clock = {'clock': transport_kwargs.get('monotonic', time.monotonic), 'sleep': transport_kwargs.get('sleep', time.sleep)}  # LINT1
    # --- end L15 ---
    r = runner.Runner(
        store=the_store, providers=main_providers, ops=ops,
        exit_providers=exit_providers, strategy_cfg=strategy_cfg,
        discovery_db=discovery_db, state_dir=state, code_version=code_version, screen_overrides=cfg['screen'],
        pool_fee_bps=cfg['pool_fee_bps'], clock=clock, scan_limit=cfg['scan_limit'],
        max_retries=cfg['max_candidate_retries'], sol_usd_ttl_s=cfg['sol_usd_ttl_s'], stale_mark_s=cfg['stale_mark_s'],
        unexitable_after_s=cfg['unexitable_after_s'], route_check=cfg['route_check'], execution=exec_model,
        held_risk=cfg['held_risk'], **l14,
        # --- M1 ---
        mark_model=marks.MarkModel(cfg.get('marks'), pool_fee_bps=cfg['pool_fee_bps']),
        blocked_log_s=cfg.get('blocked_log_s', CONFIG_KEYS['blocked_log_s']),
        exit_backoff_max_s=cfg.get('exit_backoff_max_s', CONFIG_KEYS['exit_backoff_max_s']),
        peak_persist_s=cfg.get('peak_persist_s', CONFIG_KEYS['peak_persist_s']),
        discovery_stale_s=cfg.get('discovery_stale_s', CONFIG_KEYS['discovery_stale_s']),
        lean_config_hash=lean_config_hash(cfg))
        # --- end M1 ---
    # --- L07R: the path recorder (low lane, its own paths.sqlite) is only a FACTORY here: Runner.run() builds and
    # resumes it; --once and --clear-halt never open paths.sqlite ---
    paths_cfg = paths.config(cfg.get('paths'))
    if paths_cfg['enabled']:
        r._l07r_build = lambda: paths.build(paths_cfg, state_dir=state, keys=keys, pcfg=r.pcfg, code_version=code_version,
                                            strategy_version=strategy_cfg.strategy_version,
                                            pool_fee_bps=cfg['pool_fee_bps'], clock=clock,
                                            transport_kwargs=transport_kwargs, mark_model=r.mark_model)     # M1 #7
    # --- end L07R ---
    # --- LINT1 hook: L11 probes / account checks on the shared non-blocking low lane, never on the exit lane ---
    if r.held_risk is not None:
        r.held_risk.low = providers.low(keys, **transport_kwargs)
    # --- end LINT1 ---
    # --- L12: the entry feature recorder (low lane) ---
    from lean import features
    r.features = features.build(features.config(cfg.get('features')), store=the_store, keys=keys, code_version=code_version,
                                strategy_version=strategy_cfg.strategy_version, clock=clock, transport_kwargs=transport_kwargs)
    # --- end L12 ---
    # --- L13: the wallet-signal collector (low lane) ---
    if cfg.get('wallet_signals') is not None:
        from lean import wallet_signals
        r.wallet_signals = wallet_signals.build(wallet_signals.make_config(cfg['wallet_signals']), store=the_store, keys=keys,
                                                code_version=code_version, strategy_version=strategy_cfg.strategy_version, clock=clock,
                                                transport_kwargs=transport_kwargs,
                                                paths_db=paths.db_path(paths_cfg, state) if paths_cfg['enabled'] else None)   # LINT2
    # --- end L13 ---
    # --- L20: sellability evidence (inside the L10 exit delay) and the order-flow collector, both on the shared low lane ---
    from lean import flow, sell_evidence
    se_cfg = sell_evidence.config(cfg.get('sell_evidence'))
    if se_cfg['enabled']:
        if exec_model is None:
            raise ConfigError('sell_evidence needs execution (the probe runs inside the L10 exit delay)')
        exec_model.sell_evidence = sell_evidence.build(se_cfg, store=the_store, keys=keys, clock=clock, transport_kwargs=transport_kwargs)
        exec_model.sell_evidence.stop = r.stop
    r.flow = flow.build(flow.config(cfg.get('flow')), store=the_store, keys=keys, code_version=code_version,
                        strategy_version=strategy_cfg.strategy_version, clock=clock, transport_kwargs=transport_kwargs)
    # --- end L20 ---
    # --- L22: the honeypot sell-simulation entry filter (MAIN lane, before the entry quotes; None = off) ---
    from lean import honeypot_sim
    r.honeypot = honeypot_sim.build(cfg.get('honeypot_sim'), the_store, clock=clock)
    # --- end L22 ---
    # --- L23: the bot-exit monitor (low lane, own thread in run mode; None when absent/disabled) ---
    from lean import bot_exit
    r.bot_exit = bot_exit.build(cfg.get('bot_exit'), store=the_store, keys=keys, code_version=code_version,
                                strategy_version=strategy_cfg.strategy_version, clock=clock, transport_kwargs=transport_kwargs)
    # --- end L23 ---
    # --- L24: the linked-wallet cluster collector (low lane, its own thread; recording only) ---
    if cfg.get('clusters') is not None:
        from lean import clusters
        r.clusters = clusters.build(clusters.make_config(cfg['clusters']), store=the_store, keys=keys, code_version=code_version,
                                    strategy_version=strategy_cfg.strategy_version, clock=clock, transport_kwargs=transport_kwargs,
                                    signals_enabled=cfg.get('wallet_signals') is not None)
    # --- end L24 ---
    # --- L21: external risk rows (RugCheck, its own bucket; recording only) ---
    from lean import risk_external
    r.risk_external = risk_external.build(risk_external.config(cfg.get('risk_external')), store=the_store, code_version=code_version,
                                          strategy_version=strategy_cfg.strategy_version, clock=clock, transport_kwargs=transport_kwargs)
    # --- end L21 ---
    # --- W2b #8: the prefetcher (low lane; the collectors above start their work for a candidate during the min-age wait) ---
    from lean import prefetch
    r.prefetch = prefetch.build(prefetch.config(cfg.get('prefetch')), store=the_store, keys=keys, discovery_db=discovery_db,
                                screen_cfg=r.screen_cfg, min_age_s=r.screen_cfg['min_age_seconds'], code_version=code_version,
                                strategy_version=strategy_cfg.strategy_version, clock=clock, transport_kwargs=transport_kwargs)
    if r.prefetch is not None:
        r.prefetch.attach(features=r.features, risk_external=r.risk_external, wallet_signals=r.wallet_signals, flow=r.flow)
    # --- end W2b ---
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
        r.flush_pending_states()                       # M1 #13
        r.write_health()
        return 0 if r.halted is None else 3
    # A halt does NOT end the process (exits keep running); only a dead loop does, non-zero, so systemd restarts it.
    clean = r.run(candidate_interval=float(cfg['candidate_interval_s']), position_interval=min(10.0, float(cfg['position_interval_s'])))
    return 0 if clean else 4


if __name__ == '__main__':
    sys.exit(main())
