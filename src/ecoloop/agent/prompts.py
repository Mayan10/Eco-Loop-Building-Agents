"""Render the versioned system prompt template.

Templates live in ``config/prompts/`` as plain text — deliberately not
Python code, and never passed to ``eval``/``exec`` (AGENTS.md invariant #3
is about tool output, but the same spirit applies to prompt content: it is
data, rendered by Jinja2's autoescape-off text mode, never interpreted).

The version is part of the filename (``system_v1.j2``) rather than tracked
separately, so there is exactly one place that can drift out of sync with
itself. It is recorded in the agent trace alongside every decision, so a
prompt change's effect on behaviour can be correlated after the fact.
"""

from __future__ import annotations

from collections.abc import Sequence

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ecoloop.config import PROJECT_ROOT

__all__ = ["CURRENT_PROMPT_VERSION", "render_system_prompt"]

CURRENT_PROMPT_VERSION = "v1"

_TEMPLATE_DIR = PROJECT_ROOT / "config" / "prompts"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=False,  # noqa: S701 - plain text prompts, not HTML; nothing here renders in a browser
    undefined=StrictUndefined,
)


def render_system_prompt(*, context: str, zone_names: Sequence[str]) -> tuple[str, str]:
    """Render the current system prompt template.

    Args:
        context: The assembled context text from
            :func:`ecoloop.agent.context.render_context`.
        zone_names: Zones the agent may address.

    Returns:
        A ``(rendered_text, version)`` pair. The version is carried into the
        trace alongside the decision it produced.
    """
    template = _env.get_template(f"system_{CURRENT_PROMPT_VERSION}.j2")
    rendered = template.render(context=context, zone_names=list(zone_names))
    return rendered, CURRENT_PROMPT_VERSION
