from ttlcache import TTLCache


def test_set_and_get():
    c = TTLCache(2, 10)
    c.set("a", 1)
    assert c.get("a") == 1
    assert c.get("missing", "d") == "d"
