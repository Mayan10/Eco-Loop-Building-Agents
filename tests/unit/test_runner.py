"""Tests for the full-run orchestration in ecoloop.runner.

`run_controller`'s ``agent``-rejection guard needs no engine at all. Actually
driving a run needs the real EnergyPlus install (it constructs
`EnergyPlusBackend` directly, the same shape as `agent/selfheal.py`'s
`run_with_self_healing` - see its tests for the identical precedent), so
those cases are marked `energyplus` and deselected by CI.

`run_agent_controller`'s tests pass a `ScriptedLLM` rather than depending on
a real Ollama endpoint - a real cognitive cycle against qwen2.5:7b-instruct
was observed taking around two minutes end to end (mostly LLM round trips;
EnergyPlus itself resolves a whole demo-profile run in under a second), far
too slow and non-deterministic for the regular test suite. How many cycles
actually complete in a short real run is inherently wall-clock-dependent
(the worker thread's poll loop races against EnergyPlus finishing), so these
tests assert the run completes successfully and the wiring behaves - not an
exact cycle count.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from fake_llm import ScriptedLLM

from ecoloop.agent.llm import ChatResponse
from ecoloop.config import EcoLoopSettings, load_settings
from ecoloop.runner import RunManifest, run_agent_controller, run_controller


class TestRejectsUnsupportedControllers:
    def test_agent_controller_is_rejected_without_touching_the_engine(self) -> None:
        settings = load_settings(profile="fast")

        with pytest.raises(ValueError, match="agent"):
            run_controller(settings, "agent")  # type: ignore[arg-type]


@pytest.fixture
def settings(tmp_path: Path) -> EcoLoopSettings:
    return load_settings(
        profile="fast",
        overrides={"simulation": {"idf_prepared": str(tmp_path / "prepared.idf")}},
    )


@pytest.mark.energyplus
class TestRunController:
    def test_baseline_run_succeeds_and_persists_telemetry(
        self, settings: EcoLoopSettings, tmp_path: Path
    ) -> None:
        output_dir = tmp_path / "baseline_run"

        manifest = run_controller(settings, "baseline", profile="fast", output_dir=output_dir)

        assert isinstance(manifest, RunManifest)
        assert manifest.controller == "baseline"
        assert manifest.succeeded is True
        assert manifest.timesteps_published > 0
        assert manifest.telemetry_path.is_file()
        assert (output_dir / "manifest.json").is_file()
        assert manifest.conditioned_floor_area_m2 == pytest.approx(511.16, abs=0.01)

        df = pd.read_parquet(manifest.telemetry_path)
        assert len(df) == manifest.timesteps_published
        assert "meter__ElectricityNet:Facility_j" in df.columns
        assert "zone__CORE_ZN__pmv" in df.columns

    def test_rulebased_run_succeeds(self, settings: EcoLoopSettings, tmp_path: Path) -> None:
        output_dir = tmp_path / "rulebased_run"

        manifest = run_controller(settings, "rulebased", profile="fast", output_dir=output_dir)

        assert manifest.succeeded is True
        assert manifest.controller == "rulebased"

    def test_manifest_json_round_trips_the_returned_manifest(
        self, settings: EcoLoopSettings, tmp_path: Path
    ) -> None:
        output_dir = tmp_path / "manifest_run"

        manifest = run_controller(settings, "baseline", profile="fast", output_dir=output_dir)

        reloaded = RunManifest.model_validate_json(
            (output_dir / "manifest.json").read_text(encoding="utf-8")
        )
        assert reloaded == manifest


@pytest.mark.energyplus
class TestRunAgentController:
    def test_agent_run_succeeds_with_the_worker_thread_attached(
        self, settings: EcoLoopSettings, tmp_path: Path
    ) -> None:
        output_dir = tmp_path / "agent_run"
        # Enough identical "no action needed" responses that the worker
        # thread never runs out, regardless of how many cadence windows its
        # poll loop happens to catch before EnergyPlus finishes.
        llm = ScriptedLLM(
            [ChatResponse(content="Nothing needs to change.", tool_calls=()) for _ in range(50)]
        )

        manifest = run_agent_controller(settings, profile="fast", output_dir=output_dir, llm=llm)

        assert manifest.controller == "agent"
        assert manifest.succeeded is True
        assert manifest.timesteps_published > 0
        assert manifest.telemetry_path.is_file()
        assert (output_dir / "agent_trace.jsonl").is_file()
        # dropped_samples must be a real TelemetryBus count for this
        # controller, not the always-0 placeholder run_controller reports.
        assert manifest.dropped_samples >= 0

    def test_agent_run_writes_a_reloadable_manifest(
        self, settings: EcoLoopSettings, tmp_path: Path
    ) -> None:
        output_dir = tmp_path / "agent_manifest_run"
        llm = ScriptedLLM([ChatResponse(content="hold", tool_calls=()) for _ in range(50)])

        manifest = run_agent_controller(settings, profile="fast", output_dir=output_dir, llm=llm)

        reloaded = RunManifest.model_validate_json(
            (output_dir / "manifest.json").read_text(encoding="utf-8")
        )
        assert reloaded == manifest
