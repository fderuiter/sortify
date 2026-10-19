# Privacy Policy

## Local Data Processing

Smart AutoSorter AI Pro is built around a privacy-first architecture. All processing of your documents, including text extraction, classification and clustering, happens on your own machine. Document contents, file names and the resulting folder structure are never sent to external servers or third parties.

The main local libraries involved are:
- **SQLCipher (SQLite):** local storage for extracted text, vectors, history and caches.
- **scikit-learn:** offline TF-IDF vectorization and clustering.
- **Optional `ml` extra:** local OCR, audio transcription, sentence embeddings and LLM folder naming. These models run on your machine.

## Network Access

The application has no telemetry, tracking or analytics. It makes network requests only for:
- **Model downloads.** With your consent (asked on first run), the embedding model is downloaded from Hugging Face. Declining keeps the app fully offline and uses TF-IDF clustering only.
- **Model security advisories.** Before a downloaded or side-loaded model is used, the app checks `security.huggingface.co` for advisories. The request contains the model's repository id and the model file's SHA-256 hash, never anything about your documents. The endpoint is configurable through the `SECURITY_ADVISORY_ENDPOINT` setting.

## Data Storage and Encryption

Local databases are encrypted at rest. Each database is encrypted with SQLCipher, and extracted text and vectors are additionally encrypted per column. The key is stored in your operating system's keyring, or in a key file readable only by your user account when no keyring is available. If the SQLCipher library can't be loaded on a platform, the app logs a security warning and falls back to plain SQLite pages; the per-column encryption of extracted text and vectors still applies.

Undo history and session data are kept in your user profile and the system temp folder. Deleting `~/.autosorter/` (or the folder set in `AUTOSORTER_APP_DIR`) removes stored settings and caches.
