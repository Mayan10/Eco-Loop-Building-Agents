# AGENTS.md — `agent/`

Scoped rules for the cognitive tier. Root `AGENTS.md` still applies; this file adds what
only matters *here*. ⏳ marks files that do not exist yet.

## What lives here

| File | Role |
|---|---|
| `orchestrator.py` | One reasoning cycle: context → prompt → tool-calling loop → decision. |
| `llm.py` | Ollama chat client: retry, circuit breaker, prompt-hash cache, the `LLMClient` protocol. |
| `prompts.py` | Renders the versioned Jinja2 template in `config/prompts/`; version goes in the trace. |
| `context.py` | Ten named context blocks, priority-ordered, budget-fitted by dropping from the tail. |
| `selfheal.py` | `.err` classifier → bounded patch/rerun loop; `ecoloop selfheal --idf <path>` drives it. |

**Note on `trace.py`:** it lives in `mcp/trace.py`, not here. Every MCP tool call needs
tracing regardless of who calls it (a human via Claude Desktop, this layer's tool-calling
loop, a future test harness), so the trace writer belongs with the server, not the one
caller this phase happens to add. `orchestrator.py` still writes to it — see "Determinism"
below — it just doesn't own it.

## Verified against a real model, not just unit tests

`qwen2.5:3b-instruct`, served locally by Ollama, correctly called `get_zone_telemetry` with
the right argument on the first try, and — driven end to end through
`CognitiveOrchestrator.run_cycle()` against a hand-built `ServerState` with one hot,
uncomfortable zone (PMV 1.1) — read that state, recognised the comfort violation, and called
`propose_policy` with a real, correctly-reasoned justification. This is not a claim from the
Ollama docs; it is a run that happened in this repository.

## The boundary this layer must not cross

This layer runs off the main thread (a background worker, once a future CLI `run` subcommand
wires it to a live simulation — see the landmine below), never the main thread. It may
not import `simulation/` internals and may not call the EnergyPlus API directly —
`import-linter` enforces the first, and the second segfaults C++ with no traceback if you get
it wrong. Everything in and out goes through `bus/`: read `TelemetryBus`, write `PolicyStore`.
Importing `mcp/` is fine and expected — the orchestrator is an MCP *client* of the in-process
server, calling `server.call_tool()` the same way an external client would.

The LLM's *only* actuation surface is `propose_policy` / `request_zone_setpoint` in `mcp/`.

## Treat every model output as hostile

1. **Guardrails are enforced in `control/guardrails.py`, in code.** Not in the prompt. The
   system prompt tells the model this explicitly, so it reasons about comfort and energy
   rather than trying to satisfy the clamp itself — but the clamp runs regardless of what it
   says.
2. **No LLM output ever reaches `eval`, `exec`, `subprocess`, or `os.system`,** and none is
   ever written to a file that later gets executed. Tool-call arguments only ever reach a
   Pydantic model (`mcp/models.py`) or a rejected-zone-name check — never a shell.
3. **A malformed tool call is fed back to the model as a tool-result error, not silently
   dropped.** `orchestrator._call_tool` catches any exception a tool raises (FastMCP's own
   argument validation included) and returns it as an `"error: ..."` string in the next
   message, so the model sees its own mistake and — inside the same cycle's tool-call budget
   — can correct it. There is no separate repair loop; the budget itself is the bound.
4. **Simulation logs, `.err` contents and IDF text are untrusted input.** They reach the
   model only through `mcp/tools_introspect.py`'s `get_recent_errors` /
   `read_sandboxed_text_file`, both of which already strip control characters and cap size
   before this layer ever sees the result.

## Degradation is the normal path, not the error path

The simulation must complete even if the LLM never answers. Ordered fallbacks:

```
LLM times out / errors  → retry with backoff (llm.retry)
repeated failure        → circuit opens (llm.circuit_breaker) → orchestrator's cycle aborts cleanly
no tool calls at all    → a valid decision ("hold"), not an error
policy older than TTL   → PolicyStore expires it → reflex tier falls back to rule-based
```

A run that finishes on the reflex tier alone is a *degraded success*. A run that crashes
because the model was slow is a bug. `orchestrator.run_cycle()` never raises for any of the
above — it returns a short summary string either way, and a `CircuitOpenError` /
`LLMUnavailableError` from `.chat()` ends the cycle instead of propagating.

## Determinism

`temperature: 0` and `seed: 42` in `config/default.yaml`. The prompt template version, model
name, seed and temperature go into a `cognitive_cycle` trace entry
(`orchestrator._record_cycle_trace`) once per cycle — separate from the per-tool-call entries
`mcp/server.py` writes for every individual tool invocation, since those record what a tool
returned and this records what the *cycle* decided and with what. The response cache
(`llm.py`) is keyed on a hash of the model, messages, tools, temperature and seed; a cache hit
is indistinguishable from a fresh call in the trace apart from `ChatResponse.cached`.

## Cost control

An annual run is 52,560 timesteps; the model is invoked on a cadence (`agent.cadence_minutes`)
plus event triggers (not yet wired to the orchestrator — see the landmine below), with
`agent.min_invocation_gap_minutes` as a floor and `agent.max_tool_calls_per_invocation` as a
ceiling on one cycle's tool calls. Context is budgeted to `agent.context.max_input_tokens` by
dropping whole blocks in `block_priority` order — never by truncating mid-block, which would
hand the model a confidently-wrong half-sentence instead of a visible omission.

## Landmines specific to this layer

- **The CLI's `run` subcommand does not exist yet.** Every module here is built,
  tested, and proven against a real LLM via a hand-constructed `ServerState` — the
  worker-thread wiring (its own asyncio loop running `run_cycle()` on a cadence, alongside
  EnergyPlus on the main thread, with cadence/trigger scheduling actually implemented) is a
  distinct integration task. `agent.triggers` (pmv_excursion, demand_approach, etc.) are
  validated config with no code reading them yet, for the same reason.
- **Ollama's tool-calling response shape is `message.tool_calls[].function.{name,arguments}`,
  and `id` is present but not always meaningful** — `llm.py`'s parser treats it as optional
  (`tc.get("id", "")`) rather than assuming every implementation populates it usefully.
- **`OllamaClient` is typed against, and callers should depend on, the `LLMClient` protocol**
  (structural: anything with an async `chat()` matching the shape), not the concrete class —
  this is what lets `tests/fakes/fake_llm.py`'s scripted doubles stand in for a real endpoint
  without inheriting from it, the same seam `SimulationBackend` provides for the fake
  EnergyPlus double.
- **`selfheal.py`'s classifier never tries to extract an object's *name* from EnergyPlus's
  error text.** The folded "item not found" message has no delimiter between a free-form
  object name and a free-form field label — both are words separated by spaces — so any
  open-ended capture group placed next to the other reliably swallows it. The exact broken
  object is instead found by searching the IDF for the instance whose field currently equals
  the diagnosed bad value, and the field label itself is matched against a closed
  alternation of known literals, not an open character class. This was found by running the
  real `broken_thermostat.idf` fixture against the real engine twice — once per group that
  turned out to have the same bug.

## Testing this layer

`tests/fakes/fake_llm.py` provides `ScriptedLLM` (queued responses), `FailingLLM`,
`TimeoutLLM`, and `MalformedToolCallLLM` (each always raises the failure mode it's named
for) — anything satisfying `LLMClient` works. `tests/unit/test_agent_llm.py` exercises the
real HTTP-layer retry/circuit-breaker/cache logic against `httpx.MockTransport`, so none of
it needs Ollama running. `tests/unit/test_agent_selfheal.py::TestDiagnose` covers the
classifier regex against a hand-built `ErrFileSummary` with no engine needed;
`TestRepairAndRunWithSelfHealing` is `@pytest.mark.energyplus` and runs the real
`models/faults/broken_thermostat.idf` end to end. ⏳ `tests/chaos/` (kills the LLM mid-run via
the failing variants, asserts the simulation still completes) and ⏳ `tests/property/`
(arbitrary hypothesis-generated tool-call arguments against the guardrail chain) are still
open — self-healing turned out not to need either, since the fault it recovers from is a
deterministic input error caught at parse time, not a mid-run LLM failure.

```bash
.venv/bin/pytest tests/unit -q -k "agent_ or mcp_"
make lint && make typecheck && make test
```
