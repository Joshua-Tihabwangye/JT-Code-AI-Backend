"""The Redis cache shares connection pools per process (a per-request pool leaked sockets under ASGI)."""

from __future__ import annotations

import threading
from pathlib import Path

from apps.core.cache import SharedPoolRedisCache

PARAMS = {"OPTIONS": {"socket_connect_timeout": 3, "socket_timeout": 3}}


def _pool(cache: SharedPoolRedisCache):
    return cache._cache._get_connection_pool(write=True)  # builds the pool; does not connect


def test_cache_objects_of_one_server_share_one_pool_even_from_other_threads():
    # Django creates a cache object per request context under ASGI; the stock backend built a new
    # connection pool for each, and the API leaked Redis sockets until it ran out of descriptors.
    first = SharedPoolRedisCache("redis://shared-pool-test:6379/0", PARAMS)
    pools = [_pool(first)]

    def other_request():
        pools.append(_pool(SharedPoolRedisCache("redis://shared-pool-test:6379/0", PARAMS)))

    for _ in range(3):
        thread = threading.Thread(target=other_request)
        thread.start()
        thread.join()
    assert len({id(pool) for pool in pools}) == 1


def test_different_servers_or_options_get_their_own_pools():
    a = _pool(SharedPoolRedisCache("redis://shared-pool-a:6379/0", PARAMS))
    b = _pool(SharedPoolRedisCache("redis://shared-pool-b:6379/0", PARAMS))
    c = _pool(SharedPoolRedisCache("redis://shared-pool-a:6379/0", {"OPTIONS": {"socket_timeout": 9}}))
    assert a is not b and a is not c


def test_the_deployed_cache_settings_use_the_shared_pool_backend():
    base = (Path(__file__).resolve().parents[1] / "config/settings/base.py").read_text()
    assert '"BACKEND": "apps.core.cache.SharedPoolRedisCache"' in base
    assert "django.core.cache.backends.redis.RedisCache" not in base
