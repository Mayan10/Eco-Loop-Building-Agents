@AGENTS.md

Claude-Code-specific notes only. Everything substantive is in `AGENTS.md` — do not
duplicate it here; duplicated instructions drift and then contradict each other.

## Working style

- **Use Plan Mode** for any change touching more than two files, or any change to the
  control loop, the guardrails, or the thread boundary.
- **Dispatch to a subagent**: long simulation runs, visual QA of generated charts, and
  broad codebase search. Keep the main context clean for the control logic.
- `--profile fast` (2-week) is the default while iterating. **Confirm with the user before
  starting a `--profile full` annual run** — it is slow and they may not want to wait.

## Never commit

`results/`, `.env`, `models/generated/`, `models/weather/*.epw`, or any EnergyPlus output
directory. All are in `.gitignore`; do not `git add -f` them.

## Chat with the running building

The MCP server exposes the simulation as typed tools, so you can query it directly. Add to
`~/Library/Application Support/Claude/claude_desktop_config.json` (or `.mcp.json` for
Claude Code):

```json
{
  "mcpServers": {
    "ecoloop": {
      "command": "/absolute/path/to/eco-loop/.venv/bin/ecoloop",
      "args": ["mcp", "serve", "--transport", "stdio"]
    }
  }
}
```

Questions that work once a run exists:

1. "What's the PMV in every zone right now, and is anything outside ASHRAE 55?"
2. "Why did you raise the cooling set-point at 2 pm?"
3. "Show me the last three Severe errors from the simulation log."
4. "Compare the agent run against the baseline — kWh and unmet hours."
5. "Which zone is the worst comfort offender, and what would you do about it?"
