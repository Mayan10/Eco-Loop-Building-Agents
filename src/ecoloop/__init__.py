"""Eco-Loop — an autonomous closed-loop building control agent.

A high-fidelity EnergyPlus simulation supervised in real time by a locally
served open-source LLM speaking Model Context Protocol. The controller is
two-tier by design: a sub-millisecond deterministic reflex layer on the
EnergyPlus callback thread, and a slow agentic cognitive layer on a background
worker. See ``docs/ARCHITECTURE.md``.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
