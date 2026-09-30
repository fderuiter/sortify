# API Reference

This document is automatically generated. Do not edit manually.

## Core Architecture Diagram

```mermaid
flowchart TD
    A["app.main"]
    B["app.core.session"]
    C["app.core.extractor"]
    D["app.core.analyzer"]
    E["app.core.verifier"]
    F["app.core.sanitizer"]
    G["app.core.analyzer_strategies"]
    A --> B
    B --> C
    B --> D
    B --> E
    C --> F
    D --> G
    click A "docs/api_reference.md#appmain" "CLI Entrypoint Module"
    click B "docs/api_reference.md#appcoresession" "Session Management Module"
    click C "docs/api_reference.md#appcoreextractor" "Multi-format Text Extractor Module"
    click D "docs/api_reference.md#appcoreanalyzer" "Document Analyzer Module"
    click E "docs/api_reference.md#appcoreverifier" "Virtual Sorting Verifier Module"
    click F "docs/api_reference.md#appcoresanitizer" "Path & Input Sanitizer Module"
    click G "docs/api_reference.md#appcoreanalyzer_strategies" "Analysis Strategy Implementations"

```

## `app.config`

::: app.config

## `app.core.analyzer`

::: app.core.analyzer

## `app.core.analyzer_strategies`

::: app.core.analyzer_strategies

## `app.core.cache`

::: app.core.cache

## `app.core.clinical_compliance`

::: app.core.clinical_compliance

## `app.core.clinical_renamer`

::: app.core.clinical_renamer

## `app.core.clinical_strategy`

::: app.core.clinical_strategy

## `app.core.clinical_taxonomy`

::: app.core.clinical_taxonomy

## `app.core.cro_multi_study_pipeline`

::: app.core.cro_multi_study_pipeline

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

## `app.core.forensic_scanner`

::: app.core.forensic_scanner

## `app.core.hashes_registry`

::: app.core.hashes_registry

## `app.core.history`

::: app.core.history

## `app.core.integration`

::: app.core.integration

## `app.core.jev_classifier`

::: app.core.jev_classifier

## `app.core.ledger`

::: app.core.ledger

## `app.core.link_manager`

::: app.core.link_manager

## `app.core.metadata`

::: app.core.metadata

## `app.core.mover`

::: app.core.mover

## `app.core.offline_loader`

::: app.core.offline_loader

## `app.core.path_utils`

::: app.core.path_utils

## `app.core.policy_engine`

::: app.core.policy_engine

## `app.core.progress`

::: app.core.progress

## `app.core.quarantine_interceptor`

::: app.core.quarantine_interceptor

## `app.core.resilient_file_ops`

::: app.core.resilient_file_ops

## `app.core.scanner`

::: app.core.scanner

## `app.core.semantic_embeddings`

::: app.core.semantic_embeddings

## `app.core.session`

::: app.core.session

## `app.core.shared_registry`

::: app.core.shared_registry

## `app.core.study_disambiguator`

::: app.core.study_disambiguator

## `app.core.text_utils`

::: app.core.text_utils

## `app.core.user_space_bootstrap`

::: app.core.user_space_bootstrap

## `app.core.verifier`

::: app.core.verifier

## `app.demo`

::: app.demo

## `app.log_filter`

::: app.log_filter

## `app.main`

::: app.main

