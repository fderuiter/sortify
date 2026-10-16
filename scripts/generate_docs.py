"""Backward-compatibility wrapper for documentation generator."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.cli.generate_docs import main  # noqa: E402

if __name__ == "__main__":
    main()
