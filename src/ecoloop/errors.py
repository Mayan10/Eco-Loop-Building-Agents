"""Typed exception hierarchy for Eco-Loop.

Every failure in the system raises a subclass of :class:`EcoLoopError`. Bare
``except:`` and ``except Exception: pass`` are banned repository-wide (see
``AGENTS.md``): every handler either re-raises a typed error from this module or
takes a documented recovery action and logs it.

The hierarchy mirrors the architectural layers so that a caller can decide how
much to catch:

* :class:`EnergyPlusError` — anything originating in the simulation engine.
* :class:`AgentError` — anything originating in the cognitive layer.
* :class:`PolicyError` — control-policy validity and lifetime problems.
* :class:`ConfigError` — malformed or missing configuration.

Each error carries an optional ``context`` mapping that is emitted verbatim into
the structured log record, so a failure is debuggable from the JSON log alone.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AgentError",
    "CircuitOpenError",
    "ConfigError",
    "EcoLoopError",
    "EnergyPlusError",
    "EnergyPlusNotFoundError",
    "GuardrailViolation",
    "HandleResolutionError",
    "IDFValidationError",
    "LLMUnavailableError",
    "PolicyError",
    "PolicyExpiredError",
    "SandboxViolationError",
    "SchemaValidationError",
    "SelfHealExhaustedError",
    "SimulationFatalError",
    "TokenBudgetExceededError",
    "UnfairComparisonError",
]


class EcoLoopError(Exception):
    """Root of every Eco-Loop exception.

    Args:
        message: Human-readable description of what went wrong.
        **context: Arbitrary structured fields attached to the error and
            emitted into the structured log. Prefer machine-greppable keys
            (``zone``, ``handle``, ``run_id``) over prose.
    """

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context

    def __str__(self) -> str:
        """Render the message followed by any structured context."""
        if not self.context:
            return self.message
        rendered = " ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} [{rendered}]"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class ConfigError(EcoLoopError):
    """Configuration is missing, malformed, or internally inconsistent."""


# --------------------------------------------------------------------------- #
# Simulation engine
# --------------------------------------------------------------------------- #
class EnergyPlusError(EcoLoopError):
    """Base class for failures originating in the EnergyPlus engine."""


class EnergyPlusNotFoundError(EnergyPlusError):
    """The EnergyPlus installation or its ``pyenergyplus`` package is unavailable.

    ``pyenergyplus`` is not distributed on PyPI — it ships inside the EnergyPlus
    install directory and must be injected onto ``sys.path``. This error means
    neither ``ENERGYPLUS_DIR`` nor auto-discovery located a usable install.
    """


class HandleResolutionError(EnergyPlusError):
    """A sensor or actuator handle resolved to ``-1``.

    EnergyPlus returns ``-1`` rather than raising when a variable, meter, or
    actuator does not exist. Treating that as a valid handle silently produces
    zeros forever, so it is escalated to a hard configuration error that names
    the exact component/control/key triple that failed.
    """


class SimulationFatalError(EnergyPlusError):
    """EnergyPlus terminated abnormally or returned a non-zero exit code."""


class IDFValidationError(EnergyPlusError):
    """An IDF object, field, or patch failed validation before being written."""


# --------------------------------------------------------------------------- #
# Cognitive layer
# --------------------------------------------------------------------------- #
class AgentError(EcoLoopError):
    """Base class for failures originating in the LLM cognitive layer."""


class LLMUnavailableError(AgentError):
    """The LLM endpoint could not be reached, timed out, or returned an error."""


class CircuitOpenError(LLMUnavailableError):
    """The circuit breaker guarding the LLM endpoint is open.

    Raised without attempting a network call, so a persistently failing endpoint
    costs a fast rejection rather than a timeout on every invocation.
    """


class SchemaValidationError(AgentError):
    """The model's response did not satisfy the Pydantic schema after repair."""


class TokenBudgetExceededError(AgentError):
    """Assembled prompt exceeds the configured input-token budget.

    Raised *before* dispatch. Context-block degradation is attempted first; this
    error means even the minimal block set does not fit.
    """


class SelfHealExhaustedError(AgentError):
    """The self-healing loop hit its retry cap without resolving the fault."""


# --------------------------------------------------------------------------- #
# Control policy
# --------------------------------------------------------------------------- #
class PolicyError(EcoLoopError):
    """Base class for control-policy validity and lifetime failures."""


class GuardrailViolation(PolicyError):
    """A proposed policy value violated a hard safety guardrail.

    Guardrails normally *clamp* rather than raise — clamping keeps the loop
    running and feeds the correction back to the model. This is raised only for
    violations that cannot be repaired into a safe value.
    """


class PolicyExpiredError(PolicyError):
    """The active policy passed its TTL and no replacement arrived."""


# --------------------------------------------------------------------------- #
# Security & analysis
# --------------------------------------------------------------------------- #
class SandboxViolationError(EcoLoopError):
    """A tool argument resolved to a path outside the allowlisted sandbox."""


class UnfairComparisonError(EcoLoopError):
    """Two runs cannot be compared because their experimental fingerprints differ.

    Comparing runs with different weather, geometry, run period, timestep, or
    EnergyPlus version produces a meaningless savings figure, so the comparison
    is refused rather than reported with a caveat.
    """
