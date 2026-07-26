"""Resolve and validate every filesystem path an MCP tool argument names.

AGENTS.md N3.3: every file path from a tool argument is resolved and asserted
to sit inside an allowlisted directory before it is opened. This is the one
function in the codebase that check happens in — every tool that touches a
path calls it first, so there is exactly one place to audit for the entire
project's file-access surface.

Three escape routes are rejected, all deliberately, because each is a real
technique for reaching outside an intended directory:

1. ``..`` segments that walk back up past the sandbox root.
2. An absolute path supplied where a relative one was expected.
3. A symlink *inside* the sandbox whose target resolves outside it — `..`
   filtering alone does not catch this, since the symlink itself can sit
   validly inside the root while pointing elsewhere.
"""

from __future__ import annotations

from pathlib import Path

from ecoloop.errors import SandboxViolationError

__all__ = ["resolve_sandboxed_path"]


def resolve_sandboxed_path(raw_path: str, *, roots: tuple[Path, ...], project_root: Path) -> Path:
    """Resolve a tool-supplied path and assert it stays inside an allowlisted root.

    Args:
        raw_path: The path as supplied by a tool argument. Untrusted.
        roots: Allowlisted directories, relative to ``project_root``.
        project_root: The repository root each sandbox root is resolved against.

    Returns:
        The fully resolved, real (symlinks followed) path — guaranteed to sit
        inside one of ``roots``.

    Raises:
        SandboxViolationError: If the path escapes every allowlisted root,
            whether via ``..``, an absolute path outside the sandbox, or a
            symlink resolving outside it.
    """
    candidate = Path(raw_path)
    combined = candidate if candidate.is_absolute() else project_root / candidate

    try:
        resolved = combined.resolve(strict=False)
    except OSError as exc:
        raise SandboxViolationError(
            "path could not be resolved", raw_path=raw_path, cause=str(exc)
        ) from exc

    resolved_roots = tuple((project_root / root).resolve(strict=False) for root in roots)
    for resolved_root in resolved_roots:
        if resolved == resolved_root or resolved_root in resolved.parents:
            return resolved

    raise SandboxViolationError(
        "path escapes every allowlisted sandbox root",
        raw_path=raw_path,
        resolved=str(resolved),
        roots=[str(r) for r in resolved_roots],
    )
