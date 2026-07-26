"""Tests for EnergyPlus discovery.

These run on machines with *no* EnergyPlus installed, so discovery is exercised
against synthetic directory trees rather than a real install.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ecoloop.errors import EnergyPlusNotFoundError
from ecoloop.simulation.locate import (
    EnergyPlusInstall,
    discover_energyplus,
    install_hint,
    parse_version,
)


def make_fake_install(root: Path, *, with_binary: bool = True) -> Path:
    """Create a directory tree that looks enough like an EnergyPlus install.

    Args:
        root: Directory to populate.
        with_binary: Also create an ``energyplus`` executable stub.

    Returns:
        The populated root.
    """
    (root / "pyenergyplus").mkdir(parents=True)
    (root / "pyenergyplus" / "api.py").write_text("# stub\n", encoding="utf-8")
    if with_binary:
        (root / "energyplus").write_text("#!/bin/sh\n", encoding="utf-8")
    return root


class TestParseVersion:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("EnergyPlus-25.2.0-cf7368216c-Darwin-macOS13-arm64", (25, 2, 0)),
            ("EnergyPlusV24-1-0", (24, 1, 0)),
            ("23.2.0", (23, 2, 0)),
            ("no version here", (0, 0, 0)),
            ("", (0, 0, 0)),
        ],
    )
    def test_parses_known_layouts(self, text: str, expected: tuple[int, int, int]) -> None:
        assert parse_version(text) == expected


class TestSupports:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [((25, 2, 0), True), ((23, 2, 0), True), ((23, 1, 0), False), ((27, 0, 0), False)],
    )
    def test_inclusive_range(self, version: tuple[int, int, int], expected: bool) -> None:
        install = EnergyPlusInstall(
            root=Path("/nowhere"),
            version=version,
            version_string=".".join(map(str, version)),
            executable=None,
            source="test",
        )
        assert install.supports("23.2.0", "26.99.0") is expected


class TestDiscovery:
    def test_finds_explicit_path(self, tmp_path: Path) -> None:
        root = make_fake_install(tmp_path / "EnergyPlus-25.2.0")
        install = discover_energyplus(root)
        assert install.root == root.resolve()
        assert install.version == (25, 2, 0)
        assert install.source == "explicit configuration"

    def test_explicit_path_falls_through_to_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wrong explicit path must not shadow a correct ENERGYPLUS_DIR."""
        good = make_fake_install(tmp_path / "EnergyPlus-24.1.0")
        monkeypatch.setenv("ENERGYPLUS_DIR", str(good))
        install = discover_energyplus(tmp_path / "not-an-install")
        assert install.source == "ENERGYPLUS_DIR"
        assert install.version == (24, 1, 0)

    def test_env_var_is_used(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = make_fake_install(tmp_path / "EnergyPlus-25.2.0")
        monkeypatch.setenv("ENERGYPLUS_DIR", str(root))
        assert discover_energyplus().source == "ENERGYPLUS_DIR"

    def test_directory_without_pyenergyplus_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An EnergyPlus-shaped directory with no API package is not usable."""
        bare = tmp_path / "EnergyPlus-25.2.0"
        bare.mkdir()
        monkeypatch.setenv("ENERGYPLUS_DIR", str(bare))
        monkeypatch.setattr("ecoloop.simulation.locate._candidate_roots", list)
        monkeypatch.setattr("shutil.which", lambda _: None)
        with pytest.raises(EnergyPlusNotFoundError):
            discover_energyplus()

    def test_failure_message_carries_attempts_and_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ENERGYPLUS_DIR", raising=False)
        monkeypatch.setattr("ecoloop.simulation.locate._candidate_roots", list)
        monkeypatch.setattr("shutil.which", lambda _: None)
        with pytest.raises(EnergyPlusNotFoundError) as caught:
            discover_energyplus(tmp_path / "missing")
        assert "attempts" in caught.value.context
        # The operator must be told how to fix it, not just that it failed.
        assert "EnergyPlus" in caught.value.message

    def test_records_executable_when_present(self, tmp_path: Path) -> None:
        root = make_fake_install(tmp_path / "EnergyPlus-25.2.0", with_binary=True)
        assert discover_energyplus(root).executable == root / "energyplus"

    def test_tolerates_missing_executable(self, tmp_path: Path) -> None:
        root = make_fake_install(tmp_path / "EnergyPlus-25.2.0", with_binary=False)
        assert discover_energyplus(root).executable is None


class TestInstallHint:
    def test_hint_is_actionable_on_every_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for system in ("Darwin", "Linux", "Windows"):
            monkeypatch.setattr("platform.system", lambda s=system: s)
            hint = install_hint()
            assert "EnergyPlus" in hint
            assert "releases" in hint
