# AGENTS.md — `simulation/`

Scoped rules for the EnergyPlus boundary. Root `AGENTS.md` still applies; this file
adds what only matters *here*. ⏳ marks files that do not exist yet.

## What lives here

| File | Role |
|---|---|
| `locate.py` | Finds the EnergyPlus install and injects `pyenergyplus` onto `sys.path`. |
| ⏳ `backend.py` | `SimulationBackend` protocol — the seam the fake implements. |
| ⏳ `energyplus.py` | State lifecycle, run orchestration, `delete_state()` in `finally`. |
| ⏳ `handles.py` | Lazy handle resolution, `-1` detection, request-before-run. |
| ⏳ `callbacks.py` | Callback registration and the exception firewall. |
| ⏳ `idf.py` / `prepare.py` | eppy-based IDF inspection and injection. |
| ⏳ `errfile.py` | `.err` parsing into typed, severity-tagged records. |
| ⏳ `weather.py` | EPW indexing and the disclosed forecast oracle. |

## The four rules that break the process if ignored

1. **Wrap every callback body.** An exception raised inside a callback crosses into C++
   and kills the process with no traceback. `try/except Exception` at the top level of
   *every* callback: log with context, set a degraded flag, return normally.
2. **Never touch the EnergyPlus API from the worker thread.** It is not thread-safe.
   Cross the boundary via `bus/` only.
3. **`delete_state()` goes in a `finally`.** A leaked state corrupts the next run in the
   same process — which is exactly what `make run-all` does.
4. **Check every handle for `-1`.** EnergyPlus does not raise; it returns `-1` and then
   hands you zeros forever. Raise `HandleResolutionError` with the variable name.

## Handle lifecycle — the exact order

```
before run:  api.exchange.request_variable(state, name, key)   ← or the handle never resolves
run starts
callback:    if not api.exchange.api_data_fully_ready(state): return   ← earlier is -1 forever
             resolve once, cache on the handle registry, assert != -1
             if api.exchange.warmup_flag(state): return          ← never actuate in warmup
```

## Landmines specific to this layer

- The stock `RefBldgSmallOfficeNew2004_Chicago.idf` has **zero Fanger objects**, so PMV
  output simply does not exist. `prepare` injects `People.thermal_comfort_model_type` and
  the matching `Output:Variable`. Verify with `grep -c Fanger` on the prepared IDF, not
  the baseline one.
- EnergyPlus upper-cases most identifiers. Normalise zone names before comparing.
- `callback_begin_new_environment` fires once per *environment* — sizing runs first, then
  the run period. Reset accumulators there; never blend the two.
- Meters are **Joules**, and at `Timestep` frequency they are per-timestep, not per-hour.
  Never sum a timestep series against an hourly one.
- `api.api_version()` returns the API version (`0.2`), not the EnergyPlus version. Parse
  the install directory name — `locate.parse_version` already does.
- Heating setpoint ≥ cooling setpoint causes simultaneous heating and cooling, which
  *raises* energy use. `control/guardrails.py` enforces the deadband; this layer trusts it.

## Testing this layer

`@pytest.mark.energyplus` for anything needing the real engine — CI deselects it and
asserts `energyplus` is absent from `PATH`. Everything else runs against
⏳ `tests/fakes/fake_energyplus.py`, so `backend.py`'s protocol is the contract that
matters: if the fake satisfies it, the tests are honest.

```bash
.venv/bin/pytest tests/unit -q -k simulation
make lint && make typecheck && make test
```
