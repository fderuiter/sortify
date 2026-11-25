"""Regression tests for Python 3.12 UP rule migrations (StrEnum, PEP 604, forward refs)."""

import importlib
import pkgutil
import types
import typing
from enum import StrEnum
from unittest.mock import MagicMock

import pytest

import app.core.scanner as scanner_mod
import app.ui.mixins as ui_mixins_pkg
import app.ui.modals as ui_modals_pkg
from app.core.daemon import DaemonState
from app.core.downloader import DownloadState
from app.core.extractor import ExtractionStatus
from app.core.mover import (
    DirectoryCleanupStatus,
    MoveEngineState,
    MoveOpState,
    MoveVerificationStatus,
    _record_move_state,
)
from app.core.pipeline import IllegalStateTransitionError, PipelineLifecycleState
from app.core.plugin_registry import PluginRegistry
from app.core.security_advisory import SecurityAdvisoryClient
from app.core.session import AppSession
from app.plugins.clinical_compliance.cro_multi_study_pipeline import CROPipelineState
from app.ui.mixins.tree_view import PathLookupIndex, TreeViewMixin
from app.ui.modals.command_palette import CommandPaletteModal
from app.ui.modals.drop_zone import DropZoneModal
from app.ui.modals.edit import (
    DirectorySelectModal,
    ManualPathFallbackModal,
    NewFolderModal,
    ReassignModal,
    RenameModal,
    _BaseGroupSelectionModal,
)
from app.ui.modals.reports import ExportReportModal, ExtractionPreviewModal
from app.ui.modals.rules import ExportRulesModal, RuleEditModal, RuleImportModal
from app.ui.modals.session import (
    HistoryModal,
    MultiSessionRecoveryModal,
    RollbackConfirmModal,
    SessionRecoveryModal,
)
from app.ui.modals.settings import SettingsModal
from app.ui.notifications import NotificationManager, NotificationMode
from scripts.auto_merge_controller import AutoMergeAction

MIGRATED_STR_ENUMS: tuple[type[StrEnum], ...] = (
    DaemonState,
    DownloadState,
    ExtractionStatus,
    MoveEngineState,
    MoveOpState,
    DirectoryCleanupStatus,
    MoveVerificationStatus,
    PipelineLifecycleState,
    CROPipelineState,
    NotificationMode,
    AutoMergeAction,
)


@pytest.mark.parametrize("enum_cls", MIGRATED_STR_ENUMS)
def test_migrated_enums_inherit_strenum_and_format_bare_values(
    enum_cls: type[StrEnum],
) -> None:
    """Every migrated string enum subclasses StrEnum and formats bare values in f-strings."""
    assert issubclass(enum_cls, StrEnum)
    members = list(enum_cls)
    assert members
    for member in members:
        assert f"{member}" == member.value
        assert str(member) == member.value
        assert f"{enum_cls.__name__}." not in f"{member}"


def test_record_move_state_supports_enum_and_raw_strings() -> None:
    """_record_move_state dispatches cleanly for both MoveOpState members and raw strings."""
    moves = MagicMock()

    _record_move_state(
        moves, "sess-1", 1, MoveOpState.DISPLACEMENT, backup_path="/tmp/b"
    )
    moves.record_displacement.assert_called_once_with("sess-1", 1, backup_path="/tmp/b")

    _record_move_state(moves, "sess-1", 2, MoveOpState.DONE, dest_hash="abc")
    moves.mark_done.assert_called_once_with("sess-1", 2, dest_hash="abc")

    moves.reset_mock()
    _record_move_state(moves, "sess-1", 3, "displacement", backup_path="/tmp/c")
    moves.record_displacement.assert_called_once_with("sess-1", 3, backup_path="/tmp/c")

    _record_move_state(moves, "sess-1", 4, "verified")
    moves.mark_verified.assert_called_once_with("sess-1", 4)


def test_record_move_state_noop_and_optional_error_handling() -> None:
    """_record_move_state ignores missing ledger args and honors required=False."""
    moves = MagicMock()
    _record_move_state(None, "sess-1", 1, MoveOpState.DONE)
    _record_move_state(moves, "", 1, MoveOpState.DONE)
    _record_move_state(moves, "sess-1", None, MoveOpState.DONE)
    moves.mark_done.assert_not_called()

    moves.mark_failed.side_effect = RuntimeError("ledger write error")
    _record_move_state(moves, "sess-1", 5, MoveOpState.FAILED, required=False)

    with pytest.raises(RuntimeError, match="ledger write error"):
        _record_move_state(moves, "sess-1", 5, MoveOpState.FAILED, required=True)


def test_session_transition_to_formats_enum_and_raw_string_errors() -> None:
    """AppSession.transition_to formats clean state values without class prefixes."""
    session = object.__new__(AppSession)
    session.lifecycle_state = PipelineLifecycleState.COMPLETED

    with pytest.raises(
        IllegalStateTransitionError,
        match="Illegal state transition from 'completed' to 'executing'\\.",
    ):
        session.transition_to(PipelineLifecycleState.EXECUTING)

    session.lifecycle_state = "completed"
    with pytest.raises(
        IllegalStateTransitionError,
        match="Illegal state transition from 'completed' to 'raw_invalid'\\.",
    ):
        session.transition_to("raw_invalid")


def test_quoted_pep604_forward_references_resolve_at_runtime() -> None:
    """Quoted PEP 604 forward references resolve via get_type_hints without __future__ imports."""
    for cls in (NotificationManager, PluginRegistry, SecurityAdvisoryClient):
        hints = typing.get_type_hints(cls)
        assert hints["_instance"] == (cls | None)

    scanner_hints = typing.get_type_hints(scanner_mod)
    filter_hint = scanner_hints["_ACTIVE_SCAN_FILTER"]
    assert typing.get_args(filter_hint) == (scanner_mod.ScanFilter | None,)


@pytest.mark.parametrize(
    "modal_cls",
    (
        CommandPaletteModal,
        DropZoneModal,
        RenameModal,
        NewFolderModal,
        ReassignModal,
        DirectorySelectModal,
        ManualPathFallbackModal,
        _BaseGroupSelectionModal,
        ExtractionPreviewModal,
        ExportReportModal,
        ExportRulesModal,
        RuleImportModal,
        RuleEditModal,
        SessionRecoveryModal,
        MultiSessionRecoveryModal,
        RollbackConfirmModal,
        HistoryModal,
        SettingsModal,
    ),
)
def test_modal_generic_bases_use_pep604_union_types(modal_cls: type) -> None:
    """All 18 migrated modal screen classes parameterize ResponsiveModalScreen with T | None."""
    orig_bases = getattr(modal_cls, "__orig_bases__", ())
    modal_bases = [
        base
        for base in orig_bases
        if getattr(typing.get_origin(base), "__name__", "") == "ResponsiveModalScreen"
    ]
    assert len(modal_bases) == 1
    (arg,) = typing.get_args(modal_bases[0])
    assert typing.get_origin(arg) in (types.UnionType, typing.Union)
    assert type(None) in typing.get_args(arg)


def test_tree_view_helpers_use_pep604_union_annotations() -> None:
    """Consolidated PathLookupIndex and TreeViewMixin helpers resolve PEP 604 T | None hints."""
    path_methods = (
        PathLookupIndex._matches_path,
        PathLookupIndex._matches_node,
        PathLookupIndex._matches_collision,
        TreeViewMixin._resolve_node_target_path,
        TreeViewMixin._invalid_lock_candidates,
        TreeViewMixin._has_invalid_lock,
        TreeViewMixin._get_invalid_lock_error,
    )
    for fn in path_methods:
        hints = typing.get_type_hints(fn)
        union_hints = [
            hint
            for hint in hints.values()
            if typing.get_origin(hint) in (types.UnionType, typing.Union)
        ]
        assert union_hints, f"Expected PEP 604 union annotations on {fn.__qualname__}"
        for hint in union_hints:
            assert typing.get_origin(hint) is types.UnionType
            assert type(None) in typing.get_args(hint)


def test_tui_mixins_and_modals_import_cleanly_without_eager_annotation_skew() -> None:
    """All TUI mixin, modal, and host modules evaluate class annotations without NameError."""
    discovered: list[str] = ["app.ui.tui"]
    for pkg in (ui_mixins_pkg, ui_modals_pkg):
        for mod_info in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
            discovered.append(mod_info.name)

    assert len(discovered) >= 15
    for mod_name in discovered:
        mod = importlib.import_module(mod_name)
        assert not hasattr(mod, "Optional"), (
            f"{mod_name} should use PEP 604 T | None rather than importing typing.Optional"
        )
