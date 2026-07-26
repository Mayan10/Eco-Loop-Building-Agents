"""Layered, typed configuration for Eco-Loop.

Configuration is assembled in increasing order of precedence:

1. ``config/default.yaml`` — the single source of truth for every tunable.
2. A profile overlay from ``config/profiles/`` (``--profile fast|full|demo``).
3. An explicit ``--config path.yaml``.
4. Environment variables prefixed ``ECOLOOP_``, using ``__`` for nesting
   (``ECOLOOP_LLM__MODEL=llama3.1:8b-instruct``).
5. ``${ENV_VAR}`` / ``${ENV_VAR:-default}`` interpolation inside string values.

Every section is a Pydantic v2 model, so a malformed config fails at load time
with a precise field path rather than surfacing as an ``AttributeError`` forty
minutes into an annual simulation.

Invariant (see ``AGENTS.md``): no threshold, limit, or cadence may be hard-coded
in ``src/``. If a number needs tuning, it belongs in ``default.yaml`` and is
reached through this module.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ecoloop.errors import ConfigError

__all__ = [
    "AgentSettings",
    "AnalysisSettings",
    "BusSettings",
    "ComfortSettings",
    "ControlSettings",
    "EcoLoopSettings",
    "GuardrailSettings",
    "LLMSettings",
    "MCPSettings",
    "SimulationSettings",
    "load_settings",
]

# Repository root, resolved from this file's location: src/ecoloop/config.py
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH: Path = PROJECT_ROOT / "config" / "default.yaml"
PROFILES_DIR: Path = PROJECT_ROOT / "config" / "profiles"

# ${VAR} or ${VAR:-fallback}
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class _Base(BaseModel):
    """Base for every config section: strict, and rejects unknown keys.

    ``extra="forbid"`` turns a typo in ``default.yaml`` into a load-time error
    naming the offending key, instead of a silently ignored setting.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, validate_assignment=True)


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
class ProjectSettings(_Base):
    """Top-level project paths and disk safety limits."""

    name: str = "eco-loop"
    results_dir: Path = Path("results")
    min_free_disk_gib: float = Field(default=5.0, ge=0.0)


class RunPeriod(_Base):
    """Simulation run period, mirroring the IDF ``RunPeriod`` object."""

    begin_month: int = Field(ge=1, le=12)
    begin_day: int = Field(ge=1, le=31)
    end_month: int = Field(ge=1, le=12)
    end_day: int = Field(ge=1, le=31)

    @property
    def is_annual(self) -> bool:
        """Whether this period spans a full calendar year."""
        return (self.begin_month, self.begin_day, self.end_month, self.end_day) == (1, 1, 12, 31)

    def describe(self) -> str:
        """Return a compact ``MM-DD..MM-DD`` label for manifests and logs."""
        return (
            f"{self.begin_month:02d}-{self.begin_day:02d}..{self.end_month:02d}-{self.end_day:02d}"
        )


class OutputSettings(_Base):
    """Controls on EnergyPlus output volume."""

    max_eso_mib: int = Field(default=2048, gt=0)
    keep_eplus_out: bool = True
    dump_available_api_data_on_error: bool = True
    max_err_bytes: int = Field(default=2_097_152, gt=0)
    max_forecast_horizon_hours: int = Field(default=72, gt=0)


class SimulationSettings(_Base):
    """EnergyPlus engine location, model inputs, and run period."""

    backend: Literal["energyplus", "fake"] = "energyplus"
    energyplus_dir: Path | None = None
    min_version: str = "23.2.0"
    max_version: str = "26.99.0"
    idf_baseline: Path
    idf_prepared: Path
    weather: Path
    timesteps_per_hour: int = Field(default=6, ge=1, le=60)
    run_period: RunPeriod
    skip_warmup: bool = True
    output: OutputSettings = Field(default_factory=OutputSettings)

    @model_validator(mode="after")
    def _check_timestep_divides_hour(self) -> SimulationSettings:
        """EnergyPlus requires the zone timestep to divide 60 minutes evenly."""
        if 60 % self.timesteps_per_hour != 0:
            raise ValueError(
                f"timesteps_per_hour={self.timesteps_per_hour} does not divide 60 evenly; "
                "EnergyPlus requires 1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30 or 60"
            )
        return self

    @property
    def timestep_minutes(self) -> float:
        """Length of one zone timestep in minutes."""
        return 60.0 / self.timesteps_per_hour


class ComfortSettings(_Base):
    """Thermal comfort and indoor-air-quality targets (ASHRAE 55 / 62.1)."""

    standard: Literal["ashrae55"] = "ashrae55"
    pmv_occupied_min: float = -0.5
    pmv_occupied_max: float = 0.5
    pmv_tolerance_band: float = Field(default=0.7, gt=0.0)
    ppd_max_pct: float = Field(default=10.0, gt=0.0, le=100.0)
    co2_max_ppm: float = Field(default=1000.0, gt=0.0)
    outdoor_co2_ppm: float = Field(default=400.0, gt=0.0)
    occupied_threshold_fraction: float = Field(default=0.05, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_band(self) -> ComfortSettings:
        """The PMV comfort band must be non-degenerate."""
        if self.pmv_occupied_min >= self.pmv_occupied_max:
            raise ValueError("pmv_occupied_min must be strictly below pmv_occupied_max")
        return self


class GuardrailSettings(_Base):
    """Hard safety envelope enforced in code, never in the prompt.

    These bounds are applied in the reflex layer after every policy read. Even a
    compromised or hallucinating model cannot drive a zone outside them.
    """

    heating_setpoint_min_c: float
    heating_setpoint_max_c: float
    cooling_setpoint_min_c: float
    cooling_setpoint_max_c: float
    min_deadband_c: float = Field(gt=0.0)
    max_setpoint_change_per_hour_c: float = Field(gt=0.0)
    min_hold_minutes: float = Field(ge=0.0)
    zone_temp_alarm_min_c: float
    zone_temp_alarm_max_c: float
    min_lighting_fraction_occupied: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_envelope(self) -> GuardrailSettings:
        """Verify the guardrail envelope is self-consistent and satisfiable."""
        if self.heating_setpoint_min_c >= self.heating_setpoint_max_c:
            raise ValueError("heating_setpoint_min_c must be below heating_setpoint_max_c")
        if self.cooling_setpoint_min_c >= self.cooling_setpoint_max_c:
            raise ValueError("cooling_setpoint_min_c must be below cooling_setpoint_max_c")
        if self.zone_temp_alarm_min_c >= self.zone_temp_alarm_max_c:
            raise ValueError("zone_temp_alarm_min_c must be below zone_temp_alarm_max_c")
        # The tightest legal pair must still satisfy the deadband, otherwise
        # every policy would be unsatisfiable and clamping could never converge.
        widest = self.cooling_setpoint_max_c - self.heating_setpoint_min_c
        if widest < self.min_deadband_c:
            raise ValueError(
                f"guardrail envelope ({widest:.1f} °C) is narrower than "
                f"min_deadband_c ({self.min_deadband_c:.1f} °C); no policy could ever validate"
            )
        return self


class RuleBasedSettings(_Base):
    """Tunables for the deterministic Guideline-36-flavoured benchmark controller."""

    setback_enabled: bool = True
    deadband_widening_unoccupied_c: float = Field(default=2.0, ge=0.0)
    economiser_enabled: bool = True
    economiser_max_oat_c: float = 18.0
    economiser_min_oat_c: float = 4.0
    economiser_setpoint_shift_c: float = Field(default=1.0, gt=0.0)
    supply_air_reset_enabled: bool = True
    supply_air_min_c: float = 12.0
    supply_air_max_c: float = 18.0
    precool_enabled: bool = True
    precool_lead_hours: float = Field(default=2.0, ge=0.0)


class ControlSettings(_Base):
    """Controller selection, fallback set-points, and demand limits."""

    controller: Literal["baseline", "rulebased", "agent"] = "agent"
    default_heating_setpoint_c: float
    default_cooling_setpoint_c: float
    unoccupied_heating_setpoint_c: float
    unoccupied_cooling_setpoint_c: float
    demand_cap_kw: float = Field(gt=0.0)
    demand_window_minutes: float = Field(gt=0.0)
    demand_trigger_fraction: float = Field(gt=0.0, le=1.0)
    rulebased: RuleBasedSettings = Field(default_factory=RuleBasedSettings)


class PolicyBusSettings(_Base):
    """Policy lifetime settings governing graceful degradation."""

    default_ttl_minutes: float = Field(gt=0.0)
    max_age_minutes: float = Field(gt=0.0)

    @model_validator(mode="after")
    def _check_ages(self) -> PolicyBusSettings:
        """A policy may not be refused as stale before its own TTL elapses."""
        if self.max_age_minutes < self.default_ttl_minutes:
            raise ValueError("max_age_minutes must be >= default_ttl_minutes")
        return self


class BusSettings(_Base):
    """Telemetry ring buffer and policy store settings."""

    telemetry_capacity: int = Field(gt=0)
    aggregate_window_minutes: float = Field(gt=0.0)
    policy: PolicyBusSettings


class TriggerSettings(_Base):
    """Event triggers that can invoke the cognitive layer off-cadence."""

    pmv_excursion: bool = True
    zone_temp_limit: bool = True
    demand_approach: bool = True
    carbon_spike: bool = True
    severe_error: bool = True
    carbon_spike_gco2_per_kwh: float = Field(default=450.0, gt=0.0)


class ContextSettings(_Base):
    """Token budgeting and context-block degradation order."""

    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    block_priority: tuple[str, ...]
    max_tool_result_tokens: int = Field(gt=0)
    chars_per_token: float = Field(gt=0.0)


class SelfHealSettings(_Base):
    """Bounded retry policy for the autonomous error-repair loop."""

    enabled: bool = True
    max_retries: int = Field(default=3, ge=0)
    backoff_base_seconds: float = Field(default=2.0, gt=0.0)
    backoff_max_seconds: float = Field(default=30.0, gt=0.0)


class AgentSettings(_Base):
    """Cognitive-layer cadence, triggers, budgets, and self-healing."""

    enabled: bool = True
    cadence_minutes: float = Field(gt=0.0)
    min_invocation_gap_minutes: float = Field(ge=0.0)
    max_tool_calls_per_invocation: int = Field(gt=0)
    triggers: TriggerSettings = Field(default_factory=TriggerSettings)
    context: ContextSettings
    selfheal: SelfHealSettings = Field(default_factory=SelfHealSettings)
    live_pacing_seconds_per_timestep: float = Field(default=0.0, ge=0.0)
    """Deliberate delay added per timestep only in ``--live`` mode, so a human
    watching the Rich dashboard sees the simulation progress rather than it
    finishing before the cognitive worker's first LLM round trip returns.
    Zero (the default) for every non-live run — EnergyPlus runs as fast as
    the engine allows."""


class RetrySettings(_Base):
    """Jittered exponential backoff for LLM transport."""

    max_attempts: int = Field(default=3, ge=1)
    backoff_base_seconds: float = Field(default=1.0, gt=0.0)
    backoff_max_seconds: float = Field(default=20.0, gt=0.0)
    jitter_seconds: float = Field(default=0.5, ge=0.0)


class CircuitBreakerSettings(_Base):
    """Circuit breaker guarding the LLM endpoint."""

    failure_threshold: int = Field(default=5, ge=1)
    cooldown_seconds: float = Field(default=60.0, gt=0.0)
    half_open_max_calls: int = Field(default=1, ge=1)


class CacheSettings(_Base):
    """Prompt-hash-keyed response cache enabling offline replay."""

    enabled: bool = True
    dir: Path = Path(".ollama_cache")
    max_entries: int = Field(default=5000, gt=0)


class LLMSettings(_Base):
    """Open-source LLM endpoint configuration.

    Any OpenAI-compatible server (Ollama, vLLM, llama.cpp, LM Studio) works by
    changing only this block — there is no hard-coded provider anywhere in the
    codebase.
    """

    provider: Literal["ollama", "openai_compatible"] = "ollama"
    base_url: str
    model: str
    fallback_model: str | None = None
    api_key_env: str = "ECOLOOP_LLM_API_KEY"
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    seed: int = 42
    top_p: float = Field(default=1.0, gt=0.0, le=1.0)
    num_ctx: int = Field(default=8192, gt=0)
    request_timeout_seconds: float = Field(gt=0.0)
    connect_timeout_seconds: float = Field(gt=0.0)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    circuit_breaker: CircuitBreakerSettings = Field(default_factory=CircuitBreakerSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)

    @property
    def api_key(self) -> str | None:
        """Read the API key from its environment variable, if set.

        Returns ``None`` for endpoints that do not authenticate, such as Ollama.
        Secrets are never stored in config files.
        """
        return os.environ.get(self.api_key_env) or None


class MCPSettings(_Base):
    """MCP server transport and filesystem sandbox."""

    transport: Literal["stdio", "http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = Field(default=8765, gt=0, lt=65536)
    server_name: str = "ecoloop"
    sandbox_roots: tuple[Path, ...]
    max_tool_result_bytes: int = Field(gt=0)


class SignalSettings(_Base):
    """Grid carbon-intensity and tariff signal sources."""

    carbon_intensity_csv: Path
    tariff_csv: Path
    live_fetch_enabled: bool = False
    forecast_horizon_hours: int = Field(default=12, gt=0)


class PaletteSettings(_Base):
    """Colour-blind-safe chart palette (Okabe-Ito derived)."""

    baseline: str
    rulebased: str
    agent: str
    comfort_band: str
    violation: str
    grid_carbon: str


class ReportSettings(_Base):
    """Static HTML report generation."""

    embed_plotly_js: bool = True
    title: str = "Eco-Loop — Autonomous Building Control Results"


class AnalysisSettings(_Base):
    """Metric computation constants and plausibility envelopes."""

    joules_per_kwh: float = Field(default=3_600_000.0, gt=0.0)
    plausible_annual_kwh_per_m2_min: float = Field(ge=0.0)
    plausible_annual_kwh_per_m2_max: float = Field(gt=0.0)
    palette: PaletteSettings
    report: ReportSettings = Field(default_factory=ReportSettings)


class LoggingSettings(_Base):
    """Structured logging destinations and size caps."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    json_file: bool = True
    console_rich: bool = True
    max_log_mib: int = Field(default=256, gt=0)
    max_trace_mib: int = Field(default=128, gt=0)


# --------------------------------------------------------------------------- #
# Root settings
# --------------------------------------------------------------------------- #
class EcoLoopSettings(BaseSettings):
    """Root configuration object, assembled from YAML layers and environment."""

    model_config = SettingsConfigDict(
        env_prefix="ECOLOOP_",
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
    )

    project: ProjectSettings = Field(default_factory=ProjectSettings)
    simulation: SimulationSettings
    comfort: ComfortSettings
    guardrails: GuardrailSettings
    control: ControlSettings
    bus: BusSettings
    agent: AgentSettings
    llm: LLMSettings
    mcp: MCPSettings
    signals: SignalSettings
    analysis: AnalysisSettings
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    # Provenance, populated by `load_settings`. Recorded in every run manifest so
    # a result can be traced back to the exact configuration that produced it.
    profile: str | None = None
    source_paths: tuple[Path, ...] = ()

    def resolve(self, path: Path) -> Path:
        """Resolve a config-relative path against the project root.

        Absolute paths are returned unchanged, so a user may point at a model or
        weather file anywhere on disk.

        Args:
            path: A path from configuration, typically repository-relative.

        Returns:
            An absolute path.
        """
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _interpolate(value: Any) -> Any:
    """Recursively expand ``${VAR}`` and ``${VAR:-default}`` in string values.

    Args:
        value: Any node of the parsed YAML tree.

    Returns:
        The node with environment references expanded.

    Raises:
        ConfigError: If a referenced variable is unset and has no default.
    """
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if not isinstance(value, str):
        return value

    def _sub(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        resolved = os.environ.get(name, default)
        if resolved is None:
            raise ConfigError(
                f"config references ${{{name}}} but it is unset and has no default; "
                f"set it in your environment or use ${{{name}:-fallback}}",
                variable=name,
            )
        return resolved

    return _ENV_PATTERN.sub(_sub, value)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` onto ``base``, returning a new mapping.

    Nested mappings merge key-by-key; every other type is replaced wholesale, so
    a profile can override a single field without restating its siblings.

    Args:
        base: The lower-precedence mapping.
        overlay: The higher-precedence mapping.

    Returns:
        A new merged mapping. Neither input is mutated.
    """
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    """Parse a YAML config file into a mapping.

    Args:
        path: File to read.

    Returns:
        The parsed mapping, or an empty mapping for an empty file.

    Raises:
        ConfigError: If the file is missing or does not contain a mapping.
    """
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}", path=str(path))
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}", path=str(path)) from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ConfigError(f"config file must contain a mapping at the top level: {path}")
    return parsed


def load_settings(
    config_path: Path | None = None,
    profile: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> EcoLoopSettings:
    """Assemble the layered configuration.

    Args:
        config_path: Explicit config file layered above the profile.
        profile: Name of a profile in ``config/profiles/`` (``fast``, ``full``,
            ``demo``).
        overrides: Final in-process overlay, typically from CLI flags.

    Returns:
        A validated, frozen settings object.

    Raises:
        ConfigError: If any layer is missing, malformed, or fails validation.
    """
    sources: list[Path] = [DEFAULT_CONFIG_PATH]
    merged = _read_yaml(DEFAULT_CONFIG_PATH)

    if profile:
        profile_path = PROFILES_DIR / f"{profile}.yaml"
        if not profile_path.is_file():
            available = sorted(p.stem for p in PROFILES_DIR.glob("*.yaml"))
            raise ConfigError(
                f"unknown profile {profile!r}; available: {', '.join(available) or 'none'}",
                profile=profile,
            )
        merged = _deep_merge(merged, _read_yaml(profile_path))
        sources.append(profile_path)

    if config_path:
        merged = _deep_merge(merged, _read_yaml(config_path))
        sources.append(config_path)

    if overrides:
        merged = _deep_merge(merged, overrides)

    merged = _interpolate(merged)
    merged["profile"] = profile
    merged["source_paths"] = tuple(sources)

    try:
        return EcoLoopSettings(**merged)
    except Exception as exc:  # pydantic.ValidationError and friends
        raise ConfigError(
            f"configuration failed validation: {exc}",
            sources=[str(p) for p in sources],
        ) from exc
