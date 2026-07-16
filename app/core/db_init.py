"""Database initialization logic."""

from app.core.cache import init_cache_db
from app.core.db import db
from app.core.history import history_manager, init_history_db


def init_databases():
    """Initialize all application databases."""
    db.init_db()
    init_cache_db()
    init_history_db(history_manager.db_path)
