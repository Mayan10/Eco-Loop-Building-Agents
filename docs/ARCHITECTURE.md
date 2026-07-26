# Architecture

This documents *why* Eco-Loop is built the way it is, for a human reader.
`AGENTS.md` is the terser, code-agent-facing version and is the one that gets
updated every phase — if the two ever disagree, `AGENTS.md` is current and
this file is stale and should be fixed.

## 1. The problem the architecture solves

An annual EnergyPlus run at a typical timestep resolution is on the order of
50,000 timesteps. A local LLM inference is not free — in this project, a
single real cognitive cycle against `qwen2.5:7b-instruct` (including its own
internal tool-calling round trips) was observed taking on the order of a
**minute**, end to end, on the development machine. EnergyPlus, by contrast,
routinely resolves an entire multi-day run in under a second of wall-clock
time on the same machine.

Two consequences follow directly:

1. **The model cannot be called from inside the EnergyPlus callback.** The
   callback is synchronous — EnergyPlus is blocked for as long as the
   callback runs. Calling the LLM there would stall the physics solver for
   the same amount of time the LLM takes to answer, tens of thousands of
   times.
2. **EnergyPlus will often finish before the LLM would even respond once.**
   This isn't a hypothetical: it happened during real end-to-end
   verification of this project's `demo` profile (3 simulated days), and is
   the reason `--live` mode exists at all (§6).

## 2. The two-tier controller

```
                    main thread                    │        worker thread
                                                    │
  ┌──────────────────────────────────────────┐      │   ┌────────────────────────┐
  │  EnergyPlus  (C++ physics solver)         │      │   │  TIER 2 — COGNITIVE    │
  │                                           │      │   │  slow · agentic · async│
  │  ┌────────────────────────────────────┐   │      │   │                        │
  │  │ TIER 1 — REFLEX LAYER              │   │      │   │  • aggregated windows  │
  │  │ every timestep · <1 ms · no I/O    │   │      │   │  • LLM via MCP tools   │
  │  │                                    │   │      │   │  • emits ControlPolicy │
  │  │ read policy → clamp → actuate      │   │      │   │  • cadence-gated       │
  │  └────────────────────────────────────┘   │      │   └────────────────────────┘
  └──────────────────────────────────────────┘      │        │            ▲
              │  writes                             │        │ writes     │ reads
              ▼                                     │        ▼            │
      ┌───────────────────┐  ────── reads ──────────┼──►  ┌───────────────────┐
      │   TelemetryBus    │                         │     │    PolicyStore    │
      │ ring buffer       │  ◄───── reads ──────────┼───  │ frozen · atomic   │
      │ drop-oldest       │                         │     │ swap · TTL        │
      └───────────────────┘                         │     └───────────────────┘
```

**Tier 1 (Reflex, `control/reflex.py`)** runs synchronously inside
EnergyPlus's own callback, once per timestep. It never allocates, never does
I/O, and never calls anything that can block. Depending on
`control.controller` it either computes a policy directly (`baseline`,
`rulebased` — pure functions of the current `TelemetrySample`, no latency to
hide) or reads whatever the cognitive tier last published to `PolicyStore`
(`agent`). Whatever the source, the result passes through
`control/guardrails.py`'s clamp chain — the hard setpoint envelope, deadband,
rate cap, and hold time — as a uniform final step, so the guardrails do not
know or care whether a proposal came from a heuristic or an LLM.

**Tier 2 (Cognitive, `agent/`)** runs on its own OS thread
(`agent/worker.py`'s `CognitiveWorker`), with its own `asyncio` event loop.
It polls `TelemetryBus.latest()`, and once enough **simulated** time has
passed since its last invocation (`agent.cadence_minutes` — deliberately
*simulated* minutes, not wall-clock seconds; see §1), it runs one full
reasoning cycle: build context → render the system prompt → call the LLM,
handling any tool calls in a bounded loop → optionally publish a new
`ControlPolicy`.

The two threads share exactly two objects, both internally
`threading.Lock`-protected:

- **`TelemetryBus`** — a fixed-capacity, drop-oldest ring buffer. The
  callback-side write must never block, so a full buffer silently drops its
  oldest sample rather than waiting for the worker to catch up. Drops are
  counted (`TelemetryBus.dropped_count`), not silent to the operator.
- **`PolicyStore`** — holds one frozen, immutable `ControlPolicy` at a time,
  swapped atomically, with a TTL. The reflex tier reads one reference to it
  per timestep and uses that single reference for the whole timestep — a
  mid-timestep swap would otherwise tear the decision.

Nothing else crosses the thread boundary. In particular, the cognitive layer
**never** imports `simulation/` and never calls the EnergyPlus Python API —
enforced both by `import-linter` (`simulation`/`control` may not import
`agent`/`mcp`) and by the fact that `agent/worker.py` has no such import to
begin with. Calling the EnergyPlus API from a non-owning thread is not
merely against convention; `pyenergyplus`'s C++ runtime is not thread-safe
and segfaults with no Python traceback if it happens.

## 3. Degradation is the normal path, not the error path

The simulation must complete even if the LLM never answers, answers slowly,
or answers with garbage. The reflex tier's policy-resolution ladder is:

```
LLM times out / errors  → retry with backoff (llm.retry)
repeated failure        → circuit opens (llm.circuit_breaker) → cycle aborts cleanly
no tool calls at all    → a valid decision ("hold"), not an error
policy older than TTL   → PolicyStore expires it → reflex falls back to rule-based
```

`CognitiveOrchestrator.run_cycle()` is documented to never raise — every
failure mode above is caught and turns into either a valid "hold" decision or
a short logged summary, never an exception that could propagate anywhere
near the EnergyPlus thread. `CognitiveWorker` wraps the call in its own
`try/except` regardless, on the principle that a documented contract is not
a substitute for defending against it being violated by a future bug.

**A run that finishes on the reflex tier alone, having made zero cognitive
decisions, is a degraded success — not a failure.** This happened during
real verification of this project (see `RESULTS.md`): a real `agent`
controller run against `qwen2.5:7b-instruct` completed a single real
cognitive cycle, which — having read the zone telemetry and comfort status —
decided no actuation was needed, and the run's numbers came out identical to
the `rulebased` controller's for that reason. That is the degradation ladder
working exactly as designed, not evidence something is broken.

## 4. Tool-calling architecture

The cognitive layer is an MCP **client** of an in-process MCP **server**
(`mcp/server.py`) — the same server surface a human could point Claude
Desktop at. `CognitiveOrchestrator` calls `server.call_tool()` the same way
any external MCP client would; it does not get privileged access to
`ServerState` that a real client wouldn't.

Every tool function is written as a plain, independently unit-testable
function taking `state: ServerState` as its first parameter, then bound via
`functools.partial` at registration time — this is what lets the *same*
function be called directly in a test with a fake state, and also become a
schema-correct MCP tool (the LLM's tool schema never includes a `state`
parameter to fill in, since `inspect.signature()` on a partial correctly
omits the already-bound first argument). This breaks silently without
`inspect.signature(bound, eval_str=True)`: every module here uses
`from __future__ import annotations` (PEP 563), so an unresolved signature
carries bare strings instead of the actual classes, and FastMCP's dynamic
Pydantic model builder cannot resolve a string with no module context.

**The LLM's only actuation surface is `propose_policy` /
`request_zone_setpoint`.** Every other tool is read-only (`get_zone_telemetry`,
`get_comfort_status`, `get_energy_demand`, `get_guardrail_violations`,
`get_weather_forecast`, `get_recent_errors`, `read_sandboxed_text_file`, ...).
LLM output never reaches `eval`/`exec`/`subprocess`/`os.system`, and tool-call
arguments only ever reach a Pydantic model or a rejected-zone-name check — a
malformed call is caught by `orchestrator._call_tool` and fed back to the
model as a `"error: ..."` string in the next message, inside the same cycle's
bounded tool-call budget, rather than silently dropped or allowed to crash
the cycle.

## 5. The disclosed forecast oracle

`get_weather_forecast` (backed by `WeatherFile.forecast`) reads *ahead* in
the EPW file relative to the simulation's current position, capped at
`simulation.output.max_forecast_horizon_hours` (72h). This is a deliberate
design choice, not a data-leak bug: a real deployment would have a genuine
short-horizon weather forecast (a commercial API), and simulating that with
perfect foresight into the same EPW file used to drive the simulation is
simpler than fabricating a noisy synthetic forecast model that would add
complexity without adding a fairer test.

It is, however, a capability the **agent** controller has that `baseline`
and `rulebased` do not use — `analysis/compare.py`'s fairness check does not
and cannot correct for this, since it operates on completed runs' recorded
metrics, not on what each controller was *permitted* to know while running.
Any comparison across controllers should be read with that asymmetry in
mind: the agent's own decision-making has more information available to it
than the deterministic controllers do, by design.

## 6. `--live` mode and the pacing problem

Given §1, a short profile run against a real LLM can legitimately complete
zero or one cognitive cycles before EnergyPlus itself finishes. That's an
honest outcome for an automated `ecoloop run agent --profile fast` — the run
still produces a valid manifest and telemetry, just with the reflex tier
degraded to rule-based for its whole duration.

For a *recorded demo*, that's not acceptable: a viewer needs to see the
cognitive tier actually do something. `--live` (`ecoloop run agent --profile
demo --live`) does two things neither present in a normal run:

- Adds a deliberate delay in the telemetry callback,
  `agent.live_pacing_seconds_per_timestep` (zero everywhere except the
  `demo` profile), so the run's wall-clock duration gives the worker thread
  a real chance to complete a cycle before EnergyPlus finishes.
- Starts a third daemon thread, `ui/live.py`'s `LiveDashboard`, which — like
  the cognitive worker — only ever reads `TelemetryBus` and the worker's
  `cycles_run` counter, and renders a Rich terminal view refreshed every
  0.5s.

Both of these are demo-only concerns layered on top of the architecture in
§2, not changes to it: a non-live run pays zero pacing cost and renders
nothing.

## 7. Non-determinism, honestly

`llm.temperature: 0.0` and `llm.seed: 42` select greedy decoding with a fixed
seed — the closest thing to reproducibility an LLM endpoint offers. This is
not a guarantee of bit-for-bit identical output across machines, Ollama
versions, or even repeated runs on the same machine: batching, hardware
floating-point non-associativity, and quantization details can all produce
small differences that greedy decoding does not eliminate. The response
cache (`llm.py`, keyed on a hash of model/messages/tools/temperature/seed)
exists specifically to make a *specific* recorded run replayable offline —
it sidesteps the non-determinism question for demo/test purposes rather than
solving it, and that is disclosed here rather than implied to be solved.

## 8. Everything else, briefly

- **Self-healing** (`agent/selfheal.py`): a narrow, honest capability —
  recognizes exactly one fault class (an invalid schedule-name reference)
  from EnergyPlus's `.err` output and repairs it, bounded by
  `agent.selfheal.max_retries`. Not a general EnergyPlus error whisperer;
  see `AGENTS.md` §12 for why `models/faults/` fixtures exist and must not be
  hand-fixed.
- **Analysis pipeline** (`analysis/`, `runner.py`): `run_controller`/
  `run_agent_controller` promote the same `EnergyPlusBackend` +
  `HandleRegistry` + `ReflexCallbacks` + `ReflexController` wiring proven in
  tests to production code, persisting each run's telemetry to Parquet.
  `compare_runs` refuses to compare runs with different weather, run period,
  or EnergyPlus version fingerprints — a kWh difference between runs that
  faced different conditions measures nothing about the controller.
- **Config layering** (`config.py`): a `default.yaml` base overlaid by one of
  `fast`/`full`/`demo` profiles, validated by Pydantic v2 at every boundary.
  Every threshold anywhere in `src/` traces back to a name in this layer —
  there are no bare numeric literals scattered through the control logic.
