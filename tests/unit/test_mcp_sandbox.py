"""Tests for the sandbox path resolver — the one gate every tool-supplied path passes through."""

from __future__ import annotations

from pathlib import Path

import pytest

from ecoloop.errors import SandboxViolationError
from ecoloop.mcp.sandbox import resolve_sandboxed_path


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "models").mkdir(parents=True)
    (root / "results").mkdir()
    (root / "outside").mkdir()
    (root / "models" / "file.txt").write_text("hello", encoding="utf-8")
    (root / "outside" / "secret.txt").write_text("nope", encoding="utf-8")
    return root


class TestValidPaths:
    def test_relative_path_inside_a_root_resolves(self, sandbox: Path) -> None:
        resolved = resolve_sandboxed_path(
            "models/file.txt", roots=(Path("models"),), project_root=sandbox
        )
        assert resolved == (sandbox / "models" / "file.txt").resolve()

    def test_the_root_directory_itself_resolves(self, sandbox: Path) -> None:
        resolved = resolve_sandboxed_path("models", roots=(Path("models"),), project_root=sandbox)
        assert resolved == (sandbox / "models").resolve()

    def test_multiple_roots_are_all_checked(self, sandbox: Path) -> None:
        resolved = resolve_sandboxed_path(
            "results", roots=(Path("models"), Path("results")), project_root=sandbox
        )
        assert resolved == (sandbox / "results").resolve()


class TestEscapeAttempts:
    def test_dotdot_escaping_the_root_is_rejected(self, sandbox: Path) -> None:
        with pytest.raises(SandboxViolationError):
            resolve_sandboxed_path(
                "models/../outside/secret.txt", roots=(Path("models"),), project_root=sandbox
            )

    def test_absolute_path_outside_the_sandbox_is_rejected(self, sandbox: Path) -> None:
        with pytest.raises(SandboxViolationError):
            resolve_sandboxed_path(
                str(sandbox / "outside" / "secret.txt"),
                roots=(Path("models"),),
                project_root=sandbox,
            )

    def test_sibling_directory_not_in_any_root_is_rejected(self, sandbox: Path) -> None:
        with pytest.raises(SandboxViolationError):
            resolve_sandboxed_path(
                "outside/secret.txt", roots=(Path("models"), Path("results")), project_root=sandbox
            )

    def test_symlink_escaping_the_root_is_rejected(self, sandbox: Path) -> None:
        link = sandbox / "models" / "escape_link"
        link.symlink_to(sandbox / "outside")
        with pytest.raises(SandboxViolationError):
            resolve_sandboxed_path(
                "models/escape_link/secret.txt", roots=(Path("models"),), project_root=sandbox
            )

    def test_error_names_the_attempted_path_and_the_roots(self, sandbox: Path) -> None:
        with pytest.raises(SandboxViolationError) as caught:
            resolve_sandboxed_path("outside", roots=(Path("models"),), project_root=sandbox)
        assert "outside" in str(caught.value)
