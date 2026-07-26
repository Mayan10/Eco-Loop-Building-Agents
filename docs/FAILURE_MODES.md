# Failure Modes

Every failure mode Eco-Loop is built to survive, in one table per subsystem:
**failure → detection → fallback → covering test**. This is the audit trail
for "what happens when X breaks" — if a new failure mode is found and fixed,
it gets a row here and a regression test, in the same commit, per `AGENTS.md`
§9 ("every bug fix gets a regression test, no exceptions").

## Cognitive layer (LLM unreachable, slow, or wrong)

| Failure | Detection | Fallback | Covering test |
|---|---|---|---|
| LLM endpoint unreachable / times out | `httpx` connection/timeout error inside `OllamaClient._call_with_retry` | Retry with jittered exponential backoff (`llm.retry`); after `retry.max_attempts`, raise `LLMUnavailableError` | `test_agent_llm.py` against `httpx.MockTransport` |
| LLM repeatedly failing | `_CircuitBreaker` failure count crosses `llm.circuit_breaker` threshold | Circuit opens; further calls short-circuit to `CircuitOpenError` without attempting the network, until the breaker's half-open probe budget allows a retry | `test_agent_llm.py` |
| A cognitive cycle can't get an answer at all | `CircuitOpenError` / `LLMUnavailableError` raised from `.chat()` | `CognitiveOrchestrator.run_cycle()` catches it, aborts the cycle cleanly, returns a summary containing `"aborted"` — never raises out of the cycle | `test_agent_orchestrator.py::TestLLMFailure` |
| Model returns no tool calls at all | `ChatResponse.tool_calls` is empty | Treated as a valid "hold" decision, not an error — no policy is published, the previous one (or none) stands | `test_agent_orchestrator.py::TestNoActionTaken` |
| Model asks for a tool with bad/missing arguments | The bound tool function raises (Pydantic validation, FastMCP's own checks, or a rejected zone name) | `orchestrator._call_tool` catches any exception the tool raises and feeds it back to the model as an `"error: ..."` string in the next message, inside the same cycle's tool-call budget — the model sees its own mistake and can correct it | `test_agent_orchestrator.py`, `test_mcp_tools_*.py` |
| Model tries to run away with tool calls | Cycle's tool-call count reaches `agent.max_tool_calls_per_invocation` | Loop stops, no terminal action taken this cycle — a budget ceiling, not a crash | `test_agent_orchestrator.py::TestBudgetExhaustion` |
| A published policy goes stale (worker died, or simply hasn't run recently enough) | `PolicyStore` checks `bus.policy.default_ttl_minutes`/`max_age_minutes` against the policy's age | Reflex tier reads "no current policy," falls back to the rule-based computation directly — the same computation `rulebased` mode uses | `test_policy_store.py`, `test_reflex_controller.py` |
| A cognitive cycle legitimately never fires (short profile, slow model — see `ARCHITECTURE.md` §1) | N/A — not an error condition | Entire run proceeds on the reflex tier's rule-based fallback; manifest still records success | Observed directly during real verification (`RESULTS.md`) |
| The worker thread's own loop raises unexpectedly | `CognitiveWorker._run_forever`'s outer `try/except` around `asyncio.run(self._loop())` | Logged (`"cognitive worker thread crashed"`); the daemon thread ends, the main EnergyPlus thread is unaffected and the run still completes on the reflex tier | `test_agent_worker.py::test_a_failing_llm_does_not_crash_the_worker_thread` |
| Stopping the worker takes too long (an in-flight LLM call outlasts the join timeout) | `CognitiveWorker.stop()`'s `thread.join(timeout=...)` returns with the thread still alive | Logged warning; the thread is a daemon, so process exit still proceeds — `run_agent_controller` sizes the timeout from `max_tool_calls_per_invocation * request_timeout_seconds` specifically so a legitimate in-flight cycle isn't abandoned on an arbitrarily short window | Observed directly during real verification; see `runner.py`'s `worker_stop_timeout` |

## Guardrails (the clamp chain)

| Failure | Detection | Fallback | Covering test |
|---|---|---|---|
| Independent per-field rate limiting can still violate the deadband, even when each field individually respects its own cap | Hypothesis property test generating adversarial heating/cooling pairs | `control/guardrails.py` re-enforces the deadband (and the envelope) as a **final** pass after rate/hold, not just before it | `test_guardrails.py` (found by property testing, not inspection) |
| The "hold at previous setpoint" branch trusting stored memory blindly | A corrupted/externally-constructed `ZoneActuationMemory` could smuggle an out-of-envelope value through the "nothing changed" branch | Re-clamp to the envelope after rate/hold regardless of which path produced the pre-final value | `test_guardrails.py` |
| Two different "elapsed time" clocks look interchangeable in `ZoneActuationMemory` and are not | `minutes_since_change` (for the hold check) vs. `elapsed_minutes` (for the rate cap) confused for each other | Rate cap uses `elapsed_minutes`; hold check uses `minutes_since_change` **projected forward** by the current tick, since `record()` only folds a tick in after the decision | `test_guardrails.py` |
| Heating setpoint ≥ cooling setpoint | Deadband check in the clamp chain | Rejected/clamped before it can reach an actuator — simultaneous heating and cooling raises energy use for no comfort benefit | `test_guardrails.py` |

## Simulation engine integration

| Failure | Detection | Fallback | Covering test |
|---|---|---|---|
| A requested variable/meter/actuator handle doesn't exist | EnergyPlus returns `-1` — it does not raise | `HandleRegistry` treats `-1` as `HandleResolutionError` at first resolution, rather than a valid handle silently yielding zeros forever | `test_handles.py` |
| A handle resolution failure would otherwise re-raise on every remaining timestep | First failed attempt vs. a successful one tracked separately in `HandleRegistry.ensure_resolved` | Fails once, loudly; every subsequent call is a silent no-op rather than a repeated raise-and-log that turns a config error into a multi-minute stall | `test_handles.py` |
| Sizing design-day environments produce full physics output before the real run period begins | `is_weather_run_period()` (`kind_of_sim() == 3`, empirically determined) | Non-run-period timesteps are gated out of telemetry publication entirely — they are neither warmup nor real data | `test_callbacks.py` |
| `exchange.minutes()` returns a value past 60 near an environment boundary | Observed directly against the real engine (e.g. `65`) | `(hour, minute)` rolled through `datetime.timedelta` rather than trusted field-by-field, correctly carrying overflow into day/month/year | `test_energyplus_backend.py::TestClockRollover` |
| The baseline IDF ships with weather-file run periods disabled | `SimulationControl.Run_Simulation_for_Weather_File_Run_Periods == "No"` | `prepare_idf` flips it to `"Yes"` (and sizing-period reporting to `"No"`) — otherwise EnergyPlus reports success having only run the sizing design days, with no exception anywhere | `test_prepare.py` |
| A profile's run-period dates never reach the IDF | Nothing previously read `simulation.run_period` back into the IDF's `RunPeriod` object | `prepare_idf` copies the active profile's begin/end month/day onto the IDF | `test_prepare.py` |
| `get_meter_handle("Electricity:Facility")` returns `-1` in EnergyPlus 25.2.0 specifically | Reproduced against the raw `pyenergyplus` API | `config/zones.yaml` uses `ElectricityNet:Facility` instead — numerically identical for this building | `test_handles.py`, confirmed via real runs in `RESULTS.md` |
| An exception escapes a callback body | Would cross into C++ and hard-crash the process with no traceback | Every callback body is wrapped at its top level (`EnergyPlusBackend._invoke_guarded`) | `test_callbacks.py`, `test_energyplus_backend.py` |
| An actuation bug in the control stack | Any exception from resolving/clamping a policy | Actuation is wrapped in its own `try/except` in `ReflexCallbacks`; a bug there costs that timestep's setpoints, not that timestep's telemetry too | `test_callbacks.py` |

## Self-healing (`agent/selfheal.py`)

| Failure | Detection | Fallback | Covering test |
|---|---|---|---|
| An IDF references a schedule name that doesn't exist | EnergyPlus's folded `.err` "item not found" pattern, matched by a closed alternation of known field labels (not an open character class — see below) | `diagnose()` extracts `(object_type, field, bad_value)`; `repair()` finds the existing schedule whose name is the longest prefix of the broken one and rewrites the reference | `test_agent_selfheal.py::TestDiagnose`, `TestRepairAndRunWithSelfHealing` (real engine) |
| The object name and field label in that `.err` text have no delimiter between them (both are free-form, space-containing text) | Two rounds of real bugs, found by running the real fixture against the real engine and reading its actual text | The classifier never tries to extract the object's *name* from the text at all — it searches the IDF for the instance whose field currently *equals* the bad value, which is unambiguous; the field label itself is matched against a closed alternation of known literals | `test_agent_selfheal.py` |
| A fault outside this module's narrow scope | `diagnose()` finds no matching pattern | Returns `None`; `run_with_self_healing` reports failure with a clear "no recognised, repairable fault" message rather than looping forever or guessing | `test_agent_selfheal.py::TestRunWithSelfHealing` |
| Repeated repair attempts don't converge | `agent.selfheal.max_retries` bound | Reports failure after the bound is reached, with every diagnosis made along the way | `test_agent_selfheal.py` |

## Analysis / comparison

| Failure | Detection | Fallback | Covering test |
|---|---|---|---|
| Comparing runs that faced different weather, run period, or engine version | `compare_runs`'s fairness check compares each manifest's `profile`/`weather_path`/`energyplus_version` | Raises `UnfairComparisonError` and refuses to produce a comparison — a kWh difference between mismatched runs measures nothing about the controller | `test_analysis_compare.py` |
| A zone-timestep with no measurable PMV (unconditioned zone, or a run that never requested the Fanger output) | `ZoneTelemetry.pmv is None` | Excluded from both the numerator and denominator of comfort scoring — never silently scored as comfortable | `test_analysis_comfort.py::test_none_pmv_excluded_not_scored_as_comfortable` |
| Total conditioned floor area isn't a literal IDF field (`autocalculate`) | N/A — geometry, not a simulated variable | Read from the `.eio` file's `Zone Information` records, with column positions read from that record's own header comment rather than assumed by position | `test_simulation_eio.py` (verified against a real run: exactly 511.16 m²) |
| numpy 2.5.0's bundled stub broke `mypy` under this project's Python 3.11 target | `mypy` hit a hard parse error on `numpy/__init__.pyi`'s unconditional PEP 695 `type` statement | `pyproject.toml` caps `numpy<2.5` | Confirmed by bisecting numpy releases; `make typecheck` |

## MCP / prompt boundary

| Failure | Detection | Fallback | Covering test |
|---|---|---|---|
| A tool's return type has a non-trivial Pydantic model and `from __future__ import annotations` is in effect everywhere | `inspect.signature()` on a `functools.partial`-bound tool without `eval_str=True` carries bare strings, not real classes | `mcp/server.py` always calls `inspect.signature(bound, eval_str=True)` when building each tool's schema | `test_mcp_server.py` |
| Untrusted `.err`/log/IDF text reaching the model | N/A — a design invariant, not a detected condition | Control characters stripped, size capped, before it ever becomes a tool result; never passed to `eval`/`exec`/`subprocess`/a shell | `test_errfile.py`, `test_mcp_sandbox.py` |
| A sandboxed file-read tool given a path outside its allowlist | `mcp.sandbox_roots` check | `SandboxViolationError`, refused before any filesystem access | `test_mcp_sandbox.py` |
