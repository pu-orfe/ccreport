"""The faculty-facing web application.

Imported lazily by the CLI so that ``pip install ccreport`` without the ``web``
extra still gives a working command line.
"""

from __future__ import annotations

__all__ = ["create_app"]


def create_app(*args, **kwargs):  # pragma: no cover - thin re-export
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)
