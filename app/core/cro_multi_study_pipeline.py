"""CRO Multi-Study Forensic Ingestion & Regulatory Binder Pipeline.

Coordinates deep drive scanning, study disambiguation, regulatory binder classification,
non-destructive target copying, and cryptographic chain-of-custody manifest generation.
"""

import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.core.clinical_compliance import ClinicalComplianceEngine
from app.core.clinical_renamer import ClinicalRenamer
from app.core.clinical_strategy import ClinicalTMFStrategy
from app.core.forensic_scanner import ForensicScanner
from app.core.path_utils import sanitize_name
from app.core.study_disambiguator import StudyDisambiguator

logger = logging.getLogger(__name__)


@dataclass
class StudyIngestSummary:
    """Summary metrics for an individual clinical study."""

    study_id: str
    total_documents: int
    compliance_score_percent: float
    audit_readiness_status: str
    missing_essential_count: int
    found_essential_count: int
    target_directory: str
    audit_report_html_path: str


@dataclass
class MasterPipelineResult:
    """Complete result of a CRO Multi-Study forensic ingestion run."""

    source_root: str
    target_root: str
    timestamp_utc: str
    total_scanned_files: int
    total_unique_documents: int
    total_duplicates_detected: int
    discovered_studies_count: int
    studies_summary: List[StudyIngestSummary] = field(default_factory=list)
    chain_of_custody_manifest_path: str = ""


class CROMultiStudyPipeline:
    """Executes end-to-end multi-study forensic ingestion and organization."""

    def __init__(self, mode: str = "tmf", smart_renaming: bool = True):
        self.mode = mode
        self.smart_renaming = smart_renaming
        self.scanner = ForensicScanner()
        self.disambiguator = StudyDisambiguator()
        self.compliance_engine = ClinicalComplianceEngine()

    def run_pipeline(
        self,
        source_root: str,
        target_root: str,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> MasterPipelineResult:
        """Run the full CRO forensic discovery, multi-study partition, and safe copying workflow."""
        source_root = os.path.abspath(source_root)
        target_root = os.path.abspath(target_root)
        os.makedirs(target_root, exist_ok=True)

        from app.core.shared_registry import SharedModelRegistry

        registry = SharedModelRegistry.get_instance()

        try:
            # 1. Forensic Scan & Archive Extraction
            if progress_callback:
                progress_callback(5, "Scanning source storage volume...")

            discovered_docs = self.scanner.scan_drive(
                source_root,
                progress_callback=lambda c, m: (
                    progress_callback(min(40, 5 + c), m) if progress_callback else None
                ),
                cancel_check=cancel_check,
            )

            # Stage 1 transition: Unload OCR & vision models after text extraction scan
            registry.unload_model("easyocr")
            registry.unload_model("florence-2")

            if cancel_check and cancel_check():
                logger.info("Pipeline cancelled during scanning.")
                return MasterPipelineResult(
                    source_root=source_root,
                    target_root=target_root,
                    timestamp_utc=datetime.now(timezone.utc).isoformat(),
                    total_scanned_files=len(discovered_docs),
                    total_unique_documents=0,
                    total_duplicates_detected=0,
                    discovered_studies_count=0,
                )

            # 2. Study Disambiguation & Entity Resolution
            if progress_callback:
                progress_callback(
                    45, "Resolving clinical study entities and investigator networks..."
                )

            study_partitions = self.disambiguator.discover_and_partition_studies(
                discovered_docs
            )

            # Stage 2 transition: Unload embedding models after disambiguation
            registry.unload_model("onnx")

            # 3. Process each Study Partition
            studies_summary: List[StudyIngestSummary] = []
            manifest_records: List[Dict[str, Any]] = []

            total_partitions = len(study_partitions)
            for p_idx, (study_id, docs) in enumerate(study_partitions.items()):
                if cancel_check and cancel_check():
                    break

                study_folder_name = sanitize_name(study_id)
                study_target_dir = os.path.join(target_root, study_folder_name)
                os.makedirs(study_target_dir, exist_ok=True)

                if progress_callback:
                    progress_callback(
                        50 + int((p_idx / max(1, total_partitions)) * 40),
                        f"Organizing {study_id} ({len(docs)} files)...",
                    )

                strategy = ClinicalTMFStrategy(
                    mode=self.mode,
                    smart_renaming=self.smart_renaming,
                    generate_audit_report=False,
                )
                strategy.base_dir = study_target_dir

                filenames = [d.file_name for d in docs]
                doc_texts = [d.extracted_text for d in docs]

                plan, _ = strategy.generate_plan(filenames, doc_texts)

                # Copy files non-destructively to target directory
                classified_map = {}
                for doc in docs:
                    if cancel_check and cancel_check():
                        break

                    artifact, _ = strategy._classify_document(
                        doc.file_name, doc.extracted_text
                    )
                    art_id = artifact.artifact_id if artifact else "unclassified"
                    art_name = artifact.name if artifact else "Unclassified"
                    classified_map[doc.file_name] = art_id

                    # Compute destination subfolder
                    if art_id == "99.01.01":
                        subfolder_rel = "Ancillary_Non_TMF"
                    elif art_id == "unclassified":
                        subfolder_rel = "Unclassified_Review"
                    elif self.mode == "isf":
                        subfolder_rel = sanitize_name(artifact.isf_section)
                    else:
                        subfolder_rel = os.path.join(
                            sanitize_name(artifact.tmf_zone),
                            sanitize_name(artifact.tmf_section),
                        )

                    dest_dir = os.path.join(study_target_dir, subfolder_rel)
                    os.makedirs(dest_dir, exist_ok=True)

                    if self.smart_renaming and artifact:
                        target_fn = ClinicalRenamer.generate_standard_filename(
                            doc.file_name, art_name, doc.extracted_text
                        )
                    else:
                        target_fn = doc.file_name

                    dest_file_path = os.path.join(dest_dir, target_fn)

                    # Non-destructive copy from actual staging/source file
                    src_to_copy = doc.staging_file_path or doc.source_path
                    if os.path.isfile(src_to_copy):
                        try:
                            shutil.copy2(src_to_copy, dest_file_path)
                        except Exception as e:
                            logger.warning(
                                f"Failed to copy {src_to_copy} to {dest_file_path}: {e}"
                            )

                    manifest_records.append(
                        {
                            "source_path": doc.source_path,
                            "archive_origin": doc.archive_origin,
                            "sha256_hash": doc.sha256_hash,
                            "file_size_bytes": doc.file_size_bytes,
                            "assigned_study": study_id,
                            "classified_artifact_id": art_id,
                            "classified_artifact_name": art_name,
                            "destination_path": os.path.relpath(
                                dest_file_path, target_root
                            ),
                            "is_duplicate": doc.is_duplicate,
                            "duplicate_of": doc.duplicate_of,
                            "ingest_timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )

                # Compliance evaluation & report generation for this study
                comp_eval = self.compliance_engine.evaluate_compliance(
                    classified_map, filenames, base_dir=study_target_dir
                )
                html_report_path = os.path.join(
                    study_target_dir, "compliance_audit_report.html"
                )
                json_report_path = os.path.join(
                    study_target_dir, "compliance_audit_report.json"
                )
                self.compliance_engine.generate_html_report(comp_eval, html_report_path)
                self.compliance_engine.export_json_report(comp_eval, json_report_path)

                studies_summary.append(
                    StudyIngestSummary(
                        study_id=study_id,
                        total_documents=len(docs),
                        compliance_score_percent=comp_eval["compliance_score_percent"],
                        audit_readiness_status=comp_eval["audit_readiness_status"],
                        missing_essential_count=comp_eval["total_essential_missing"],
                        found_essential_count=comp_eval["total_essential_found"],
                        target_directory=study_target_dir,
                        audit_report_html_path=html_report_path,
                    )
                )

            # Stage 3 transition: Unload generative models after classification/renaming
            registry.unload_model("generative_naming")

            # 4. Generate Master Chain of Custody Manifest
            manifest_path = os.path.join(target_root, "chain_of_custody_manifest.json")
            unique_docs_count = len([d for d in discovered_docs if not d.is_duplicate])
            dups_count = len([d for d in discovered_docs if d.is_duplicate])

            master_manifest = {
                "title": "CRO Multi-Study Ingestion Chain-of-Custody Manifest",
                "regulatory_standard": "ICH-GCP E6(R2), 21 CFR Part 11, DIA TMF Reference Model",
                "source_storage_root": source_root,
                "target_destination_root": target_root,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "summary_metrics": {
                    "total_documents_scanned": len(discovered_docs),
                    "total_unique_documents": unique_docs_count,
                    "total_duplicates_detected": dups_count,
                    "total_studies_discovered": len(self.disambiguator.studies),
                },
                "discovered_studies": [asdict(s) for s in studies_summary],
                "document_manifest": manifest_records,
            }

            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(master_manifest, f, indent=2)

            if progress_callback:
                progress_callback(100, "CRO Multi-Study Ingestion complete!")

            return MasterPipelineResult(
                source_root=source_root,
                target_root=target_root,
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                total_scanned_files=len(discovered_docs),
                total_unique_documents=unique_docs_count,
                total_duplicates_detected=dups_count,
                discovered_studies_count=len(self.disambiguator.studies),
                studies_summary=studies_summary,
                chain_of_custody_manifest_path=manifest_path,
            )
        finally:
            # Stage 4 transition: Guarantee full cleanup at pipeline end
            try:
                self.scanner.cleanup()
            except Exception as e:
                logger.warning(f"Error cleaning up scanner staging directory: {e}")
            registry.unload_all_models()
