# Architecture Decision Record: Production-Scoped Duplication Check

## Date
2026-07-22

## Context
Previously, global code duplication checks would flag false positives on test datasets, experimental CLI scripts, and rapid sandbox prototyping files. This created friction for developers iterating on prototypes, slowing down velocity without providing real quality guardrails for core production code. We needed a way to strictly target core application code (`app/`) while freeing sandbox and root environments from blocking duplicate-code checks, without adding bulky external language-specific linting configurations.

## Decision
We decided to introduce a lightweight, production-scoped duplication gatekeeper using a custom, zero-dependency Python script (`scripts/validate_duplication.py`). The script parses code in the `app/` directory, normalizes lines by removing inline comments, multi-line docstrings, and whitespace, and implements a sliding window search to catch blocks of duplicated logic across core modules.

## Consequences
- **Velocity**: The script completes execution on core files in under 1 second, well within the CI limits.
- **Accuracy**: Normalization prevents trivial code blocks (like docstrings, comments, or short import structures) from triggering failures.
- **Maintainability**: The zero-dependency script avoids bulky external linting dependencies and does not require local setup.
- **Scope**: Experimental scripts and tests in non-production directories are successfully bypassed, enabling faster prototyping without friction.