"""Clinical Compliance Plugin Entry Point."""

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def quarantine_scan_hook(orig_rel_path: str, extracted_text: str, base_dir: str) -> None:
    """Quarantine inspection hook executing clinical compliance gap analysis when relevant."""
    if extracted_text and (
        "clinical" in orig_rel_path.lower() or "trial" in orig_rel_path.lower()
    ):
        try:
            from app.plugins.clinical_compliance.clinical_compliance import (
                ClinicalComplianceEngine,
            )

            engine = ClinicalComplianceEngine()
            _ = engine.evaluate_compliance(
                classified_artifacts={os.path.basename(orig_rel_path): "01.01.01"},
                all_filenames=[os.path.basename(orig_rel_path)],
                base_dir=base_dir,
            )
        except Exception as e:
            logger.warning(f"Clinical compliance quarantine hook evaluation warning: {e}")


def register_plugin(registry: Any) -> None:
    """Register clinical compliance plugin hooks, strategies, and UI components with PluginRegistry."""
    # 1. Register quarantine lifecycle scan hook
    registry.register_quarantine_scan_hook(quarantine_scan_hook)

    # 2. Register clustering strategies
    try:
        from app.plugins.clinical_compliance.clinical_strategy import (
            ClinicalTMFStrategy,
        )

        registry.register_clustering_strategy("clinical_tmf", ClinicalTMFStrategy(mode="tmf"))
        registry.register_clustering_strategy("clinical_isf", ClinicalTMFStrategy(mode="isf"))
    except Exception as e:
        logger.error(f"Failed registering clinical clustering strategies: {e}")

    # 3. Register TUI strategy dropdown options
    registry.register_tui_strategy_options([
        ("Clinical TMF", "clinical_tmf"),
        ("Clinical ISF", "clinical_isf"),
    ])

    # 4. Register TUI modal views
    try:
        from app.plugins.clinical_compliance.tui_views import CROForensicModal

        registry.register_tui_view("cro_forensic", CROForensicModal)
    except Exception as e:
        logger.error(f"Failed registering clinical TUI views: {e}")

    # 5. Register TUI settings switches
    registry.register_tui_switch({
        "key": "CLINICAL_SMART_RENAMING",
        "id": "switch-clinical-renaming",
        "label": " Clinical Smart Renaming",
        "tooltip": "Toggle clinical smart renaming compliance mode",
    })
