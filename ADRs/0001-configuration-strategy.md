# ADR 0001: Configuration Strategy

## Status
Accepted

## Context
Developers have been implementing local logic that shadows global configuration settings (such as `MAX_FOLDERS`), leading to inconsistent behavior and silent failures. This lack of clear configuration boundaries causes difficulty in debugging and maintaining the system. 

## Decision
We will use a centralized configuration validator (`core/validator.py`) to enforce all application-level limits. 
- All limits and constraints (e.g., maximum folders to generate based on dataset size) must be calculated in the central validator. 
- Local modules (like `core/analyzer.py`) must delegate constraint checking to this validator.
- The validator will run on application startup to ensure core parameters are valid and inspect the analysis engine to block any hardcoded logic shadowing these settings.

## Consequences
- **Positive:** Centralized source of truth. Any limits applied by the system will be trackable via logs.
- **Positive:** Reduced debugging time and clear visibility into logic paths.
- **Negative:** Slightly more overhead when adding new configuration parameters, as they must be incorporated into the validator class.
