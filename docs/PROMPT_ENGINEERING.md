# Prompt Engineering

How Eco-Loop builds the cognitive tier's prompt, keeps it inside a fixed
token budget under real telemetry load, and handles untrusted or lengthy
simulation output. Code references throughout are to `agent/context.py`,
`agent/prompts.py`, and `config/prompts/system_v1.j2`.

## 1. The system prompt

`config/prompts/system_v1.j2` is a single versioned Jinja2 template. The
version string (`v1`, from `agent/prompts.py`'s `CURRENT_PROMPT_VERSION`) is
recorded in every cognitive-cycle trace entry — a prompt change is
attributable to a specific past run, not silently retroactive.

The prompt is deliberately short on rules and long on framing:

- **The actuation surface is named explicitly** (`propose_policy`,
  `request_zone_setpoint`) and the model is told, directly, that every hard
  safety limit is enforced in code after it proposes — "you cannot violate
  them and do not need to reason about satisfying them precisely." This is
  not a courtesy; it is what lets the model spend its reasoning budget on
  comfort/energy trade-offs instead of on trying to compute a clamp it
  cannot see and does not control. The clamp runs regardless of what the
  model says — see `AGENTS.md` invariant #2, "guardrails are enforced in
  code, never in the prompt."
- **Success is stated as an ordered pair, not a weighted sum**: comfort
  first, energy second, subject to comfort. "There is no such thing as
  saving energy at the cost of comfort in this project" is stated verbatim,
  because a plausible-sounding but wrong framing ("balance comfort and
  energy") invites exactly the trade-off this project's success criterion
  explicitly forbids.
- **"Holding is a valid decision"** is stated explicitly, at the end of the
  prompt: "if nothing needs to change, say so briefly and do not call an
  actuate tool at all." Without this, a model under instruction-following
  pressure tends to invent a small, unjustified change just to look
  active — the "few_shot" block (§2) reinforces this with a worked example
  in the opposite direction (relaxing a setpoint that's already comfortable
  rather than tightening one that doesn't need it).

## 2. Ten context blocks, one priority order

`agent/context.py`'s `build_context()` renders ten named blocks, in the
order `agent.context.block_priority` declares:

```
zone_summary → comfort_status → active_policy → energy_demand → alerts
→ grid_signal → reflection → weather_lookahead → occupancy_forecast → few_shot
```

This order is a genuine priority ranking, not an arbitrary list: it is *also*
the order blocks are dropped from when the assembled context would exceed
`agent.context.max_input_tokens`, dropped from the **tail** first. So
`zone_summary` and `comfort_status` — the two blocks the model needs to make
any decision at all — are the last two things ever dropped, while
`few_shot` (a worked reasoning example, useful but not load-bearing) is the
first thing sacrificed under pressure.

Each block is capped individually to `agent.context.max_tool_result_tokens`
*before* the collective budget check runs, with an explicit truncation
notice appended (`"... [truncated to fit the token budget]"`) rather than a
silent cut. **Truncation only ever happens at a block boundary, never
mid-sentence** — a half-sentence reads as a confidently complete (and wrong)
fact; a block that says "I was cut off" reads as a visible gap. Concretely:
`_fit_to_budget()` drops whole blocks from the tail of the priority list
until the running total fits, and each individual block's own
`max_tool_result_tokens` cap is applied by truncating at a character
boundary derived from the token estimate, then appending the notice — the
model is never handed a data block that looks complete but silently isn't.

`occupancy_forecast` is present in the priority list and enforced by the
budget machinery like every other block, but currently renders a fixed
disclosure string (`_OCCUPANCY_FORECAST_UNAVAILABLE`) rather than real data:
there is no occupancy-schedule lookahead to query yet, only each zone's
*current* `occupancy_fraction`. This is disclosed to the model in the block
itself, not silently omitted — a documented gap, not a bug.

## 3. Token budgeting is an estimate, not a real tokenizer

Every token count in this layer is `len(text) / chars_per_token`
(`agent.context.chars_per_token`, `3.6` by default), not a real tokenizer
call. This is a **deliberate, disclosed approximation**: config's own
comment calls it "rough." Pulling in a real tokenizer dependency for a
budgeting heuristic that only needs to be roughly right — the actual hard
limit enforcement (`llm.num_ctx`, the model's real context window) sits well
above the padding this estimate leaves — would be exactly the kind of
premature precision this project's "config, not constants" philosophy
argues against elsewhere.

Budgeting is enforced **before dispatch, not discovered afterward**: the
assembled prompt is fit to `max_input_tokens` in Python before the HTTP call
is made, not sent optimistically and truncated server-side or retried on a
context-length error.

## 4. Untrusted input: `.err`, logs, and IDF text

Anything the model reads that originated from a running simulation —
`.err` content, recent log lines, IDF text via `read_sandboxed_text_file` —
is treated as **data, never instruction**, per `AGENTS.md` invariant #3. In
practice this means:

- Control characters are stripped and total size is capped
  (`simulation.output.max_err_bytes`) before `.err` text ever reaches a tool
  result — `simulation/errfile.py` does this at parse time, not at prompt
  time, so there is exactly one place this discipline can be forgotten.
- `read_sandboxed_text_file` refuses any path outside a fixed allowlist of
  roots (`mcp.sandbox_roots`) — a model that has read a `.err` line
  mentioning a suspicious-looking path cannot use that path as an argument
  to actually read arbitrary filesystem content.
- None of this text is ever concatenated into something that gets
  `eval`'d, `exec`'d, or passed to a shell. The only thing any of it does is
  become part of a chat message's content.

## 5. The forecast oracle is a prompt-context asymmetry, not a secret

`weather_lookahead` (backed by `get_weather_forecast`) is real forecast data,
not synthetic — it reads ahead in the same EPW file driving the simulation,
capped at `simulation.output.max_forecast_horizon_hours` (72h). This is
disclosed in `docs/ARCHITECTURE.md` §5 as a deliberate asymmetry between the
agent controller and the deterministic ones: the prompt context this block
contributes is information `baseline`/`rulebased` structurally cannot use,
by design, not an engineering oversight.

## 6. Determinism, and what it actually buys

`llm.temperature: 0.0` and `llm.seed: 42` are set for every call. The
response cache (`llm.py`, keyed on a hash of model, messages, tools,
temperature, and seed) is what actually makes a specific past run
*replayable* offline and identically on every take — useful for the demo
recording and for regression tests — not a claim that the underlying model
is bit-for-bit reproducible across machines or Ollama versions (see
`ARCHITECTURE.md` §7). `ChatResponse.cached` distinguishes a cache hit from
a fresh call in the trace, so a replayed run is still auditable as such.
