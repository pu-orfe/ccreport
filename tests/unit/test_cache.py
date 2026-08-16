from __future__ import annotations

from tests.fakes.connector import header

from ccreport.cache import CacheKey, HeaderCache, get_header_cache, reset_header_cache
from ccreport.settings import Settings


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def key(account: str = "acct", period: str = "2026-07", user: str = "user") -> CacheKey:
    return CacheKey(user_id=user, account_id=account, period=period)


def test_entries_expire_after_the_ttl() -> None:
    clock = Clock()
    cache = HeaderCache(ttl_seconds=600, max_entries=10, clock=clock)
    cache.put(key(), [header("m1")])

    assert cache.get(key()) is not None
    clock.now += 601
    assert cache.get(key()) is None
    assert cache.stats.expirations == 1


def test_least_recently_used_entry_is_evicted_first() -> None:
    cache = HeaderCache(ttl_seconds=600, max_entries=2, clock=Clock())
    cache.put(key(account="a"), [header("m1")])
    cache.put(key(account="b"), [header("m2")])
    cache.get(key(account="a"))  # touch a, so b is now the oldest
    cache.put(key(account="c"), [header("m3")])

    assert cache.get(key(account="a")) is not None
    assert cache.get(key(account="b")) is None
    assert cache.stats.evictions == 1


def test_values_are_copied_so_scoring_one_result_cannot_rewrite_the_cache() -> None:
    cache = HeaderCache(ttl_seconds=600, max_entries=10, clock=Clock())
    cache.put(key(), [header("m1", "Original")])

    borrowed = cache.get(key())
    borrowed[0].subject = "Mutated"
    borrowed[0].receipt_score = 99

    fresh = cache.get(key())
    assert fresh[0].subject == "Original"
    assert fresh[0].receipt_score == 0


def test_one_users_cache_entry_is_invisible_to_another() -> None:
    cache = HeaderCache(ttl_seconds=600, max_entries=10, clock=Clock())
    cache.put(key(user="ada"), [header("m1")])
    assert cache.get(key(user="grace")) is None


def test_disconnecting_an_account_drops_its_entries_immediately() -> None:
    cache = HeaderCache(ttl_seconds=600, max_entries=10, clock=Clock())
    cache.put(key(account="gone", period="2026-07"), [header("m1")])
    cache.put(key(account="gone", period="2026-06"), [header("m2")])
    cache.put(key(account="kept"), [header("m3")])

    assert cache.invalidate_account("user", "gone") == 2
    assert cache.get(key(account="gone")) is None
    assert cache.get(key(account="kept")) is not None


def test_a_zero_ttl_disables_caching_entirely() -> None:
    """Retention can be turned down to nothing without a code change."""
    cache = HeaderCache(ttl_seconds=0, max_entries=10, clock=Clock())
    cache.put(key(), [header("m1")])
    assert not cache.enabled
    assert cache.get(key()) is None
    assert len(cache) == 0


def test_process_cache_is_built_from_settings_and_resettable() -> None:
    reset_header_cache()
    cache = get_header_cache(Settings(header_cache_ttl_seconds=30, header_cache_max_entries=7))
    assert cache is get_header_cache()
    assert cache._ttl == 30 and cache._max == 7
    reset_header_cache()
    assert get_header_cache(Settings()) is not cache
