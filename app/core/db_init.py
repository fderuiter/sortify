from app.core.db import db
from app.core.cache import init_cache_db, DB_PATH
from app.core.history import init_history_db, history_manager

def init_databases():
    db.init_db()
    init_cache_db()
    init_history_db(history_manager.db_path)
