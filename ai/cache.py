"""
Lightweight in-memory AI cache to store intent extraction and generated responses.
"""
import time
from typing import Any, Optional

class AICache:
    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self._cache = {}

    def get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            value, timestamp = self._cache[key]
            if time.time() - timestamp <= self.ttl:
                return value
            else:
                del self._cache[key]
        return None

    def set(self, key: str, value: Any):
        self._cache[key] = (value, time.time())

    def clear(self):
        self._cache.clear()

# Global cache instance for shared use
ai_cache = AICache()
