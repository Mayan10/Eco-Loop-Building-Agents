# AGENTS.md — Eco-Loop

Operational briefing for coding agents. Not documentation — `README.md` does that.
If a line here doesn't change what you'd *do*, delete it.

> **Build status:** Phase 8 of 9 complete (analysis: `runner.py` promotes the
> EnergyPlusBackend + ReflexCallbacks + ReflexController wiring that previously only existed
> in test helpers to production code for `baseline`/`rulebased`; `analysis/` computes energy
> (kWh, electricity + gas) and ASHRAE 55 comfort metrics from the persisted per-run telemetry,
> compares runs with a fairness check that refuses to compare mismatched weather/period/engine
> fingerprints, and renders both a self-contained offline HTML report and an interactive
> Streamlit dashboard from the same functions. `ecoloop run baseline --profile fast`,
> `ecoloop compare --latest`, and `ecoloop report --latest` all run end to end against the
> real engine: baseline totals 3241 kWh with a 4.7% comfort-violation rate, rulebased totals
> 3435 kWh with a 0.1% rate — matching the numbers Phase 3 first observed. 347 tests).
> The `agent` controller is deliberately out of scope for `run_controller` — it needs the
> cognitive worker-thread wiring, still a distinct integration task for Phase 9.
> Sections marked ⏳ describe files that do not exist yet. Everything unmarked is real
> and verified. This file is updated at the close of every phase.

## 1. What this project is

An EnergyPlus whole-building simulation is supervised in real time by a locally-served
open-source LLM over MCP: it reads live telemetry, reasons about comfort/energy/carbon, and
injects new set-points into the *still-running* simulation. Success is a measured kWh
reduction vs. baseline scheduling **with comfort held inside ASHRAE 55 PMV ±0.5** — saving
energy by making occupants uncomfortable is an explicit failure, not a trade-off.

## 2. Prerequisites & environment

- **Python 3.11+** (repo is developed on 3.12; `mypy` targets 3.11).
- **EnergyPlus 25.2.0** (supported range 23.2.0–26.99.0, set in `config/default.yaml`).
  - `pyenergyplus` **is not on PyPI**. It lives *inside* the EnergyPlus install directory
    and must be injected onto `sys.path`. `src/ecoloop/simulation/locate.py` is the only
    place that happens — never `sys.path.append` it yourself.
  - Located via, in order: explicit config → `$ENERGYPLUS_DIR` → `energyplus` on `PATH` →
    per-OS glob of standard install roots. On this machine it resolved from
    `~/.local/opt/EnergyPlus-*` with no env var set.
- **Ollama** serving `qwen2.5:7b-instruct` (default) with `qwen2.5:3b-instruct` as the
  fast/fallback model: `ollama serve` then `ollama pull qwen2.5:7b-instruct`.
- **Run `ecoloop doctor` first.** It names every missing piece and the exact fix.

## 3. Commands

| Purpose | Command |
|---|---|
| Install everything | `make setup` |
| **Diagnose environment (do this first)** | `make doctor` / `ecoloop doctor` |
| Lint + format check | `make lint` |
| Auto-fix formatting | `make format` |
| Typecheck (`--strict`) | `make typecheck` |
| Full test suite | `make test` |
| Fast tests only (use while iterating) | `make test-fast` |
| One test file | `.venv/bin/pytest tests/unit/test_config.py -q` |
| One test | `.venv/bin/pytest tests/unit/test_config.py::TestLoading -q` |
| Everything CI runs | `make check` |
| Regenerate grid signals | `.venv/bin/python scripts/generate_signals.py` |
| Verify agent files aren't stale | `.venv/bin/python scripts/check_agent_commands.py` |
| Diagnose and patch a broken IDF | `ecoloop selfheal --idf <path>` / `make demo-selfheal` |
| Serve the live building over MCP | `ecoloop mcp serve` / `make mcp` |
| Run a controller against the real engine | `ecoloop run <baseline\|rulebased\|agent\|all>` / `make run-baseline` etc. |
| Compare controllers' energy/comfort metrics | `ecoloop compare --latest` / `make compare` |
| Build the offline HTML report | `ecoloop report --latest` / `make report` |
| Launch the interactive dashboard | `ecoloop dashboard` / `make dashboard` (needs `pip install -e ".[dashboard]"`) |

⏳ `make prepare` and `make demo` are declared in the `Makefile` but their `ecoloop`
subcommands (a standalone `prepare`, and the live-TUI-driven demo) land in Phase 9.
`ecoloop run agent` (and therefore `make run-agent`) exits non-zero with an explanation
rather than running — it needs the cognitive worker-thread wiring, Phase 9's job.

**Profiles matter.** `--profile fast` is a 2-week run period and is the default for
iteration. `--profile full` is annual and slow — do not run it to check a one-line change.
`--profile demo` is 3 days, tuned for the recorded demo.

## 4. Architecture in 20 lines

Two tiers, two threads, two shared objects. This is the whole design.

```
        MAIN THREAD (EnergyPlus owns it)          │       WORKER THREAD (daemon)
                                                  │
   EnergyPlus C++ solver                          │   Tier 2 — COGNITIVE
     └── callbacks (synchronous!)                 │     cadence + event triggers
          └── Tier 1 — REFLEX                     │     aggregated telemetry only
              every timestep, <1 ms, no I/O       │     LLM via MCP tools
              read policy → clamp → actuate       │     emits ControlPolicy
                                                  │
              │ writes                            │        │ writes      ▲ reads
              ▼                                   │        ▼             │
        TelemetryBus ────────── reads ────────────┼──►  PolicyStore ─────┘
        ring buffer, drop-oldest                  │     frozen, atomic swap, TTL
```

Data flows one way through each object. The callback writes telemetry and reads policy;
the worker reads telemetry and writes policy. Nothing else crosses the thread boundary.

**Why:** an annual run is 52,560 timesteps and an LLM call is 1–10 s. Calling the model
in-callback would take 15–145 hours *and* stall the physics solver, because the callback is
synchronous.

## 5. Layering rules (enforced by `lint-imports` in CI — violating this fails the build)

- `simulation/` must **not** import `agent/` or `mcp/`.
- `control/` must **not** import `agent/` or `mcp/`.
- `bus/` imports nothing from other Eco-Loop layers — it is the innermost layer.
- Cross-layer communication goes through `bus/` only.

Contracts live in `[tool.importlinter]` in `pyproject.toml`.

## 6. Non-negotiable invariants

1. **No exception may escape a callback body.** It crosses into C++ and hard-crashes the
   process with no traceback. Every callback body is wrapped at its top level.
2. **Guardrails are enforced in code, never in the prompt.** Prompt-based safety is not
   safety.
3. **LLM output never reaches `eval`/`exec`/`subprocess`/`os.system`,** and is never written
   as executable code. The only surface is the fixed MCP tool allowlist.
4. **Meters are Joules.** Divide by `3.6e6` (`analysis.joules_per_kwh`). Get this wrong and
   every number in the project is wrong.
5. **`ControlPolicy` is frozen; read the reference once at the top of a callback** and use
   that one reference for the whole timestep, or a mid-timestep swap tears the decision.
6. **Never actuate during warmup** (`exchange.warmup_flag`), and never let warmup telemetry
   into metrics or LLM context.
7. **Never call the EnergyPlus API from the worker thread** — it is not thread-safe and will
   segfault the C++ runtime with no traceback.
8. **No number hard-coded in `src/`.** Every threshold lives in `config/default.yaml`.

## 7. Landmines

- A handle of `-1` is EnergyPlus saying "does not exist" — it does not raise. Treated as
  valid it silently yields zeros forever. Always check.
- `request_variable()` must be called **before** the run starts, or `get_variable_handle()`
  fails afterwards.
- Resolve handles lazily, on the first callback where `api_data_fully_ready()` is true.
  Earlier is `-1` permanently.
- **PMV output does not exist unless the `People` object asks for it.** In the stock
  `RefBldgSmallOfficeNew2004_Chicago.idf` all five `People` objects *do* declare
  `FANGER` — but in upper case, so `grep -c Fanger` returns 0 and reads as "missing".
  What is genuinely absent is the `Output:Variable`; that is what `prepare` injects.
- EnergyPlus upper-cases most identifiers. Normalise before comparing zone names —
  and before grepping the IDF, per the line above.
- `delete_state()` belongs in a `finally`. A leaked state corrupts the next run.
- Meter values at `Timestep` frequency are per-timestep, not per-hour. Never sum a timestep
  series with an hourly one.
- `callback_begin_new_environment` fires **per environment** (sizing, then run period).
  Reset accumulators; never blend environments.
- Heating ≥ cooling causes simultaneous heating and cooling and *raises* energy use.
- `api.api_version()` returns the **API** version (`0.2`), not the EnergyPlus version. Parse
  the install directory name instead.
- **The baseline IDF ships with weather-file run periods disabled.** Its `SimulationControl`
  object has `Run Simulation for Weather File Run Periods = No` — DOE reference models are
  distributed for sizing studies, not annual simulation. Unpatched, EnergyPlus runs only the
  design-day sizing environments, reports success (`rc == 0`, zero Severe errors), and never
  touches the weather file. Every meter and variable still resolves and reads back a real
  number — just the sizing-period one, not the run anyone asked for. No exception anywhere;
  `prepare` flips this to `Yes` (and `Run Simulation for Sizing Periods` to `No`, since
  autosizing is controlled by the separate `Do *Sizing Calculation` flags and doesn't need
  the sizing periods to also report output). Found by actually running the full spine against
  the real engine, not by reasoning about the API in the abstract — reinforces the value of §11.
- **`get_meter_handle` returns -1 for `"Electricity:Facility"` specifically, for the whole run,
  in EnergyPlus 25.2.0.** It is a real, actively-reported meter — it has its own index in
  `eplusout.mtr` with genuine hourly values — and every other Facility-level meter
  (`NaturalGas:Facility`, `ElectricityNet:Facility`, `ElectricityPurchased:Facility`, ...)
  resolves normally via the same call. Reproduced against the raw `pyenergyplus` API, so it is
  not a bug in this codebase. Use `ElectricityNet:Facility` instead — numerically identical here
  since this building has no on-site generation to net out. See `config/zones.yaml`.
- **A missing handle must fail once, not every timestep.** `HandleRegistry.ensure_resolved`
  tracks a failed attempt separately from a successful one — without that, one bad meter or
  actuator re-attempts full resolution (and re-raises) on every remaining timestep, and each
  raise passes through the callback guard's exception logging, turning a config error into a
  multi-minute stall that reads like a performance bug. Found by watching a real run hang.
- **EnergyPlus still runs full physics — and fires every registered callback — for the
  sizing design-days**, before the weather-file run period even begins. Those timesteps are
  neither warmup nor real telemetry; publishing them mixes a design day's numbers into the
  run anyone asked for, and for an autosized system they can outnumber the real run by an
  order of magnitude. Gate on `is_weather_run_period()` (`kind_of_sim() == 3`, determined
  empirically — undocumented in the Python API).
- **Selecting `--profile fast`/`--profile demo` changes nothing about the IDF by itself.**
  The baseline `RunPeriod` object ships spanning the full year, and nothing reads
  `simulation.run_period` back into it — every profile silently ran the same annual period
  until `prepare` started copying the active profile's begin/end month/day onto the IDF's
  `RunPeriod`. Without this, "fast" iteration was exactly as slow as "full".
- **`exchange.minutes()` is not reliably `0-59`** — it has been observed returning values
  past 60 (e.g. 65) near an environment boundary. Building `SimClock` from the raw field
  values crashes Pydantic validation deep inside the reflex callback. Roll `(hour, minute)`
  through `datetime.timedelta` instead of trusting either field in isolation.
- **Independent per-field rate-limiting can violate the deadband even when every stage
  individually upholds it.** Capping heating and cooling separately, each relative to its
  own previous value, can pull them toward each other faster than either one alone would
  suggest. `control/guardrails.py` re-enforces the deadband (and the envelope, for the same
  reason — see next item) as a **final** pass after rate/hold, not just before it. Found by
  a hypothesis property test, not by inspection.
- **The "hold at previous setpoint" branch must not trust `memory` blindly.** It returns
  `ZoneActuationMemory`'s stored values verbatim on the assumption they came from a prior
  valid clamp. A corrupted or externally-constructed memory can smuggle an out-of-envelope
  value straight through otherwise, bypassing the clamp entirely under the "nothing changed"
  branch. Re-clamp to the envelope after rate/hold regardless of which path produced the
  pre-final value.
- **Two different clocks look interchangeable in `ZoneActuationMemory` and are not.**
  `minutes_since_change` (time since the setpoint last *changed*, keeps accumulating while
  it doesn't) is right for the **hold** check but wrong for the **rate cap** — using it there
  lets a setpoint that has sat still for two hours jump twice as far in one tick as one that
  changed a minute ago. The rate cap needs `elapsed_minutes` (time since the *previous call*,
  i.e. one control-tick's duration) instead. Relatedly, the hold check itself must use
  `memory.minutes_since_change` **projected forward** by the current tick's `elapsed_minutes`
  — `ZoneActuationMemory.record()` only folds that tick in *after* the decision, so checking
  the stale, not-yet-updated figure refuses changes that are actually long overdue.
- **A "smarter" controller can legitimately cost more energy than a naive one, and that is
  not a bug.** Comparing `rulebased` against `baseline` on the fast profile:
  rule-based uses ~6% *more* energy (3435 vs. 3241 kWh) while cutting comfort violations from
  7.7% to 0.1% of occupied timesteps (max |PMV| 1.34 → 0.52). Baseline's deep, config-defined
  unoccupied setback (29.4 °C cooling) saves energy specifically by tolerating a very hot
  unoccupied zone — exactly the failure mode §1 calls out ("saving energy by making occupants
  uncomfortable is an explicit failure"). This is not what the rule-based-vs-agent comparison
  in later phases should look like; it is what makes baseline a *floor*, not a fair target.
- **Every MCP tool function takes `state: ServerState` as its first parameter, and `mcp/server.py`
  binds it via `functools.partial` before registration** — this is what lets a plain,
  independently-testable function (call it directly with a fake state in a unit test) also
  become a schema-correct MCP tool (the LLM never sees a `state` argument to fill in, since
  `inspect.signature()` on a partial correctly omits the already-bound first argument).
  **This breaks silently without `inspect.signature(bound, eval_str=True)`** — every module
  here uses `from __future__ import annotations` (PEP 563), so an unresolved signature carries
  bare strings (`"ZoneTelemetryResult"`) instead of the actual classes, and FastMCP's dynamic
  pydantic model builder cannot resolve a string with no module context to look the name up
  in. Without `eval_str=True` the server fails to start with a wall of
  `PydanticUserError: ... is not fully defined` — one per tool with a non-trivial return type.
- **`mcp.max_tool_result_bytes` is validated but not yet enforced.** No tool built so far
  plausibly returns more than a few KB, so truncation was left undone rather than added
  speculatively — a tracked gap, not an oversight, revisit if a future tool (e.g. a large
  trace dump) can exceed it.
- **Ollama's tool-calling works on even the 3B model, confirmed against a real running
  server, not assumed from documentation.** `qwen2.5:3b-instruct` correctly selected
  `get_zone_telemetry` with the right argument on the first try, and — end to end through
  `CognitiveOrchestrator` — correctly read a PMV comfort violation and proposed a sensible
  setpoint change with real reasoning text. `message.tool_calls[].function.{name,arguments}`
  is the exact shape to parse; `id` is present but not always meaningful, so
  `agent/llm.py`'s parser treats it as optional (`tc.get("id", "")`) rather than required.
- **The CLI's `run` subcommand — the one that would start a live simulation with this
  cognitive layer attached on a worker thread — does not exist yet.** Every piece is built
  and tested in isolation and proven against a real LLM via a hand-constructed
  `ServerState` — the worker-thread wiring (its own asyncio loop running the orchestrator on
  a cadence, alongside EnergyPlus on the main thread) is a distinct integration task, not
  done here. Tracked as a gap, not an oversight; Phase 8's annual runs need it too.
- **`economiser_shift` in `control/ecm.py` does not model real free cooling** — this project
  has no outdoor-air-damper actuator wired, only zone thermostats, so lowering the cooling
  setpoint when outdoor air is mild still costs full compressor energy; it is a modest,
  honestly-limited heuristic, not genuine economiser savings. It also fired on well under 1%
  of timesteps in the fast profile's July window (Chicago summer rarely sits in a 4-18 °C
  band during the day), so it is not what drove the energy difference above.
- **`min_hold_minutes` and the rate cap interact multiplicatively for a monotonic change,
  not just for oscillation.** A controller that consistently proposes movement in the same
  direction (e.g. baseline's several-degree unoccupied setback) still gets its first step
  refused by the hold check, then waits out `min_hold_minutes` again after every subsequent
  step, because each real change resets `minutes_since_change` to zero. The result is a
  "move a little, then wait, then move a little more" ratchet, not a continuous ramp at the
  rate cap — over one unoccupied window it can converge far short of the target. This is a
  legitimate reading of "hold for at least `min_hold_minutes` between changes," not a bug,
  but it means the rate cap alone does not predict convergence time; the two settings
  together do. Found via the fake-backend end-to-end test expecting full convergence within
  a 6-hour unoccupied window and getting 17.35 °C instead of 15.6 °C.
- **EnergyPlus's folded "item not found" error text has no delimiter between a free-form
  object name and a free-form field label — both are just words separated by spaces.**
  `agent/selfheal.py`'s classifier regex originally tried to capture the object name with a
  non-greedy group and immediately mis-split it (`"CORE_ZN"` instead of the real
  `"Core_ZN DualSPSched"`), because the adjacent field-label group's open character class
  absorbed the rest. Fixing the object-name group only moved the same ambiguity into the
  field group next. The actual fix drops object-name extraction entirely — the exact broken
  object is found by searching the IDF for the one instance whose field currently *equals*
  the bad value, which is unambiguous — and constrains the field group to a closed
  alternation of known field-label literals rather than any open character class. An open
  class for either free-form span reintroduces the same bug one level down. Found by
  running the real `broken_thermostat.idf` fixture against the real engine and reading its
  actual Severe-record text, not by guessing at EnergyPlus's format.
- **numpy 2.5.0 broke `mypy` under this project's `python_version = "3.11"` target** by
  shipping an unconditional (not `sys.version_info`-gated) PEP 695 `type` statement in its
  bundled `__init__.pyi` — a hard parse error, not a suppressible one; per-module
  `follow_imports = "skip"` does not help, since the failure happens before per-module
  import-following settings apply. `pyproject.toml` caps `numpy<2.5`; 2.4.x's stub still
  uses the older `TypeAlias` form. Found by being the first phase to actually import
  `pandas` (which pulls in `numpy`) anywhere in `src/` — every earlier phase's `pandas`/
  `plotly`/`streamlit` dependencies were declared but code-untouched.
- **A run's full telemetry history lives nowhere until `analysis.collect.TelemetryRecorder`
  buffers it.** `TelemetryBus` is a bounded, drop-oldest ring buffer by design (rolling-window
  reads for the cognitive tier, not a durable record), and EnergyPlus's own `.eso`/`.mtr`
  output uses a different variable vocabulary and reporting frequency than the runtime
  data-exchange point map in `config/zones.yaml`. `TelemetryRecorder` buffers flattened rows
  in a plain Python list during the run — never written to disk from inside the EnergyPlus
  callback, which would be I/O in a synchronous physics callback — and flushes to Parquet
  once `run_controller()`'s call to `backend.run()` returns.
- **Total conditioned floor area cannot be read off the IDF.** Every `Zone` object declares
  `Floor Area = autocalculate` (this building's floor plates come from
  `BuildingSurface:Detailed` polygons, not a literal field), and the Python data-exchange API
  has no reportable "zone floor area" output variable. EnergyPlus computes and echoes it per
  zone in the `.eio` file's `Zone Information` records instead, including a "Part of Total
  Building Area" flag that already excludes the attic — `simulation/eio.py` reads the column
  positions from that record's own header comment rather than assuming them, so a future
  EnergyPlus version reordering columns fails loudly instead of silently misreading one.
  Verified against a real run: sums to exactly 511.16 m², the published DOE Small Office
  prototype total.
- **`run_controller()` only supports `baseline`/`rulebased`, and this is a scope boundary,
  not a missing feature waiting to be filled in casually.** Both compute their policy
  synchronously in the callback with no LLM involved, so nothing beyond
  `EnergyPlusBackend` + `HandleRegistry` + `ReflexCallbacks` + `ReflexController` is needed.
  `agent` reads from a `PolicyStore` that only a worker thread running the cognitive tier's
  cadence loop can keep current, and wiring that thread alongside EnergyPlus's blocking,
  main-thread-owning `run()` call is a distinct integration task — `ecoloop run agent`
  exits non-zero with that explanation rather than a stack trace.
- **`compare_runs()` refuses to compare runs with different profiles, weather files, or
  EnergyPlus versions (`UnfairComparisonError`), and deliberately excludes `idf_path` from
  that check.** Every run overwrites the same `simulation.idf_prepared` destination, so the
  path is identical across controllers regardless of what was actually simulated — including
  it in the fairness check would validate nothing.

## 8. Conventions

- Full type annotations on every public function; `mypy --strict` must pass.
- **Pydantic v2 at every boundary** — config, LLM I/O, MCP schemas, metrics. No untyped
  dicts crossing a module boundary.
- Config, not constants. Google-style docstrings on every module, class and public function.
- `structlog` only: `get_logger(__name__, component="reflex")`. Never `print`, never
  `logging.getLogger` directly.
- **Bare `except:` and `except Exception: pass` are banned.** Every handler logs with
  context and either re-raises a typed `EcoLoopError` or takes a documented recovery action.
- Functions ≤ ~50 lines, files ≤ ~500 lines.
- Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, `test:`, `refactor:`).

## 9. Testing

- **The suite runs with EnergyPlus and Ollama absent.** CI actively asserts `energyplus` is
  not on `PATH` before running, so the fakes cannot silently stop being exercised.
- `tests/fakes/fake_energyplus.py` works: a lumped-capacitance thermal double implementing
  `SimulationBackend`, understanding the exact variable/meter/actuator vocabulary in
  `config/zones.yaml`. `tests/unit/test_end_to_end_control.py` runs `HandleRegistry`,
  `ReflexCallbacks` and every `control/` controller — the same unmodified production code
  that ran against real EnergyPlus — against this fake instead, which is the strongest test
  in the suite: it proves the `SimulationBackend` protocol seam is real, not just that the
  fake behaves. `tests/fakes/fake_llm.py` works too: `ScriptedLLM` (queued responses, for
  exercising the orchestrator's tool-calling loop deterministically) and `FailingLLM` /
  `TimeoutLLM` / `MalformedToolCallLLM` (each always raises the failure mode it's named for)
  — anything satisfying `OllamaClient`'s duck-typed `chat()` works, since
  `CognitiveOrchestrator` only ever calls that one method.
- `tests/unit/test_agent_selfheal.py` covers `selfheal.py`: `diagnose()` against a
  hand-built `ErrFileSummary` needs no engine at all, but `repair()` and
  `run_with_self_healing()` are marked `@pytest.mark.energyplus` — they run the real
  `models/faults/broken_thermostat.idf` end to end and assert it succeeds on attempt 2.
- ⏳ `tests/property/` (dedicated hypothesis suite over *arbitrary* LLM output) and
  `tests/chaos/` (kills the LLM mid-run via `fake_llm.py`'s failing variants, asserts the
  simulation still completes) are still open — self-healing turned out to need its own
  targeted engine-backed tests rather than a chaos harness, since the fault it recovers from
  is a deterministic input error, not a mid-run LLM failure. Property tests on the guardrail
  chain itself already exist inline in `tests/unit/test_guardrails.py` — see §7's landmines
  on the two real bugs they found before this ever ran against a controller.
- Mark anything needing the real engine `@pytest.mark.energyplus`.
- `tests/unit/test_runner.py` marks the actual-run cases `@pytest.mark.energyplus` (same
  precedent as `test_agent_selfheal.py`: `run_controller` constructs `EnergyPlusBackend`
  directly, so it can't be exercised against the fake without an injection seam nobody
  asked for); the "agent controller is rejected" guard test needs no engine, since it
  returns before touching one. `test_analysis_collect.py`, `test_analysis_metrics.py`,
  `test_analysis_comfort.py`, `test_analysis_compare.py`, `test_analysis_charts.py`, and
  `test_analysis_report.py` all run on hand-built `DataFrame`s/manifests with no engine at
  all — `analysis/` only ever consumes already-persisted telemetry, never EnergyPlus
  directly, so nothing under it needs the mark.
- **Every bug fix gets a regression test.** No exceptions.
- Coverage on `control/` and `bus/` is 96% (`pytest --cov=src/ecoloop/control --cov=src/ecoloop/bus`).
  `dashboard/` and `ui/` are excluded from coverage entirely (`pyproject.toml`'s
  `[tool.coverage.run] omit`) — a Streamlit page isn't meaningfully unit-testable, so it was
  smoke-tested by hand instead (`streamlit run` against real fast-profile run data, confirmed
  serving HTTP 200 with no errors in the server log) rather than given hollow coverage.

## 10. Where things live

```
config/          default.yaml is the single source of truth; profiles/ overlay it
  prompts/       versioned Jinja2 templates (system_v1.j2; version recorded in the trace)
  signals/       synthetic carbon + tariff CSVs (regenerate: scripts/generate_signals.py)
models/          baseline/ pristine IDF · prepared/ post-inject · generated/ per-run
  faults/        deliberately broken IDFs for the self-heal demo — see §12
  weather/       EPW files (gitignored; copied from the EnergyPlus install)
src/ecoloop/
  config.py      layered settings   errors.py  typed hierarchy   logging.py  structlog
  doctor.py      environment checks  cli.py    Typer entry point
  runner.py      run_controller(): the full EnergyPlusBackend + ReflexCallbacks +
                 ReflexController wiring, promoted from test-only to production, for
                 baseline/rulebased — writes a RunManifest + telemetry.parquet per run
  simulation/    locate/energyplus/handles/callbacks/idf/prepare/errfile/weather/eio all work
  bus/           models/telemetry/policy all work; ⏳ events (arrives with P6's triggers)
  control/       base/guardrails/ecm/baseline/rulebased/reflex all work
  agent/         context/prompts/llm/orchestrator/selfheal all work
  mcp/           server/tools_observe/tools_actuate/tools_introspect/sandbox/trace all work
  analysis/      collect/metrics/comfort/compare/charts/report all work
  dashboard/     app.py — Streamlit dashboard, reuses analysis/ directly (ecoloop dashboard)
scripts/         generate_signals.py · check_agent_commands.py
```

## 11. How to verify a change

```bash
make lint && make typecheck && make test
```

Run these three before declaring anything done. **A change is not complete until all three
pass.** If your change touched a command, an invariant, or the layering, update the relevant
agent file *in the same commit* — a stale agent file is a bug, and CI checks the commands.

## 12. Things that look wrong but are correct

- **`models/faults/` contains deliberately broken IDFs.** They are fixtures for the
  self-healing demo (`ecoloop selfheal --idf models/faults/broken_thermostat.idf`). Do not
  fix them by hand — the point is that `agent/selfheal.py` fixes them at run time.
  `broken_thermostat.idf` was built by running it against the real engine *first* to capture
  EnergyPlus's exact Severe-record text, then writing the classifier regex against that
  captured text — not the other way around.
- **`get_weather_forecast` reads ahead in the EPW.** This is a deliberate *forecast oracle*,
  disclosed as such in `docs/ARCHITECTURE.md`. It is not a data leak bug.
- **The telemetry queue drops the oldest sample when full, silently by design.** Blocking
  the callback to preserve a sample would stall the physics solver — the wrong trade. Drops
  are counted and surfaced in the run manifest.
- **The tariff peak is deliberately offset from the carbon peak** in `config/signals/`. If
  they coincided, "cheap" and "clean" would be one objective and the agent's multi-objective
  reasoning would be untestable.
- **`config/default.yaml` looks over-parameterised.** That is the point: no magic numbers in
  `src/` is an enforced invariant, so every threshold has to live somewhere.
- **Metrics are computed twice, from meters and from variables.** ⏳ They are not redundant —
  meters are cumulative per reporting period, variables are instantaneous. Cross-checking
  them is how a unit error gets caught.
