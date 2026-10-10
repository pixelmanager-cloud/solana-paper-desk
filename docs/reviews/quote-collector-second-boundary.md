# Quote acquisition versus collector completion

CI run38048536983 at PR20623e0e9d failed one of2930 tests after7 fresh requests.
An isolated original scenario passed23.868s, but a bounded native0.756s delay
after quote acquisition reproduced `ValueError('quote envelope binding')`:
original quote time1791633449, collector completion1791633450, decision1791633453.
The quote was fresh; equality of acquisition and completion was incorrect.

New transport quote payloads retain the exact successful attempt hash. Shared
entry/exit replay checks its hash, source, scan, method, parameters, HTTP200,
absence of failure, original acquisition time, bounded exact query bytes and
parsed original response. Duplicate/noncanonical query encoding is refused.
Only this bound new payload may have later collector completion. Both original
timestamps remain unchanged, with start <= acquisition <= completion <= decision,
10-second acquisition freshness and bounded collection elapsed time. Legacy
payloads retain equality semantics. Budgets, pacing and deadlines are unchanged.

Focused entry/exit/negative replay:25 PASS/3.550s. New native-clock composition
fixture:1 PASS/21.903s, crossing a real second boundary during both entry and held
exit persistence, then verifying accounting, restart and retained runtime rows.
Controls reject stale/future times, completion before acquisition, acquisition
before start, missing/forged attempts and hash-valid wrong source/scan/status/wire.
No provider calls, source authentication, actual execution or readiness claim.
Full combined Linux/CI and independent review remain required. PR207 is unchanged.
