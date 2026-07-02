"""
The enrichment cache — the highest-value piece of the caching story.

Enrichment labels (allergens, diet flags) are STABLE per ingredient set, so we memoize
by ingredient-set hash. Re-indexing, Kafka replays, and re-runs then skip the LLM/API
entirely. In production this is Redis; here it defaults to a disk-backed JSON cache so
the demo needs no running server. Set REDIS_URL to use real Redis.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from .config import REDIS_URL

_HERE = Path(__file__).resolve().parent
_DISK_PATH = _HERE.parent / "data" / ".enrich_cache.json"
_lock = threading.Lock()


class _DiskCache:
    def __init__(self, path: Path):
        self.path = path
        self._d: dict[str, str] = {}
        try:
            if path.is_file():
                self._d = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            self._d = {}

    def get(self, k: str) -> Optional[str]:
        return self._d.get(k)

    def set(self, k: str, v: str) -> None:
        with _lock:
            self._d[k] = v
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._d))
            except Exception:  # noqa: BLE001
                pass


class _RedisCache:
    def __init__(self, url: str):
        import redis  # type: ignore
        self.r = redis.from_url(url, decode_responses=True)
        self.prefix = "enrich:"

    def get(self, k: str) -> Optional[str]:
        try:
            return self.r.get(self.prefix + k)
        except Exception:  # noqa: BLE001
            return None

    def set(self, k: str, v: str) -> None:
        try:
            self.r.set(self.prefix + k, v)
        except Exception:  # noqa: BLE001
            pass


def _make_cache():
    if REDIS_URL:
        try:
            c = _RedisCache(REDIS_URL)
            c.r.ping()
            return c
        except Exception:  # noqa: BLE001
            pass  # fall back to disk if redis is unreachable
    return _DiskCache(_DISK_PATH)


_cache = _make_cache()

# simple hit/miss counters so the demo can *show* the cache working
stats = {"hits": 0, "misses": 0}


def get_json(key: str) -> Optional[dict]:
    raw = _cache.get(key)
    if raw is None:
        stats["misses"] += 1
        return None
    stats["hits"] += 1
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def set_json(key: str, value: dict) -> None:
    _cache.set(key, json.dumps(value))


def backend() -> str:
    return "redis" if isinstance(_cache, _RedisCache) else "disk"
