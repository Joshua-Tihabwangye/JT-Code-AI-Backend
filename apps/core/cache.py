"""Django's Redis cache with connection pools shared by the whole process.

Under ASGI, Django keeps cache objects per request context, so the stock ``RedisCache`` built a new
client, and with it a new connection pool, for every request (health probes included). Nothing closed
those pools, and under uvicorn they were not collected either: the API's Redis sockets grew with every
request until the worker ran out of file descriptors and failed requests. Here each (servers, options)
pair has one set of pools per process. redis-py pools are thread-safe and reset themselves after a fork.
"""

from __future__ import annotations

import threading
from typing import Any

from django.core.cache.backends.redis import RedisCache, RedisCacheClient

_SHARED_POOLS: dict[tuple[Any, ...], dict[int, Any]] = {}
_LOCK = threading.Lock()


class SharedPoolRedisCacheClient(RedisCacheClient):
    def __init__(self, servers: list[str], **options: Any) -> None:
        super().__init__(servers, **options)
        key = (tuple(servers), repr(sorted(self._pool_options.items(), key=lambda item: item[0])))
        with _LOCK:
            self._pools = _SHARED_POOLS.setdefault(key, {})

    def _get_connection_pool(self, write: bool) -> Any:
        with _LOCK:
            return super()._get_connection_pool(write)


class SharedPoolRedisCache(RedisCache):
    def __init__(self, server: str, params: dict[str, Any]) -> None:
        super().__init__(server, params)
        self._class = SharedPoolRedisCacheClient
