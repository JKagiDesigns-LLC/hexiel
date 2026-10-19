# ttlcache

`TTLCache(maxsize, ttl, clock=time.monotonic)` — a small in-memory cache that combines a **time limit** and a
**least-recently-used** size limit.

* `maxsize` must be an `int >= 1` and `ttl` a number `> 0`, otherwise `ValueError`.
* `clock` is a zero-argument function returning the current time in seconds (tests pass a fake one).
* `set(key, value, ttl=None)` stores the value. A per-entry `ttl` overrides the default; `None` means "use the default";
  any `ttl <= 0` raises `ValueError`. Setting an existing key replaces its value and expiry and makes it the most recently used.
  When the cache is full, first drop every expired entry; if it is still full, evict the least recently used entry.
* `get(key, default=None)` returns the value if the key is present and **not expired**, and marks it the most recently used.
  Reading does **not** extend the entry's lifetime. An expired entry is removed when it is looked up and `default` is returned.
* An entry expires when `clock() >= stored_at + ttl` — exactly at the deadline it is already expired.
* `key in cache` is `True` only for live entries and does **not** change the recency order.
* `len(cache)` counts only live (non-expired) entries.
* `delete(key)` removes the key and returns `True`, or `False` if it was not there (an expired entry counts as not there).
* `purge()` removes every expired entry and returns how many were removed.
* `items()` returns a list of `(key, value)` for the live entries, ordered from least to most recently used.
* All methods are safe to call from several threads at once.
