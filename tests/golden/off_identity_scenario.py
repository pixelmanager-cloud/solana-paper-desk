"""Self-contained scenario (desk.engine + desk.ledger + tests.helpers only) so it runs unchanged on an older tree.

Three positions opened on the previous day, held legs across UTC midnight, then a fourth entry. Plain config: no
concurrency, marks or rollover flag. Used to prove the flag-absent byte identity of the engine (T35F item 8).
"""
import json
import tempfile
from pathlib import Path

from desk.engine import initial_state, transition
from desk.ledger import Ledger
from desk.model import canonical, digest
from tests.helpers import T, config, event


def run():
    cfg = {**config(), 'max_positions': 4}
    with tempfile.TemporaryDirectory() as folder:
        ledger = Ledger(Path(folder) / 'l.sqlite')
        try:
            apply = lambda e: ledger.apply(e, cfg, transition, initial_state)
            ev = lambda ts, mint: event(ts, mint=mint, graduated_at=ts - 600)
            outcomes = []
            base = T - 600
            for i, mint in enumerate('ABC'):
                for held in 'ABC'[:i]:
                    outcomes += apply(ev(base + 60 * i - 1, held))
                outcomes += apply(ev(base + 60 * i, mint))
            for i, mint in enumerate('ABC'):
                outcomes += apply(ev(T + 10 + 8 * i, mint))
            outcomes += apply(ev(T + 57, 'D'))
            state = json.loads(ledger.db.execute('SELECT payload FROM state').fetchone()[0])
        finally:
            ledger.close()
    return {'state_digest': digest(state), 'outcomes_digest': digest(outcomes), 'outcome_count': len(outcomes),
            'day': state['day'], 'positions': sorted(state['positions']), 'day_start_equity': state['day_start_equity'],
            'cash': state['cash'], 'extra_position_keys': sorted({k for p in state['positions'].values() for k in p
                                                                   if k.startswith('portfolio_mark')})}


if __name__ == '__main__':
    print(canonical(run()))
