"""Standalone Component Catalog and Accessibility Gate Module for Textual TUI.

Provides a registry of active Textual screens and modals, and CLI entry points
for inspecting components and running automated accessibility audits via
`inspect_tui_component`.
"""

import argparse
import asyncio
import contextlib
import sys
from typing import Any, Dict, List, Optional, Tuple

from app.config import AppSettings
from app.ui.a11y import A11yViolation, inspect_tui_component
from app.ui.modals.drop_zone import DropPlanConfirmModal, DropZoneModal
from app.ui.modals.edit import (
    DirectorySelectModal,
    DuplicateReviewModal,
    ManualPathFallbackModal,
    NewFolderModal,
    ReassignModal,
    RenameModal,
)
from app.ui.modals.help import ShortcutCheatSheetModal
from app.ui.modals.models import ModelManagerModal
from app.ui.modals.quarantine import QuarantineVaultModal
from app.ui.modals.reports import (
    AuditReportModal,
    ExportReportModal,
    ExtractionPreviewModal,
)
from app.ui.modals.rules import RuleImportModal, TemplateGalleryModal
from app.ui.modals.session import (
    HistoryModal,
    RollbackConfirmModal,
    RollbackErrorModal,
    SessionRecoveryModal,
)
from app.ui.modals.settings import (
    ConsentRevocationModal,
    ScrubArtifactsModal,
    SettingsModal,
)
from app.ui.modals.wizard import WizardModal
from app.ui.tui import AutoSorterTUI


def _build_default_settings() -> AppSettings:
    """Instantiate AppSettings for catalog preview/audits."""
    settings = AppSettings()
    with contextlib.suppress(Exception):
        settings.AI_CONSENT_GRANTED = True
    return settings


# Registry mapping component IDs to metadata and factory functions.
CATALOG_REGISTRY: List[Dict[str, Any]] = [
    {
        "id": "autosorter_tui",
        "name": "AutoSorter TUI Main App Screen",
        "category": "screen",
        "description": "Main dual-pane interactive tree and metadata panel application screen.",
        "factory": lambda settings=None: AutoSorterTUI(
            settings=settings or _build_default_settings()
        ),
    },
    {
        "id": "rename_modal",
        "name": "Rename Modal",
        "category": "modal",
        "description": "Modal dialog for renaming files and directories with pattern tokens.",
        "factory": lambda settings=None: RenameModal(
            title="Rename Test", current_name="sample_file", extension=".txt"
        ),
    },
    {
        "id": "new_folder_modal",
        "name": "New Folder Modal",
        "category": "modal",
        "description": "Modal dialog for creating new subfolders.",
        "factory": lambda settings=None: NewFolderModal(),
    },
    {
        "id": "reassign_modal",
        "name": "Reassign Category Modal",
        "category": "modal",
        "description": "Modal dialog for reassigning items to target categories.",
        "factory": lambda settings=None: ReassignModal(
            existing_folders=["/documents/work", "/documents/personal"]
        ),
    },
    {
        "id": "directory_select_modal",
        "name": "Directory Selection Modal",
        "category": "modal",
        "description": "Modal dialog for browsing and selecting workspace directories.",
        "factory": lambda settings=None: DirectorySelectModal(),
    },
    {
        "id": "manual_path_fallback_modal",
        "name": "Manual Path Fallback Modal",
        "category": "modal",
        "description": "Fallback modal dialog for entering folder paths manually when path picker is unavailable.",
        "factory": lambda settings=None: ManualPathFallbackModal(
            current_dir="/app", reason="Native dialog unavailable"
        ),
    },
    {
        "id": "duplicate_review_modal",
        "name": "Duplicate Review Modal",
        "category": "modal",
        "description": "Modal dialog for inspecting duplicate file clusters.",
        "factory": lambda settings=None: DuplicateReviewModal(),
    },
    {
        "id": "shortcut_cheatsheet_modal",
        "name": "Shortcut Cheat Sheet Modal",
        "category": "modal",
        "description": "Modal cheat sheet displaying keyboard shortcut bindings.",
        "factory": lambda settings=None: ShortcutCheatSheetModal(),
    },
    {
        "id": "model_manager_modal",
        "name": "Model Manager Modal",
        "category": "modal",
        "description": "Modal dialog for downloading and managing machine learning model assets.",
        "factory": lambda settings=None: ModelManagerModal(
            settings=settings or _build_default_settings()
        ),
    },
    {
        "id": "quarantine_vault_modal",
        "name": "Quarantine Vault Modal",
        "category": "modal",
        "description": "Modal vault manager for viewing and restoring quarantined files.",
        "factory": lambda settings=None: QuarantineVaultModal(),
    },
    {
        "id": "extraction_preview_modal",
        "name": "Extraction Preview Modal",
        "category": "modal",
        "description": "Modal dialog for previewing extracted text and adjusting OCR density thresholds.",
        "factory": lambda settings=None: ExtractionPreviewModal(
            file_path="/tmp/sample.pdf", settings=settings or _build_default_settings()
        ),
    },
    {
        "id": "export_report_modal",
        "name": "Export Report Modal",
        "category": "modal",
        "description": "Modal dialog for selecting export format and destination for simulation reports.",
        "factory": lambda settings=None: ExportReportModal(),
    },
    {
        "id": "audit_report_modal",
        "name": "Audit Report Modal",
        "category": "modal",
        "description": "Modal dialog displaying full verification and compliance audit summaries.",
        "factory": lambda settings=None: AuditReportModal(report_data={}),
    },
    {
        "id": "template_gallery_modal",
        "name": "Template Gallery Modal",
        "category": "modal",
        "description": "Modal gallery for selecting pre-packaged organization rule templates.",
        "factory": lambda settings=None: TemplateGalleryModal(
            settings=settings or _build_default_settings()
        ),
    },
    {
        "id": "rule_import_modal",
        "name": "Rule Import Modal",
        "category": "modal",
        "description": "Modal dialog for importing external YAML rule definitions.",
        "factory": lambda settings=None: RuleImportModal(
            settings=settings or _build_default_settings()
        ),
    },
    {
        "id": "drop_plan_confirm_modal",
        "name": "Drop Plan Confirmation Modal",
        "category": "modal",
        "description": "Modal dialog confirming drop zone action execution.",
        "factory": lambda settings=None: DropPlanConfirmModal(
            summary="Proposed drop action for 3 items",
            plan_lines=["/tmp/a.pdf -> /sorted/a.pdf"],
        ),
    },
    {
        "id": "drop_zone_modal",
        "name": "Drop Zone Modal",
        "category": "modal",
        "description": "Modal dialog for staging drag-and-dropped or pasted file paths.",
        "factory": lambda settings=None: DropZoneModal(
            settings=settings or _build_default_settings()
        ),
    },
    {
        "id": "session_recovery_modal",
        "name": "Session Recovery Modal",
        "category": "modal",
        "description": "Modal dialog prompting for recovery or rollback of interrupted sorting sessions.",
        "factory": lambda settings=None: SessionRecoveryModal(
            {"session_id": "session_001", "base_dir": "/app", "status": "failed"}
        ),
    },
    {
        "id": "rollback_confirm_modal",
        "name": "Rollback Confirmation Modal",
        "category": "modal",
        "description": "Modal dialog asking confirmation before rolling back file moves.",
        "factory": lambda settings=None: RollbackConfirmModal(
            {"session_id": "session_001", "items": []}
        ),
    },
    {
        "id": "rollback_error_modal",
        "name": "Rollback Error Modal",
        "category": "modal",
        "description": "Modal error dialog displaying rollback execution failures.",
        "factory": lambda settings=None: RollbackErrorModal(),
    },
    {
        "id": "history_modal",
        "name": "History Modal",
        "category": "modal",
        "description": "Modal dialog displaying past sorting operations and rollback entry points.",
        "factory": lambda settings=None: HistoryModal(),
    },
    {
        "id": "scrub_artifacts_modal",
        "name": "Scrub Artifacts Modal",
        "category": "modal",
        "description": "Modal dialog asking confirmation for scrubbing cached embeddings and vector indexes.",
        "factory": lambda settings=None: ScrubArtifactsModal(
            vector_count=100, cache_size_mb=12.5
        ),
    },
    {
        "id": "consent_revocation_modal",
        "name": "Consent Revocation Modal",
        "category": "modal",
        "description": "Modal dialog asking confirmation for revoking AI processing consent.",
        "factory": lambda settings=None: ConsentRevocationModal(
            vector_count=100, cache_size_mb=12.5
        ),
    },
    {
        "id": "settings_modal",
        "name": "Settings Modal",
        "category": "modal",
        "description": "Modal dialog for editing application configurations and AI consent options.",
        "factory": lambda settings=None: SettingsModal(
            settings=settings or _build_default_settings()
        ),
    },
    {
        "id": "wizard_modal",
        "name": "Setup Wizard Modal",
        "category": "modal",
        "description": "Modal onboarding wizard for configuring initial preferences.",
        "factory": lambda settings=None: WizardModal(
            settings=settings or _build_default_settings()
        ),
    },
]


def get_catalog_registry() -> List[Dict[str, Any]]:
    """Return list of registered catalog component definitions."""
    return CATALOG_REGISTRY


async def _audit_components_async(
    target_components: List[Dict[str, Any]], settings: AppSettings
) -> Tuple[int, List[A11yViolation]]:
    """Run accessibility scans against target components inside a headless Textual test pilot."""
    all_violations: List[A11yViolation] = []
    scanned_count = 0

    app = AutoSorterTUI(settings=settings)
    async with app.run_test() as pilot:
        for comp_def in target_components:
            comp_id = comp_def["id"]
            factory = comp_def["factory"]

            if comp_id == "autosorter_tui":
                v = inspect_tui_component(app)
                all_violations.extend(v)
                scanned_count += 1
            else:
                comp_instance = factory(settings=settings)
                app.push_screen(comp_instance)
                await pilot.pause(0.02)
                v = inspect_tui_component(comp_instance)
                all_violations.extend(v)
                scanned_count += 1
                with contextlib.suppress(Exception):
                    await pilot.press("escape")
                    await pilot.pause(0.01)
                with contextlib.suppress(Exception):
                    app.pop_screen()

    return scanned_count, all_violations


def run_catalog_audit(
    component_id: Optional[str] = None, settings: Optional[AppSettings] = None
) -> Tuple[int, List[A11yViolation]]:
    """Execute headless accessibility audit against all or specified component(s)."""
    settings_obj = settings or _build_default_settings()
    registry = get_catalog_registry()

    if component_id:
        norm_id = component_id.strip().lower()
        filtered = [
            c
            for c in registry
            if c["id"].lower() == norm_id or c["name"].lower() == norm_id
        ]
        if not filtered:
            raise ValueError(f"Unknown component identifier: '{component_id}'")
        target_components = filtered
    else:
        target_components = registry

    return asyncio.run(_audit_components_async(target_components, settings_obj))


def main(argv: Optional[List[str]] = None) -> int:
    """CLI launcher and accessibility gate handler for Component Catalog."""
    parser = argparse.ArgumentParser(
        description="Standalone Textual TUI Component Catalog & Accessibility Gate"
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Execute headless accessibility scans against registered TUI components and exit",
    )
    parser.add_argument(
        "--component",
        type=str,
        default=None,
        help="Target component ID or name to inspect or audit",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all registered catalog components and exit",
    )

    args = parser.parse_args(argv)

    if args.list:
        print("=== Registered Textual TUI Component Catalog ===")
        print(f"{'ID':<30} | {'Type':<8} | {'Name':<35}")
        print("-" * 80)
        for comp in CATALOG_REGISTRY:
            print(f"{comp['id']:<30} | {comp['category']:<8} | {comp['name']:<35}")
        print(f"\nTotal components registered: {len(CATALOG_REGISTRY)}")
        return 0

    if args.audit_only:
        print("Starting headless Textual TUI accessibility audit...")
        try:
            total_scans, violations = run_catalog_audit(component_id=args.component)
        except ValueError as err:
            print(f"Error: {err}", file=sys.stderr)
            return 1
        except Exception as err:  # noqa: BLE001
            print(f"Audit execution error: {err}", file=sys.stderr)
            return 1

        if violations:
            print(
                f"\nFAILED: Found {len(violations)} accessibility violation(s) across {total_scans} scanned component(s).",
                file=sys.stderr,
            )
            for v in violations:
                print(
                    f"  [{v.rule_id}] Component '{v.component_id}': {v.message} (locator: {v.locator})",
                    file=sys.stderr,
                )
            return 1
        else:
            print(
                f"\nSUCCESS: All {total_scans} TUI component accessibility scan(s) passed with 0 violations."
            )
            return 0

    # Interactive launch mode
    settings = _build_default_settings()
    if args.component:
        norm_id = args.component.strip().lower()
        matched = [
            c
            for c in CATALOG_REGISTRY
            if c["id"].lower() == norm_id or c["name"].lower() == norm_id
        ]
        if not matched:
            print(
                f"Error: Unknown component identifier: '{args.component}'",
                file=sys.stderr,
            )
            return 1
        comp_def = matched[0]
        comp_instance = comp_def["factory"](settings=settings)
        if comp_def["id"] == "autosorter_tui":
            comp_instance.run()
        else:
            app = AutoSorterTUI(settings=settings)

            def _on_mount() -> None:
                app.push_screen(comp_instance)

            app.on_mount = _on_mount
            app.run()
        return 0

    app = AutoSorterTUI(settings=settings)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
