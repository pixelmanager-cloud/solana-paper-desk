"""Generate explicit synthetic scenarios. This is not a historical backtest."""
import json
from pathlib import Path
from tests.helpers import T, bundled_evidence, control, event

base = Path(__file__).parent
events = [event()]
for delta, reserve in [(5, "160"), (10, "240"), (15, "350"), (20, "190")]:
    events.append(event(T + delta, reserve_sol=reserve))
events += [event(T + 60, "SYNTHETIC_BUNDLED", bundle_evidence=bundled_evidence(T + 60)),
           event(T + 65, "SYNTHETIC_UNKNOWN", bundle_evidence=None),
           event(T + 120, "SYNTHETIC_LOSER"),
           event(T + 125, "SYNTHETIC_LOSER", reserve_sol="60"),
           control(T + 180, "PAUSE_ENTRY"), event(T + 181, "SYNTHETIC_PAUSED"),
           control(T + 182, "RESUME"), event(T + 185, "SYNTHETIC_TIME_STOP"),
           event(T + 2900, "SYNTHETIC_TIME_STOP"),
           event(T + 4000, "SYNTHETIC_STALE", holder_at=T)]
(base / "demo.jsonl").write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in events))
(base / "bundled.json").write_text(json.dumps(bundled_evidence(), indent=2) + "\n")
