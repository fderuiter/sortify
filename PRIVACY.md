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

Local databases are encrypted at rest. Each database is encrypted with SQLCipher, and extracted text and vectors are additionally encrypted per column. The key is stored in your operating system's keyring, or in a `0600` key file readable only by your user account when no keyring is available. If the SQLCipher library cannot be loaded or verified on a platform, Sortify fails closed and raises an error when opening a database.

Outside the folders you sort, Sortify writes local state to the following host directories (remove all of them to delete local application data from a machine):
- **`~/.autosorter/`** (or the directory set in `AUTOSORTER_APP_DIR`): the encrypted application store (`sortify.db`, including undo history, extracted text, vectors, and caches), configuration settings, logs, and daemon IPC files.
- **`~/.sortify/keys`** (POSIX/macOS) or **`%APPDATA%\Sortify\keys`** (Windows): fallback encryption key files (`0600` permissions) used when no OS keyring is available, alongside per-database `secret.key` files.
- **`~/.smart-autosorter/`**: downloaded or hydrated offline ML/ONNX model weights and bundles.
- **`autosorter_sessions_<user>`** inside the system temporary directory (unless `AUTOSORTER_APP_DIR` redirects sessions under `<AUTOSORTER_APP_DIR>/sessions`): user-isolated (`0700`) session directories and legacy session databases.
