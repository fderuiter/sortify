"""Clinical research taxonomy, TMF reference model, and ICH-GCP regulatory definitions.

Defines the structure, artifact metadata, deterministic regex signatures,
and essential document compliance checklists for clinical trial document organization.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Pattern


@dataclass
class ClinicalArtifactDefinition:
    """Definition of a clinical trial document artifact."""

    artifact_id: str
    name: str
    tmf_zone: str
    tmf_section: str
    isf_section: str
    description: str
    is_essential_gcp: bool = False
    regex_signatures: List[Pattern] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    filename_patterns: List[Pattern] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DIA TMF Reference Model Zones (v3.x) & ISF Binder Sections
# ---------------------------------------------------------------------------

TMF_ZONES = {
    "01": "01 Trial Management",
    "02": "02 Central Trial Documents",
    "03": "03 Regulatory",
    "04": "04 IRB and IEC",
    "05": "05 Site Management",
    "06": "06 IP and Trial Supplies",
    "07": "07 Safety Reporting",
    "08": "08 Central Laboratory and Testing",
    "09": "09 Third Parties and Vendors",
    "10": "10 Data Management and Statistics",
}

ISF_SECTIONS = {
    "01": "01_Protocol_and_Amendments",
    "02": "02_Investigators_Brochure",
    "03": "03_FDA_Form_1572_and_Agreements",
    "04": "04_Financial_Disclosure_Forms",
    "05": "05_IRB_IEC_Approvals_and_Correspondence",
    "06": "06_Informed_Consent_Forms",
    "07": "07_Curriculum_Vitae_Licenses_and_GCP",
    "08": "08_Laboratory_Certifications_and_Ranges",
    "09": "09_Safety_Reports_and_SUSARs",
    "10": "10_Subject_Screening_and_Enrollment_Logs",
    "11": "11_Delegation_of_Authority_Log",
    "12": "12_Investigational_Product_Accountability",
    "13": "13_Monitoring_and_Site_Visit_Logs",
    "14": "14_Staff_Training_and_SOPs",
    "15": "15_Correspondence_and_Notes_to_File",
}

# ---------------------------------------------------------------------------
# Comprehensive Clinical Artifact Catalog
# ---------------------------------------------------------------------------

CLINICAL_ARTIFACTS: List[ClinicalArtifactDefinition] = [
    # --- PROTOCOL & AMENDMENTS ---
    ClinicalArtifactDefinition(
        artifact_id="02.01.01",
        name="Clinical Protocol",
        tmf_zone="02 Central Trial Documents",
        tmf_section="02.01 Protocol and Amendments",
        isf_section="01_Protocol_and_Amendments",
        description="Clinical trial study protocol outlining trial design, objectives, endpoints, eligibility criteria, and schedule of assessments.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"(?:clinical\s+study\s+protocol|protocol\s+version|study\s+protocol\s+number)",
                re.IGNORECASE,
            ),
            re.compile(
                r"(?:schedule\s+of\s+assessments|inclusion\s+criteria|exclusion\s+criteria)",
                re.IGNORECASE,
            ),
            re.compile(r"investigational\s+plan\s+and\s+protocol", re.IGNORECASE),
        ],
        keywords=[
            "protocol",
            "study design",
            "inclusion criteria",
            "exclusion criteria",
            "schedule of assessments",
            "primary endpoint",
        ],
        filename_patterns=[
            re.compile(r"protocol.*\.pdf$", re.IGNORECASE),
            re.compile(r"csp_v\d+.*", re.IGNORECASE),
        ],
    ),
    ClinicalArtifactDefinition(
        artifact_id="02.01.02",
        name="Protocol Amendment",
        tmf_zone="02 Central Trial Documents",
        tmf_section="02.01 Protocol and Amendments",
        isf_section="01_Protocol_and_Amendments",
        description="Formal amendment or modification to the approved clinical protocol with summary of changes.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"protocol\s+amendment\s+(?:no\.|number|#)?\s*\d+", re.IGNORECASE
            ),
            re.compile(
                r"summary\s+of\s+protocol\s+changes|substantial\s+amendment",
                re.IGNORECASE,
            ),
        ],
        keywords=[
            "protocol amendment",
            "substantial amendment",
            "summary of changes",
            "amended protocol",
        ],
        filename_patterns=[
            re.compile(r"amendment.*protocol", re.IGNORECASE),
            re.compile(r"protocol_amend", re.IGNORECASE),
        ],
    ),
    ClinicalArtifactDefinition(
        artifact_id="02.01.03",
        name="Protocol Signature Page",
        tmf_zone="02 Central Trial Documents",
        tmf_section="02.01 Protocol and Amendments",
        isf_section="01_Protocol_and_Amendments",
        description="Signed investigator commitment agreeing to conduct the study strictly in accordance with the protocol.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"protocol\s+signature\s+page|investigator\s+agreement\s+signature|principal\s+investigator\s+signature",
                re.IGNORECASE,
            ),
            re.compile(
                r"i\s+agree\s+to\s+conduct\s+the\s+study\s+in\s+accordance\s+with\s+the\s+protocol",
                re.IGNORECASE,
            ),
        ],
        keywords=[
            "protocol signature",
            "investigator signature",
            "signed protocol agreement",
        ],
        filename_patterns=[
            re.compile(r"protocol.*sign", re.IGNORECASE),
            re.compile(r"signature_page", re.IGNORECASE),
        ],
    ),
    # --- INVESTIGATOR'S BROCHURE ---
    ClinicalArtifactDefinition(
        artifact_id="02.02.01",
        name="Investigator's Brochure (IB)",
        tmf_zone="02 Central Trial Documents",
        tmf_section="02.02 Investigator Brochure",
        isf_section="02_Investigators_Brochure",
        description="Investigator's Brochure compiling clinical and non-clinical data on the investigational product.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(r"investigator(?:'s)?\s+brochure", re.IGNORECASE),
            re.compile(
                r"summary\s+of\s+data\s+for\s+the\s+investigator", re.IGNORECASE
            ),
            re.compile(
                r"non-clinical\s+and\s+clinical\s+pharmacology\s+and\s+toxicology",
                re.IGNORECASE,
            ),
        ],
        keywords=[
            "investigator brochure",
            "investigators brochure",
            "ib edition",
            "investigational product summary",
            "reference safety information",
        ],
        filename_patterns=[
            re.compile(r"\bib_v\d+.*", re.IGNORECASE),
            re.compile(r"investigator.*brochure", re.IGNORECASE),
        ],
    ),
    # --- FORM FDA 1572 & SITE REGULATORY ---
    ClinicalArtifactDefinition(
        artifact_id="05.02.01",
        name="Form FDA 1572 (Statement of Investigator)",
        tmf_zone="05 Site Management",
        tmf_section="05.02 Form FDA 1572 and Agreements",
        isf_section="03_FDA_Form_1572_and_Agreements",
        description="Form FDA 1572 Statement of Investigator committing the Principal Investigator to FDA regulations and GCP.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"form\s+fda\s+1572|statement\s+of\s+investigator", re.IGNORECASE
            ),
            re.compile(r"omb\s+no\.\s*0910-0014", re.IGNORECASE),
            re.compile(
                r"department\s+of\s+health\s+and\s+human\s+services.*food\s+and\s+drug\s+administration",
                re.IGNORECASE,
            ),
            re.compile(
                r"subinvestigators\s+who\s+will\s+be\s+assisting\s+the\s+investigator",
                re.IGNORECASE,
            ),
        ],
        keywords=[
            "form fda 1572",
            "statement of investigator",
            "1572",
            "subinvestigators",
            "clinical facility name",
        ],
        filename_patterns=[
            re.compile(r"1572.*\.pdf$", re.IGNORECASE),
            re.compile(r"fda.*1572", re.IGNORECASE),
        ],
    ),
    ClinicalArtifactDefinition(
        artifact_id="03.02.01",
        name="Form FDA 1571 (Investigational New Drug Application)",
        tmf_zone="03 Regulatory",
        tmf_section="03.02 FDA and Competent Authority Filings",
        isf_section="03_FDA_Form_1572_and_Agreements",
        description="Form FDA 1571 Investigational New Drug Application (IND) cover sheet.",
        is_essential_gcp=False,
        regex_signatures=[
            re.compile(r"form\s+fda\s+1571", re.IGNORECASE),
            re.compile(
                r"investigational\s+new\s+drug\s+application\s+\(ind\)", re.IGNORECASE
            ),
            re.compile(r"omb\s+no\.\s*0910-0014.*1571", re.IGNORECASE),
        ],
        keywords=["form fda 1571", "1571", "ind application", "sponsor information"],
        filename_patterns=[
            re.compile(r"1571.*\.pdf$", re.IGNORECASE),
            re.compile(r"fda.*1571", re.IGNORECASE),
        ],
    ),
    ClinicalArtifactDefinition(
        artifact_id="05.03.01",
        name="Financial Disclosure Form (FDA 3454 / 3455)",
        tmf_zone="05 Site Management",
        tmf_section="05.03 Financial Disclosures",
        isf_section="04_Financial_Disclosure_Forms",
        description="Clinical Investigator Financial Disclosure Form (FDA 3454 or FDA 3455) documenting financial interests and conflicts.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"financial\s+disclosure\s+form|financial\s+interest\s+disclosure",
                re.IGNORECASE,
            ),
            re.compile(r"form\s+fda\s+3454|form\s+fda\s+3455", re.IGNORECASE),
            re.compile(
                r"certification:\s*financial\s+interests\s+and\s+arrangements\s+of\s+clinical\s+investigators",
                re.IGNORECASE,
            ),
            re.compile(
                r"significant\s+payments\s+of\s+other\s+sorts|equity\s+interest",
                re.IGNORECASE,
            ),
        ],
        keywords=[
            "financial disclosure",
            "fda 3454",
            "fda 3455",
            "financial conflict",
            "equity interest",
            "clinical investigator certification",
        ],
        filename_patterns=[
            re.compile(r"fdf.*\.pdf$", re.IGNORECASE),
            re.compile(r"financial_disclosure", re.IGNORECASE),
            re.compile(r"3454|3455", re.IGNORECASE),
        ],
    ),
    # --- IRB / IEC & ETHICS ---
    ClinicalArtifactDefinition(
        artifact_id="04.01.01",
        name="IRB / IEC Approval Letter",
        tmf_zone="04 IRB and IEC",
        tmf_section="04.01 Ethics Committee Approvals",
        isf_section="05_IRB_IEC_Approvals_and_Correspondence",
        description="Institutional Review Board (IRB) or Independent Ethics Committee (IEC) formal protocol and consent approval letter.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"institutional\s+review\s+board|independent\s+ethics\s+committee",
                re.IGNORECASE,
            ),
            re.compile(
                r"(?:irb|iec)\s+(?:approval\s+letter|favorable\s+opinion|approved\s+the\s+protocol)",
                re.IGNORECASE,
            ),
            re.compile(
                r"effective\s+date\s+of\s+approval|expiration\s+date\s+of\s+approval|period\s+of\s+approval",
                re.IGNORECASE,
            ),
            re.compile(
                r"federalwide\s+assurance\s+\(fwa\)|irb\s+registration\s+number",
                re.IGNORECASE,
            ),
        ],
        keywords=[
            "irb approval",
            "iec approval",
            "ethics committee approval",
            "fwa",
            "advarra",
            "wcg irb",
            "quorum",
            "institutional review board",
        ],
        filename_patterns=[
            re.compile(r"irb.*approval", re.IGNORECASE),
            re.compile(r"ethics.*approval", re.IGNORECASE),
            re.compile(r"iec_approval", re.IGNORECASE),
        ],
    ),
    ClinicalArtifactDefinition(
        artifact_id="04.02.01",
        name="IRB Continuing Review / Annual Renewal",
        tmf_zone="04 IRB and IEC",
        tmf_section="04.02 Continuing Review and Annual Approvals",
        isf_section="05_IRB_IEC_Approvals_and_Correspondence",
        description="IRB Continuing Review approval letter and annual re-authorization to continue study conduct.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"continuing\s+review\s+(?:approval|determination)|annual\s+re-approval",
                re.IGNORECASE,
            ),
            re.compile(
                r"re-approval\s+period|continuation\s+of\s+human\s+subject\s+research",
                re.IGNORECASE,
            ),
        ],
        keywords=["continuing review", "annual renewal", "re-approval", "irb renewal"],
        filename_patterns=[
            re.compile(r"continuing_review", re.IGNORECASE),
            re.compile(r"annual_irb", re.IGNORECASE),
        ],
    ),
    ClinicalArtifactDefinition(
        artifact_id="04.03.01",
        name="IRB Roster / Composition",
        tmf_zone="04 IRB and IEC",
        tmf_section="04.03 IRB Roster and Compliance",
        isf_section="05_IRB_IEC_Approvals_and_Correspondence",
        description="IRB membership roster or statement of GCP/21 CFR Part 56 compliance.",
        is_essential_gcp=False,
        regex_signatures=[
            re.compile(
                r"irb\s+roster|membership\s+roster|committee\s+membership",
                re.IGNORECASE,
            ),
            re.compile(
                r"compliance\s+with\s+21\s+cfr\s+part\s+56|ich\s+gcp\s+e6",
                re.IGNORECASE,
            ),
        ],
        keywords=[
            "irb roster",
            "committee roster",
            "ethics board composition",
            "21 cfr 56",
        ],
        filename_patterns=[
            re.compile(r"irb_roster", re.IGNORECASE),
            re.compile(r"board_composition", re.IGNORECASE),
        ],
    ),
    # --- INFORMED CONSENT & ASSENT ---
    ClinicalArtifactDefinition(
        artifact_id="02.04.01",
        name="Informed Consent Form (ICF) - Master / Approved",
        tmf_zone="02 Central Trial Documents",
        tmf_section="02.04 Subject Information and Consent",
        isf_section="06_Informed_Consent_Forms",
        description="Approved IRB-stamped Master Informed Consent Form (ICF) and Subject Information Sheet.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"informed\s+consent\s+form|consent\s+to\s+participate\s+in\s+a\s+research\s+study",
                re.IGNORECASE,
            ),
            re.compile(
                r"volunteer\s+research\s+subject|risks\s+and\s+discomforts|voluntary\s+participation",
                re.IGNORECASE,
            ),
            re.compile(
                r"signature\s+of\s+subject\s+or\s+legally\s+authorized\s+representative",
                re.IGNORECASE,
            ),
        ],
        keywords=[
            "informed consent",
            "icf",
            "subject consent",
            "patient consent form",
            "hipaa authorization",
        ],
        filename_patterns=[
            re.compile(r"icf.*\.pdf$", re.IGNORECASE),
            re.compile(r"consent_form", re.IGNORECASE),
            re.compile(r"patient_consent", re.IGNORECASE),
        ],
    ),
    ClinicalArtifactDefinition(
        artifact_id="02.04.02",
        name="Assent Form (Pediatric / Minor)",
        tmf_zone="02 Central Trial Documents",
        tmf_section="02.04 Subject Information and Consent",
        isf_section="06_Informed_Consent_Forms",
        description="Pediatric or adolescent assent document for minor subject participation.",
        is_essential_gcp=False,
        regex_signatures=[
            re.compile(
                r"minor\s+assent\s+form|pediatric\s+assent\s+document|assent\s+to\s+participate",
                re.IGNORECASE,
            ),
            re.compile(r"signature\s+of\s+minor\s+child|child\s+assent", re.IGNORECASE),
        ],
        keywords=["assent form", "pediatric assent", "child assent", "minor assent"],
        filename_patterns=[
            re.compile(r"assent.*\.pdf$", re.IGNORECASE),
        ],
    ),
    # --- INVESTIGATOR CVS, LICENSES & GCP TRAINING ---
    ClinicalArtifactDefinition(
        artifact_id="05.04.01",
        name="Curriculum Vitae (CV) - PI / Sub-I",
        tmf_zone="05 Site Management",
        tmf_section="05.04 Staff Qualifications and Training",
        isf_section="07_Curriculum_Vitae_Licenses_and_GCP",
        description="Signed and dated Curriculum Vitae of Principal Investigator or Sub-Investigator documenting clinical trial experience.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(r"curriculum\s+vitae|resume\s+of\s+investigator", re.IGNORECASE),
            re.compile(
                r"education\s+and\s+training|board\s+certification|medical\s+licensure",
                re.IGNORECASE,
            ),
            re.compile(
                r"clinical\s+trials?\s+experience|principal\s+investigator",
                re.IGNORECASE,
            ),
        ],
        keywords=[
            "curriculum vitae",
            "investigator cv",
            "principal investigator cv",
            "medical education",
            "board certification",
        ],
        filename_patterns=[
            re.compile(r"cv.*\.pdf$", re.IGNORECASE),
            re.compile(r"investigator_cv", re.IGNORECASE),
            re.compile(r"resume_dr", re.IGNORECASE),
        ],
    ),
    ClinicalArtifactDefinition(
        artifact_id="05.04.02",
        name="Medical License / Professional Registration",
        tmf_zone="05 Site Management",
        tmf_section="05.04 Staff Qualifications and Training",
        isf_section="07_Curriculum_Vitae_Licenses_and_GCP",
        description="Current valid state medical license or professional board registration for study physicians and staff.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"state\s+board\s+of\s+medical\s+examiners|medical\s+license|physician\s+license",
                re.IGNORECASE,
            ),
            re.compile(
                r"license\s+number\s*:\s*[A-Z0-9]+|expiration\s+date\s*:\s*\d+",
                re.IGNORECASE,
            ),
            re.compile(r"department\s+of\s+public\s+health\s+license", re.IGNORECASE),
        ],
        keywords=[
            "medical license",
            "physician license",
            "state license",
            "medical board",
            "license renewal",
        ],
        filename_patterns=[
            re.compile(r"medical_license", re.IGNORECASE),
            re.compile(r"license.*\.pdf$", re.IGNORECASE),
        ],
    ),
    ClinicalArtifactDefinition(
        artifact_id="05.04.03",
        name="Good Clinical Practice (GCP) Certificate",
        tmf_zone="05 Site Management",
        tmf_section="05.04 Staff Qualifications and Training",
        isf_section="07_Curriculum_Vitae_Licenses_and_GCP",
        description="Good Clinical Practice (GCP) and Human Subjects Protection (HSP) training certificate (CITI, NIDA, TransCelerate).",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"citi\s+program|collaborative\s+institutional\s+training\s+initiative",
                re.IGNORECASE,
            ),
            re.compile(
                r"good\s+clinical\s+practice\s+(?:certificate|course)|ich\s+gcp\s+training",
                re.IGNORECASE,
            ),
            re.compile(
                r"transcelerate\s+(?:recognized|compliant)|nih\s+human\s+subjects\s+protection",
                re.IGNORECASE,
            ),
            re.compile(
                r"completion\s+report\s+record|valid\s+through\s*:\s*\d+", re.IGNORECASE
            ),
        ],
        keywords=[
            "gcp certificate",
            "citi certificate",
            "good clinical practice",
            "human subjects training",
            "transcelerate",
        ],
        filename_patterns=[
            re.compile(r"gcp.*cert", re.IGNORECASE),
            re.compile(r"citi.*cert", re.IGNORECASE),
            re.compile(r"training_cert", re.IGNORECASE),
        ],
    ),
    # --- LAB CERTIFICATIONS & NORMAL RANGES ---
    ClinicalArtifactDefinition(
        artifact_id="08.01.01",
        name="Laboratory Certification (CLIA / CAP / ISO)",
        tmf_zone="08 Central Laboratory and Testing",
        tmf_section="08.01 Laboratory Accreditations",
        isf_section="08_Laboratory_Certifications_and_Ranges",
        description="Clinical Laboratory Improvement Amendments (CLIA) certificate, CAP accreditation, or ISO certification.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"clia|clinical\s+laboratory\s+improvement\s+amendments", re.IGNORECASE
            ),
            re.compile(
                r"college\s+of\s+american\s+pathologists|cap\s+accredited",
                re.IGNORECASE,
            ),
            re.compile(
                r"centers\s+for\s+medicare\s+&\s+medicaid\s+services.*certificate\s+of\s+accreditation",
                re.IGNORECASE,
            ),
            re.compile(
                r"clia\s+identification\s+number\s*:\s*[A-Z0-9]+", re.IGNORECASE
            ),
        ],
        keywords=[
            "clia certificate",
            "cap accreditation",
            "laboratory accreditation",
            "clia id",
            "cap certificate",
            "central lab cert",
        ],
        filename_patterns=[
            re.compile(r"clia.*\.pdf$", re.IGNORECASE),
            re.compile(r"cap.*cert", re.IGNORECASE),
            re.compile(r"lab_cert", re.IGNORECASE),
        ],
    ),
    ClinicalArtifactDefinition(
        artifact_id="08.02.01",
        name="Laboratory Normal Reference Ranges",
        tmf_zone="08 Central Laboratory and Testing",
        tmf_section="08.02 Reference Ranges and Test Manuals",
        isf_section="08_Laboratory_Certifications_and_Ranges",
        description="Clinical laboratory reference ranges / normal laboratory values signed by the Laboratory Director.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"laboratory\s+reference\s+ranges|normal\s+lab\s+ranges|reference\s+intervals",
                re.IGNORECASE,
            ),
            re.compile(
                r"chemistry\s+reference\s+ranges|hematology\s+reference\s+values",
                re.IGNORECASE,
            ),
            re.compile(r"lab(?:oratory)?\s+director\s+signature", re.IGNORECASE),
        ],
        keywords=[
            "normal ranges",
            "reference ranges",
            "lab reference intervals",
            "normal values",
            "laboratory director approval",
        ],
        filename_patterns=[
            re.compile(r"normal_ranges", re.IGNORECASE),
            re.compile(r"lab_ranges", re.IGNORECASE),
            re.compile(r"reference_ranges", re.IGNORECASE),
        ],
    ),
    # --- SAFETY REPORTING & SUSARS ---
    ClinicalArtifactDefinition(
        artifact_id="07.01.01",
        name="Serious Adverse Event (SAE) Report",
        tmf_zone="07 Safety Reporting",
        tmf_section="07.01 Serious Adverse Events",
        isf_section="09_Safety_Reports_and_SUSARs",
        description="Serious Adverse Event (SAE) initial/follow-up report form submitted to the sponsor and safety team.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"serious\s+adverse\s+event\s+(?:report|form)|sae\s+report\s+form",
                re.IGNORECASE,
            ),
            re.compile(
                r"event\s+term|causality\s+assessment|outcome\s+of\s+sae", re.IGNORECASE
            ),
            re.compile(
                r"life-threatening|hospitalization|congenital\s+anomaly|death",
                re.IGNORECASE,
            ),
        ],
        keywords=[
            "serious adverse event",
            "sae report",
            "medwatch",
            "cioms i",
            "adverse reaction report",
        ],
        filename_patterns=[
            re.compile(r"sae.*report", re.IGNORECASE),
            re.compile(r"sae_form", re.IGNORECASE),
            re.compile(r"safety_event", re.IGNORECASE),
        ],
    ),
    ClinicalArtifactDefinition(
        artifact_id="07.02.01",
        name="SUSAR / IND Safety Notification",
        tmf_zone="07 Safety Reporting",
        tmf_section="07.02 Safety Notifications and SUSARs",
        isf_section="09_Safety_Reports_and_SUSARs",
        description="Suspected Unexpected Serious Adverse Reaction (SUSAR) or IND Safety Report distributed by Sponsor to sites.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"suspected\s+unexpected\s+serious\s+adverse\s+reaction|susar",
                re.IGNORECASE,
            ),
            re.compile(
                r"ind\s+safety\s+report|dear\s+investigator\s+letter\s*-\s*safety",
                re.IGNORECASE,
            ),
            re.compile(
                r"council\s+for\s+international\s+organizations\s+of\s+medical\s+sciences|cioms",
                re.IGNORECASE,
            ),
        ],
        keywords=[
            "susar",
            "ind safety report",
            "safety notification",
            "cioms form",
            "dear investigator letter",
        ],
        filename_patterns=[
            re.compile(r"susar.*\.pdf$", re.IGNORECASE),
            re.compile(r"ind_safety", re.IGNORECASE),
            re.compile(r"safety_letter", re.IGNORECASE),
        ],
    ),
    # --- LOGS: SUBJECT SCREENING, ENROLLMENT, DELEGATION, ACCOUNTABILITY, MONITORING ---
    ClinicalArtifactDefinition(
        artifact_id="05.05.01",
        name="Delegation of Authority (DOA) Log",
        tmf_zone="05 Site Management",
        tmf_section="05.05 Site Logs and Delegation",
        isf_section="11_Delegation_of_Authority_Log",
        description="Delegation of Authority (DOA) / Signature Log documenting study duties assigned by the PI to site staff.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"delegation\s+of\s+(?:authority|responsibilities|duties)\s+log",
                re.IGNORECASE,
            ),
            re.compile(
                r"site\s+signature\s+and\s+delegation\s+log|responsibility\s+matrix",
                re.IGNORECASE,
            ),
            re.compile(
                r"delegated\s+study\s+tasks|pi\s+initials\s+and\s+approval",
                re.IGNORECASE,
            ),
        ],
        keywords=[
            "delegation of authority",
            "doa log",
            "delegation log",
            "site signature log",
            "task delegation",
        ],
        filename_patterns=[
            re.compile(r"doa.*log", re.IGNORECASE),
            re.compile(r"delegation.*log", re.IGNORECASE),
            re.compile(r"signature_log", re.IGNORECASE),
        ],
    ),
    ClinicalArtifactDefinition(
        artifact_id="05.05.02",
        name="Subject Screening and Enrollment Log",
        tmf_zone="05 Site Management",
        tmf_section="05.05 Site Logs and Delegation",
        isf_section="10_Subject_Screening_and_Enrollment_Logs",
        description="Master log tracking subject screening numbers, randomization IDs, enrollment status, and screen failures.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"subject\s+screening\s+(?:and\s+enrollment\s+)?log|master\s+subject\s+log",
                re.IGNORECASE,
            ),
            re.compile(
                r"screening\s+number|randomization\s+number|screen\s+failure\s+reason",
                re.IGNORECASE,
            ),
            re.compile(
                r"date\s+of\s+informed\s+consent|date\s+enrolled", re.IGNORECASE
            ),
        ],
        keywords=[
            "screening log",
            "enrollment log",
            "screening and enrollment log",
            "subject identification code list",
        ],
        filename_patterns=[
            re.compile(r"screening.*log", re.IGNORECASE),
            re.compile(r"enrollment.*log", re.IGNORECASE),
        ],
    ),
    ClinicalArtifactDefinition(
        artifact_id="06.02.01",
        name="Investigational Product (IP) Accountability Log",
        tmf_zone="06 IP and Trial Supplies",
        tmf_section="06.02 IP Accountability and Dispensation",
        isf_section="12_Investigational_Product_Accountability",
        description="Drug / Device accountability log documenting receipt, storage, dispensation, return, and destruction of IP.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"investigational\s+product\s+accountability\s+log|drug\s+accountability\s+log",
                re.IGNORECASE,
            ),
            re.compile(
                r"ip\s+dispensing\s+log|clinical\s+supply\s+accountability",
                re.IGNORECASE,
            ),
            re.compile(
                r"kit\s+number|lot\s+number|quantity\s+dispensed|quantity\s+returned",
                re.IGNORECASE,
            ),
        ],
        keywords=[
            "ip accountability",
            "drug accountability",
            "dispensing log",
            "ip destruction record",
            "drug accountability log",
        ],
        filename_patterns=[
            re.compile(r"drug_accountability", re.IGNORECASE),
            re.compile(r"ip_accountability", re.IGNORECASE),
            re.compile(r"dispensation_log", re.IGNORECASE),
        ],
    ),
    ClinicalArtifactDefinition(
        artifact_id="05.01.01",
        name="Site Visit / Monitoring Log",
        tmf_zone="05 Site Management",
        tmf_section="05.01 Site Monitoring",
        isf_section="13_Monitoring_and_Site_Visit_Logs",
        description="Site visit log signed by Clinical Research Associates (CRAs) during SIV, IMV, or Close-out visits.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"site\s+visit\s+log|monitoring\s+visit\s+log|cra\s+sign-in\s+sheet",
                re.IGNORECASE,
            ),
            re.compile(
                r"site\s+initiation\s+visit|interim\s+monitoring\s+visit|close-out\s+visit",
                re.IGNORECASE,
            ),
            re.compile(
                r"monitor\s+name\s+and\s+signature|reason\s+for\s+visit", re.IGNORECASE
            ),
        ],
        keywords=[
            "site visit log",
            "monitoring log",
            "cra visit sign-in",
            "imv log",
            "siv log",
        ],
        filename_patterns=[
            re.compile(r"site_visit_log", re.IGNORECASE),
            re.compile(r"monitoring_log", re.IGNORECASE),
            re.compile(r"visit_log", re.IGNORECASE),
        ],
    ),
    ClinicalArtifactDefinition(
        artifact_id="05.04.04",
        name="Site Training Log",
        tmf_zone="05 Site Management",
        tmf_section="05.04 Staff Qualifications and Training",
        isf_section="14_Staff_Training_and_SOPs",
        description="Documentation of protocol-specific, EDC, and laboratory training attended by the site research staff.",
        is_essential_gcp=True,
        regex_signatures=[
            re.compile(
                r"protocol\s+training\s+log|site\s+training\s+record|investigator\s+meeting\s+attendance",
                re.IGNORECASE,
            ),
            re.compile(
                r"training\s+topic|trainer\s+signature|trainee\s+signature",
                re.IGNORECASE,
            ),
        ],
        keywords=[
            "training log",
            "protocol training record",
            "siv training log",
            "edc training certificate",
        ],
        filename_patterns=[
            re.compile(r"training_log", re.IGNORECASE),
            re.compile(r"staff_training", re.IGNORECASE),
        ],
    ),
    ClinicalArtifactDefinition(
        artifact_id="01.02.01",
        name="Note to File (NTF) / Regulatory Correspondence",
        tmf_zone="01 Trial Management",
        tmf_section="01.02 Tracking and Communication",
        isf_section="15_Correspondence_and_Notes_to_File",
        description="Note to File (NTF) or formal regulatory memo clarifying site events, discrepancies, or protocol deviations.",
        is_essential_gcp=False,
        regex_signatures=[
            re.compile(
                r"note\s+to\s+file\s*(?:\(ntf\))?|memo\s+to\s+file", re.IGNORECASE
            ),
            re.compile(
                r"protocol\s+deviation\s+documentation|file\s+clarification\s+memo",
                re.IGNORECASE,
            ),
        ],
        keywords=["note to file", "ntf", "memo to file", "regulatory correspondence"],
        filename_patterns=[
            re.compile(r"ntf.*\.pdf$", re.IGNORECASE),
            re.compile(r"note_to_file", re.IGNORECASE),
            re.compile(r"memo.*file", re.IGNORECASE),
        ],
    ),
    # --- ANCILLARY STUDY FILES (NON-REGULATORY TMF) ---
    ClinicalArtifactDefinition(
        artifact_id="99.01.01",
        name="Ancillary Study Operations / Expense Records",
        tmf_zone="Ancillary_Non_TMF",
        tmf_section="Study Invoices and Receipts",
        isf_section="Ancillary_Non_TMF",
        description="Ancillary non-regulatory operational files such as travel receipts, meeting invoices, catering, flight tickets.",
        is_essential_gcp=False,
        regex_signatures=[
            re.compile(
                r"flight\s+ticket|airline\s+itinerary|hotel\s+reservation|lodging\s+receipt",
                re.IGNORECASE,
            ),
            re.compile(
                r"expense\s+report|reimbursement\s+request|catering\s+invoice",
                re.IGNORECASE,
            ),
        ],
        keywords=[
            "receipt",
            "airline ticket",
            "hotel receipt",
            "expense reimbursement",
            "catering invoice",
        ],
        filename_patterns=[
            re.compile(r"receipt.*", re.IGNORECASE),
            re.compile(r"hotel.*", re.IGNORECASE),
            re.compile(r"flight.*", re.IGNORECASE),
        ],
    ),
]

# ---------------------------------------------------------------------------
# Essential Documents Checklist for ICH-GCP Audit Readiness
# ---------------------------------------------------------------------------

ICH_GCP_ESSENTIAL_CHECKLIST = [
    {
        "key": "protocol",
        "title": "Clinical Study Protocol",
        "artifact_id": "02.01.01",
        "importance": "Mandatory",
        "gcp_ref": "ICH-GCP E6 8.2.2",
    },
    {
        "key": "ib",
        "title": "Investigator's Brochure (IB)",
        "artifact_id": "02.02.01",
        "importance": "Mandatory",
        "gcp_ref": "ICH-GCP E6 8.2.1",
    },
    {
        "key": "fda_1572",
        "title": "Form FDA 1572 / Investigator Agreement",
        "artifact_id": "05.02.01",
        "importance": "Mandatory (FDA/US)",
        "gcp_ref": "21 CFR 312.60",
    },
    {
        "key": "financial_disclosure",
        "title": "Financial Disclosure Forms (FDA 3454/3455)",
        "artifact_id": "05.03.01",
        "importance": "Mandatory",
        "gcp_ref": "21 CFR 54",
    },
    {
        "key": "irb_approval",
        "title": "IRB / IEC Protocol Approval Letter",
        "artifact_id": "04.01.01",
        "importance": "Mandatory",
        "gcp_ref": "ICH-GCP E6 8.2.7",
    },
    {
        "key": "icf",
        "title": "Informed Consent Form (IRB Approved)",
        "artifact_id": "02.04.01",
        "importance": "Mandatory",
        "gcp_ref": "ICH-GCP E6 8.2.3",
    },
    {
        "key": "pi_cv",
        "title": "Investigator CVs and Licenses",
        "artifact_id": "05.04.01",
        "importance": "Mandatory",
        "gcp_ref": "ICH-GCP E6 8.2.10",
    },
    {
        "key": "gcp_training",
        "title": "GCP / Human Subjects Training Certificates",
        "artifact_id": "05.04.03",
        "importance": "Mandatory",
        "gcp_ref": "ICH-GCP E6 2.8",
    },
    {
        "key": "lab_cert",
        "title": "Laboratory Accreditation (CLIA / CAP)",
        "artifact_id": "08.01.01",
        "importance": "Mandatory",
        "gcp_ref": "ICH-GCP E6 8.2.11",
    },
    {
        "key": "lab_ranges",
        "title": "Laboratory Normal Reference Ranges",
        "artifact_id": "08.02.01",
        "importance": "Mandatory",
        "gcp_ref": "ICH-GCP E6 8.2.12",
    },
    {
        "key": "doa_log",
        "title": "Delegation of Authority (DOA) Log",
        "artifact_id": "05.05.01",
        "importance": "Mandatory",
        "gcp_ref": "ICH-GCP E6 4.1.5",
    },
    {
        "key": "screening_log",
        "title": "Subject Screening and Enrollment Log",
        "artifact_id": "05.05.02",
        "importance": "Mandatory",
        "gcp_ref": "ICH-GCP E6 8.3.20",
    },
    {
        "key": "ip_accountability",
        "title": "Investigational Product Accountability Log",
        "artifact_id": "06.02.01",
        "importance": "Mandatory",
        "gcp_ref": "ICH-GCP E6 8.2.16",
    },
]


def get_artifact_by_id(artifact_id: str) -> Optional[ClinicalArtifactDefinition]:
    """Retrieve an artifact definition by its unique identifier."""
    for art in CLINICAL_ARTIFACTS:
        if art.artifact_id == artifact_id:
            return art
    return None
