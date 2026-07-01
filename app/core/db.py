import sqlite3

import numpy as np


class Database:
    CURRENT_VERSION = 1
    
    def __init__(self, db_path="autosorter.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA user_version")
            db_version = cursor.fetchone()[0]
            
            if db_version == 0:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS documents (
                        base_dir TEXT,
                        filepath TEXT,
                        file_hash TEXT,
                        extracted_text TEXT,
                        embedding BLOB,
                        PRIMARY KEY (base_dir, filepath)
                    )
                ''')
                conn.execute(f"PRAGMA user_version = {self.CURRENT_VERSION}")
            elif db_version < self.CURRENT_VERSION:
                pass
            conn.commit()

    def get_document(self, base_dir, filepath):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('SELECT file_hash, extracted_text, embedding FROM documents WHERE base_dir = ? AND filepath = ?', (base_dir, filepath))
            row = cursor.fetchone()
            if row:
                embedding = np.frombuffer(row[2], dtype=np.float32) if row[2] else None
                return {"file_hash": row[0], "extracted_text": row[1], "embedding": embedding}
            return None

    def upsert_document(self, base_dir, filepath, file_hash, extracted_text, embedding):
        with sqlite3.connect(self.db_path) as conn:
            if embedding is not None:
                embedding_blob = embedding.astype(np.float32).tobytes()
            else:
                embedding_blob = None
            conn.execute('''
                INSERT INTO documents (base_dir, filepath, file_hash, extracted_text, embedding)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(base_dir, filepath) DO UPDATE SET
                    file_hash = excluded.file_hash,
                    extracted_text = excluded.extracted_text,
                    embedding = excluded.embedding
            ''', (base_dir, filepath, file_hash, extracted_text, embedding_blob))
            conn.commit()
            
    def get_all_documents(self, base_dir):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('SELECT filepath, extracted_text, embedding FROM documents WHERE base_dir = ? AND embedding IS NOT NULL', (base_dir,))
            results = []
            for row in cursor.fetchall():
                embedding = np.frombuffer(row[2], dtype=np.float32)
                results.append((row[0], row[1], embedding))
            return results

    def clear(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('DELETE FROM documents')
            conn.commit()

db = Database()
