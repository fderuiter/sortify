"""Central progress tracking dataclass and emission helper."""

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

logger = logging.getLogger(__name__)


@dataclass
class ProgressUpdate:
    """Standardized progress event structure across pipeline core modules."""

    progress: float = 0.0
    stage: Optional[str] = None
    unit_count: Optional[int] = None
    unit_type: Optional[str] = None

    def __post_init__(self) -> None:
        """Normalize progress ratio and coerce unit count."""
        try:
            val = float(self.progress)
        except (TypeError, ValueError):
            val = 0.0

        if val > 1.0 and val <= 100.0:
            val = val / 100.0

        self.progress = max(0.0, min(1.0, val))

        if self.unit_count is not None:
            try:
                self.unit_count = int(self.unit_count)
            except (TypeError, ValueError):
                self.unit_count = None


def emit_progress(
    callback: Optional[Callable[..., Any]],
    progress_or_update: Union[ProgressUpdate, float, int, str, None] = 0.0,
    stage: Optional[str] = None,
    unit_count: Optional[int] = None,
    unit_type: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """Normalize callback arguments and emit a ProgressUpdate instance to callback."""
    if not callback:
        return

    if isinstance(progress_or_update, ProgressUpdate):
        update = progress_or_update
    else:
        progress_val = 0.0
        stage_val = stage
        unit_count_val = unit_count
        unit_type_val = unit_type

        if isinstance(progress_or_update, str):
            stage_val = progress_or_update
            progress_val = float(kwargs.get("progress", 0.0))
        elif isinstance(progress_or_update, (int, float)):
            progress_val = float(progress_or_update)

        if unit_count_val is None and "unit_count" in kwargs:
            unit_count_val = kwargs["unit_count"]
        if unit_type_val is None and "unit_type" in kwargs:
            unit_type_val = kwargs["unit_type"]

        if stage_val is None and "message" in kwargs:
            stage_val = str(kwargs["message"])
        elif stage_val is None and "stage" in kwargs:
            stage_val = str(kwargs["stage"])

        update = ProgressUpdate(
            progress=progress_val,
            stage=stage_val,
            unit_count=unit_count_val,
            unit_type=unit_type_val,
        )

    # Inspect callback signature to determine argument delivery
    try:
        sig = inspect.signature(callback)
        params = list(sig.parameters.values())

        expects_update = False
        if params:
            first_p = params[0]
            if first_p.name == "update" or first_p.annotation == ProgressUpdate:
                expects_update = True

        if expects_update:
            callback(update)
            return

        pos_params = [
            p
            for p in params
            if p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]

        if len(pos_params) >= 2:
            if update.stage is not None:
                try:
                    callback(update.progress, update.stage)
                    return
                except TypeError:
                    pass
            try:
                callback(update.progress, update.stage)
                return
            except TypeError:
                pass

        if len(pos_params) == 1:
            try:
                callback(update.progress)
                return
            except TypeError:
                pass

        if len(pos_params) == 0:
            try:
                callback()
                return
            except TypeError:
                pass
    except (ValueError, TypeError):
        pass

    # Generic fallback if inspection unavailable or failed
    try:
        callback(update)
    except TypeError:
        try:
            if update.stage is not None:
                try:
                    callback(update.progress, update.stage)
                    return
                except TypeError:
                    pass
            try:
                callback(update.progress)
                return
            except TypeError:
                pass
            if update.stage is not None:
                try:
                    callback(update.stage)
                    return
                except TypeError:
                    pass
            callback()
        except Exception as e:
            logger.debug(f"Error invoking legacy progress callback: {e}")
    except Exception as e:
        logger.debug(f"Error invoking progress callback: {e}")


__all__ = ["ProgressUpdate", "emit_progress"]
