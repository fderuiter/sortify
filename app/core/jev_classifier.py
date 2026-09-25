"""Jev Non-Generative Fast-Path Classifier Engine.

Provides ultra-fast, local, deterministic document classification, sensitivity rating,
and archival prioritization within a sub-150 ms SLA to bypass heavy generative AI processing.
"""

import logging
import os
import time
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class JevClassificationResult(BaseModel):
    """Typed Pydantic schema for Jev non-generative classification results."""

    category: str = Field(default="Unclassified")
    sensitivity_rating: str = Field(default="LOW")
    sensitivity_score: float = Field(default=0.0)
    archival_priority: int = Field(default=3)
    archival_priority_score: float = Field(default=0.0)
    confidence: float = Field(default=0.0)
    is_classified: bool = Field(default=False)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def model_dump(self, *args, **kwargs) -> Dict[str, Any]:
        """Dump the result as a standard dictionary."""
        d = super().model_dump(*args, **kwargs)
        return d

    def dict(self, *args, **kwargs) -> Dict[str, Any]:
        """Backward compatibility method for dict serialization."""
        return self.model_dump(*args, **kwargs)

    def __getitem__(self, item: str) -> Any:
        """Support bracket access for dictionary compatibility."""
        if hasattr(self, item):
            return getattr(self, item)
        extra = getattr(self, "__pydantic_extra__", None)
        if extra and item in extra:
            return extra[item]
        raise KeyError(item)

    def get(self, item: str, default: Any = None) -> Any:
        """Support dictionary get method."""
        try:
            return self[item]
        except KeyError:
            return default

    def __contains__(self, item: str) -> bool:
        """Support membership check for dictionary compatibility."""
        return hasattr(self, item) or (
            getattr(self, "__pydantic_extra__", None) is not None
            and item in self.__pydantic_extra__
        )


class JevClassifierEngine:
    """Thread-safe, non-generative document classification engine."""

    CATEGORY_RULES = {
        "Financial": {
            "keywords": [
                "invoice", "receipt", "tax", "statement", "w2", "financial",
                "payroll", "accounting", "balance", "ledger", "audit",
                "billing", "expense", "revenue", "invoice_", "receipt_"
            ],
            "extensions": [".csv", ".xlsx", ".xls"],
            "sensitivity_rating": "HIGH",
            "sensitivity_score": 0.85,
            "archival_priority": 1,
            "archival_priority_score": 0.90,
            "target_category": "Financial Reports",
        },
        "Legal": {
            "keywords": [
                "contract", "nda", "agreement", "patent", "compliance",
                "legal", "terms", "license", "privacy", "lawsuit", "court",
                "bylaws", "affidavit", "subpoena"
            ],
            "extensions": [".pdf", ".docx"],
            "sensitivity_rating": "HIGH",
            "sensitivity_score": 0.80,
            "archival_priority": 1,
            "archival_priority_score": 0.85,
            "target_category": "Legal Documents",
        },
        "Medical": {
            "keywords": [
                "patient", "clinical", "medical", "lab_report", "prescription",
                "health", "doctor", "hipaa", "diagnosis", "hospital", "pharma",
                "pathology", "radiology", "blood_work"
            ],
            "extensions": [],
            "sensitivity_rating": "CRITICAL",
            "sensitivity_score": 0.95,
            "archival_priority": 1,
            "archival_priority_score": 0.95,
            "target_category": "Medical & Clinical Records",
        },
        "Personal": {
            "keywords": [
                "passport", "id_card", "driver_license", "resume", "cv",
                "ssn", "tax_return", "personal", "birth_certificate"
            ],
            "extensions": [],
            "sensitivity_rating": "HIGH",
            "sensitivity_score": 0.90,
            "archival_priority": 2,
            "archival_priority_score": 0.70,
            "target_category": "Personal Records",
        },
        "Technical": {
            "keywords": [
                "log", "config", "spec", "build", "code", "readme",
                "script", "source", "repo", "database", "schema", "api_spec"
            ],
            "extensions": [".py", ".js", ".json", ".yaml", ".yml", ".xml", ".log", ".sql", ".sh", ".bat"],
            "sensitivity_rating": "LOW",
            "sensitivity_score": 0.20,
            "archival_priority": 4,
            "archival_priority_score": 0.30,
            "target_category": "Technical & Data Assets",
        },
        "Administrative": {
            "keywords": [
                "memo", "agenda", "minutes", "report", "presentation",
                "deck", "slides", "meeting", "notice", "schedule", "plan"
            ],
            "extensions": [".pptx", ".ppt", ".key"],
            "sensitivity_rating": "MEDIUM",
            "sensitivity_score": 0.50,
            "archival_priority": 3,
            "archival_priority_score": 0.50,
            "target_category": "Administrative Records",
        },
    }

    def __init__(self, confidence_threshold: float = 0.5):
        """Initialize JevClassifierEngine with confidence threshold."""
        self.confidence_threshold = confidence_threshold

    def classify(
        self, file_path: str, text_content: Optional[str] = None
    ) -> JevClassificationResult:
        """Classify a file using deterministic non-generative fast-path rules.

        Executes locally within sub-150 ms SLA.
        """
        start_time = time.perf_counter()
        try:
            if not file_path:
                return JevClassificationResult()

            fn_lower = os.path.basename(file_path).lower()
            ext = os.path.splitext(file_path)[1].lower()

            # Fast snippet read if text content not provided
            snippet = ""
            if text_content is not None:
                snippet = str(text_content)[:4096].lower()
            elif os.path.exists(file_path) and os.path.isfile(file_path):
                # Read at most 4KB snippet if readable file and size < 10MB
                try:
                    if os.path.getsize(file_path) <= 10 * 1024 * 1024 and ext in {
                        ".txt", ".csv", ".json", ".xml", ".yaml", ".yml",
                        ".log", ".md", ".py", ".js", ".html", ".htm"
                    }:
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            snippet = f.read(4096).lower()
                except Exception:
                    pass

            text_to_check = f"{fn_lower} {snippet}"

            best_match = None
            best_score = 0.0

            for cat_name, rules in self.CATEGORY_RULES.items():
                match_score = 0.0
                # Check keyword matches
                kw_matches = sum(1 for kw in rules["keywords"] if kw in text_to_check)
                if kw_matches > 0:
                    match_score += min(0.6 + (kw_matches - 1) * 0.15, 0.95)

                # Check extension matches
                if ext in rules["extensions"]:
                    match_score += 0.35

                if match_score > best_score:
                    best_score = min(match_score, 1.0)
                    best_match = (cat_name, rules)

            elapsed_ms = (time.perf_counter() - start_time) * 1000.0

            if best_match and best_score >= self.confidence_threshold:
                cat_name, rules = best_match
                target_cat = rules.get("target_category", cat_name)
                return JevClassificationResult(
                    category=target_cat,
                    sensitivity_rating=rules["sensitivity_rating"],
                    sensitivity_score=rules["sensitivity_score"],
                    archival_priority=rules["archival_priority"],
                    archival_priority_score=rules["archival_priority_score"],
                    confidence=best_score,
                    is_classified=True,
                    metadata={
                        "raw_category": cat_name,
                        "latency_ms": elapsed_ms,
                        "file_path": file_path,
                    },
                )

            return JevClassificationResult(
                category="Unclassified",
                sensitivity_rating="LOW",
                sensitivity_score=0.0,
                archival_priority=5,
                archival_priority_score=0.0,
                confidence=best_score,
                is_classified=False,
                metadata={
                    "latency_ms": elapsed_ms,
                    "file_path": file_path,
                },
            )

        except Exception as e:
            try:
                path_str = str(file_path)
            except Exception:
                path_str = "<unprintable file_path>"
            logger.warning(
                f"Jev classification exception for {path_str}: {e}. Returning unclassified fallback."
            )
            return JevClassificationResult(
                category="Unclassified",
                confidence=0.0,
                is_classified=False,
                metadata={"error": str(e)},
            )

    def classify_file(
        self, file_path: str, text_content: Optional[str] = None
    ) -> JevClassificationResult:
        """Alias for classify method for API consistency."""
        return self.classify(file_path, text_content=text_content)
