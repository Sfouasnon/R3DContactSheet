"""Shared resolver for ffmpeg and ffprobe executables.

Packaged macOS .app bundles do not inherit the shell PATH (e.g. /opt/homebrew/bin
is not on the process PATH even though it is available in Terminal). This module
provides a single resolution path used by both the metadata provider and the
renderer so discovery logic is never duplicated.

Resolution order
----------------
1. Explicit caller-supplied override path  (e.g. from user preferences)
2. shutil.which()                          (honours whatever PATH the process has)
3. Hard-coded common macOS install prefixes:
       /opt/homebrew/bin   – Apple-silicon Homebrew
       /usr/local/bin      – Intel Homebrew / manual installs
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Candidate directories to probe when shutil.which() comes up empty.
_FALLBACK_DIRS: tuple[str, ...] = (
    "/opt/homebrew/bin",   # Apple-silicon Homebrew (not on .app PATH)
    "/usr/local/bin",      # Intel Homebrew / manual installs
)


def _is_executable_file(path: str) -> bool:
    """Return True if *path* is an existing, regular, executable file."""
    p = Path(path)
    return p.exists() and p.is_file() and os.access(path, os.X_OK)


def resolve_tool(
    tool_name: str,
    *,
    override: Optional[str] = None,
) -> Optional[str]:
    """Return an absolute path to *tool_name* or ``None`` if it cannot be found.

    Parameters
    ----------
    tool_name:
        The bare executable name, e.g. ``"ffmpeg"`` or ``"ffprobe"``.
    override:
        Caller-supplied explicit path (e.g. from application preferences).
        When provided and valid it is used immediately without further search.

    Side-effects
    ------------
    Logs the resolved path at DEBUG level on success, or lists every candidate
    that was checked at WARNING level on failure.
    """
    candidates_checked: list[str] = []

    # 1. Explicit override
    if override:
        candidates_checked.append(override)
        if _is_executable_file(override):
            logger.debug("Tool %r resolved via override: %s", tool_name, override)
            return override
        logger.warning(
            "Tool %r override path %r is not a valid executable; continuing search.",
            tool_name,
            override,
        )

    # 2. shutil.which (uses the process PATH)
    which_result = shutil.which(tool_name)
    if which_result:
        candidates_checked.append(which_result)
        if _is_executable_file(which_result):
            logger.debug("Tool %r resolved via PATH (shutil.which): %s", tool_name, which_result)
            return which_result

    # 3. Hard-coded macOS fallback directories
    for directory in _FALLBACK_DIRS:
        candidate = os.path.join(directory, tool_name)
        candidates_checked.append(candidate)
        if _is_executable_file(candidate):
            logger.debug(
                "Tool %r resolved via fallback directory %r: %s",
                tool_name,
                directory,
                candidate,
            )
            return candidate

    logger.warning(
        "Tool %r could not be found. Candidates checked: %s",
        tool_name,
        ", ".join(candidates_checked) if candidates_checked else "(none)",
    )
    return None


def resolve_ffmpeg(override: Optional[str] = None) -> Optional[str]:
    """Resolve the ``ffmpeg`` executable. See :func:`resolve_tool`."""
    return resolve_tool("ffmpeg", override=override)


def resolve_ffprobe(override: Optional[str] = None) -> Optional[str]:
    """Resolve the ``ffprobe`` executable. See :func:`resolve_tool`."""
    return resolve_tool("ffprobe", override=override)
