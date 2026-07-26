"""Structured logging for Eco-Loop.

Emits JSON to a rotating file and Rich-rendered output to the console, from one
call site. Every record carries ``run_id``, ``sim_datetime``, ``component`` and
``severity``, so logs are both human-readable during a run and machine-parseable
afterwards.

Typical use::

    from ecoloop.logging import bind_run_context, configure_logging, get_logger

    configure_logging(settings.logging, log_dir=run_dir)
    bind_run_context(run_id="agent-20260726-2230")
    log = get_logger(__name__, component="reflex")
    log.info("policy_applied", zone="Core_ZN", heating_c=21.0, cooling_c=24.0)

The simulated datetime is context-bound rather than passed explicitly, because
the reflex layer logs from deep inside EnergyPlus callbacks where threading it
through every signature would be noise. Bind it once per timestep with
:func:`bind_sim_datetime`.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checking only
    from ecoloop.config import LoggingSettings

__all__ = [
    "bind_run_context",
    "bind_sim_datetime",
    "clear_context",
    "configure_logging",
    "get_logger",
]

_MIB = 1024 * 1024
# Keep four rotations: enough to reconstruct an annual run, bounded on disk.
_LOG_BACKUP_COUNT = 4


def _add_component_default(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    """Ensure every record carries a ``component`` field.

    Records emitted through the stdlib bridge (from third-party libraries) have
    no component bound, and a missing key makes log queries awkward.

    Args:
        _logger: Unused wrapped logger.
        _method: Unused log method name.
        event_dict: The record under construction.

    Returns:
        The record with ``component`` guaranteed present.
    """
    event_dict.setdefault("component", "ecoloop")
    return event_dict


def _rename_level_to_severity(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    """Rename ``level`` to ``severity`` to match the documented record schema.

    Args:
        _logger: Unused wrapped logger.
        _method: Unused log method name.
        event_dict: The record under construction.

    Returns:
        The record with ``severity`` in place of ``level``.
    """
    if "level" in event_dict:
        event_dict["severity"] = event_dict.pop("level")
    return event_dict


def _shared_processors() -> list[Processor]:
    """Build the processor chain applied to every record on both sinks.

    Returns:
        Processors ordered from context merging through to level annotation.
    """
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _add_component_default,
    ]


def _build_file_handler(log_dir: Path, max_log_mib: int) -> logging.Handler:
    """Create the rotating JSON file handler.

    Args:
        log_dir: Directory to write ``ecoloop.jsonl`` into. Created if absent.
        max_log_mib: Size cap per file before rotation, in MiB.

    Returns:
        A configured rotating file handler rendering records as JSON lines.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "ecoloop.jsonl",
        maxBytes=max_log_mib * _MIB,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=_shared_processors(),
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                _rename_level_to_severity,
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(sort_keys=True),
            ],
        )
    )
    return handler


def _build_console_handler(*, rich: bool) -> logging.Handler:
    """Create the console handler.

    Args:
        rich: Render with colour and aligned columns. Disabled during live-TUI
            runs so console output does not fight the Rich full-screen view.

    Returns:
        A configured stream handler writing to stderr.
    """
    # stderr, so that piping stdout (e.g. MCP stdio transport) stays clean.
    handler = logging.StreamHandler(stream=sys.stderr)
    renderer: Processor = (
        structlog.dev.ConsoleRenderer(colors=True)
        if rich
        else structlog.processors.KeyValueRenderer(key_order=["event", "component"])
    )
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=_shared_processors(),
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
    )
    return handler


def configure_logging(
    settings: LoggingSettings | None = None,
    log_dir: Path | None = None,
    *,
    force: bool = True,
) -> None:
    """Configure structlog and the stdlib root logger.

    Safe to call more than once; later calls replace the handler set when
    ``force`` is true.

    Args:
        settings: Logging configuration. Defaults are used when omitted, which
            keeps ``ecoloop doctor`` usable even if the config fails to load.
        log_dir: Directory for the rotating JSON log. When omitted, no file sink
            is installed and logging is console-only.
        force: Remove existing root handlers before installing ours.
    """
    level_name = settings.level if settings else "INFO"
    console_rich = settings.console_rich if settings else True
    json_file = settings.json_file if settings else True
    max_log_mib = settings.max_log_mib if settings else 256

    level = getattr(logging, level_name)

    root = logging.getLogger()
    if force:
        for existing in list(root.handlers):
            root.removeHandler(existing)
    root.setLevel(level)

    root.addHandler(_build_console_handler(rich=console_rich))
    if json_file and log_dir is not None:
        root.addHandler(_build_file_handler(log_dir, max_log_mib))

    structlog.configure(
        processors=[
            *_shared_processors(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Third-party libraries are chatty at DEBUG and drown the run log.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "mcp"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def get_logger(name: str, component: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger.

    Args:
        name: Logger name, conventionally ``__name__``.
        component: Architectural component this logger speaks for
            (``reflex``, ``orchestrator``, ``mcp``, ...). Used for filtering.

    Returns:
        A bound logger carrying the component field.
    """
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    if component:
        logger = logger.bind(component=component)
    return logger


def bind_run_context(**fields: Any) -> None:
    """Bind fields onto every subsequent record in this context.

    Typically called once at run start with ``run_id`` and the controller name.

    Args:
        **fields: Key-value pairs to attach to all later records.
    """
    structlog.contextvars.bind_contextvars(**fields)


def bind_sim_datetime(sim_datetime: str) -> None:
    """Bind the current simulated datetime onto subsequent records.

    Called once per timestep from the callback layer. Wall-clock timestamps are
    nearly useless when debugging a simulation: what matters is *when in the
    simulated year* something happened.

    Args:
        sim_datetime: ISO-8601 simulated datetime.
    """
    structlog.contextvars.bind_contextvars(sim_datetime=sim_datetime)


def clear_context() -> None:
    """Clear all context-bound fields. Call at the end of a run."""
    structlog.contextvars.clear_contextvars()
