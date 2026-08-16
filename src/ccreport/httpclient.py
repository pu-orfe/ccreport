"""One HTTP client for the process.

Every OAuth refresh and every provider call used to build its own
``httpx.Client``. Each one opens a connection pool that nothing ever closes, so
a long-running worker accumulated pools — and paid a fresh TLS handshake on
every request to hosts it had just finished talking to.

A single pooled client fixes both. ``httpx.Client`` is safe to share across
threads, and connections to Graph, Gmail and the token endpoints are exactly the
kind of repeated, same-host traffic pooling exists for.

Tests never touch this: they pass their own client with a mock transport.
"""

from __future__ import annotations

import threading

import httpx

#: Providers occasionally take their time; a request that hangs forever would
#: hold a worker until App Service kills it.
DEFAULT_TIMEOUT = 30.0
MAX_CONNECTIONS = 20
MAX_KEEPALIVE = 10

_client: httpx.Client | None = None
_lock = threading.Lock()


def shared_client() -> httpx.Client:
    global _client
    with _lock:
        if _client is None or _client.is_closed:
            _client = httpx.Client(
                timeout=DEFAULT_TIMEOUT,
                limits=httpx.Limits(
                    max_connections=MAX_CONNECTIONS, max_keepalive_connections=MAX_KEEPALIVE
                ),
                follow_redirects=False,
            )
        return _client


def close_shared_client() -> None:
    """Close the pool. For tests and for a clean shutdown."""
    global _client
    with _lock:
        if _client is not None and not _client.is_closed:
            _client.close()
        _client = None


__all__ = ["DEFAULT_TIMEOUT", "close_shared_client", "shared_client"]
