# AGENTS.md — `agent/`

Scoped rules for the cognitive tier. Root `AGENTS.md` still applies; this file adds what
only matters *here*. ⏳ marks files that do not exist yet.

## What lives here

| File | Role |
|---|---|
| ⏳ `orchestrator.py` | Worker thread + asyncio loop: cadence, triggers, the decide cycle. |
| ⏳ `llm.py` | Ollama/OpenAI-compatible client, retry, repair loop, circuit breaker, cache. |
| ⏳ `prompts.py` | Versioned Jinja2 templates from `config/prompts/`; version goes in the trace. |
| ⏳ `context.py` | Token budgeting and priority-ordered block degradation. |
| ⏳ `selfheal.py` | `.err` classifier → bounded patch/rerun loop. |
| ⏳ `trace.py` | Append-only `agent_trace.jsonl`, size-capped. |

## The boundary this layer must not cross

This layer runs on a **daemon worker thread**, never the main thread. It may not import
`simulation/` internals and may not call the EnergyPlus API — `import-linter` enforces the
first, and the second segfaults C++ with no traceback if you get it wrong. Everything in
and out goes through `bus/`: read `TelemetryBus`, write `PolicyStore`.

The LLM's *only* actuation surface is the fixed MCP tool allowlist in `mcp/`.

## Treat every model output as hostile

1. **Guardrails are enforced in `control/guardrails.py`, in code.** Not in the prompt. A
   prompt that says "never exceed 30 °C" is a suggestion; a clamp is a guarantee. If you
   find yourself adding a safety rule to a template, it belongs in the guardrail module.
2. **No LLM output ever reaches `eval`, `exec`, `subprocess`, or `os.system`,** and none is
   ever written to a file that later gets executed.
3. **Parse, don't trust.** Every response is validated against a Pydantic v2 model. Invalid
   → one bounded repair attempt with the validation error fed back → still invalid → fall
   back to the last good policy and log it. Never partially apply a malformed policy.
4. **Simulation logs, `.err` contents and IDF text are untrusted input.** They land in the
   prompt inside delimited, labelled blocks with control characters stripped, and are
   described to the model as data, never as instruction.

## Degradation is the normal path, not the error path

The simulation must complete even if the LLM never answers. Ordered fallbacks:

```
LLM times out / errors  → retry with backoff (llm.retry)
repeated failure        → circuit opens (llm.circuit_breaker) → skip cognitive tier
schema invalid twice    → keep last good policy
policy older than TTL   → PolicyStore expires it → reflex tier falls back to rule-based
```

A run that finishes on the reflex tier alone is a *degraded success*. A run that crashes
because the model was slow is a bug.

## Determinism

`temperature: 0` and `seed: 42` in `config/default.yaml`. The prompt template version, the
model name, the seed and the prompt hash all go into the trace, so a decision can be
replayed and argued about later. The response cache is keyed on the prompt hash — which
means a cache hit and a fresh call must be indistinguishable in the trace apart from a
`cached: true` flag.

## Cost control

An annual run is 52,560 timesteps; the model is invoked on a cadence (`agent.cadence_minutes`)
plus event triggers, with `agent.min_invocation_gap_minutes` as a floor and
`agent.max_tool_calls_per_invocation` as a ceiling. Context is budgeted to
`agent.context.max_input_tokens` by dropping whole blocks in `block_priority` order —
never by truncating mid-block, which produces confidently wrong readings.

## Testing this layer

⏳ `tests/fakes/fake_llm.py` scripts valid / malformed / absurd / timeout responses;
⏳ `tests/chaos/` kills the LLM mid-run and asserts the simulation still completes;
⏳ `tests/property/` throws arbitrary hypothesis-generated model output at the guardrails.
No test in this layer may require Ollama to be running.

```bash
.venv/bin/pytest tests/unit tests/property -q
make lint && make typecheck && make test
```
