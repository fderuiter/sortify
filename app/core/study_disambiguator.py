"""Study entity disambiguation and co-occurrence graph resolution for CRO document collections.

Discovers clinical trial protocols across scanned drives, builds an investigator/site
co-occurrence network, and resolves study associations for ambiguous documents.
"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from app.core.clinical_renamer import ClinicalRenamer
from app.core.forensic_scanner import DiscoveredDocument

logger = logging.getLogger(__name__)


@dataclass
class StudyEntity:
    """Represents a discovered clinical study protocol."""

    protocol_id: str
    protocol_name: str
    investigators: Set[str] = field(default_factory=set)
    site_numbers: Set[str] = field(default_factory=set)
    associated_documents: List[DiscoveredDocument] = field(default_factory=list)


class StudyDisambiguator:
    """Resolves study associations across documents using direct matches, co-occurrence graphs, and path heuristics."""

    def __init__(self):
        self.studies: Dict[str, StudyEntity] = {}
        self.investigator_to_studies: Dict[str, Set[str]] = defaultdict(set)
        self.site_to_studies: Dict[str, Set[str]] = defaultdict(set)
        self.cross_study_shared_docs: List[DiscoveredDocument] = []
        self.unassigned_docs: List[DiscoveredDocument] = []

    def extract_site_number(self, text: str, path: str) -> Optional[str]:
        """Extract clinical trial site number from text or file path."""
        match = re.search(
            r"\bsite\s*(?:id|number|no\.?|#)?\s*[:\s]*(\d{2,5})\b", text, re.IGNORECASE
        )
        if match:
            return match.group(1)
        path_match = re.search(r"\bsite[-_]?(\d{2,5})\b", path, re.IGNORECASE)
        if path_match:
            return path_match.group(1)
        return None

    def discover_and_partition_studies(
        self, documents: List[DiscoveredDocument]
    ) -> Dict[str, List[DiscoveredDocument]]:
        """Analyze all documents, discover study entities, and partition documents by study."""
        self.studies.clear()
        self.investigator_to_studies.clear()
        self.site_to_studies.clear()
        self.cross_study_shared_docs.clear()
        self.unassigned_docs.clear()

        # PASS 1: Seed study discovery and investigator/site network from explicit documents
        for doc in documents:
            proto_id = ClinicalRenamer.extract_protocol_id(
                doc.extracted_text, doc.file_name
            )
            if not proto_id:
                # Check path tokens
                proto_id = ClinicalRenamer.extract_protocol_id("", doc.relative_path)

            pi_name = ClinicalRenamer.extract_investigator_name(doc.extracted_text)
            site_num = self.extract_site_number(doc.extracted_text, doc.relative_path)

            if proto_id:
                if proto_id not in self.studies:
                    self.studies[proto_id] = StudyEntity(
                        protocol_id=proto_id,
                        protocol_name=f"Study_{proto_id}",
                    )

                if pi_name:
                    self.studies[proto_id].investigators.add(pi_name)
                    self.investigator_to_studies[pi_name].add(proto_id)

                if site_num:
                    self.studies[proto_id].site_numbers.add(site_num)
                    self.site_to_studies[site_num].add(proto_id)

        # PASS 2: Assign documents based on direct match, PI co-occurrence, or path proximity
        partitioned: Dict[str, List[DiscoveredDocument]] = defaultdict(list)

        for doc in documents:
            assigned_study = None

            # 1. Direct Protocol ID
            direct_proto = ClinicalRenamer.extract_protocol_id(
                doc.extracted_text, doc.file_name
            )
            if not direct_proto:
                direct_proto = ClinicalRenamer.extract_protocol_id(
                    "", doc.relative_path
                )

            if direct_proto and direct_proto in self.studies:
                assigned_study = direct_proto

            # 2. Investigator Co-occurrence Mapping (for CVs, medical licenses, GCP certs)
            if not assigned_study:
                pi_name = ClinicalRenamer.extract_investigator_name(doc.extracted_text)
                if pi_name and pi_name in self.investigator_to_studies:
                    matched_studies = self.investigator_to_studies[pi_name]
                    if len(matched_studies) == 1:
                        assigned_study = next(iter(matched_studies))
                    elif len(matched_studies) > 1:
                        assigned_study = "Cross_Study_Shared"

            # 3. Path Proximity Heuristics
            if not assigned_study:
                for proto_id in self.studies.keys():
                    if (
                        proto_id.lower() in doc.relative_path.lower()
                        or proto_id.replace("_", "-").lower()
                        in doc.relative_path.lower()
                    ):
                        assigned_study = proto_id
                        break

            # 4. Route assignment
            if assigned_study == "Cross_Study_Shared":
                self.cross_study_shared_docs.append(doc)
                partitioned["Cross_Study_Shared"].append(doc)
            elif assigned_study:
                self.studies[assigned_study].associated_documents.append(doc)
                partitioned[assigned_study].append(doc)
            else:
                self.unassigned_docs.append(doc)
                partitioned["Unassigned_Study_Documents"].append(doc)

        return dict(partitioned)
