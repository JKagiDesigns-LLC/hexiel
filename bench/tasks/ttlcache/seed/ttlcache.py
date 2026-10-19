"""A TTL + LRU cache. See README.md."""
import time


class TTLCache:
    def __init__(self, maxsize, ttl, clock=time.monotonic):
        raise NotImplementedError

    def set(self, key, value, ttl=None):
        raise NotImplementedError

    def get(self, key, default=None):
        raise NotImplementedError

    def __contains__(self, key):
        raise NotImplementedError

    def __len__(self):
        raise NotImplementedError

    def delete(self, key):
        raise NotImplementedError

    def purge(self):
        raise NotImplementedError

    def items(self):
        raise NotImplementedError
