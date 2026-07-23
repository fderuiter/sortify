# Contributor Guide

Welcome to the Smart AutoSorter AI Pro project! This guide will help you get started with testing and contributing.

## Development Setup

To set up your local development environment and sync dependencies, run the standard uv commands:

```bash
uv sync
uv run pre-commit install
```

Once the setup is complete, verify your environment by running the test suite:

```bash
uv run pytest
```

## Interactive CLI Demo Mode

To help new developers quickly understand the end-to-end data flow without reviewing source code, the system includes an interactive demo mode.

### Running the Demo

The demo automatically generates a sample corpus of documents that meet the pipeline requirements (minimum of 3 documents). It then simulates the background extraction and clustering logic, ultimately printing the resulting sorting plan.

To run the interactive demo, use the following command:

```bash
uv run smart-autosorter --demo
```

### Automated Sample Corpus Utility

The demo mode leverages an internal automated utility (`generate_sample_corpus` in `app/demo.py`) that quickly builds a test dataset containing at least 3 documents. This satisfies the minimum document constraint required by the ML clustering engine. Developers can review this script to see how sample data (e.g., mock text files on finance and technology) is assembled for local testing.

## Centralized Backend System Utilities

To prevent redundant patterns, platform-specific bugs, and visual/functional defects across application scopes, we consolidate all system packaging, path character validation, and session database directory setup / encryption key lookups.

### 1. Unified Helpers Overview
All shared system utilities must reside in or be exposed through `app.core.path_utils`. Direct usage of custom platform/frozen hacks is strictly prohibited.

* **`is_packaged() -> bool`**: Returns `True` if running inside a frozen bundle (e.g., PyInstaller).
* **`get_base_path(caller_file_path: str = None) -> str`**: Standard application base path resolver. Always pass `__file__` when calling from another module so that mocked environments are correctly handled.
* **`validate_target_path(target_path: str, keyword: str = None) -> None`**: Standard target path validation for illegal characters, absolute paths, or traversal segments.
* **`setup_session_directory(session_id: str = None) -> tuple[str, Path]`**: Sets up the standard data/session directory in the OS temp folder.
* **`resolve_db_crypto(db_path: Path | str) -> SessionCrypto`**: Standard lookup function for session encryption keys and database decrypters.

### 2. Architectural Decisions & Rules
* **No Direct `sys.frozen` Checks:** Never use `getattr(sys, "frozen", False)` in modules. Use `is_packaged()` instead.
* **Consolidated Illegal Characters:** All paths and filenames must validate against `ILLEGAL_PATH_CHARS_SET` or `ILLEGAL_NAME_CHARS_SET` inside `app/core/path_utils.py`.
* **Standard Key Resolution:** Any database connection or database initialization must obtain its `SessionCrypto` instance via `resolve_db_crypto(db_path)`. Do not hardcode standard `secret.key` filenames or paths inside database modules.

