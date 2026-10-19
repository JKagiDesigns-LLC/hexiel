"""A TTL + LRU cache. See README.md."""
import threading
import time
from collections import OrderedDict


class TTLCache:
    def __init__(self, maxsize, ttl, clock=time.monotonic):
        if not isinstance(maxsize, int) or isinstance(maxsize, bool) or maxsize < 1:
            raise ValueError("maxsize must be an int >= 1")
        if ttl <= 0:
            raise ValueError("ttl must be > 0")
        self.maxsize, self.ttl, self._clock = maxsize, ttl, clock
        self._data = OrderedDict()            # key -> (value, expires_at); order = LRU -> MRU
        self._lock = threading.RLock()

    def _expired(self, expires_at):
        return self._clock() >= expires_at

    def _drop_expired(self):
        dead = [k for k, (_, exp) in self._data.items() if self._expired(exp)]
        for k in dead:
            del self._data[k]
        return len(dead)

    def set(self, key, value, ttl=None):
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl must be > 0")
        with self._lock:
            life = self.ttl if ttl is None else ttl
            if key in self._data:
                del self._data[key]
            elif len(self._data) >= self.maxsize:
                self._drop_expired()
                if len(self._data) >= self.maxsize:
                    self._data.popitem(last=False)
            self._data[key] = (value, self._clock() + life)

    def get(self, key, default=None):
        with self._lock:
            if key not in self._data:
                return default
            value, exp = self._data[key]
            if self._expired(exp):
                del self._data[key]
                return default
            self._data.move_to_end(key)
            return value

    def __contains__(self, key):
        with self._lock:
            return key in self._data and not self._expired(self._data[key][1])

    def __len__(self):
        with self._lock:
            return sum(1 for _, exp in self._data.values() if not self._expired(exp))

    def delete(self, key):
        with self._lock:
            if key not in self._data:
                return False
            live = not self._expired(self._data[key][1])
            del self._data[key]
            return live

    def purge(self):
        with self._lock:
            return self._drop_expired()

    def items(self):
        with self._lock:
            return [(k, v) for k, (v, exp) in self._data.items() if not self._expired(exp)]
