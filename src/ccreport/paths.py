"""Where ccreport keeps local state.

Mirrors the ccworks convention: macOS uses Application Support, Linux honours
``XDG_STATE_HOME``, and ``CCREPORT_STATE_DIR`` overrides both. Nothing here is
used by the deployed container, which keeps state in PostgreSQL and Blob
Storage — this is for the CLI on somebody's laptop.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "ccreport"


def state_dir() -> Path:
    """Return the directory for local CLI state, creating it if needed."""
    override = os.environ.get("CCREPORT_STATE_DIR")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        xdg = os.environ.get("XDG_STATE_HOME")
        base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
        path = base / APP_NAME

    path.mkdir(parents=True, exist_ok=True)
    return path


def bundle_dir() -> Path:
    """Where ``ccreport report export`` writes bundles by default."""
    override = os.environ.get("CCREPORT_BUNDLE_DIR")
    path = Path(override).expanduser() if override else state_dir() / "bundles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    """Scratch space for renderer intermediates. Safe to delete at any time."""
    path = state_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path
