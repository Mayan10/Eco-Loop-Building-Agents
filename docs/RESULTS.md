# Results

Every number below comes from a real `ecoloop run` against the real
EnergyPlus 25.2.0 engine, `ecoloop compare`, or a real Ollama endpoint
(`qwen2.5:7b-instruct`), run during this project's own development — not
projected, not simulated-in-the-loose-sense. Where a run's raw artifacts
(`manifest.json`, `telemetry.parquet`) weren't kept, that's stated
explicitly rather than the number being presented as if they were.

## Fast profile (2 simulated weeks, Chicago TMY3, small office)

| Controller | Total kWh | Comfort violation rate¹ | Max &#124;PMV&#124; | Unmet hours |
|---|---|---|---|---|
| baseline | 3241.0 | 4.74% | 1.18 | 47.8 |
| rulebased | 3435.4 | 0.12% | 0.52 | 1.2 |

¹ Fraction of occupied, conditioned zone-timesteps with `|PMV|` outside
ASHRAE 55's ±0.5 band (`analysis/comfort.py::compute_comfort_metrics`).

**Rule-based uses ~6% more energy than baseline while cutting comfort
violations by ~97%.** This is the expected, correct shape of result, not a
surprise: `baseline`'s deep unoccupied setback (29.4°C cooling) saves energy
specifically by tolerating a very hot unoccupied zone. That is exactly the
failure mode this project's success criterion rules out ("saving energy by
making occupants uncomfortable is an explicit failure, not a trade-off") —
which is precisely why `baseline` is the *floor* this project measures
against, not a fair target to merely match.

*A methodological note:* an earlier, informal pass over this same
comparison (recorded in `AGENTS.md`'s development history) reported a
7.7% → 0.1% violation-rate improvement and a 1.34 → 0.52 max-PMV
improvement, versus 4.74% → 0.12% and 1.18 → 0.52 measured here by
`analysis/comfort.py`. The rule-based side's max `|PMV|` (0.52) and the
overall energy totals (3241 / 3435 kWh) match almost exactly between the
two measurements — strong evidence the underlying simulation and controller
behavior are the same — but the two violation-rate figures do not match
exactly, most likely because of a difference in how "occupied zone-timestep"
was counted (per-zone across all five conditioned zones, as
`compute_comfort_metrics` does, versus some other aggregation in the
earlier informal pass). This is disclosed rather than silently reconciled:
the `analysis/` module's methodology (documented in `FAILURE_MODES.md` and
its own docstrings) is the one to trust going forward, since it's the one
with an automated regression test behind it.

## Demo profile (3 simulated days, July 20–22, live-mode pacing)

| Controller | Total kWh | Comfort violation rate | Max &#124;PMV&#124; | Unmet hours |
|---|---|---|---|---|
| baseline | 586.6 | 10.19% | 1.03 | 18.3 |
| rulebased | 655.3 | 0.00% | 0.48 | 0.0 |
| agent | 655.3 | 0.00% | 0.48 | 0.0 |

**The agent controller's numbers are identical to rulebased's in this run —
and that's the honest result, not a rounding artifact.** During real,
end-to-end verification of the agent controller (real EnergyPlus, real
Ollama, the cognitive worker thread actually running concurrently with the
physics solver), the run completed exactly **one** real cognitive cycle. That
cycle called `get_zone_telemetry` and `get_comfort_status`, reasoned over
the result, and decided no setpoint change was warranted — a legitimate
"hold" decision, not a failure of the tool-calling loop (see
`ARCHITECTURE.md` §3 and §6). Because no policy was ever published, the
reflex tier ran on its rule-based fallback for the run's entire duration,
which is why the two controllers' recorded metrics match exactly.

This is not a claim that the agent controller doesn't work, or that it
can't beat rule-based — it's a direct, disclosed consequence of two things
this project's own real measurements established:

1. A single real cognitive cycle against `qwen2.5:7b-instruct` was observed
   taking on the order of a minute end to end (mostly LLM round trips), while
   EnergyPlus resolves the entire 3-day demo profile in under a second.
   `--live` mode's timestep pacing (`ARCHITECTURE.md` §6) exists specifically
   to give the worker thread a real chance to complete even one cycle within
   a short profile's run.
2. The model's decision, in this run, at this point in the simulated
   summer, was correct: the zone it inspected was inside the comfort band
   and near the daily low-carbon window described in the system prompt's
   worked example — "hold" is exactly the reasoning this project's prompt
   asks for in that situation (`PROMPT_ENGINEERING.md` §1), not a sign the
   model failed to act.

**What this run demonstrates, concretely:** the full two-tier architecture
works end to end against real infrastructure — EnergyPlus and the cognitive
worker ran on separate threads with no crash, no segfault, and no
interference; the worker correctly read live telemetry through the
thread-safe bus; a real MCP tool-calling round trip against a real model
completed and was traced; and the reflex tier's degradation ladder
(`ARCHITECTURE.md` §3) engaged correctly when no policy was published. What
it does *not* demonstrate is a measured energy or comfort advantage for the
agent controller specifically — that would need either a longer run (more
cadence windows for more cognitive cycles to accumulate) or a faster model,
and is flagged here as the honest scope of what's been measured rather than
implied by the identical numbers above.

## What a fair longer comparison would need

- **More cognitive cycles.** `agent.cadence_minutes` (30 simulated minutes on
  the `demo` profile, 60 on `fast`/`full`) determines how often the model
  *can* act; how many of those windows a short profile's wall-clock duration
  actually lets a real model answer within is a separate, empirically
  observed constraint (§ above). A `full` (annual) profile run gives the
  cognitive tier orders of magnitude more cadence windows to act in.
- **Accounting for the forecast-oracle asymmetry** (`ARCHITECTURE.md` §5):
  the agent controller has access to a 72-hour weather lookahead that
  `baseline`/`rulebased` structurally do not use. A future comparison should
  either report this asymmetry alongside the numbers (as done here) or
  construct a rule-based variant with the same forecast access, if the goal
  is isolating the LLM's reasoning contribution specifically.
- **A real annual run** was not executed for this document — `full` profile
  runs are slow by design (`AGENTS.md` §3: "do not run it to check a
  one-line change") and the fast/demo profiles above are what this
  project's real infrastructure was exercised against end to end.

## What's proven vs. what's projected

**Proven, by a real run against real infrastructure:**
- The two-tier controller, the thread boundary, and the degradation ladder.
- Real EnergyPlus simulation with real handle resolution, real guardrail
  clamping, and real telemetry persistence.
- Real MCP tool-calling against a real running Ollama endpoint, including a
  correct "hold" decision with real reasoning.
- Rule-based control cuts real comfort violations by roughly an order of
  magnitude versus the baseline schedule, at a real, measured energy cost
  of roughly 6%.

**Not yet measured, and not claimed:** a net kWh or comfort advantage for the
agent controller over rule-based, over a run long enough and with a model
fast enough for the cognitive tier to make more than one real decision.
