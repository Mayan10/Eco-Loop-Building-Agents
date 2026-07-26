# Demo Script

A timed shot-list for a single, unedited screen recording of `make demo`
(`ecoloop run agent --profile demo --live`). Every timing below is grounded
in a real recorded run of this exact command against `qwen2.5:7b-instruct`
on the development machine (see `RESULTS.md`) — treat the numbers as
realistic estimates for that setup, not a guarantee for every machine/model
combination. The recording budget is **≤3 minutes**; the pacing and model
notes below explain the actual levers if a take runs long.

## Before recording

1. `ollama serve` running, with `qwen2.5:7b-instruct` (or the faster
   `qwen2.5:3b-instruct` — see the timing note below) already pulled and
   warm — a cold model load adds real seconds to the first cognitive cycle
   for no narrative benefit. A throwaway `curl localhost:11434/api/chat`
   call before recording warms it.
2. `ecoloop doctor` passes clean (EnergyPlus found, Ollama reachable, models
   present).
3. Terminal font large enough that the Rich dashboard's table is legible on
   camera; a wide-enough terminal that `Zones` table columns don't wrap.

## Shot list

**0:00–0:10 — Set the scene (talking head or title card, optional)**
One sentence: "Eco-Loop supervises a live EnergyPlus building simulation
with a local LLM over MCP, and adjusts setpoints in the still-running
simulation — no human in the loop." Cut to terminal.

**0:10–0:15 — Launch**
Run `make demo` on camera. This resolves to `ecoloop run agent --profile
demo --live` — say the command out loud or let it show in the terminal
history before running, so a viewer can reproduce it.

**0:15–0:55 — Warmup and early pacing**
EnergyPlus runs its sizing/warmup environment (not shown in the dashboard —
`--live` pacing and the dashboard's data both only start once real,
non-warmup telemetry is published), then the Rich panel appears: "Simulated
time," "Wall-clock elapsed," "Cognitive cycles run" (starts at 0), and the
per-zone table (temp, PMV, occupied) updating roughly every half second as
`agent.live_pacing_seconds_per_timestep` (0.1s/timestep on this profile)
spaces the run out. Let the simulated clock visibly advance through July 20.
Narrate over this: point at the PMV column, note the comfort band the
project targets (±0.5).

**~0:55–2:15 — The cognitive cycle**
Somewhere in this window (timing is LLM-latency-bound, not
simulation-bound — see the note below), "Cognitive cycles run" increments
from 0 to 1. If narrating live, this is the moment to say what's actually
happening off-screen: the worker thread has read aggregated telemetry
through MCP tools (`get_zone_telemetry`, `get_comfort_status`), reasoned
over it against the ASHRAE 55 target, and either published a new setpoint
or explicitly decided to hold. Both outcomes are legitimate and worth
showing — a "hold" decision is not a stall (`ARCHITECTURE.md` §3).

**2:15–2:50 — Finish and results**
Let the run complete (the simulated clock reaches July 22, the process
exits, prints `✓ agent succeeded — N timesteps`). Cut to `ecoloop compare
--latest` run beforehand (baseline/rulebased already generated) so the
recording can show the three-controller table landing on screen — total
kWh, comfort violation %, max |PMV|, unmet hours — without waiting for two
more full runs on camera.

**2:50–3:00 — Close**
One sentence on where the honest gap is: this recording proves the
architecture end to end (real engine, real thread boundary, real LLM
tool-calling); a measured energy/comfort win for the agent specifically
needs a longer run for more cognitive cycles to accumulate — see
`RESULTS.md`. Ending on the documented limitation rather than an
inflated claim is the point, not a hedge.

## Timing risk and the actual levers

A single real cognitive cycle against `qwen2.5:7b-instruct` was observed
taking on the order of a minute end to end in this repository — mostly LLM
round trips, not simulation time. Combined with ~43 seconds of deliberate
pacing (432 timesteps × 0.1s), a take showing even one real cycle can run
close to the 3-minute budget by itself. If a take runs long:

- **Switch to the faster fallback model** (`llm.fallback_model:
  qwen2.5:3b-instruct` is already configured, though nothing switches to it
  automatically yet — pass `--config` with an override, or edit
  `config/profiles/demo.yaml`'s `llm.model` for the recording). The smaller
  model was the one originally confirmed working correctly on tool-calling
  in this project's Phase 6 development, and responds faster.
- **Lower `agent.live_pacing_seconds_per_timestep`** if the physics portion
  is what's eating the budget, or raise it slightly if the cognitive cycle
  needs more wall-clock room to land before the run ends.
- **Cut on the "cognitive cycles run" counter incrementing** rather than
  waiting for the full 3 simulated days to finish playing out on screen —
  the narrated point (a real decision happened) is made the moment the
  counter moves; the remaining simulated days add nothing new to show.

None of this is tuned to hide a limitation — it's tuned to fit a real,
disclosed constraint (LLM latency vs. a short profile's wall-clock budget,
`ARCHITECTURE.md` §1 and §6) inside a fixed recording length.
