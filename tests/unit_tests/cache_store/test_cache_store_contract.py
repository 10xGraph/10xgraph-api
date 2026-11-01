"""The public cache store contract, run against both built-in stores.

``RedisCacheStore`` runs only when ``TEST_REDIS_URL`` points at a Redis to use (a spare DB
number; the checks write keys under ``contract:<random>`` and let them expire).
"""

import os

import pytest

from tenxgraph_api import InMemoryCacheStore, RedisCacheStore
from tenxgraph_api.testing.cache_store_contract import CacheStoreContract


class TestInMemoryCacheStoreContract(CacheStoreContract):
    def make_store(self):
        return InMemoryCacheStore()


@pytest.mark.skipif(not os.environ.get("TEST_REDIS_URL"), reason="TEST_REDIS_URL is not set")
class TestRedisCacheStoreContract(CacheStoreContract):
    def make_store(self):
        return RedisCacheStore.from_url(os.environ["TEST_REDIS_URL"])
