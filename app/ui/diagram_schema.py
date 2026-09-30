"""Declarative Pydantic schema models for application, sequence, and state diagrams.

Re-exports core schema models from app.core.diagram_schema for presentation backward compatibility.
"""

from typing import Any, List

import app.core.diagram_schema as _core_ds
from app.core.diagram_schema import *  # noqa: F401, F403


def __getattr__(name: str) -> Any:
    """Delegate attribute access to app.core.diagram_schema for backward compatibility."""
    return getattr(_core_ds, name)


def __dir__() -> List[str]:
    """Delegate directory listing to app.core.diagram_schema for backward compatibility."""
    return dir(_core_ds)
