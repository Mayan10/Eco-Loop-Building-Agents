# AGENTS.md — Eco-Loop

Operational briefing for coding agents. Not documentation — `README.md` does that.
If a line here doesn't change what you'd *do*, delete it.

> **Build status:** Phase 1 of 9 complete (skeleton, config, logging, errors, `doctor`, CI).
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

⏳ `make prepare`, `make run-baseline`, `make run-rulebased`, `make run-agent`, `make run-all`,
`make compare`, `make report`, `make dashboard`, `make mcp`, `make demo`, `make demo-selfheal`
are declared in the `Makefile` but their `ecoloop` subcommands land in Phases 2–8.

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
- **PMV output does not exist unless the `People` object asks for it.** The stock
  `RefBldgSmallOfficeNew2004_Chicago.idf` has *zero* Fanger objects — `prepare` injects them.
- EnergyPlus upper-cases most identifiers. Normalise before comparing zone names.
- `delete_state()` belongs in a `finally`. A leaked state corrupts the next run.
- Meter values at `Timestep` frequency are per-timestep, not per-hour. Never sum a timestep
  series with an hourly one.
- `callback_begin_new_environment` fires **per environment** (sizing, then run period).
  Reset accumulators; never blend environments.
- Heating ≥ cooling causes simultaneous heating and cooling and *raises* energy use.
- `api.api_version()` returns the **API** version (`0.2`), not the EnergyPlus version. Parse
  the install directory name instead.

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
- ⏳ `tests/fakes/fake_energyplus.py` (lumped-capacitance thermal double) and
  `fake_llm.py` (scripted, can emit valid / malformed / absurd / timeout) arrive in Phase 4.
- ⏳ `tests/property/` asserts guardrail invariants with `hypothesis` over *arbitrary* LLM
  output. `tests/chaos/` kills the LLM mid-run and asserts the simulation still completes.
- Mark anything needing the real engine `@pytest.mark.energyplus`.
- **Every bug fix gets a regression test.** No exceptions.

## 10. Where things live

```
config/          default.yaml is the single source of truth; profiles/ overlay it
  prompts/       ⏳ versioned Jinja2 templates (version recorded in the trace)
  signals/       synthetic carbon + tariff CSVs (regenerate: scripts/generate_signals.py)
models/          baseline/ pristine IDF · prepared/ post-inject · generated/ per-run
  faults/        ⏳ deliberately broken IDFs for the self-heal demo — see §12
  weather/       EPW files (gitignored; copied from the EnergyPlus install)
src/ecoloop/
  config.py      layered settings   errors.py  typed hierarchy   logging.py  structlog
  doctor.py      environment checks  cli.py    Typer entry point
  simulation/    locate.py works; ⏳ E+ lifecycle, handles, callbacks, IDF patching
  bus/           ⏳ TelemetryBus, PolicyStore, events, Pydantic models
  control/       ⏳ reflex, guardrails, baseline/rulebased controllers, ECM catalogue
  agent/         ⏳ orchestrator, LLM client, prompts, context budgeting, self-heal, trace
  mcp/           ⏳ MCP server + observe/actuate/introspect tools
  analysis/      ⏳ metrics, comfort, compare, charts, static HTML report
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

- **`models/faults/` contains deliberately broken IDFs.** ⏳ They are fixtures for the
  self-healing demo. Do not fix them.
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
