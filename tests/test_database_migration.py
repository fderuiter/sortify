import sqlite3

from app.core.db import Database


def test_migration_from_v1(tmp_path):
    db_path = tmp_path / "test_v1.db"
    
    # Create v1 database
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 1")
        conn.execute("""
            CREATE TABLE documents (
                base_dir TEXT,
                filepath TEXT,
                file_hash TEXT,
                extracted_text TEXT,
                embedding BLOB,
                PRIMARY KEY (base_dir, filepath)
            )
        """)
        
    # Initialize Database, which should trigger migration
    Database(db_path=str(db_path))
    
    # Verify migration
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA user_version")
        assert cursor.fetchone()[0] == 4
        
        cursor.execute("PRAGMA table_info(documents)")
        columns = [row[1] for row in cursor.fetchall()]
        
        assert "user_verified_target_path" in columns
        assert "model_name" in columns
        assert "vector_dimension" in columns

def test_migration_from_v2(tmp_path):
    db_path = tmp_path / "test_v2.db"
    
    # Create v2 database
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 2")
        conn.execute("""
            CREATE TABLE documents (
                base_dir TEXT,
                filepath TEXT,
                file_hash TEXT,
                extracted_text TEXT,
                embedding BLOB,
                user_verified_target_path TEXT,
                PRIMARY KEY (base_dir, filepath)
            )
        """)
        
    # Initialize Database, which should trigger migration
    Database(db_path=str(db_path))
    
    # Verify migration
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA user_version")
        assert cursor.fetchone()[0] == 4
        
        cursor.execute("PRAGMA table_info(documents)")
        columns = [row[1] for row in cursor.fetchall()]
        
        assert "user_verified_target_path" in columns
        assert "model_name" in columns
        assert "vector_dimension" in columns

def test_migration_from_empty(tmp_path):
    db_path = tmp_path / "test_empty.db"
    
    # Initialize Database on empty file
    Database(db_path=str(db_path))
    
    # Verify creation
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA user_version")
        assert cursor.fetchone()[0] == 4
        
        cursor.execute("PRAGMA table_info(documents)")
        columns = [row[1] for row in cursor.fetchall()]
        
        assert "user_verified_target_path" in columns
        assert "model_name" in columns
        assert "vector_dimension" in columns
