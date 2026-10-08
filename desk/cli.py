import argparse
import asyncio
import json
import importlib.util
import os
import stat
import sys
from pathlib import Path

from .decode import decode_capture
from .bundles import audit
from .engine import initial_state, transition
from .ledger import Ledger
from .model import load_config
from .providers import PUMP, PUMPSWAP, SOL, backfill, inspect_mint, jupiter_probe, record_stream


def write_json(value, path=None):
    text = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text)
    else:
        print(text, end="")


def main():
    parser = argparse.ArgumentParser(description="Solana desk v0.1 - paper research and read-only data")
    parser.add_argument("--secrets-file", help="owner-only JSON API keys; never a wallet key")
    commands = parser.add_subparsers(dest="command", required=True)
    simulation = commands.add_parser("simulate-sell", help="Unsigned public-holder sell diagnostic")
    simulation.add_argument("--mint", required=True)
    simulation.add_argument("--wallet", required=True)
    simulation.add_argument("--holding", required=True)
    simulation.add_argument("--amount", type=int, required=True)
    simulation.add_argument("--output")
    saved_sell=commands.add_parser("replay-sell",help="Recompute saved sell diagnostics offline; never fresh trading evidence")
    saved_sell.add_argument("--evidence-db",required=True)
    saved_sell.add_argument("--hash",required=True)
    saved_sell.add_argument("--output")
    web = commands.add_parser("serve", help="Loopback-only research dashboard")
    web.add_argument("--db", required=True)
    web.add_argument("--port", type=int, default=8765)
    scanner = commands.add_parser("screen", help="Bounded live token investigation")
    scanner.add_argument("--mint", required=True)
    scanner.add_argument("--output")
    decoder = commands.add_parser("decode-capture", help="Decode recorded observations; never authorizes trading")
    decoder.add_argument("--db", required=True)
    decoder.add_argument("--output", required=True)
    decoder.add_argument("--limit", type=int, default=10000)
    commands.add_parser("doctor", help="Local readiness only; no network calls or secret values")
    mint = commands.add_parser("inspect-mint", help="Read on-chain mint and apply conservative SPL-only policy")
    mint.add_argument("--mint", required=True)
    mint.add_argument("--output")
    replay = commands.add_parser("replay", help="Process ordered normalized JSONL snapshots, restart-safe")
    replay.add_argument("--input", required=True)
    replay.add_argument("--config", default="config/paper.json")
    replay.add_argument("--db", required=True)
    replay.add_argument("--report")
    monitor = commands.add_parser("paper-monitor", help="Expire stale marks in an existing paper ledger; no quotes, entries or fills")
    monitor.add_argument("--db", required=True)
    monitor.add_argument("--config", default="config/paper.json")
    consume=commands.add_parser("consume-scans",help="Persist reject-only paper candidate decisions from completed research")
    consume.add_argument("--db",required=True)
    consume.add_argument("--journal",required=True)
    consume.add_argument("--evidence-db")
    ownership=commands.add_parser("ownership-advance",help="Resume saved ownership history within the original scan RPC budget")
    ownership.add_argument("--db",required=True)
    ownership.add_argument("--evidence-db",required=True)
    ownership.add_argument("--scan-id",required=True)
    report = commands.add_parser("report")
    report.add_argument("--db", required=True)
    report.add_argument("--output")
    screening = commands.add_parser("audit-bundles", help="Screen normalized evidence; not raw RPC data")
    screening.add_argument("--input", required=True)
    screening.add_argument("--as-of", required=True, type=int)
    screening.add_argument("--output")
    record = commands.add_parser("record", help="Bounded, read-only Helius capture")
    record.add_argument("--db", required=True)
    record.add_argument("--address", action="append", help="Filter to a pool/address; default Pump and PumpSwap programs")
    record.add_argument("--seconds", type=int, default=60)
    record.add_argument("--max-records", type=int, default=1000)
    record.add_argument("--max-bytes", type=int, default=20_000_000)
    history = commands.add_parser("backfill", help="Bounded finalized address history; resumes its cursor")
    history.add_argument("--db", required=True)
    history.add_argument("--address", required=True)
    history.add_argument("--start", type=int, required=True)
    history.add_argument("--end", type=int, required=True)
    history.add_argument("--max-pages", type=int, default=10)
    probe = commands.add_parser("quote-probe", help="Fetch unsigned Jupiter V2 route; never sends a transaction")
    probe.add_argument("--input-mint", default=SOL)
    probe.add_argument("--output-mint", required=True)
    probe.add_argument("--amount", type=int, required=True, help="integer input-token base units")
    probe.add_argument("--taker", required=True, help="public address only")
    probe.add_argument("--output", required=True)
    args = parser.parse_args()
    ledger = None
    try:
        if args.secrets_file:
            secret_path = Path(args.secrets_file)
            mode = stat.S_IMODE(secret_path.stat().st_mode)
            credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
            managed_credential = bool(credential_dir and secret_path.parent.resolve() == Path(credential_dir).resolve())
            allowed_modes = (0o400, 0o600, 0o440) if managed_credential else (0o400, 0o600)
            if mode not in allowed_modes:
                raise ValueError("secrets file must have owner-only permissions (chmod 600)")
            secrets = json.loads(secret_path.read_text())
            for name in ("HELIUS_API_KEY", "JUPITER_API_KEY"):
                value = secrets.get(name)
                if isinstance(value, str) and value.strip():
                    os.environ[name] = value.strip()
        if args.command == "replay-sell":
            from .evidence import EvidenceStore
            from .replay_sell import replay_sell
            write_json(replay_sell(EvidenceStore(args.evidence_db,read_only=True),args.hash),args.output)
            return
        if args.command == "simulate-sell":
            from .simulate import simulate_sell
            write_json(simulate_sell(args.mint, args.wallet, args.holding, args.amount), args.output)
            return
        if args.command == "serve":
            from .dashboard import serve
            serve(args.db, args.port)
            return
        if args.command == "screen":
            from .screen import screen
            write_json(screen(args.mint), args.output)
            return
        if args.command == "decode-capture":
            write_json(decode_capture(args.db, args.output, args.limit))
            return
        if args.command == "doctor":
            write_json({"python": sys.version.split()[0], "mode": "PAPER_ONLY",
                        "stream_dependency_installed": importlib.util.find_spec("websockets") is not None,
                        "helius_key_configured": bool(os.environ.get("HELIUS_API_KEY")),
                        "jupiter_key_configured": bool(os.environ.get("JUPITER_API_KEY")),
                        "live_signing_available": False, "raw_to_paper_pipeline_complete": False,
                        "notice": "Credentials not validated with provider; no network request performed."})
            return
        if args.command == "inspect-mint":
            write_json(inspect_mint(args.mint), args.output)
            return
        if args.command == "audit-bundles":
            write_json(audit(json.loads(Path(args.input).read_text()), args.as_of), args.output)
            return
        if args.command == "quote-probe":
            write_json(jupiter_probe(args.input_mint, args.output_mint, args.amount, args.taker), args.output)
            return
        if args.command == "paper-monitor":
            from .monitor import tick
            write_json(tick(args.db,load_config(args.config)))
            return
        if args.command == "consume-scans":
            from .decision_runner import consume
            write_json(consume(args.db,args.journal,evidence_db=args.evidence_db))
            return
        if args.command == "ownership-advance":
            from .ownership_worker import advance
            from .providers import helius_rpc
            write_json(advance(args.db,args.evidence_db,args.scan_id,helius_rpc))
            return
        ledger = Ledger(args.db)
        if args.command == "replay":
            cfg = load_config(args.config)
            with Path(args.input).open() as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        ledger.apply(json.loads(line), cfg, transition, initial_state)
                    except (ValueError, KeyError, TypeError) as exc:
                        raise ValueError(f"input line {line_number}: {exc}") from None
            write_json(ledger.report(), args.report)
        elif args.command == "report":
            write_json(ledger.report(), args.output)
        elif args.command == "backfill":
            write_json(backfill(ledger, args.address, args.start, args.end, args.max_pages))
        elif args.command == "record":
            write_json(asyncio.run(record_stream(ledger, args.address or [PUMP, PUMPSWAP],
                                                 args.seconds, args.max_records, args.max_bytes)))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("Stopped; committed ledger records are preserved.", file=sys.stderr)
    finally:
        if ledger:
            ledger.close()
