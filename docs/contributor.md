# Contributor Guide

Welcome to the Smart AutoSorter AI Pro project! This page covers local setup and the architectural rules contributors must follow. Checks, baselines and pull request conventions are described in `CONTRIBUTING.md` at the repository root.

## Development Setup

To set up your local development environment and sync every dependency group (dev tools, docs, build), run:

![Contributor Onboarding Workflow](assets/diagrams/contributor_onboarding.svg)

```bash
uv sync --all-groups
uv run pre-commit install
```

Add `--extra ml` to also install the optional OCR, audio and embedding libraries.

Once the setup is complete, verify your environment by running the test suite:

```bash
uv run pytest -m "not slow"
```

## CLI Demo Mode

To help new developers see the end-to-end data flow without reading the source, the app has a demo mode. It runs on its own and needs no input.

### Running the Demo

The demo automatically generates a sample corpus of documents that meet the pipeline requirements (minimum of 3 documents). It then simulates the background extraction and clustering logic, ultimately printing the resulting sorting plan.

To run the interactive demo, use the following command:

```bash
uv run smart-autosorter --demo
```

### Automated Sample Corpus Utility

The demo builds its data with `generate_sample_corpus(base_dir, overwrite=False)` from `app/core/sample_corpus.py`, which `app/demo.py` and the TUI's sample-dataset key both use. It writes four sample files (finance, technology and health texts plus an empty file), enough for the clustering engine's three-document minimum. Read it to see how test data is assembled.

## Centralized Backend System Utilities

To prevent redundant patterns, platform-specific bugs, and visual/functional defects across application scopes, we consolidate all system packaging, path character validation, and session database directory setup / encryption key lookups.

### 1. Unified Helpers Overview
Shared path and packaging helpers live in `app.core.path_utils`, cryptography in `app.core.crypto` and secret scrubbing in `app.core.text_utils`. Don't write your own platform or frozen-bundle checks.

* **`is_packaged() -> bool`**: Returns `True` if running inside a frozen bundle (e.g., PyInstaller).
* **`get_base_path(caller_file_path: str = None) -> str`**: Standard application base path resolver. Always pass `__file__` when calling from another module so that mocked environments are correctly handled.
* **`validate_target_path(target_path: str, keyword: str = None) -> None`**: Standard target path validation for illegal characters, absolute paths, or traversal segments.
* **`is_subpath_or_equal(child: str, parent: str) -> bool`** (in `app.core.mover`, not `path_utils`): Cross-platform check that a candidate path is inside a parent directory.
* **`setup_session_directory(session_id: str = None) -> tuple[str, Path]`**: Creates the session directory under `$AUTOSORTER_APP_DIR/sessions`, or in the OS temp folder when that variable is unset.
* **`resolve_db_crypto(db_path: Path | str) -> SessionCrypto`**: Standard lookup function for session encryption keys and database decrypters.
* **`CryptoManager`**: Centralized facade (`app.core.crypto`) providing static methods for key derivation (`derive_db_hash`, `derive_keyring_account`, `derive_isolated_key_path`), envelope encryption (`encrypt_proxy_setting`, `decrypt_proxy_setting`), and pre-flight key generation (`generate_bootstrap_key`).
* **`sanitize_secret_patterns(text: str, replacement: str = "") -> str`**: Centralized secret scrubbing delegate (`app.core.text_utils`) that redacts API keys, JWTs, Bearer tokens, private keys, and high-entropy secret payloads across text streams and logging filters.

### 2. Architectural Decisions & Rules
* **No Direct `sys.frozen` Checks:** Never use `getattr(sys, "frozen", False)` in modules. Use `is_packaged()` instead.
* **Consolidated Illegal Characters:** All paths and filenames must validate against `ILLEGAL_PATH_CHARS_SET` or `ILLEGAL_NAME_CHARS_SET` inside `app/core/path_utils.py`.
* **Standard Key Resolution:** Any database connection or database initialization must obtain its `SessionCrypto` instance via `resolve_db_crypto(db_path)`. Do not hardcode standard `secret.key` filenames or paths inside database modules.
* **Centralized Cryptography & Secret Scrubbing:** Use `CryptoManager` static methods for key derivation and proxy envelope encryption. Route diagnostic and log scrubbing through `sanitize_secret_patterns()`.

* **Schema Changes Are Numbered Migrations:** Never change a table with `ALTER TABLE` inside `try/except`, or with `CREATE TABLE IF NOT EXISTS` on every open. Add a `Migration(version, name, apply)` to the end of that database's list (for the main database, `MAIN_DB_MIGRATIONS` in `app/core/db.py`). `app.core.migrations.migrate` runs each one once, in a transaction, and refuses a database newer than the code. Never edit a migration after it ships (ADR 0008).
