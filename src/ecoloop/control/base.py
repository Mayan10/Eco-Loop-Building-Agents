"""The shared shape of every synchronous controller.

Baseline and rule-based controllers both compute a complete
:class:`~ecoloop.bus.policy.ControlPolicy` directly from the current
telemetry, with no I/O and no latency to hide — unlike the cognitive
(agent) controller, which runs on its own worker thread and publishes into
:class:`~ecoloop.bus.policy.PolicyStore` asynchronously. This protocol
describes only the synchronous half; the reflex tier calls it inline, once
per timestep, when ``control.controller`` selects ``baseline`` or
``rulebased``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ecoloop.bus.models import TelemetrySample
from ecoloop.bus.policy import ControlPolicy

__all__ = ["SynchronousController"]


@runtime_checkable
class SynchronousController(Protocol):
    """A controller that computes a policy directly from telemetry, no I/O."""

    def decide(self, sample: TelemetrySample) -> ControlPolicy:
        """Compute a complete control policy for the current timestep.

        Args:
            sample: The latest telemetry sample.

        Returns:
            A fresh, fully-populated policy. Never partial: every zone this
            controller addresses gets a proposal, even if that proposal is
            later clamped or refused by the guardrail chain.
        """
        ...
