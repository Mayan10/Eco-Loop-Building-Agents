# =========================================================================== #
# Eco-Loop — developer entry points.
#
# Every target here is referenced from AGENTS.md and verified by the
# `agent-file command check` in CI, so these two cannot silently drift apart.
# =========================================================================== #

.DEFAULT_GOAL := help
SHELL := /bin/bash

VENV    := .venv
PY      := $(VENV)/bin/python
ECOLOOP := $(VENV)/bin/ecoloop
PYTEST  := $(VENV)/bin/pytest
RUFF    := $(VENV)/bin/ruff
MYPY    := $(VENV)/bin/mypy

.PHONY: help setup doctor lint format typecheck test test-fast test-cov \
        prepare run-baseline run-rulebased run-agent run-all compare report \
        dashboard mcp demo demo-selfheal clean check

help: ## Show this help
	@echo "Eco-Loop — available targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #
setup: ## Create the venv and install the project with dev extras
	uv venv --python 3.12
	UV_HTTP_TIMEOUT=300 uv pip install -e ".[dev,dashboard]"
	@echo ""
	@echo "Setup complete. Next: make doctor"

doctor: ## Diagnose the environment (EnergyPlus, Ollama, models, inputs)
	$(ECOLOOP) doctor

# --------------------------------------------------------------------------- #
# Quality gates — a change is not done until all three pass
# --------------------------------------------------------------------------- #
lint: ## Lint and check formatting
	$(RUFF) check src tests
	$(RUFF) format --check src tests

format: ## Auto-format and apply safe lint fixes
	$(RUFF) format src tests
	$(RUFF) check --fix src tests

typecheck: ## Run mypy --strict over src/
	$(MYPY) src/ecoloop

test: ## Run the full test suite (no EnergyPlus or Ollama required)
	$(PYTEST)

test-fast: ## Run only the fast unit and property tests
	$(PYTEST) tests/unit tests/property -q

test-cov: ## Run tests with a coverage report
	$(PYTEST) --cov --cov-report=term-missing --cov-report=xml

check: lint typecheck test ## Everything CI runs, locally

# --------------------------------------------------------------------------- #
# Simulation runs
#
# `--profile fast` (2 weeks) is the default for iteration. Annual runs use
# `--profile full` and take substantially longer — be deliberate about them.
# --------------------------------------------------------------------------- #
prepare: ## Inject required Output:Variables, Fanger comfort and CO2 into the baseline IDF
	$(ECOLOOP) prepare

run-baseline: ## Uncontrolled reference run (fast profile)
	$(ECOLOOP) run baseline --profile fast

run-rulebased: ## Deterministic benchmark controller (fast profile)
	$(ECOLOOP) run rulebased --profile fast

run-agent: ## Full LLM closed loop (fast profile)
	$(ECOLOOP) run agent --profile fast

run-all: ## All three controllers over the annual period, then compare
	$(ECOLOOP) run all --profile full

# --------------------------------------------------------------------------- #
# Analysis & presentation
# --------------------------------------------------------------------------- #
compare: ## Metric delta table across the most recent runs
	$(ECOLOOP) compare --latest

report: ## Self-contained static HTML report (opens with no server, no internet)
	$(ECOLOOP) report --latest

dashboard: ## Launch the Streamlit dashboard
	$(ECOLOOP) dashboard

mcp: ## Start the MCP server on stdio (connect Claude Desktop / Claude Code)
	$(ECOLOOP) mcp serve --transport stdio

# --------------------------------------------------------------------------- #
# Demos — designed so one unedited screen recording satisfies the video brief
# --------------------------------------------------------------------------- #
demo: ## Live TUI closed-loop demo (<= 3 minutes, recordable)
	$(ECOLOOP) run agent --profile demo --live

demo-selfheal: ## Agent autonomously repairs a deliberately broken IDF
	$(ECOLOOP) selfheal --idf models/faults/broken_thermostat.idf

# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #
clean: ## Remove run artefacts and tool caches (keeps the venv)
	rm -rf results/* models/generated/* .pytest_cache .mypy_cache .ruff_cache \
	       .hypothesis htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	@echo "Cleaned. The venv and pulled models were left alone."
