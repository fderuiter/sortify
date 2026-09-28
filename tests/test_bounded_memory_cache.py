import concurrent.futures
import time

from app.core.cache import BoundedMemoryCache
from app.core.db import Database
from app.core.db_conn import (
    _connection_cache,
)
from app.core.db_worker import DBWorker
from app.core.link_manager import LinkManager
from app.core.semantic_embeddings import (
    _model_properties_cache,
)


def test_bounded_memory_cache_basic_ops():
    cache = BoundedMemoryCache(max_size=3)
    
    cache["a"] = 1
    cache["b"] = 2
    cache.put("c", 3)

    assert len(cache) == 3
    assert cache["a"] == 1
    assert cache.get("b") == 2
    assert cache.get("c") == 3
    assert "a" in cache
    assert "z" not in cache

    assert cache.invalidate("b") is True
    assert "b" not in cache
    assert len(cache) == 2

    assert cache.pop("c") == 3
    assert "c" not in cache

    cache.clear()
    assert len(cache) == 0


def test_bounded_memory_cache_lru_eviction():
    evicted_items = []

    def on_evict(k, v):
        evicted_items.append((k, v))

    cache = BoundedMemoryCache(max_size=3, on_evict=on_evict)

    cache["key1"] = 10
    cache["key2"] = 20
    cache["key3"] = 30

    # Access key1 to move it to most recently used
    _ = cache["key1"]

    # Insert key4 -> key2 should be evicted as least recently used
    cache["key4"] = 40

    assert len(cache) == 3
    assert "key2" not in cache
    assert "key1" in cache
    assert "key3" in cache
    assert "key4" in cache

    assert len(evicted_items) == 1
    assert evicted_items[0] == ("key2", 20)


def test_bounded_memory_cache_ttl_expiration():
    cache = BoundedMemoryCache(max_size=10, ttl=0.1)

    cache["temp1"] = "val1"
    cache.set("temp2", "val2", ttl=0.5)

    assert cache.get("temp1") == "val1"
    assert cache.get("temp2") == "val2"

    time.sleep(0.15)

    # temp1 should be expired now, temp2 should still be valid
    assert "temp1" not in cache
    assert cache.get("temp1") is None
    assert cache.get("temp2") == "val2"

    time.sleep(0.4)
    assert cache.get("temp2") is None


def test_bounded_memory_cache_on_evict_exception_resilience():
    def faulty_callback(k, v):
        raise RuntimeError("Callback failure!")

    cache = BoundedMemoryCache(max_size=2, on_evict=faulty_callback)

    # Adding items beyond max_size shouldn't raise exception despite callback failure
    cache["x"] = 1
    cache["y"] = 2
    cache["z"] = 3

    assert len(cache) == 2
    assert "x" not in cache


def test_bounded_memory_cache_multithreaded_concurrency():
    cache = BoundedMemoryCache(max_size=50)

    def worker(worker_id):
        for i in range(100):
            key = f"key_{worker_id}_{i % 20}"
            cache[key] = i
            _ = cache.get(f"key_{(worker_id - 1) % 5}_{i % 20}")
            if i % 10 == 0:
                cache.invalidate(key)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker, w) for w in range(8)]
        for f in futures:
            f.result()

    assert len(cache) <= 50


def test_link_manager_uses_bounded_memory_cache():
    assert isinstance(LinkManager._registry, BoundedMemoryCache)
    LinkManager.clear()
    assert len(LinkManager._registry) == 0


def test_db_conn_uses_bounded_memory_cache():
    assert isinstance(_connection_cache, BoundedMemoryCache)


def test_semantic_embeddings_uses_bounded_memory_cache():
    assert isinstance(_model_properties_cache, BoundedMemoryCache)


def test_database_cached_documents_uses_bounded_memory_cache(tmp_path):
    worker = DBWorker()
    try:
        db = Database(tmp_path / "test_docs.db", worker=worker)
        base_dir = str(tmp_path / "base")
        db.upsert_document(base_dir, "file1.txt", "hash1", "hello text")
        
        docs = db.get_all_documents(base_dir)
        assert len(docs) == 1
        assert isinstance(db._cached_documents, BoundedMemoryCache)
    finally:
        worker.stop()
