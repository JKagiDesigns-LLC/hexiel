import threading
import pytest
from ttlcache import TTLCache


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(maxsize=3, ttl=10):
    clock = Clock()
    return TTLCache(maxsize, ttl, clock=clock), clock


def test_argument_validation():
    for bad in (0, -1, 1.5, "3"):
        with pytest.raises(ValueError):
            TTLCache(bad, 5)
    for bad in (0, -2):
        with pytest.raises(ValueError):
            TTLCache(2, bad)
    c, _ = make()
    with pytest.raises(ValueError):
        c.set("a", 1, ttl=0)
    with pytest.raises(ValueError):
        c.set("a", 1, ttl=-1)


def test_expires_exactly_at_the_deadline():
    c, clock = make(ttl=10)
    c.set("a", 1)
    clock.t += 9.999
    assert c.get("a") == 1
    clock.t += 0.001
    assert c.get("a", "gone") == "gone"


def test_per_entry_ttl_overrides_default():
    c, clock = make(ttl=10)
    c.set("short", 1, ttl=2)
    c.set("long", 2, ttl=50)
    clock.t += 5
    assert "short" not in c and c.get("long") == 2


def test_reading_does_not_extend_lifetime():
    c, clock = make(ttl=10)
    c.set("a", 1)
    clock.t += 6
    assert c.get("a") == 1
    clock.t += 5
    assert c.get("a") is None


def test_set_replaces_value_and_expiry():
    c, clock = make(ttl=10)
    c.set("a", 1)
    clock.t += 8
    c.set("a", 2)
    clock.t += 8
    assert c.get("a") == 2


def test_lru_eviction_uses_get_recency():
    c, _ = make(maxsize=2)
    c.set("a", 1)
    c.set("b", 2)
    c.get("a")
    c.set("c", 3)
    assert "b" not in c and "a" in c and "c" in c


def test_full_cache_drops_expired_before_evicting_a_live_entry():
    c, clock = make(maxsize=2, ttl=10)
    c.set("old", 1, ttl=1)
    c.set("keep", 2)
    clock.t += 5                       # old expired, keep alive
    c.set("new", 3)
    assert set(k for k, _ in c.items()) == {"keep", "new"}


def test_replacing_a_key_in_a_full_cache_evicts_nothing():
    c, _ = make(maxsize=2)
    c.set("a", 1)
    c.set("b", 2)
    c.set("a", 9)
    assert len(c) == 2 and c.get("b") == 2


def test_contains_does_not_change_recency():
    c, _ = make(maxsize=2)
    c.set("a", 1)
    c.set("b", 2)
    assert "a" in c
    c.set("c", 3)
    assert "a" not in c                # a was still the least recently used


def test_len_counts_only_live_entries():
    c, clock = make(ttl=10)
    c.set("a", 1, ttl=1)
    c.set("b", 2)
    clock.t += 2
    assert len(c) == 1


def test_delete_results():
    c, clock = make(ttl=10)
    c.set("a", 1, ttl=1)
    c.set("b", 2)
    clock.t += 2
    assert c.delete("a") is False       # expired counts as not there
    assert c.delete("b") is True
    assert c.delete("b") is False


def test_purge_returns_count():
    c, clock = make(ttl=10)
    c.set("a", 1, ttl=1)
    c.set("b", 2, ttl=1)
    c.set("c", 3)
    clock.t += 2
    assert c.purge() == 2
    assert c.purge() == 0
    assert len(c) == 1


def test_items_order_is_lru_to_mru_and_live_only():
    c, clock = make(maxsize=5, ttl=10)
    c.set("a", 1)
    c.set("b", 2, ttl=1)
    c.set("c", 3)
    c.get("a")
    clock.t += 2
    assert c.items() == [("c", 3), ("a", 1)]


def test_thread_safety():
    cache = TTLCache(50, 1000)
    errors = []

    def worker(n):
        try:
            for i in range(400):
                cache.set((n, i % 80), i)
                cache.get((n, (i * 7) % 80))
                len(cache)
                cache.items()
                if i % 50 == 0:
                    cache.purge()
        except Exception as exc:       # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(cache) <= 50
