"""Hidden tests for A1 — TTL LRU cache."""
import pytest

from solution import TTLCache


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def clock():
    return Clock()


def test_basic_set_get(clock):
    c = TTLCache(3, 60, clock=clock)
    c.set("a", 1)
    assert c.get("a") == 1
    assert c.stats() == {"hits": 1, "misses": 0, "evictions": 0, "expirations": 0}


def test_miss_unknown(clock):
    c = TTLCache(3, 60, clock=clock)
    assert c.get("nope") is None
    assert c.stats()["misses"] == 1


def test_ttl_expiry(clock):
    c = TTLCache(3, 10, clock=clock)
    c.set("a", 1)
    clock.advance(11)
    assert c.get("a") is None
    s = c.stats()
    assert s["misses"] == 1 and s["expirations"] == 1
    assert len(c) == 0


def test_ttl_boundary_still_alive(clock):
    c = TTLCache(3, 10, clock=clock)
    c.set("a", 1)
    clock.advance(10)  # ровно ttl — ещё жив (граница включительна)
    assert c.get("a") == 1


def test_set_refreshes_ttl(clock):
    c = TTLCache(3, 10, clock=clock)
    c.set("a", 1)
    clock.advance(9)
    c.set("a", 2)
    clock.advance(9)
    assert c.get("a") == 2


def test_lru_eviction(clock):
    c = TTLCache(2, 60, clock=clock)
    c.set("a", 1)
    c.set("b", 2)
    c.get("a")          # a — свежее b
    c.set("c", 3)       # вытесняет b
    assert c.get("b") is None
    assert c.get("a") == 1
    assert c.get("c") == 3
    assert c.stats()["evictions"] == 1


def test_update_no_eviction(clock):
    c = TTLCache(2, 60, clock=clock)
    c.set("a", 1)
    c.set("b", 2)
    c.set("a", 10)      # обновление, не вставка
    assert len(c) == 2
    assert c.stats()["evictions"] == 0


def test_update_refreshes_recency(clock):
    c = TTLCache(2, 60, clock=clock)
    c.set("a", 1)
    c.set("b", 2)
    c.set("a", 10)      # a теперь свежее b
    c.set("c", 3)       # вытесняет b
    assert c.get("b") is None
    assert c.get("a") == 10


def test_len_counts_live_only(clock):
    c = TTLCache(5, 10, clock=clock)
    c.set("a", 1)
    c.set("b", 2)
    clock.advance(11)
    c.set("c", 3)
    assert len(c) == 1


def test_delete(clock):
    c = TTLCache(3, 60, clock=clock)
    c.set("a", 1)
    assert c.delete("a") is True
    assert c.delete("a") is False
    assert c.get("a") is None


def test_expiration_counted_once(clock):
    c = TTLCache(3, 5, clock=clock)
    c.set("a", 1)
    clock.advance(6)
    c.get("a")
    c.get("a")
    assert c.stats()["expirations"] == 1
    assert c.stats()["misses"] == 2


def test_expired_not_evicted_but_expired(clock):
    # протухший ключ, обнаруженный при переполнении, — expiration, не eviction
    c = TTLCache(2, 5, clock=clock)
    c.set("a", 1)
    clock.advance(6)
    c.set("b", 2)
    c.set("c", 3)       # место есть? a протух — освобождается как expiration
    assert len(c) == 2
    s = c.stats()
    assert s["expirations"] == 1
    assert s["evictions"] == 0


def test_value_none_is_stored(clock):
    c = TTLCache(3, 60, clock=clock)
    c.set("a", None)
    s0 = c.stats()
    assert c.get("a") is None  # hit по хранимому None
    assert c.stats()["hits"] == s0["hits"] + 1


def test_default_clock_works():
    c = TTLCache(2, 60)
    c.set("a", 1)
    assert c.get("a") == 1


def test_invalid_maxsize():
    with pytest.raises(ValueError):
        TTLCache(0, 10)


def test_invalid_ttl(clock):
    with pytest.raises(ValueError):
        TTLCache(1, 0, clock=clock)


def test_stress_recency_order(clock):
    c = TTLCache(3, 100, clock=clock)
    for k in "abcde":
        c.set(k, k)
    assert len(c) == 3
    assert c.stats()["evictions"] == 2
    assert c.get("a") is None and c.get("b") is None
    assert c.get("c") == "c" and c.get("d") == "d" and c.get("e") == "e"
