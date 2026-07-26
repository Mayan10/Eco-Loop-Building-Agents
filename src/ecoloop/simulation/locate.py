"""Locate an EnergyPlus installation and make ``pyenergyplus`` importable.

``pyenergyplus`` is **not** distributed on PyPI. It ships inside the EnergyPlus
installation directory, so importing it requires injecting that directory onto
``sys.path`` first. This module is the single place that happens; every other
module imports the API through :func:`import_energyplus_api`.

Resolution order:

1. An explicit path argument (``--energyplus-dir`` or ``simulation.energyplus_dir``).
2. The ``ENERGYPLUS_DIR`` environment variable.
3. The directory containing an ``energyplus`` binary found on ``PATH``.
4. Glob of the standard per-OS install locations, newest version first.

The module deliberately does not cache a failed lookup: a user may install
EnergyPlus and re-run ``ecoloop doctor`` in the same shell session.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ecoloop.errors import EnergyPlusNotFoundError

__all__ = [
    "EnergyPlusInstall",
    "discover_energyplus",
    "import_energyplus_api",
    "install_hint",
    "parse_version",
]

# Standard install roots, by platform. Globbed newest-first.
_SEARCH_GLOBS: dict[str, tuple[str, ...]] = {
    "Darwin": (
        "/Applications/EnergyPlus-*",
        "/usr/local/EnergyPlus-*",
        "~/.local/opt/EnergyPlus-*",
        "~/EnergyPlus-*",
    ),
    "Linux": (
        "/usr/local/EnergyPlus-*",
        "/opt/EnergyPlus-*",
        "~/.local/opt/EnergyPlus-*",
        "~/EnergyPlus-*",
    ),
    "Windows": (
        "C:/EnergyPlusV*",
        "C:/Program Files/EnergyPlusV*",
    ),
}

# Matches 25.2.0 in "EnergyPlus-25.2.0-cf7368216c-Darwin-macOS13-arm64" and
# 24.1.0 in "EnergyPlusV24-1-0".
_VERSION_RE = re.compile(r"(\d+)[.\-](\d+)[.\-](\d+)")


@dataclass(frozen=True, slots=True)
class EnergyPlusInstall:
    """A resolved EnergyPlus installation.

    Attributes:
        root: Installation directory containing ``pyenergyplus/``.
        version: Parsed ``(major, minor, patch)`` version tuple.
        version_string: Version rendered as ``major.minor.patch``.
        executable: Path to the ``energyplus`` binary, if present.
        source: How this install was found, for diagnostics.
    """

    root: Path
    version: tuple[int, int, int]
    version_string: str
    executable: Path | None
    source: str

    def supports(self, minimum: str, maximum: str) -> bool:
        """Check the version sits within an inclusive supported range.

        Args:
            minimum: Lowest supported version, e.g. ``"23.2.0"``.
            maximum: Highest supported version, e.g. ``"26.99.0"``.

        Returns:
            True when this install is within range.
        """
        return parse_version(minimum) <= self.version <= parse_version(maximum)


def parse_version(text: str) -> tuple[int, int, int]:
    """Extract a ``(major, minor, patch)`` tuple from a version-bearing string.

    Args:
        text: A version string or a directory name containing one.

    Returns:
        The parsed version, or ``(0, 0, 0)`` when no version is present.
    """
    match = _VERSION_RE.search(text)
    if not match:
        return (0, 0, 0)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _candidate_roots() -> list[Path]:
    """Collect plausible installation roots for the current platform.

    Returns:
        Candidate directories, newest version first, without deduplication.
    """
    globs = _SEARCH_GLOBS.get(platform.system(), ())
    found: list[Path] = []
    for pattern in globs:
        expanded = Path(pattern).expanduser()
        # Split the glob into a fixed parent and a pattern tail.
        parent, _, tail = str(expanded).rpartition("/")
        parent_path = Path(parent)
        if not parent_path.is_dir():
            continue
        found.extend(
            sorted(parent_path.glob(tail), key=lambda p: parse_version(p.name), reverse=True)
        )
    return found


def _validate_root(root: Path, source: str) -> EnergyPlusInstall | None:
    """Build an install record if ``root`` looks like a real EnergyPlus tree.

    Args:
        root: Directory to test.
        source: Human-readable description of how this root was found.

    Returns:
        An :class:`EnergyPlusInstall`, or ``None`` if the directory does not
        contain a ``pyenergyplus`` package.
    """
    if not (root / "pyenergyplus" / "api.py").is_file():
        return None

    version = parse_version(root.name)
    if version == (0, 0, 0):
        # Some layouts do not encode the version in the directory name; fall
        # back to the versioned IDD, e.g. "Energy+.idd".
        version = _version_from_idd(root)

    executable = next(
        (root / name for name in ("energyplus", "energyplus.exe") if (root / name).is_file()),
        None,
    )
    return EnergyPlusInstall(
        root=root,
        version=version,
        version_string=".".join(str(part) for part in version),
        executable=executable,
        source=source,
    )


def _version_from_idd(root: Path) -> tuple[int, int, int]:
    """Read the EnergyPlus version from the IDD header.

    Args:
        root: Installation root containing ``Energy+.idd``.

    Returns:
        The parsed version, or ``(0, 0, 0)`` if it cannot be determined.
    """
    idd = root / "Energy+.idd"
    if not idd.is_file():
        return (0, 0, 0)
    try:
        with idd.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    break
                if "IDD_Version" in line:
                    return parse_version(line)
    except OSError:
        return (0, 0, 0)
    return (0, 0, 0)


def discover_energyplus(explicit: Path | None = None) -> EnergyPlusInstall:
    """Find a usable EnergyPlus installation.

    Args:
        explicit: A path supplied by configuration or CLI flag, tried first.

    Returns:
        The resolved installation.

    Raises:
        EnergyPlusNotFoundError: If no installation containing ``pyenergyplus``
            could be located. The message includes OS-specific install guidance.
    """
    attempts: list[str] = []

    if explicit is not None:
        root = Path(explicit).expanduser().resolve()
        install = _validate_root(root, source="explicit configuration")
        if install:
            return install
        attempts.append(f"explicit path {root} (no pyenergyplus/api.py inside)")

    env_dir = os.environ.get("ENERGYPLUS_DIR")
    if env_dir:
        root = Path(env_dir).expanduser().resolve()
        install = _validate_root(root, source="ENERGYPLUS_DIR")
        if install:
            return install
        attempts.append(f"$ENERGYPLUS_DIR={root} (no pyenergyplus/api.py inside)")

    which = shutil.which("energyplus")
    if which:
        root = Path(which).resolve().parent
        install = _validate_root(root, source="energyplus binary on PATH")
        if install:
            return install
        attempts.append(f"PATH binary at {root} (no pyenergyplus/api.py alongside)")

    for root in _candidate_roots():
        install = _validate_root(root, source="standard install location")
        if install:
            return install
        attempts.append(f"{root} (no pyenergyplus/api.py inside)")

    raise EnergyPlusNotFoundError(
        "no EnergyPlus installation with an importable pyenergyplus package was found.\n"
        f"{install_hint()}\n"
        "Then either set ENERGYPLUS_DIR or pass simulation.energyplus_dir in your config.",
        attempts=attempts or ["no candidate locations existed"],
        platform=platform.system(),
    )


def install_hint() -> str:
    """Return OS-specific installation guidance.

    Returns:
        A copy-pasteable hint naming where to get EnergyPlus for this platform.
    """
    system = platform.system()
    releases = "https://github.com/NREL/EnergyPlus/releases"
    if system == "Darwin":
        arch = "arm64" if platform.machine() == "arm64" else "x86_64"
        return (
            f"Install EnergyPlus 25.2.0 for macOS ({arch}) from {releases} — the .tar.gz\n"
            "  needs no administrator rights:\n"
            "    mkdir -p ~/.local/opt && cd ~/.local/opt\n"
            f"    curl -L -o ep.tar.gz <EnergyPlus-25.2.0-...-Darwin-macOS13-{arch}.tar.gz>\n"
            "    tar xzf ep.tar.gz && rm ep.tar.gz"
        )
    if system == "Linux":
        return (
            f"Install EnergyPlus from {releases}:\n"
            "    curl -L -o ep.tar.gz <EnergyPlus-25.2.0-...-Linux-Ubuntu24.04-x86_64.tar.gz>\n"
            "    sudo tar xzf ep.tar.gz -C /usr/local"
        )
    return (
        f"Install EnergyPlus from {releases} (Windows installer: EnergyPlus-25.2.0-...-x86_64.exe)"
    )


def import_energyplus_api(install: EnergyPlusInstall) -> Any:
    """Inject the install onto ``sys.path`` and return an ``EnergyPlusAPI``.

    Args:
        install: A resolved installation.

    Returns:
        A fresh ``pyenergyplus.api.EnergyPlusAPI`` instance.

    Raises:
        EnergyPlusNotFoundError: If the package is present but fails to import,
            which usually means an architecture mismatch between the EnergyPlus
            build and the running Python interpreter.
    """
    root = str(install.root)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        # Deliberate late import: the package only becomes importable after the
        # sys.path injection above.
        from pyenergyplus.api import EnergyPlusAPI
    except ImportError as exc:
        raise EnergyPlusNotFoundError(
            f"found EnergyPlus at {install.root} but importing pyenergyplus failed: {exc}. "
            "This usually means the EnergyPlus build and the Python interpreter target "
            f"different architectures (Python is {platform.machine()}).",
            root=root,
        ) from exc
    return EnergyPlusAPI()
