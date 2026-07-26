"""Shared pytest fixtures.

The entire suite runs without EnergyPlus or Ollama installed — that is a hard
requirement, enforced by a CI job that asserts ``energyplus`` is absent from
``PATH`` before running. Anything needing the real engine is marked
``@pytest.mark.energyplus`` and deselected there.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ecoloop.config import EcoLoopSettings, load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Repository root, for tests that need to reach real config or model files."""
    return PROJECT_ROOT


@pytest.fixture
def settings() -> EcoLoopSettings:
    """Settings loaded from the real ``default.yaml`` under the fast profile.

    Tests deliberately exercise the shipped configuration rather than a fixture
    copy, so a change to ``default.yaml`` that breaks an invariant is caught here
    instead of in a simulation twenty minutes later.
    """
    return load_settings(profile="fast")


@pytest.fixture
def tmp_results_dir(tmp_path: Path) -> Iterator[Path]:
    """An isolated results directory for a single test."""
    results = tmp_path / "results"
    results.mkdir()
    yield results
