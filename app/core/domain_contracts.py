"""Pydantic v2 domain schema contract models and boundary validation functions."""

import logging
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.exceptions import InvalidStateTransitionError, SchemaValidationError
from app.core.jev_classifier import JevClassificationResult

logger = logging.getLogger(__name__)

VALID_QUARANTINE_STATES: frozenset[str] = frozenset(
    {
        "STAGED",
        "IN_INSPECTION",
        "QUARANTINED",
        "REDACTED",
        "RELEASED",
        "ARCHIVED",
        "UNARCHIVED",
        "DISMISSED",
        "MANUAL_REVIEW_REQUIRED",
        "DEAD_LETTER_QUEUE",
    }
)

TERMINAL_QUARANTINE_STATES: frozenset[str] = frozenset(
    {
        "RELEASED",
        "DISMISSED",
    }
)

QUARANTINE_TRANSITION_MATRIX: dict[Optional[str], frozenset[str]] = {
    None: frozenset({"STAGED"}),
    "STAGED": frozenset(
        {
            "STAGED",
            "IN_INSPECTION",
            "RELEASED",
            "DISMISSED",
            "MANUAL_REVIEW_REQUIRED",
            "DEAD_LETTER_QUEUE",
        }
    ),
    "IN_INSPECTION": frozenset(
        {
            "IN_INSPECTION",
            "QUARANTINED",
            "REDACTED",
            "RELEASED",
            "ARCHIVED",
            "UNARCHIVED",
            "MANUAL_REVIEW_REQUIRED",
            "DEAD_LETTER_QUEUE",
            "STAGED",
        }
    ),
    "REDACTED": frozenset(
        {
            "REDACTED",
            "RELEASED",
            "MANUAL_REVIEW_REQUIRED",
            "DEAD_LETTER_QUEUE",
        }
    ),
    "QUARANTINED": frozenset(
        {
            "QUARANTINED",
            "IN_INSPECTION",
            "RELEASED",
            "DISMISSED",
            "MANUAL_REVIEW_REQUIRED",
            "DEAD_LETTER_QUEUE",
        }
    ),
    "ARCHIVED": frozenset(
        {
            "ARCHIVED",
            "RELEASED",
            "DISMISSED",
            "MANUAL_REVIEW_REQUIRED",
            "DEAD_LETTER_QUEUE",
        }
    ),
    "UNARCHIVED": frozenset(
        {
            "UNARCHIVED",
            "RELEASED",
            "DISMISSED",
            "MANUAL_REVIEW_REQUIRED",
            "DEAD_LETTER_QUEUE",
        }
    ),
    "MANUAL_REVIEW_REQUIRED": frozenset(
        {
            "MANUAL_REVIEW_REQUIRED",
            "IN_INSPECTION",
            "RELEASED",
            "DISMISSED",
            "STAGED",
            "DEAD_LETTER_QUEUE",
        }
    ),
    "DEAD_LETTER_QUEUE": frozenset(
        {
            "DEAD_LETTER_QUEUE",
            "IN_INSPECTION",
            "RELEASED",
            "DISMISSED",
            "STAGED",
            "MANUAL_REVIEW_REQUIRED",
        }
    ),
    "RELEASED": frozenset(),
    "DISMISSED": frozenset(),
}


def validate_quarantine_transition(
    current_status: Optional[str], target_status: str, job_id: Optional[str] = None
) -> None:
    """Validate whether a quarantine status transition is permitted by the state machine.

    Raises InvalidStateTransitionError if the transition is disallowed or originates from a terminal state.
    Executes in microseconds (<1ms latency guardrail).
    """
    if target_status not in VALID_QUARANTINE_STATES:
        job_info = f" for job '{job_id}'" if job_id else ""
        raise InvalidStateTransitionError(
            f"Invalid target quarantine status '{target_status}'{job_info}. "
            f"Must be one of: {sorted(VALID_QUARANTINE_STATES)}"
        )

    if current_status is not None:
        if current_status not in VALID_QUARANTINE_STATES:
            job_info = f" for job '{job_id}'" if job_id else ""
            raise InvalidStateTransitionError(
                f"Invalid source quarantine status '{current_status}'{job_info}."
            )

        if current_status in TERMINAL_QUARANTINE_STATES:
            job_info = f" '{job_id}'" if job_id else ""
            raise InvalidStateTransitionError(
                f"Cannot transition terminal quarantine record{job_info} "
                f"from '{current_status}' to '{target_status}'. Terminal states are immutable."
            )

    allowed = QUARANTINE_TRANSITION_MATRIX.get(current_status, frozenset())
    if target_status not in allowed:
        job_info = f" for job '{job_id}'" if job_id else ""
        raise InvalidStateTransitionError(
            f"Invalid quarantine lifecycle transition from '{current_status}' to '{target_status}'{job_info}. "
            f"Allowed target states: {sorted(allowed)}"
        )


_SENTINEL = object()


class DictCompatMixin:
    """Reusable mixin providing dictionary subscripting, get, containment, and dict conversion."""

    @classmethod
    def _resolve_field_name(cls, key: str) -> str:
        """Resolve field alias to its underlying model field name if applicable."""
        fields = getattr(cls, "model_fields", None)
        if not fields:
            return key
        if key in fields:
            return key
        for field_name, field_info in fields.items():
            if (
                getattr(field_info, "alias", None) == key
                or getattr(field_info, "validation_alias", None) == key
            ):
                return field_name
        return key

    def __getitem__(self, item: str) -> Any:
        """Provide item lookup subscripting for dictionary backward compatibility."""
        attr = self._resolve_field_name(item)
        try:
            return getattr(self, attr)
        except AttributeError:
            extra = getattr(self, "__pydantic_extra__", None)
            if extra is not None and item in extra:
                return extra[item]
            raise KeyError(item) from None

    def get(self, item: str, default: Any = None) -> Any:
        """Provide dictionary get method for backward compatibility."""
        attr = self._resolve_field_name(item)
        val = getattr(self, attr, _SENTINEL)
        if val is not _SENTINEL:
            return val
        extra = getattr(self, "__pydantic_extra__", None)
        if extra is not None and item in extra:
            return extra[item]
        return default

    def __contains__(self, item: str) -> bool:
        """Provide in containment check for dictionary backward compatibility."""
        fields = getattr(type(self), "model_fields", None)
        if fields is not None:
            if item in fields:
                return True
            for field_info in fields.values():
                if (
                    getattr(field_info, "alias", None) == item
                    or getattr(field_info, "validation_alias", None) == item
                ):
                    return True
        extra = getattr(self, "__pydantic_extra__", None)
        return extra is not None and item in extra

    def dict(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Provide dict conversion method for backward compatibility."""
        if "by_alias" not in kwargs:
            kwargs["by_alias"] = True
        return self.model_dump(*args, **kwargs)  # ty: ignore[missing-attribute]


class QuarantineRecordModel(DictCompatMixin, BaseModel):
    """Pydantic v2 contract model for quarantine staging records."""

    job_id: str
    base_dir: Optional[str] = None
    original_filepath: Optional[str] = None
    staged_filepath: Optional[str] = None
    file_hash: Optional[str] = None
    status: str = "STAGED"
    policy_action: Optional[str] = None
    audit_log: list[Any] = Field(default_factory=list)
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
    error_message: Optional[str] = None
    error: Optional[str] = None

    model_config = ConfigDict(extra="allow")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        """Validate that the status value is a known quarantine lifecycle state."""
        if v not in VALID_QUARANTINE_STATES:
            raise ValueError(
                f"Invalid quarantine status: '{v}'. Must be one of {sorted(VALID_QUARANTINE_STATES)}"
            )
        return v


class SortingPlanNodeModel(DictCompatMixin, BaseModel):
    """Pydantic v2 contract model for sorting plan node payloads."""

    node_type: str = Field(default="file", alias="__type__")
    relative_source: Optional[str] = None
    routed_by: Optional[str] = None
    keyword: Optional[str] = None
    match: Optional[str] = None
    status: Optional[str] = None
    extraction_status: Optional[str | Any] = None
    is_corrected: Optional[bool] = None
    corrected: Optional[bool] = None
    is_overridden: Optional[bool] = None
    overridden: Optional[bool] = None
    original_lock_path: Optional[str] = None
    original_path: Optional[str] = None
    user_lock_path: Optional[str] = None
    historical_path: Optional[str] = None
    policy_path: Optional[str] = None
    new_policy_path: Optional[str] = None
    is_conflicted: Optional[bool] = None
    compliance_path: Optional[str] = None
    new_filename: Optional[str] = None
    category: Optional[str] = None
    sensitivity_rating: Optional[str] = None
    sensitivity_score: Optional[float] = None
    archival_priority: Optional[int | float] = None
    archival_priority_score: Optional[float] = None
    confidence: Optional[float] = None
    target_filename: Optional[str] = None
    is_locked: Optional[bool] = None
    confirmed: Optional[bool] = None
    is_confirmed: Optional[bool] = None
    user_confirmed: Optional[bool] = None
    jev_category: Optional[str] = None
    source_root: Optional[str] = None
    action: Optional[str] = None
    post_unarchive_action: Optional[str] = None
    post_unarchive_target: Optional[str] = None
    destination_override: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    @field_validator("confidence", mode="before")
    @classmethod
    def validate_confidence(cls, v: Any) -> Optional[float]:
        """Validate and clamp confidence score between 0.0 and 1.0."""
        if v is None:
            return None
        try:
            val = float(v)
            return max(0.0, min(1.0, val))
        except (ValueError, TypeError):
            return None

    @field_validator("node_type", mode="before")
    @classmethod
    def validate_node_type(cls, v: Any) -> str:
        """Validate and ensure node_type defaults to 'file' if empty."""
        if not v or not isinstance(v, str):
            return "file"
        return v


class SortingPlanModel(BaseModel):
    """Pydantic v2 contract model for complete sorting plans."""

    nodes: dict[str, Any] = Field(default_factory=dict)
    model_status: str = "HEALTHY"
    degradation_reason: Optional[str] = None
    recovery_action: Optional[str] = None
    suggested_recovery_action: Optional[str] = None
    failure_reason: Optional[str] = None
    invalid_locks: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow")

    def __getitem__(self, item: str) -> Any:
        """Provide item lookup subscripting for plan node dictionary compatibility."""
        if item in self.nodes:
            return self.nodes[item]
        try:
            return getattr(self, item)
        except AttributeError:
            raise KeyError(item) from None

    def get(self, item: str, default: Any = None) -> Any:
        """Provide dictionary get method for plan nodes."""
        if item in self.nodes:
            return self.nodes[item]
        return getattr(self, item, default)

    def __contains__(self, item: str) -> bool:
        """Provide in containment check for plan nodes."""
        if item in self.nodes or item in type(self).model_fields:
            return True
        return self.__pydantic_extra__ is not None and item in self.__pydantic_extra__

    def items(self) -> Any:
        """Return plan dictionary items."""
        return self.nodes.items()

    def keys(self) -> Any:
        """Return plan dictionary keys."""
        return self.nodes.keys()

    def values(self) -> Any:
        """Return plan dictionary values."""
        return self.nodes.values()

    def dict(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Provide dict conversion method for backward compatibility."""
        return self.model_dump(*args, **kwargs)


class CorpusExampleModel(DictCompatMixin, BaseModel):
    """Pydantic v2 contract model for individual historical corpus examples."""

    filepath: str
    user_verified_target_path: Optional[str] = None
    vector: Optional[list[float]] = None
    text: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class CorpusPreFetchBatchModel(BaseModel):
    """Pydantic v2 contract model for pre-fetched historical corpus batches."""

    model_metadata: dict[str, Any] = Field(default_factory=dict)
    examples: list[CorpusExampleModel] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")

    def get(self, key: str, default: Any = None) -> Any:
        """Provide dictionary get method for batch metadata and examples."""
        if key == "model_metadata":
            return self.model_metadata
        if key == "examples":
            return self.examples
        return getattr(self, key, default)

    def clear(self) -> None:
        """Clear model metadata and example lists."""
        self.model_metadata.clear()
        self.examples.clear()

    def __getitem__(self, item: str) -> Any:
        """Provide item lookup subscripting for model metadata and examples."""
        if item == "model_metadata":
            return self.model_metadata
        if item == "examples":
            return self.examples
        try:
            return getattr(self, item)
        except AttributeError:
            raise KeyError(item) from None

    def __contains__(self, item: str) -> bool:
        """Provide in containment check for batch attributes."""
        if item in ("model_metadata", "examples"):
            return True
        return item in type(self).model_fields or (
            self.__pydantic_extra__ is not None and item in self.__pydantic_extra__
        )

    def dict(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Provide dict conversion method for backward compatibility."""
        return self.model_dump(*args, **kwargs)


class VectorBatchPayloadModel(DictCompatMixin, BaseModel):
    """Pydantic v2 contract model for vector batch payloads."""

    base_dir: Optional[str] = None
    vectors: dict[str, Optional[list[float]]] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow")


def validate_quarantine_record(payload: Any) -> QuarantineRecordModel:
    """Validate quarantine record payload against QuarantineRecordModel."""
    if isinstance(payload, QuarantineRecordModel):
        return payload
    try:
        if isinstance(payload, dict):
            return QuarantineRecordModel.model_validate(payload)
        raise ValueError(f"Expected dict or QuarantineRecordModel, got {type(payload)}")
    except (ValidationError, ValueError, TypeError) as err:
        logger.error(
            "Schema contract validation failed for QuarantineRecordModel",
            extra={"payload": str(payload), "error": str(err)},
        )
        raise SchemaValidationError(
            f"QuarantineRecordModel validation failed: {err}",
            payload_context=str(payload),
        ) from err


def validate_jev_classification_result(payload: Any) -> JevClassificationResult:
    """Validate Jev classification payload against JevClassificationResult."""
    if isinstance(payload, JevClassificationResult):
        return payload
    try:
        if isinstance(payload, dict):
            return JevClassificationResult.model_validate(payload)
        raise ValueError(
            f"Expected dict or JevClassificationResult, got {type(payload)}"
        )
    except (ValidationError, ValueError, TypeError) as err:
        logger.error(
            "Schema contract validation failed for JevClassificationResult",
            extra={"payload": str(payload), "error": str(err)},
        )
        raise SchemaValidationError(
            f"JevClassificationResult validation failed: {err}",
            payload_context=str(payload),
        ) from err


def validate_corpus_prefetch_batch(payload: Any) -> CorpusPreFetchBatchModel:
    """Validate corpus pre-fetch batch payload against CorpusPreFetchBatchModel."""
    if isinstance(payload, CorpusPreFetchBatchModel):
        return payload
    try:
        if isinstance(payload, dict):
            return CorpusPreFetchBatchModel.model_validate(payload)
        raise ValueError(
            f"Expected dict or CorpusPreFetchBatchModel, got {type(payload)}"
        )
    except (ValidationError, ValueError, TypeError) as err:
        logger.error(
            "Schema contract validation failed for CorpusPreFetchBatchModel",
            extra={"payload": str(payload), "error": str(err)},
        )
        raise SchemaValidationError(
            f"CorpusPreFetchBatchModel validation failed: {err}",
            payload_context=str(payload),
        ) from err


def validate_vector_batch_payload(payload: Any) -> VectorBatchPayloadModel:
    """Validate vector batch payload against VectorBatchPayloadModel."""
    if isinstance(payload, VectorBatchPayloadModel):
        return payload
    try:
        if isinstance(payload, dict):
            return VectorBatchPayloadModel.model_validate(payload)
        raise ValueError(
            f"Expected dict or VectorBatchPayloadModel, got {type(payload)}"
        )
    except (ValidationError, ValueError, TypeError) as err:
        logger.error(
            "Schema contract validation failed for VectorBatchPayloadModel",
            extra={"payload": str(payload), "error": str(err)},
        )
        raise SchemaValidationError(
            f"VectorBatchPayloadModel validation failed: {err}",
            payload_context=str(payload),
        ) from err


def validate_sorting_plan_node(payload: Any) -> SortingPlanNodeModel:
    """Validate sorting plan node payload against SortingPlanNodeModel."""
    if isinstance(payload, SortingPlanNodeModel):
        return payload
    try:
        if isinstance(payload, dict):
            return SortingPlanNodeModel.model_validate(payload)
        raise ValueError(f"Expected dict or SortingPlanNodeModel, got {type(payload)}")
    except (ValidationError, ValueError, TypeError) as err:
        logger.error(
            "Schema contract validation failed for SortingPlanNodeModel",
            extra={"payload": str(payload), "error": str(err)},
        )
        raise SchemaValidationError(
            f"SortingPlanNodeModel validation failed: {err}",
            payload_context=str(payload),
        ) from err


def _make_json_serializable(obj: Any) -> Any:
    """Recursively convert model objects and dicts to JSON-serializable structures."""
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, dict):
        return {k: _make_json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_make_json_serializable(v) for v in obj]
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    return str(obj)


def _get_val(obj: Any, attr: str, default: Any = None) -> Any:
    """Safely retrieve attribute or key value from dictionary or domain model object."""
    if hasattr(obj, attr):
        val = getattr(obj, attr)
        if val is not None:
            return val
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return default


def _set_val(obj: Any, attr: str, val: Any) -> None:
    """Safely set attribute or key value on dictionary or domain model object."""
    if isinstance(obj, dict):
        obj[attr] = val
    elif hasattr(obj, attr):
        setattr(obj, attr, val)
    elif hasattr(obj, "plan") and isinstance(getattr(obj, "plan"), dict):
        obj.plan[attr] = val
    elif hasattr(obj, "__dict__"):
        setattr(obj, attr, val)
