"""Smart Clinical Document Renamer.

Extracts clinical trial metadata (protocol ID, document type, PI/Site, version date)
to generate standardized, audit-ready filenames adhering to clinical research conventions.
"""

import os
import re
from typing import Optional

from app.core.path_utils import sanitize_name


class ClinicalRenamer:
    """Renames clinical trial documents according to standardized regulatory naming schemas."""

    @staticmethod
    def extract_protocol_id(text: str, filename: str) -> Optional[str]:
        """Extract study/protocol identifier from text or filename."""
        # Check filename first
        fn_match = re.search(
            r"(?:^|[^A-Za-z0-9])([A-Z]{2,6}[-_][0-9]{3,6}(?:[-_][A-Za-z0-9]+)?)(?:[^A-Za-z0-9]|$)",
            filename,
        )
        if fn_match:
            return fn_match.group(1).upper().replace("-", "_")

        # Check text patterns
        patterns = [
            r"\bprotocol\s*(?:id|number|no\.?|#|code)\s*[:\s]*([A-Z0-9_\-]+)",
            r"\bstudy\s*(?:id|number|no\.?|#|code)\s*[:\s]*([A-Z0-9_\-]+)",
            r"\bind\s*(?:number|no\.?|#)\s*[:\s]*(\d{4,8})",
            r"\bprotocol\s*[:#]\s*([A-Z0-9_\-]+)",
        ]
        for pat in patterns:
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                candidate = (
                    match.group(1).strip().strip(".,;:").upper().replace("-", "_")
                )
                if (
                    len(candidate) >= 3
                    and len(candidate) <= 25
                    and candidate
                    not in ("VERSION", "AMENDMENT", "SIGNATURE", "STUDY", "DESIGN")
                ):
                    return candidate
        return None

    @staticmethod
    def extract_investigator_name(text: str) -> Optional[str]:
        """Extract Principal Investigator / Physician name from document text."""
        patterns = [
            r"principal\s+investigator\s*[:\s]+(?:Dr\.?\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
            r"investigator\s+name\s*[:\s]+(?:Dr\.?\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
            r"(?:Dr\.?\s+)([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*,\s*(?:M\.?D\.?|P\.?h\.?D\.?|D\.?O\.?)",
            r"\bDr\.?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b",
        ]
        for pat in patterns:
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                name = match.group(1).strip().replace(" ", "_")
                return f"PI_{name}"
        return None

    @staticmethod
    def extract_date_or_version(text: str, filename: str) -> Optional[str]:
        """Extract date (YYYYMMDD or YYYY-MM-DD) or version string from text or filename."""
        # Version pattern
        v_match = re.search(
            r"(?:^|[^a-zA-Z0-9])(v(?:ersion)?[-_\s]?\d+(?:\.\d+)?)(?:[^a-zA-Z0-9]|$)",
            filename,
            re.IGNORECASE,
        )
        if v_match:
            return (
                v_match.group(1)
                .lower()
                .replace("ersion", "")
                .replace(" ", "")
                .replace("-", "")
            )

        # Date in filename
        d_match = re.search(
            r"(?:^|[^0-9])(20\d{2}[-_]?[0-1]\d[-_]?[0-3]\d)(?:[^0-9]|$)", filename
        )
        if d_match:
            return d_match.group(1).replace("-", "").replace("_", "")

        # Date in text (e.g. 2024-05-12 or 2024/05/12)
        iso_match = re.search(r"\b(202[0-9][-/][0-1][0-9][-/][0-3][0-9])\b", text)
        if iso_match:
            return iso_match.group(1).replace("-", "").replace("/", "")

        return None

    @classmethod
    def generate_standard_filename(
        cls,
        original_filename: str,
        artifact_name: str,
        text: str,
    ) -> str:
        """Generate a clean, standardized clinical filename.

        Format: [ProtocolID]_[ArtifactType]_[PI/Site]_[Date/Version].[ext]
        """
        base, ext = os.path.splitext(original_filename)

        # Strip parentheses explanations e.g. "Form FDA 1572 (Statement of Investigator)" -> "Form FDA 1572"
        clean_name = re.sub(r"\(.*?\)", "", artifact_name).strip()
        # Normalize artifact name into a slug
        artifact_slug = re.sub(r"[^\w\s-]", "", clean_name).strip().replace(" ", "_")
        artifact_slug = re.sub(r"_+", "_", artifact_slug)

        protocol_id = cls.extract_protocol_id(text, original_filename)
        pi_name = cls.extract_investigator_name(text)
        date_ver = cls.extract_date_or_version(text, original_filename)

        parts = []
        if protocol_id:
            parts.append(protocol_id)

        parts.append(artifact_slug)

        if pi_name:
            parts.append(pi_name)

        if date_ver:
            parts.append(date_ver)

        new_base = "_".join(parts)
        safe_base = sanitize_name(new_base)
        if not safe_base or len(safe_base) < 3:
            return original_filename

        return f"{safe_base}{ext}"
