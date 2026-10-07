"""Pydantic v2 domain schema contract models and boundary validation functions."""

import logging
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.exceptions import SchemaValidationError
from app.core.jev_classifier import JevClassificationResult

logger = logging.getLogger(__name__)


class QuarantineRecordModel(BaseModel):
    """Pydantic v2 contract model for quarantine staging records."""

    job_id: str
    base_dir: Optional[str] = None
    original_filepath: Optional[str] = None
    staged_filepath: Optional[str] = None
    file_hash: Optional[str] = None
    status: str = "STAGED"
    policy_action: Optional[str] = None
    audit_log: List[Any] = Field(default_factory=list)
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
    error_message: Optional[str] = None
    error: Optional[str] = None

    model_config = ConfigDict(extra="allow")

    def __getitem__(self, item: str) -> Any:
        """Provide item lookup subscripting for dictionary backward compatibility."""
        try:
            return getattr(self, item)
        except AttributeError:
            raise KeyError(item) from None

    def get(self, item: str, default: Any = None) -> Any:
        """Provide dictionary get method for backward compatibility."""
        return getattr(self, item, default)

    def __contains__(self, item: str) -> bool:
        """Provide in containment check for dictionary backward compatibility."""
        return item in self.model_fields or (
            self.__pydantic_extra__ is not None and item in self.__pydantic_extra__
        )

    def dict(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Provide dict conversion method for backward compatibility."""
        return self.model_dump(*args, **kwargs)


class SortingPlanNodeModel(BaseModel):
    """Pydantic v2 contract model for sorting plan node payloads."""

    node_type: str = Field(default="file", alias="__type__")
    relative_source: Optional[str] = None
    routed_by: Optional[str] = None
    keyword: Optional[str] = None
    match: Optional[str] = None
    status: Optional[str] = None
    extraction_status: Optional[Union[str, Any]] = None
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
    archival_priority: Optional[Union[int, float]] = None
    archival_priority_score: Optional[float] = None
    confidence: Optional[float] = None
    target_filename: Optional[str] = None
    is_locked: Optional[bool] = None
    confirmed: Optional[bool] = None
    is_confirmed: Optional[bool] = None
    user_confirmed: Optional[bool] = None
    jev_category: Optional[str] = None

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

    def __getitem__(self, item: str) -> Any:
        """Provide item lookup subscripting for dictionary backward compatibility."""
        if item == "__type__":
            return getattr(self, "node_type", "file")
        try:
            return getattr(self, item)
        except AttributeError:
            raise KeyError(item) from None

    def get(self, item: str, default: Any = None) -> Any:
        """Provide dictionary get method for backward compatibility."""
        if item == "__type__":
            return getattr(
                self, "node_type", default if default is not None else "file"
            )
        return getattr(self, item, default)

    def __contains__(self, item: str) -> bool:
        """Provide in containment check for dictionary backward compatibility."""
        if item == "__type__":
            return True
        return item in self.model_fields or (
            self.__pydantic_extra__ is not None and item in self.__pydantic_extra__
        )

    def dict(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Provide dict conversion method for backward compatibility."""
        if "by_alias" not in kwargs:
            kwargs["by_alias"] = True
        return self.model_dump(*args, **kwargs)


class SortingPlanModel(BaseModel):
    """Pydantic v2 contract model for complete sorting plans."""

    nodes: Dict[str, Any] = Field(default_factory=dict)

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
        if item in self.nodes or item in self.model_fields:
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

    def dict(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Provide dict conversion method for backward compatibility."""
        return self.model_dump(*args, **kwargs)


class CorpusExampleModel(BaseModel):
    """Pydantic v2 contract model for individual historical corpus examples."""

    filepath: str
    user_verified_target_path: Optional[str] = None
    vector: Optional[List[float]] = None
    text: Optional[str] = None

    model_config = ConfigDict(extra="allow")

    def __getitem__(self, item: str) -> Any:
        """Provide item lookup subscripting for dictionary backward compatibility."""
        try:
            return getattr(self, item)
        except AttributeError:
            raise KeyError(item) from None

    def get(self, item: str, default: Any = None) -> Any:
        """Provide dictionary get method for backward compatibility."""
        return getattr(self, item, default)

    def __contains__(self, item: str) -> bool:
        """Provide in containment check for dictionary backward compatibility."""
        return item in self.model_fields or (
            self.__pydantic_extra__ is not None and item in self.__pydantic_extra__
        )

    def dict(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Provide dict conversion method for backward compatibility."""
        return self.model_dump(*args, **kwargs)


class CorpusPreFetchBatchModel(BaseModel):
    """Pydantic v2 contract model for pre-fetched historical corpus batches."""

    model_metadata: Dict[str, Any] = Field(default_factory=dict)
    examples: List[CorpusExampleModel] = Field(default_factory=list)

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
        return item in self.model_fields or (
            self.__pydantic_extra__ is not None and item in self.__pydantic_extra__
        )

    def dict(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Provide dict conversion method for backward compatibility."""
        return self.model_dump(*args, **kwargs)


class VectorBatchPayloadModel(BaseModel):
    """Pydantic v2 contract model for vector batch payloads."""

    base_dir: Optional[str] = None
    vectors: Dict[str, Optional[List[float]]] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow")

    def get(self, key: str, default: Any = None) -> Any:
        """Provide dictionary get method for vector payload keys."""
        if key in ("base_dir", "vectors", "metadata"):
            return getattr(self, key)
        return getattr(self, key, default)

    def __getitem__(self, item: str) -> Any:
        """Provide item lookup subscripting for vector payload keys."""
        if item in ("base_dir", "vectors", "metadata"):
            return getattr(self, item)
        try:
            return getattr(self, item)
        except AttributeError:
            raise KeyError(item) from None

    def __contains__(self, item: str) -> bool:
        """Provide in containment check for vector payload keys."""
        if item in ("base_dir", "vectors", "metadata"):
            return True
        return item in self.model_fields or (
            self.__pydantic_extra__ is not None and item in self.__pydantic_extra__
        )

    def dict(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Provide dict conversion method for backward compatibility."""
        return self.model_dump(*args, **kwargs)


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
