# Smart AutoSorter AI Pro

Smart AutoSorter AI Pro (package `smart-autosorter`, displayed as **Sortify AI Pro** in the terminal interface) is a local, offline-first document organizer. It scans a directory, extracts document text across common office and media formats, applies deterministic routing rules before content clustering, and moves files into a structured folder tree with full undo support.

```mermaid
flowchart TD
    subgraph ui_layer ["Presentation & User Interfaces (app.tui)"]
        app_main("app.main (CLI Entry Point)")
        app_tui["app.tui (Textual Terminal Interface)"]
    end
    subgraph session_orchestration ["Session Orchestration & Lifecycle (app.core)"]
        core_daemon["app.core.daemon"]
        core_domain_contracts["app.core.domain_contracts"]
        core_folder_lock["app.core.folder_lock"]
        core_ipc["app.core.ipc"]
        core_lifecycle["app.core.lifecycle"]
        core_pipeline["app.core.pipeline"]
        core_services["app.core.services"]
        core_session["app.core.session"]
    end
    subgraph ingestion_extraction ["Ingestion & Extraction Engine (app.core)"]
        core_downloader["app.core.downloader"]
        core_extractor["app.core.extractor"]
        core_extractor_strategies["app.core.extractor_strategies"]
        core_forensic_scanner["app.core.forensic_scanner"]
        core_offline_loader["app.core.offline_loader"]
    end
    subgraph analytics_intelligence ["Analytics & Machine Learning (app.core)"]
        core_analyzer["app.core.analyzer"]
        core_analyzer_strategies["app.core.analyzer_strategies"]
        core_jev_classifier["app.core.jev_classifier"]
        core_semantic_embeddings["app.core.semantic_embeddings"]
    end
    subgraph plugin_extensibility ["Plugin Extensibility Architecture (app.core)"]
        core_plugin_registry["app.core.plugin_registry"]
    end
    subgraph memory_cache ["Caching & Memory Layer (app.core)"]
        core_cache[("app.core.cache (BoundedMemoryCache)")]
        core_db_conn["app.core.db_conn"]
        core_hashes_registry["app.core.hashes_registry"]
        core_link_manager["app.core.link_manager"]
    end
    subgraph concurrency_shared ["Concurrency & Shared Registry (app.core)"]
        core_concurrency["app.core.concurrency"]
        core_db_worker["app.core.db_worker"]
        core_model_registry["app.core.model_registry"]
        core_network_guard["app.core.network_guard"]
        core_shared_registry["app.core.shared_registry (re-exports)"]
        core_worker_pool["app.core.worker_pool (SharedWorkerPool)"]
    end
    subgraph storage_persistence ["Storage & Database Persistence (app.core)"]
        core_db[("app.core.db")]
        core_history[("app.core.history")]
        core_ledger[("app.core.ledger")]
        core_migrations["app.core.migrations"]
        core_moves[("app.core.moves")]
        core_session_catalog["app.core.session_catalog"]
        core_store[("app.core.store")]
    end
    subgraph execution_operations ["Policy, Validation & File Operations (app.core)"]
        core_accessible_html["app.core.accessible_html"]
        core_audit_reporter["app.core.audit_reporter"]
        core_file_renamer["app.core.file_renamer"]
        core_metadata["app.core.metadata"]
        core_move_retry["app.core.move_retry"]
        core_mover["app.core.mover"]
        core_pattern_formatter["app.core.pattern_formatter"]
        core_policy_engine{app.core.policy_engine}
        core_progress["app.core.progress"]
        core_quarantine_interceptor["app.core.quarantine_interceptor"]
        core_resilient_file_ops["app.core.resilient_file_ops"]
        core_rule_importers["app.core.rule_importers"]
        core_rule_scopes["app.core.rule_scopes"]
        core_rule_templates["app.core.rule_templates"]
        core_scanner["app.core.scanner"]
        core_simulation_exporter["app.core.simulation_exporter"]
        core_verifier{app.core.verifier}
    end
    subgraph utilities_security ["Security & Utilities (app.core)"]
        core_crypto["app.core.crypto"]
        core_env_helper["app.core.env_helper"]
        core_exceptions["app.core.exceptions"]
        core_native_dialog["app.core.native_dialog"]
        core_path_utils["app.core.path_utils"]
        core_plan_format["app.core.plan_format"]
        core_sample_corpus["app.core.sample_corpus"]
        core_security_advisory["app.core.security_advisory"]
        core_text_utils["app.core.text_utils"]
    end
    app_main --> app_tui
    app_main --> core_daemon
    app_main --> core_db_conn
    app_main --> core_domain_contracts
    app_main --> core_downloader
    app_main --> core_exceptions
    app_main --> core_ipc
    app_main --> core_metadata
    app_main --> core_moves
    app_main --> core_native_dialog
    app_main --> core_path_utils
    app_main --> core_pipeline
    app_main --> core_plan_format
    app_main --> core_quarantine_interceptor
    app_main --> core_resilient_file_ops
    app_main --> core_rule_importers
    app_main --> core_rule_templates
    app_main --> core_scanner
    app_main --> core_services
    app_main --> core_session
    app_main --> core_session_catalog
    app_main --> core_simulation_exporter
    app_main --> core_store
    app_main --> core_verifier
    app_tui --> core_audit_reporter
    app_tui --> core_db
    app_tui --> core_db_conn
    app_tui --> core_domain_contracts
    app_tui --> core_downloader
    app_tui --> core_extractor
    app_tui --> core_extractor_strategies
    app_tui --> core_ipc
    app_tui --> core_metadata
    app_tui --> core_mover
    app_tui --> core_native_dialog
    app_tui --> core_network_guard
    app_tui --> core_path_utils
    app_tui --> core_pattern_formatter
    app_tui --> core_pipeline
    app_tui --> core_plan_format
    app_tui --> core_plugin_registry
    app_tui --> core_policy_engine
    app_tui --> core_quarantine_interceptor
    app_tui --> core_resilient_file_ops
    app_tui --> core_rule_importers
    app_tui --> core_rule_templates
    app_tui --> core_sample_corpus
    app_tui --> core_scanner
    app_tui --> core_services
    app_tui --> core_session
    app_tui --> core_session_catalog
    app_tui --> core_shared_registry
    app_tui --> core_simulation_exporter
    app_tui --> core_text_utils
    app_tui --> core_verifier
    core_analyzer --> core_analyzer_strategies
    core_analyzer --> core_db
    core_analyzer --> core_db_conn
    core_analyzer --> core_domain_contracts
    core_analyzer --> core_exceptions
    core_analyzer --> core_extractor_strategies
    core_analyzer --> core_file_renamer
    core_analyzer --> core_jev_classifier
    core_analyzer --> core_metadata
    core_analyzer --> core_path_utils
    core_analyzer --> core_pattern_formatter
    core_analyzer --> core_policy_engine
    core_analyzer --> core_quarantine_interceptor
    core_analyzer --> core_resilient_file_ops
    core_analyzer --> core_rule_scopes
    core_analyzer --> core_scanner
    core_analyzer --> core_semantic_embeddings
    core_analyzer --> core_shared_registry
    core_analyzer --> core_text_utils
    core_analyzer_strategies --> core_analyzer
    core_analyzer_strategies --> core_cache
    core_analyzer_strategies --> core_crypto
    core_analyzer_strategies --> core_domain_contracts
    core_analyzer_strategies --> core_extractor_strategies
    core_analyzer_strategies --> core_path_utils
    core_analyzer_strategies --> core_plugin_registry
    core_analyzer_strategies --> core_semantic_embeddings
    core_analyzer_strategies --> core_shared_registry
    core_analyzer_strategies --> core_text_utils
    core_audit_reporter --> core_exceptions
    core_audit_reporter --> core_path_utils
    core_audit_reporter --> core_verifier
    core_cache --> core_db_conn
    core_cache --> core_db_worker
    core_cache --> core_exceptions
    core_cache --> core_migrations
    core_concurrency --> core_network_guard
    core_crypto --> core_db_conn
    core_crypto --> core_exceptions
    core_crypto --> core_ledger
    core_crypto --> core_path_utils
    core_daemon --> core_db
    core_daemon --> core_domain_contracts
    core_daemon --> core_ipc
    core_daemon --> core_lifecycle
    core_daemon --> core_mover
    core_daemon --> core_path_utils
    core_daemon --> core_pipeline
    core_daemon --> core_quarantine_interceptor
    core_daemon --> core_resilient_file_ops
    core_daemon --> core_rule_scopes
    core_daemon --> core_scanner
    core_daemon --> core_session
    core_daemon --> core_shared_registry
    core_daemon --> core_verifier
    core_db --> core_analyzer_strategies
    core_db --> core_cache
    core_db --> core_crypto
    core_db --> core_db_conn
    core_db --> core_db_worker
    core_db --> core_domain_contracts
    core_db --> core_exceptions
    core_db --> core_migrations
    core_db --> core_path_utils
    core_db --> core_services
    core_db --> core_shared_registry
    core_db --> core_store
    core_db --> core_text_utils
    core_db_conn --> core_cache
    core_db_conn --> core_crypto
    core_db_conn --> core_path_utils
    core_db_conn --> core_resilient_file_ops
    core_db_worker --> core_db_conn
    core_db_worker --> core_network_guard
    core_db_worker --> core_semantic_embeddings
    core_db_worker --> core_shared_registry
    core_domain_contracts --> core_exceptions
    core_domain_contracts --> core_jev_classifier
    core_downloader --> core_exceptions
    core_downloader --> core_lifecycle
    core_downloader --> core_network_guard
    core_downloader --> core_path_utils
    core_downloader --> core_progress
    core_downloader --> core_resilient_file_ops
    core_downloader --> core_security_advisory
    core_downloader --> core_shared_registry
    core_extractor --> core_extractor_strategies
    core_extractor --> core_network_guard
    core_extractor --> core_path_utils
    core_extractor --> core_progress
    core_extractor --> core_resilient_file_ops
    core_extractor --> core_scanner
    core_extractor --> core_shared_registry
    core_extractor --> core_text_utils
    core_extractor_strategies --> core_env_helper
    core_extractor_strategies --> core_exceptions
    core_extractor_strategies --> core_progress
    core_extractor_strategies --> core_shared_registry
    core_file_renamer --> core_mover
    core_file_renamer --> core_path_utils
    core_file_renamer --> core_pattern_formatter
    core_file_renamer --> core_text_utils
    core_forensic_scanner --> core_exceptions
    core_forensic_scanner --> core_extractor
    core_forensic_scanner --> core_extractor_strategies
    core_forensic_scanner --> core_path_utils
    core_forensic_scanner --> core_progress
    core_forensic_scanner --> core_resilient_file_ops
    core_history --> core_audit_reporter
    core_history --> core_db_conn
    core_history --> core_exceptions
    core_history --> core_extractor
    core_history --> core_ledger
    core_history --> core_lifecycle
    core_history --> core_migrations
    core_history --> core_mover
    core_history --> core_moves
    core_history --> core_path_utils
    core_history --> core_policy_engine
    core_history --> core_resilient_file_ops
    core_history --> core_services
    core_history --> core_shared_registry
    core_ipc --> core_pipeline
    core_jev_classifier --> core_cache
    core_jev_classifier --> core_db_conn
    core_jev_classifier --> core_extractor_strategies
    core_ledger --> core_crypto
    core_ledger --> core_db_conn
    core_ledger --> core_migrations
    core_ledger --> core_store
    core_ledger --> core_text_utils
    core_lifecycle --> core_text_utils
    core_link_manager --> core_cache
    core_link_manager --> core_path_utils
    core_metadata --> core_db
    core_metadata --> core_extractor
    core_metadata --> core_extractor_strategies
    core_metadata --> core_policy_engine
    core_metadata --> core_progress
    core_metadata --> core_rule_scopes
    core_metadata --> core_scanner
    core_model_registry --> core_env_helper
    core_model_registry --> core_exceptions
    core_model_registry --> core_hashes_registry
    core_model_registry --> core_jev_classifier
    core_model_registry --> core_network_guard
    core_model_registry --> core_offline_loader
    core_model_registry --> core_path_utils
    core_model_registry --> core_resilient_file_ops
    core_move_retry --> core_audit_reporter
    core_move_retry --> core_db_conn
    core_move_retry --> core_extractor
    core_move_retry --> core_mover
    core_move_retry --> core_path_utils
    core_move_retry --> core_resilient_file_ops
    core_mover --> core_audit_reporter
    core_mover --> core_db
    core_mover --> core_domain_contracts
    core_mover --> core_exceptions
    core_mover --> core_extractor
    core_mover --> core_forensic_scanner
    core_mover --> core_lifecycle
    core_mover --> core_link_manager
    core_mover --> core_move_retry
    core_mover --> core_moves
    core_mover --> core_path_utils
    core_mover --> core_pipeline
    core_mover --> core_progress
    core_mover --> core_quarantine_interceptor
    core_mover --> core_resilient_file_ops
    core_mover --> core_scanner
    core_mover --> core_shared_registry
    core_mover --> core_verifier
    core_moves --> core_db_conn
    core_moves --> core_lifecycle
    core_moves --> core_resilient_file_ops
    core_native_dialog --> core_env_helper
    core_network_guard --> core_concurrency
    core_offline_loader --> core_exceptions
    core_offline_loader --> core_path_utils
    core_offline_loader --> core_progress
    core_offline_loader --> core_resilient_file_ops
    core_offline_loader --> core_security_advisory
    core_offline_loader --> core_shared_registry
    core_offline_loader --> core_text_utils
    core_path_utils --> core_crypto
    core_path_utils --> core_text_utils
    core_pattern_formatter --> core_path_utils
    core_pipeline --> core_domain_contracts
    core_pipeline --> core_extractor
    core_pipeline --> core_forensic_scanner
    core_pipeline --> core_lifecycle
    core_pipeline --> core_metadata
    core_pipeline --> core_mover
    core_pipeline --> core_moves
    core_pipeline --> core_path_utils
    core_pipeline --> core_progress
    core_pipeline --> core_rule_scopes
    core_pipeline --> core_scanner
    core_pipeline --> core_services
    core_pipeline --> core_session
    core_pipeline --> core_verifier
    core_plugin_registry --> core_analyzer_strategies
    core_policy_engine --> core_extractor_strategies
    core_policy_engine --> core_path_utils
    core_policy_engine --> core_text_utils
    core_quarantine_interceptor --> core_analyzer_strategies
    core_quarantine_interceptor --> core_crypto
    core_quarantine_interceptor --> core_db
    core_quarantine_interceptor --> core_domain_contracts
    core_quarantine_interceptor --> core_exceptions
    core_quarantine_interceptor --> core_extractor
    core_quarantine_interceptor --> core_forensic_scanner
    core_quarantine_interceptor --> core_jev_classifier
    core_quarantine_interceptor --> core_mover
    core_quarantine_interceptor --> core_path_utils
    core_quarantine_interceptor --> core_pattern_formatter
    core_quarantine_interceptor --> core_plugin_registry
    core_quarantine_interceptor --> core_policy_engine
    core_quarantine_interceptor --> core_resilient_file_ops
    core_quarantine_interceptor --> core_text_utils
    core_resilient_file_ops --> core_exceptions
    core_resilient_file_ops --> core_mover
    core_resilient_file_ops --> core_path_utils
    core_rule_importers --> core_path_utils
    core_rule_importers --> core_pattern_formatter
    core_rule_importers --> core_policy_engine
    core_rule_importers --> core_rule_scopes
    core_rule_scopes --> core_policy_engine
    core_rule_templates --> core_rule_importers
    core_rule_templates --> core_rule_scopes
    core_sample_corpus --> core_policy_engine
    core_sample_corpus --> core_rule_templates
    core_scanner --> core_link_manager
    core_scanner --> core_path_utils
    core_security_advisory --> core_downloader
    core_security_advisory --> core_exceptions
    core_security_advisory --> core_network_guard
    core_semantic_embeddings --> core_cache
    core_semantic_embeddings --> core_db_conn
    core_semantic_embeddings --> core_exceptions
    core_semantic_embeddings --> core_hashes_registry
    core_semantic_embeddings --> core_resilient_file_ops
    core_semantic_embeddings --> core_shared_registry
    core_semantic_embeddings --> core_text_utils
    core_services --> core_db_worker
    core_services --> core_store
    core_session --> core_analyzer
    core_session --> core_analyzer_strategies
    core_session --> core_domain_contracts
    core_session --> core_folder_lock
    core_session --> core_move_retry
    core_session --> core_mover
    core_session --> core_path_utils
    core_session --> core_pipeline
    core_session --> core_resilient_file_ops
    core_session --> core_services
    core_session --> core_shared_registry
    core_session_catalog --> core_db_conn
    core_session_catalog --> core_history
    core_session_catalog --> core_moves
    core_session_catalog --> core_pipeline
    core_session_catalog --> core_services
    core_session_catalog --> core_session
    core_session_catalog --> core_store
    core_shared_registry --> core_concurrency
    core_shared_registry --> core_model_registry
    core_shared_registry --> core_network_guard
    core_shared_registry --> core_worker_pool
    core_simulation_exporter --> core_accessible_html
    core_simulation_exporter --> core_domain_contracts
    core_simulation_exporter --> core_path_utils
    core_simulation_exporter --> core_text_utils
    core_simulation_exporter --> core_verifier
    core_store --> core_cache
    core_store --> core_db
    core_store --> core_db_conn
    core_store --> core_exceptions
    core_store --> core_history
    core_store --> core_ledger
    core_store --> core_migrations
    core_store --> core_moves
    core_store --> core_path_utils
    core_verifier --> core_domain_contracts
    core_verifier --> core_link_manager
    core_verifier --> core_mover
    core_verifier --> core_offline_loader
    core_verifier --> core_path_utils
    core_verifier --> core_resilient_file_ops
    core_verifier --> core_scanner
    core_verifier --> core_shared_registry
    core_worker_pool --> core_concurrency
    core_worker_pool --> core_network_guard
```
## Key Capabilities

- **Rules first, clustering second.** Locked paths, compliance policies, sensitivity quarantine rules, keyword and imported rules, learned corrections, and file-extension categories run before unsupervised clustering.
- **Offline content clustering.** Remaining documents are vectorized with TF-IDF (or local ONNX sentence embeddings when optional ML extras are installed) and grouped hierarchically with `MiniBatchKMeans`. Folders are named from top TF-IDF terms or by a local LLM when generative naming is enabled.
- **Multi-format text extraction.** Extracts text from `.txt`, `.docx`, `.csv`, `.xlsx`, `.xls`, and `.pdf` out of the box, plus images (`.png`, `.jpg`, `.jpeg`) via OCR and audio (`.mp3`, `.wav`, `.m4a`) via Whisper when the `ml` extra is installed.
- **Verified, reversible file moves.** A pre-flight verifier checks every sorting plan for collisions, path-length limits, and directory boundary violations before touching the filesystem. Physical moves are journaled in a transactional ledger and can be rolled back with `undo`.
- **Encrypted local state.** Extracted text and embeddings are stored in a local SQLCipher database with column-level encryption; keys are managed through the OS keyring with a permission-restricted fallback key file.
- **Three runtime modes.** Drive sorting interactively in the Textual terminal UI (TUI), automate workflows from the scriptable CLI, or monitor folders continuously with the background daemon.

## Quick Start

Install from source with [uv](https://docs.astral.sh/uv/) and Python 3.12:

```bash
git clone https://github.com/fderuiter/sortify.git
cd sortify
uv sync                 # core install (TF-IDF offline clustering)
uv sync --extra ml      # optional: OCR, audio transcription, ONNX embeddings, local LLM naming
```

### Terminal UI (TUI)

Launch the full-screen terminal interface to browse directories, inspect and customize proposed folder trees, manage rules, and execute verified moves:

```bash
uv run smart-autosorter
uv run smart-autosorter --tui
```

Press `[Ctrl+B]` to choose a directory, `[Ctrl+S]` to scan and preview the plan, `[Ctrl+K]` to open the searchable Command Palette, `[Ctrl+E]` to execute verified moves, and `[?]` or `[F1]` to view shortcuts.

### Command Line (CLI)

Preview plans, run dry-run verifications, sort directories, or roll back the latest transaction directly from your shell:

```bash
uv run smart-autosorter scan ~/Downloads            # print proposed folder tree without moving files
uv run smart-autosorter sort ~/Downloads --dry-run  # run pre-flight verification without moving files
uv run smart-autosorter sort ~/Downloads            # execute verified sort in place
uv run smart-autosorter undo --latest               # roll back the most recent sort session
uv run smart-autosorter --demo                      # run end-to-end demo on a generated sample corpus
uv run smart-autosorter --help                      # list all subcommands and options
```

## Documentation Guide

### User & Administrator Guides

- [User Guide](user_guide.md) — First-run setup wizard, TUI workflows, CLI flags, rule management, imported rule migration, and undo history.
- [Keyboard Shortcuts](keyboard_shortcuts.md) — Complete keybinding reference across the main TUI workspace and modal dialogs.
- [Administrator Guide](admin_guide.md) — Layered configuration (`SORTIFY_` environment variables, `settings.json`, CLI flags), compliance policies, and enterprise deployment controls.
- [Troubleshooting](troubleshooting.md) — Solutions for common setup, permission, model download, keyring, and recovery issues.

### Interactive Tutorials & Runbooks

- [Stateful ML Analyzer Clustering](tutorials/01_ml_analyzer_clustering.md) — Walkthrough of incremental TF-IDF / embedding ingestion and recursive clustering.
- [Multi-Format Text Extraction](tutorials/02_multi_format_text_extraction.md) — How documents, spreadsheets, PDFs, images, and audio files are parsed into text chunks.
- [Virtual Sorting Verification](tutorials/03_virtual_sorting_verification.md) — Pre-flight collision checks and dry-run simulation before physical moves.
- [Ledger Reconciliation Runbook](runbooks/ledger_reconciliation.md) — Operational recovery procedures for inspecting and reconciling move ledger states.

### Developer Reference

- [Architecture & Internals](architecture.md) — End-to-end data flow, worker concurrency, state machines, and security boundaries.
- [API Reference](api_reference.md) — Auto-generated Python module, class, and function reference.
- [Contributor Guide](contributor.md) — Local development setup, test commands, and shared utility conventions.
- [Core Developer Guide](core.md) & [UI Developer Guide](ui.md) — Deep dives into `app/core/` backend subsystems and `app/ui/` Textual components.
- [Auto-Merge Policy](auto_merge_policy.md) & [Branch Protection Rules](branch_protection.md) — Repository risk tiers, CI gates, and merge governance.
