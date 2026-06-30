# ADR 0002: Logging Strategy

## Status
Accepted

## Context
Previously, errors in the analysis engine were either silently caught (e.g. ValueError when TF-IDF fitting fails) or logging was duplicated across different modules (e.g. `extractor.py` configuring its own `logging.basicConfig`). This fragmentation makes it hard to trace application-wide execution flows and diagnose failures.

## Decision
We will implement a unified logging framework (`core/logger.py`) that exports a centrally configured `logger` instance.
- All core modules (`analyzer.py`, `extractor.py`, `mover.py`) must import and use this central logger.
- The analysis engine will log failures with full stack traces (`exc_info=True`) and input context instead of returning silently.
- The configuration validator will log warnings and info messages when limits are actively applied, giving developers clear insight into system decisions.

## Consequences
- **Positive:** A single standard format for logs across the entire pipeline.
- **Positive:** Complete visibility into silent failures, particularly during document text processing and ML modeling.
- **Negative:** Requires ensuring the logger is initialized correctly prior to parallel processing to avoid file locking issues, although basic Python `logging` handles this adequately for our current scale.
