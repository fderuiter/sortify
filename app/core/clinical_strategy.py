"""Clinical Research & Regulatory Document Sorting Strategy.

Classifies and sorts documents according to DIA TMF (Trial Master File) Reference Model
and ICH-GCP Investigator Site File (ISF) regulatory binder structures with compliance gap analysis.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from app.core.analyzer_strategies import IsolatedStrategyMixin
from app.core.clinical_compliance import ClinicalComplianceEngine
from app.core.clinical_renamer import ClinicalRenamer
from app.core.clinical_taxonomy import (
    CLINICAL_ARTIFACTS,
    ClinicalArtifactDefinition,
)
from app.core.path_utils import sanitize_name

logger = logging.getLogger(__name__)


class ClinicalTMFStrategy(IsolatedStrategyMixin):
    """Strategy that classifies documents into clinical trial regulatory structures."""

    def __init__(
        self,
        mode: str = "tmf",
        smart_renaming: bool = False,
        generate_audit_report: bool = True,
    ):
        super().__init__()
        self.mode = (
            mode  # 'tmf' (Sponsor Zone > Section) or 'isf' (Site Binder Sections)
        )
        self.smart_renaming = smart_renaming
        self.generate_audit_report = generate_audit_report
        self.compliance_engine = ClinicalComplianceEngine()
        self.last_compliance_result: Optional[Dict[str, Any]] = None

    def _classify_document(
        self,
        filename: str,
        text: str,
        confidence_threshold: float = 3.0,
    ) -> Tuple[Optional[ClinicalArtifactDefinition], float]:
        """Match document against clinical artifact signatures and keyword weights."""
        best_artifact: Optional[ClinicalArtifactDefinition] = None
        best_score = 0.0

        clean_text = text or ""
        clean_fn = os.path.basename(filename)

        for artifact in CLINICAL_ARTIFACTS:
            score = 0.0

            # 1. Regex signatures (+10.0 per match)
            for sig in artifact.regex_signatures:
                if sig.search(clean_text):
                    score += 10.0

            # 2. Filename patterns (+6.0 per match)
            for fn_pat in artifact.filename_patterns:
                if fn_pat.search(clean_fn):
                    score += 6.0

            # 3. Keyword matches (+1.5 per keyword)
            for kw in artifact.keywords:
                if re.search(rf"\b{re.escape(kw)}\b", clean_text, re.IGNORECASE):
                    score += 1.5

            if score > best_score:
                best_score = score
                best_artifact = artifact

        if best_score >= confidence_threshold:
            return best_artifact, best_score

        return None, best_score

    def generate_plan(
        self,
        filenames: List[str],
        documents: List[str],
        max_folders: int = 15,
        stop_words: Optional[set] = None,
        max_depth: int = 5,
        max_features: int = 3,
        pre_fetched_vectors: Optional[List[list]] = None,
        cancel_check=None,
        pre_fetched_corpus: Optional[Any] = None,
    ) -> Tuple[dict, float]:
        """Generate a structured clinical regulatory sorting plan and compliance audit."""
        self._error = 0.0
        plan: dict = {}
        classified_map: Dict[str, str] = {}  # filename -> artifact_id

        doc_map = dict(zip(filenames, documents))

        for idx, fn in enumerate(filenames):
            if cancel_check and cancel_check():
                logger.info("Clinical classification cancelled by user.")
                break

            doc_text = doc_map.get(fn, "")
            artifact, score = self._classify_document(fn, doc_text)

            if artifact is not None:
                classified_map[fn] = artifact.artifact_id

                if artifact.artifact_id == "99.01.01":
                    # Ancillary non-regulatory
                    target_folder_path = ["Ancillary_Non_TMF"]
                elif self.mode == "isf":
                    target_folder_path = [sanitize_name(artifact.isf_section)]
                else:
                    # TMF mode: Zone > Section
                    zone_name = sanitize_name(artifact.tmf_zone)
                    sec_name = sanitize_name(artifact.tmf_section)
                    target_folder_path = [zone_name, sec_name]

                # Determine target filename
                if self.smart_renaming:
                    target_fn = ClinicalRenamer.generate_standard_filename(
                        fn, artifact.name, doc_text
                    )
                    leaf_node = {
                        "__type__": "file",
                        "relative_source": fn,
                        "target_filename": target_fn,
                    }
                else:
                    leaf_node = None

                self._insert_into_plan(plan, target_folder_path, fn, leaf_node)
            else:
                # Unclassified / Review Required
                classified_map[fn] = "unclassified"
                self._insert_into_plan(plan, ["Unclassified_Review"], fn, None)

        # Evaluate compliance gap analysis
        base_dir_val = getattr(self, "base_dir", "") or ""
        compliance_result = self.compliance_engine.evaluate_compliance(
            classified_map, filenames, base_dir=base_dir_val
        )
        self.last_compliance_result = compliance_result

        # Generate audit reports if requested and base_dir is valid
        if self.generate_audit_report and base_dir_val and os.path.isdir(base_dir_val):
            try:
                json_path = os.path.join(base_dir_val, "compliance_audit_report.json")
                html_path = os.path.join(base_dir_val, "compliance_audit_report.html")
                self.compliance_engine.export_json_report(compliance_result, json_path)
                self.compliance_engine.generate_html_report(
                    compliance_result, html_path
                )
                logger.info(f"Generated compliance audit reports in {base_dir_val}")
            except Exception as e:
                logger.warning(f"Could not write audit reports to {base_dir_val}: {e}")

        return plan, 0.0

    def _insert_into_plan(
        self,
        plan: dict,
        folder_path: List[str],
        filename: str,
        leaf_node: Any,
    ) -> None:
        """Insert a file leaf node into the nested plan dictionary."""
        current = plan
        for folder in folder_path:
            if folder not in current or not isinstance(current[folder], dict):
                current[folder] = {}
            current = current[folder]
        current[filename] = leaf_node
