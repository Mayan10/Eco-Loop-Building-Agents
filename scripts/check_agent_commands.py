#!/usr/bin/env python3
"""Verify every command quoted in an agent file actually exists.

Agent context files (``AGENTS.md``, ``CLAUDE.md``, and the nested per-directory
files) are prepended to a coding agent's context on every turn. An agent will
*trust* a command it finds there, so a stale command is worse than no command:
it costs a full cycle to discover it is wrong.

This script extracts every ``make <target>`` and ``ecoloop <subcommand>``
mentioned in those files and asserts each one is real — a target declared
``.PHONY`` in the ``Makefile``, or a command registered on the Typer app.

Run directly, or via pre-commit / CI:

    python scripts/check_agent_commands.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Agent files to scan. Missing files are reported, not silently skipped: the
# build directive requires all four to exist.
AGENT_FILES: tuple[Path, ...] = (
    PROJECT_ROOT / "AGENTS.md",
    PROJECT_ROOT / "CLAUDE.md",
    PROJECT_ROOT / "src" / "ecoloop" / "simulation" / "AGENTS.md",
    PROJECT_ROOT / "src" / "ecoloop" / "agent" / "AGENTS.md",
)

MAKE_RE = re.compile(r"\bmake\s+([a-z][a-z0-9_-]*)")
ECOLOOP_RE = re.compile(r"\becoloop\s+([a-z][a-z0-9_-]*)")

# Words that follow `make`/`ecoloop` in prose without naming a target, plus
# option-like tokens. Keeps the checker from flagging English sentences.
IGNORED_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "changes",
        "in",
        "is",
        "it",
        "sure",
        "target",
        "targets",
        "the",
        "them",
        "this",
        "to",
        "will",
    }
)


def declared_make_targets() -> set[str]:
    """Collect every target declared ``.PHONY`` in the Makefile.

    Returns:
        The set of phony target names.
    """
    makefile = PROJECT_ROOT / "Makefile"
    if not makefile.is_file():
        return set()
    targets: set[str] = set()
    # Fold backslash continuations before matching. Doing it in the regex is
    # subtly wrong: a greedy `.*` swallows the trailing backslash itself, so the
    # continuation branch never fires and only the first line is ever seen.
    text = re.sub(r"\\\n\s*", " ", makefile.read_text(encoding="utf-8"))
    for match in re.finditer(r"^\.PHONY:(.*)$", text, re.MULTILINE):
        targets.update(match.group(1).split())
    return targets


def registered_ecoloop_commands() -> set[str]:
    """Collect every command and sub-app registered on the Typer application.

    Returns:
        The set of invocable ``ecoloop`` subcommand names.

    Raises:
        SystemExit: If the CLI module cannot be imported, since that is itself a
            failure worth surfacing loudly.
    """
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    try:
        from ecoloop.cli import app
    except ImportError as exc:  # pragma: no cover - exercised only when broken
        sys.exit(f"FAIL: could not import ecoloop.cli — {exc}")

    names: set[str] = set()
    for command in app.registered_commands:
        # Typer derives the CLI name from the function name when unset.
        name = command.name or (command.callback.__name__ if command.callback else None)
        if name:
            names.add(name.replace("_", "-"))
    for group in app.registered_groups:
        if group.name:
            names.add(group.name)
        elif group.typer_instance and group.typer_instance.info.name:
            names.add(group.typer_instance.info.name)
    return names


def _extract(pattern: re.Pattern[str], text: str) -> set[str]:
    """Extract candidate command names, discarding prose words.

    Args:
        pattern: Regex whose first group captures the command name.
        text: File contents to scan.

    Returns:
        Plausible command names.
    """
    return {m.group(1) for m in pattern.finditer(text) if m.group(1) not in IGNORED_WORDS}


def main() -> int:
    """Check all agent files and report any command that does not exist.

    Returns:
        Process exit code: 0 when every quoted command resolves.
    """
    make_targets = declared_make_targets()
    ecoloop_commands = registered_ecoloop_commands()

    problems: list[str] = []
    scanned = 0

    for path in AGENT_FILES:
        if not path.is_file():
            problems.append(f"{path.relative_to(PROJECT_ROOT)}: required agent file is missing")
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(PROJECT_ROOT)

        for target in sorted(_extract(MAKE_RE, text)):
            if target not in make_targets:
                problems.append(f"{rel}: 'make {target}' is not a target in the Makefile")

        for command in sorted(_extract(ECOLOOP_RE, text)):
            if command not in ecoloop_commands:
                problems.append(f"{rel}: 'ecoloop {command}' is not a registered CLI command")

    if problems:
        print(f"FAIL: {len(problems)} problem(s) in agent context files\n")
        for problem in problems:
            print(f"  ✗ {problem}")
        print(
            "\nAgent files are loaded into context on every turn and are trusted. "
            "Fix the command or update the file."
        )
        return 1

    print(
        f"OK: every command in {scanned} agent file(s) resolves "
        f"({len(make_targets)} make targets, {len(ecoloop_commands)} ecoloop commands)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
