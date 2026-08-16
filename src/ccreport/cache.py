"""The in-process header cache.

Browsing a month means listing headers, scoring them, and showing them. None of
that is written to the database — the schema has nowhere to put it — so the
results live here instead: bounded, expiring, and lost on restart.

That is the whole retention story for unselected mail. A cache entry survives
``header_cache_ttl_seconds`` and no longer, the cache holds at most
``header_cache_max_entries`` entries, and both numbers are settings so an
operator can turn retention down to zero without a code change.

Entries are keyed per user as well as per account, so two people browsing the
same shared mailbox never see each other's cache entry.
"""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .connectors.base import MessageHeader
from .settings import Settings, get_settings


@dataclass(frozen=True, slots=True)
class CacheKey:
    """Everything that changes the answer, and nothing that does not."""

    user_id: str
    account_id: str
    period: str
    folder_ids: tuple[str, ...] = ()
    subject_contains: str | None = None
    from_contains: str | None = None
    has_attachments_only: bool = False


@dataclass(slots=True)
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expirations: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "expirations": self.expirations,
        }


class HeaderCache:
    """A TTL + LRU cache of browse results.

    Values are deep-copied on the way in and on the way out. Callers score,
    annotate and sort the headers they get back; without copies one caller's
    ``score_message`` would quietly rewrite another's cached entry.
    """

    def __init__(self, *, ttl_seconds: int, max_entries: int, clock: Any = time.monotonic):
        self._ttl = max(0, int(ttl_seconds))
        self._max = max(0, int(max_entries))
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[CacheKey, tuple[float, list[MessageHeader]]] = OrderedDict()
        self.stats = CacheStats()

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> HeaderCache:
        settings = settings or get_settings()
        return cls(
            ttl_seconds=settings.header_cache_ttl_seconds,
            max_entries=settings.header_cache_max_entries,
        )

    @property
    def enabled(self) -> bool:
        return self._ttl > 0 and self._max > 0

    def get(self, key: CacheKey) -> list[MessageHeader] | None:
        if not self.enabled:
            self.stats.misses += 1
            return None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.stats.misses += 1
                return None
            expires_at, headers = entry
            if expires_at <= self._clock():
                del self._entries[key]
                self.stats.expirations += 1
                self.stats.misses += 1
                return None
            self._entries.move_to_end(key)
            self.stats.hits += 1
            return copy.deepcopy(headers)

    def put(self, key: CacheKey, headers: list[MessageHeader]) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._entries[key] = (self._clock() + self._ttl, copy.deepcopy(headers))
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
                self.stats.evictions += 1

    def invalidate_account(self, user_id: str, account_id: str) -> int:
        """Drop every entry for one connection.

        Called on disconnect: a revoked mailbox must stop being browsable
        immediately, not when its cache entry happens to expire.
        """
        with self._lock:
            doomed = [
                k for k in self._entries if k.user_id == user_id and k.account_id == account_id
            ]
            for key in doomed:
                del self._entries[key]
        return len(doomed)

    def purge_expired(self) -> int:
        now = self._clock()
        with self._lock:
            doomed = [k for k, (expires_at, _) in self._entries.items() if expires_at <= now]
            for key in doomed:
                del self._entries[key]
            self.stats.expirations += len(doomed)
        return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


_default_cache: HeaderCache | None = None
_default_lock = threading.Lock()


def get_header_cache(settings: Settings | None = None) -> HeaderCache:
    """The process-wide cache. One per worker, never shared between them."""
    global _default_cache
    with _default_lock:
        if _default_cache is None:
            _default_cache = HeaderCache.from_settings(settings)
        return _default_cache


def reset_header_cache() -> None:
    """Drop the process-wide cache. For tests and for settings reloads."""
    global _default_cache
    with _default_lock:
        _default_cache = None


__all__ = [
    "CacheKey",
    "CacheStats",
    "HeaderCache",
    "get_header_cache",
    "reset_header_cache",
]
