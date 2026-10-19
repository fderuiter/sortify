"""CacheManager opens both fresh and pre-manual_folders cache databases."""

from unittest.mock import MagicMock

from app.core.cache import CacheManager
from app.core.db_conn import clear_connection_cache, get_db_connection


def _columns(path):
    conn = get_db_connection(str(path))
    return {row[1] for row in conn.execute("PRAGMA table_info(directory_cache)")}


def test_fresh_cache_database_opens_twice(tmp_path):
    path = tmp_path / "cache.db"
    CacheManager(str(path), MagicMock())
    CacheManager(str(path), MagicMock())
    assert "manual_folders" in _columns(path)


def test_old_cache_database_gains_manual_folders(tmp_path):
    path = tmp_path / "cache.db"
    conn = get_db_connection(str(path))
    with conn:
        conn.execute(
            "CREATE TABLE directory_cache (source_directory TEXT PRIMARY KEY, "
            "corpus TEXT, locked_files TEXT, index_to_word TEXT)"
        )
    clear_connection_cache(only_current_and_inactive=False)

    CacheManager(str(path), MagicMock())
    assert "manual_folders" in _columns(path)
