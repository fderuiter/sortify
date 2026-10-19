---
status: accepted (recorded retroactively on 2026-10-19; the rationale is inferred from the code, so correct it if it is wrong)
---

# Extracted text and vectors are encrypted at rest with one per-database key

Every database that holds extracted text is opened through SQLCipher (`app/core/db_conn.py` swaps `sys.modules["sqlite3"]` for the vendored `sqlcipher3`), and the text and vector columns are additionally Fernet-encrypted (`app/core/crypto.py`). Both layers use the same key, looked up from the OS keyring first and a 0600 key file second. We accepted the cost (a native SQLCipher dependency, slower queries, key-migration code) because the extracted text is a plain-text copy of the user's sensitive documents.

## Consequences

- New stores of file content go through `resolve_db_crypto(db_path)`; a plain `sqlite3.connect` or an unencrypted cache file contradicts this ADR.
- When SQLCipher fails to import, `db_conn.py` currently falls back to the standard library `sqlite3`. That fallback is a gap against this decision, not a second supported mode.
