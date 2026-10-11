"""python -m lean --config CFG.json --state-dir DIR --discovery-db DB --keys-file KEYS.json"""
import argparse
import json
import os
import subprocess
import sys


def _code_version():
    if os.environ.get('LEAN_CODE_VERSION'):
        return os.environ['LEAN_CODE_VERSION']
    try:
        return subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except Exception:
        return 'unknown'


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('--state-dir', required=True)
    p.add_argument('--discovery-db', required=True)
    p.add_argument('--keys-file', required=True, help='JSON {helius, jupiter}; a systemd credential, never logged')
    p.add_argument('--kill-switch', default=None, help='default <state-dir>/KILL')
    p.add_argument('--once', action='store_true', help='one candidate pass and one position pass, then exit')
    a = p.parse_args(argv)
    from lean import candidates, paper, providers, strategy, store, runner
    cfg = json.load(open(a.config))
    keys = json.load(open(a.keys_file))
    strategy_cfg = strategy.StrategyConfig.load(cfg['strategy_config']) if hasattr(strategy.StrategyConfig, 'load') else cfg
    state = os.path.realpath(a.state_dir)
    os.makedirs(state, mode=0o700, exist_ok=True)
    r = runner.Runner(
        store=store.Store(os.path.join(state, 'lean.sqlite')), helius=providers.Helius(keys['helius']),
        jupiter=providers.Jupiter(keys['jupiter']), kraken=providers.Kraken(), candidates=candidates, strategy=strategy,
        paper=paper, cfg=cfg, strategy_cfg=strategy_cfg, discovery_db=a.discovery_db,
        health_path=os.path.join(state, 'health.json'), code_version=_code_version(),
        strategy_version=cfg.get('strategy_version', 'default'), kill_switch=a.kill_switch or os.path.join(state, 'KILL'))
    if a.once:
        r.candidate_pass()
        r.position_pass()
        r.write_health()
        return 0
    r.run(candidate_interval=cfg.get('candidate_interval_s', 5.0), position_interval=min(10.0, cfg.get('position_interval_s', 10.0)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
