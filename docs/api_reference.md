# API Reference

This document is automatically generated. Do not edit manually.

## Core Architecture Diagram

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
    core_crypto --> core_resilient_file_ops
    core_daemon --> core_domain_contracts
    core_daemon --> core_ipc
    core_daemon --> core_lifecycle
    core_daemon --> core_metadata
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
    core_resilient_file_ops --> core_extractor
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
    core_services --> core_db_conn
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

<a id="appcoresanitizer"></a>

## `app.cli`

::: app.cli

## `app.cli.error_emitter`

::: app.cli.error_emitter

## `app.cli.ledger_cli`

::: app.cli.ledger_cli

## `app.cli.model_cli`

::: app.cli.model_cli

## `app.cli.quarantine_cli`

::: app.cli.quarantine_cli

## `app.cli.rules_cli`

::: app.cli.rules_cli

## `app.cli.rules_crud`

::: app.cli.rules_crud

## `app.cli.settings_args`

::: app.cli.settings_args

## `app.cli.undo_cli`

::: app.cli.undo_cli

## `app.config`

::: app.config

## `app.core.accessible_html`

::: app.core.accessible_html

## `app.core.analyzer`

::: app.core.analyzer

## `app.core.analyzer_strategies`

::: app.core.analyzer_strategies

## `app.core.audit_reporter`

::: app.core.audit_reporter

## `app.core.cache`

::: app.core.cache

## `app.core.concurrency`

::: app.core.concurrency

## `app.core.crypto`

::: app.core.crypto

## `app.core.daemon`

::: app.core.daemon

## `app.core.db`

::: app.core.db

## `app.core.db_conn`

::: app.core.db_conn

## `app.core.db_worker`

::: app.core.db_worker

## `app.core.domain_contracts`

::: app.core.domain_contracts

## `app.core.downloader`

::: app.core.downloader

## `app.core.env_helper`

::: app.core.env_helper

## `app.core.exceptions`

::: app.core.exceptions

## `app.core.extractor`

::: app.core.extractor

## `app.core.extractor_strategies`

::: app.core.extractor_strategies

## `app.core.file_renamer`

::: app.core.file_renamer

## `app.core.folder_lock`

::: app.core.folder_lock

## `app.core.forensic_scanner`

::: app.core.forensic_scanner

## `app.core.hashes_registry`

::: app.core.hashes_registry

## `app.core.history`

::: app.core.history

## `app.core.ipc`

::: app.core.ipc

## `app.core.jev_classifier`

::: app.core.jev_classifier

## `app.core.ledger`

::: app.core.ledger

## `app.core.lifecycle`

::: app.core.lifecycle

## `app.core.link_manager`

::: app.core.link_manager

## `app.core.metadata`

::: app.core.metadata

## `app.core.migrations`

::: app.core.migrations

## `app.core.model_registry`

::: app.core.model_registry

## `app.core.move_retry`

::: app.core.move_retry

## `app.core.mover`

::: app.core.mover

## `app.core.moves`

::: app.core.moves

## `app.core.native_dialog`

::: app.core.native_dialog

## `app.core.network_guard`

::: app.core.network_guard

## `app.core.offline_loader`

::: app.core.offline_loader

## `app.core.path_utils`

::: app.core.path_utils

## `app.core.pattern_formatter`

::: app.core.pattern_formatter

## `app.core.pipeline`

::: app.core.pipeline

## `app.core.plan_format`

::: app.core.plan_format

## `app.core.plugin_registry`

::: app.core.plugin_registry

## `app.core.policy_engine`

::: app.core.policy_engine

## `app.core.progress`

::: app.core.progress

## `app.core.quarantine_interceptor`

::: app.core.quarantine_interceptor

## `app.core.resilient_file_ops`

::: app.core.resilient_file_ops

## `app.core.rule_importers`

::: app.core.rule_importers

## `app.core.rule_scopes`

::: app.core.rule_scopes

## `app.core.rule_templates`

::: app.core.rule_templates

## `app.core.sample_corpus`

::: app.core.sample_corpus

## `app.core.scanner`

::: app.core.scanner

## `app.core.security_advisory`

::: app.core.security_advisory

## `app.core.semantic_embeddings`

::: app.core.semantic_embeddings

## `app.core.services`

::: app.core.services

## `app.core.session`

::: app.core.session

## `app.core.session_catalog`

::: app.core.session_catalog

## `app.core.shared_registry`

::: app.core.shared_registry

## `app.core.simulation_exporter`

::: app.core.simulation_exporter

## `app.core.store`

::: app.core.store

## `app.core.text_utils`

::: app.core.text_utils

## `app.core.verifier`

::: app.core.verifier

## `app.core.worker_pool`

::: app.core.worker_pool

## `app.demo`

::: app.demo

## `app.frozen_runtime`

::: app.frozen_runtime

## `app.log_filter`

::: app.log_filter

## `app.main`

::: app.main

## `app.plugins.clinical_compliance`

::: app.plugins.clinical_compliance

## `app.plugins.clinical_compliance.clinical_compliance`

::: app.plugins.clinical_compliance.clinical_compliance

## `app.plugins.clinical_compliance.clinical_renamer`

::: app.plugins.clinical_compliance.clinical_renamer

## `app.plugins.clinical_compliance.clinical_strategy`

::: app.plugins.clinical_compliance.clinical_strategy

## `app.plugins.clinical_compliance.clinical_taxonomy`

::: app.plugins.clinical_compliance.clinical_taxonomy

## `app.plugins.clinical_compliance.cro_multi_study_pipeline`

::: app.plugins.clinical_compliance.cro_multi_study_pipeline

## `app.plugins.clinical_compliance.plugin`

::: app.plugins.clinical_compliance.plugin

## `app.plugins.clinical_compliance.study_disambiguator`

::: app.plugins.clinical_compliance.study_disambiguator

## `app.plugins.clinical_compliance.tui_views`

::: app.plugins.clinical_compliance.tui_views

