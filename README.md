<div align="center">

# 🌱 Eco-Loop

### An autonomous closed-loop building control agent

**A high-fidelity EnergyPlus building simulation, supervised in real time by a local open-source LLM speaking Model Context Protocol — closing the control loop with no human in it.**

[![CI](https://github.com/Mayan10/Eco-Loop-Building-Agents/actions/workflows/ci.yml/badge.svg)](https://github.com/Mayan10/Eco-Loop-Building-Agents/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Ruff](https://img.shields.io/badge/lint-ruff-261230.svg)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-2a6db2.svg)](https://mypy-lang.org/)

</div>

---

> **Status:** all 9 build phases complete and verified against the real EnergyPlus
> engine and a real running Ollama endpoint. Headline numbers, measured not projected:
> rule-based control cuts real comfort violations by roughly an order of magnitude
> versus baseline scheduling at a real, measured energy cost of ~6% (2-week profile,
> 3241 → 3435 kWh). Full numbers, including what's proven vs. what still needs a
> longer run, in [`docs/RESULTS.md`](docs/RESULTS.md).

## What this is

A building is normally a passive consumer of energy: schedules are set at
commissioning and drift for a decade. Eco-Loop turns a simulated building into an
**active agent** that observes itself and corrects its own operation.

An EnergyPlus whole-building simulation runs as a digital sandbox. While it is
running it streams telemetry — zone temperatures, humidity, CO₂, Fanger PMV/PPD
comfort indices, HVAC power draw, outdoor conditions — to a locally-served
open-source LLM. The model reasons over that telemetry against comfort targets,
peak-demand limits and grid carbon intensity, and **injects new set-points
forward into the still-running simulation**. No human in the loop.

The project then has to *prove*, quantitatively, that this beats standard
baseline scheduling on energy **without degrading thermal comfort**. Saving
energy by making occupants uncomfortable is an explicit failure condition.

## The core architectural insight

An annual simulation at 6 timesteps/hour is **52,560 timesteps**. An LLM
inference is 1–10 seconds. Calling the model inside the EnergyPlus timestep
callback would mean 15–145 hours of wall clock — and because the callback is
*synchronous*, every one of those seconds stalls the physics solver.

So Eco-Loop is a **two-tier hierarchical controller**:

```
                    main thread                    │        worker thread
                                                   │
  ┌──────────────────────────────────────────┐     │   ┌────────────────────────┐
  │  EnergyPlus  (C++ physics solver)        │     │   │  TIER 2 — COGNITIVE    │
  │                                          │     │   │  slow · agentic · async│
  │  ┌────────────────────────────────────┐  │     │   │                        │
  │  │ TIER 1 — REFLEX LAYER              │  │     │   │  • aggregated windows  │
  │  │ every timestep · <1 ms · no I/O    │  │     │   │  • LLM via MCP tools   │
  │  │                                    │  │     │   │  • emits ControlPolicy │
  │  │ read policy → clamp → actuate      │  │     │   │  • cadence-gated       │
  │  └────────────────────────────────────┘  │     │   └────────────────────────┘
  └──────────────────────────────────────────┘     │        │            ▲
              │  writes                            │        │ writes     │ reads
              ▼                                    │        ▼            │
      ┌───────────────────┐  ────── reads ─────────┼──►  ┌───────────────────┐
      │   TelemetryBus    │                        │     │    PolicyStore    │
      │ ring buffer       │  ◄───── reads ─────────┼───  │ frozen · atomic   │
      │ drop-oldest       │                        │     │ swap · TTL        │
      └───────────────────┘                        │     └───────────────────┘
```

**Tier 1 (Reflex)** runs on every EnergyPlus timestep. Pure Python, no network,
sub-millisecond. It reads the currently active immutable policy and applies it
through actuators, enforcing guardrails in code.

**Tier 2 (Cognitive)** runs on a background worker at a configurable cadence
(event triggers are validated config today, not yet wired to the orchestrator —
see `AGENTS.md`). It sees *aggregated* telemetry, reasons through MCP tools, and
atomically swaps in a new validated policy.

The simulation is therefore **never blocked by the agent**. If the LLM is slow,
unreachable, or dead, the reflex layer keeps running on the last valid policy
until its TTL, then degrades to a deterministic rule-based controller, then to
the building's native schedule.

## Quickstart

```bash
git clone https://github.com/Mayan10/Eco-Loop-Building-Agents.git
cd Eco-Loop-Building-Agents
make setup          # uv venv + dependencies
ecoloop doctor      # ← run this first; diagnoses EnergyPlus, Ollama, models
```

`ecoloop doctor` tells you exactly what is missing and how to fix it. From there:

```bash
make run-baseline   # uncontrolled reference schedule
make run-rulebased  # deterministic heuristic controller
make run-agent      # LLM-supervised controller (needs Ollama running)
make compare        # side-by-side energy + ASHRAE 55 comfort metrics
make report         # self-contained offline HTML report (no server needed to view)
make dashboard      # interactive Streamlit version of the same comparison
make demo           # ≤3-minute live-TUI recording of the closed loop
```

Every `run-*` target persists its full telemetry history and a manifest under
`results/runs/`; `compare`/`report`/`dashboard` all read the same functions
against those manifests, so they can never disagree about a number.

## Documentation

| Document | Contents |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Tool-calling architecture, prompt latency management, concurrency model, design decisions |
| [`docs/PROMPT_ENGINEERING.md`](docs/PROMPT_ENGINEERING.md) | Prompt strategy, token budgeting, handling lengthy simulation logs |
| [`docs/FAILURE_MODES.md`](docs/FAILURE_MODES.md) | Every failure → detection → fallback → covering test |
| [`docs/RESULTS.md`](docs/RESULTS.md) | Narrated findings, honest analysis including where the agent loses |
| [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) | Timed shot-list for the ≤3-minute video |
| [`AGENTS.md`](AGENTS.md) | Operational briefing for coding agents working on this repo |

## License

MIT — see [LICENSE](LICENSE).
