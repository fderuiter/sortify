#!/usr/bin/env python3
"""[DEPRECATED] Linter to prevent duplicate system utilities.

This script has been absorbed into 'scripts/validate_architecture.py'.
"""

from scripts.validate_architecture import (  # noqa: F401
    ALLOWED_FOR_CHARS,
    ALLOWED_FOR_FROZEN,
    ALLOWED_FOR_KEYS,
    ALLOWED_FOR_SESSIONS,
    DuplicatePatternVisitor,
)
from scripts.validate_architecture import (
    main as validate_architecture_main,
)


def main():
    """Deprecated entry point. Print warning and delegate to validate_architecture.py."""
    print(
        "DeprecationWarning: 'scripts/validate_duplicates.py' is deprecated. "
        "Running unified 'scripts/validate_architecture.py' instead.\n"
    )
    validate_architecture_main()


if __name__ == "__main__":
    main()
