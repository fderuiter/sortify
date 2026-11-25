"""Dialogs for editing the plan tree: rename, new folder, reassign, directory pickers and duplicate review."""

import contextlib
import logging
import os
import re
from typing import Any

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, Select, Static

from app.core.path_utils import _is_absolute_path
from app.ui.heading import Heading
from app.ui.layout import ModalBox, ResponsiveModalScreen

logger = logging.getLogger(__name__)


class RenameResult(str):
    """String subclass representing rename outcome with optional destination override."""

    destination_override: str | None = None
    batch_items: list[dict[str, Any]] | None = None
    is_batch: bool = False

    def __new__(
        cls,
        value: str,
        destination_override: str | None = None,
        batch_items: list[dict[str, Any]] | None = None,
        is_batch: bool = False,
    ):
        """Instantiate string value with optional destination path override."""
        instance = super().__new__(cls, value)
        instance.destination_override = destination_override
        instance.batch_items = batch_items
        instance.is_batch = is_batch
        return instance

    @property
    def name(self) -> str:
        """Return base string name value."""
        return str(self)

    def to_tuple(self) -> tuple[str, str | None]:
        """Return (name, destination_override) tuple representation."""
        return (str(self), self.destination_override)


def _set_collision_warning_helper(modal: Any, msg: str, is_error: bool = False) -> None:
    """Update collision warning label widget text and styles on a modal screen."""
    try:
        lbl = modal.query_one("#warning-collision", Label)
        lbl.update(msg)
        if is_error:
            lbl.remove_class("modal-warning")
            lbl.add_class("modal-error")
        else:
            lbl.remove_class("modal-error")
            lbl.add_class("modal-warning")
    except Exception:
        pass


_set_collision_warning = _set_collision_warning_helper
_update_collision_warning = _set_collision_warning_helper


class _SubmittableModalMixin:
    """Mixin forwarding Enter submission on modal Input fields to ``action_confirm``."""

    @on(Input.Submitted)
    def action_submit(self) -> None:
        """Submit input on Enter key press."""
        getattr(self, "action_confirm")()


class _CollisionWarningMixin:
    """Mixin updating ``#warning-collision`` inline feedback state."""

    def _set_collision_warning(self, msg: str, is_error: bool = False) -> None:
        """Update collision warning label widget text and styles."""
        _set_collision_warning_helper(self, msg, is_error=is_error)


class RenameModal(
    _CollisionWarningMixin,
    _SubmittableModalMixin,
    ResponsiveModalScreen[str | RenameResult | None],
):
    """Modal dialog for renaming a file or folder node with pattern token formatting and destination path override support."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .modal-subtitle {
        color: $text-muted;
        margin-bottom: 1;
    }
    .token-help {
        color: $text-muted;
        margin-bottom: 1;
    }
    .modal-warning {
        color: $warning;
        text-style: bold;
        margin-top: 1;
        margin-bottom: 1;
    }
    .modal-preview {
        color: $accent;
        margin-top: 1;
        margin-bottom: 1;
        text-style: bold;
    }
    .button-row {
        min-height: 3;
    }
    .button-row Button {
        margin-left: 1;
    }
    Button:focus, Input:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(
        self,
        title: str,
        current_name: str = "",
        extension: str = "",
        category: str = "",
        file_date: str = "",
        seq: int = 1,
        destination_override: str = "",
        is_folder: bool = False,
        plan: dict[str, Any] | None = None,
        base_dir: str | None = None,
        source_path: str | None = None,
        files: list[Any] | None = None,
    ):
        super().__init__()
        self.modal_title = title
        self.files = list(files) if files is not None else None
        self.current_name = current_name
        self.extension = extension
        self.category = category
        self.file_date = file_date
        self.seq = seq
        self.destination_override = destination_override
        self.is_folder = is_folder
        self.plan = plan
        self.base_dir = base_dir
        self.source_path = source_path
        if not self.file_date and not self.is_folder and self.source_path:
            from app.core.pattern_formatter import resolve_disk_file_date

            self.file_date = resolve_disk_file_date(
                self.source_path, base_dir=self.base_dir
            )
        self._last_collision_state: str | None = None

    def _extract_file_metadata(
        self, f: Any, seq: int
    ) -> tuple[dict[str, Any], str, str, str, dict[str, Any] | None]:
        """Extract metadata dict, fallback filename, key, folder, and info dict from a file item."""
        from app.core.pattern_formatter import resolve_disk_file_date

        if isinstance(f, str):
            key = f
            stem, ext = os.path.splitext(os.path.basename(f))
            cat = self.category
            date = self.file_date or resolve_disk_file_date(f, base_dir=self.base_dir)
            info = None
            folder = self.category
            fallback = f"{stem}{ext}" if ext else stem
            meta = {
                "original": stem,
                "stem": stem,
                "extension": ext,
                "category": cat,
                "date": date,
                "seq": seq,
                "base_dir": self.base_dir,
                "filepath": f,
            }
            return meta, fallback, key, folder, info

        if isinstance(f, dict):
            key = (
                f.get("key")
                or f.get("target_filename")
                or f.get("name")
                or f.get("original")
                or ""
            )
            info = f.get("info") if isinstance(f.get("info"), dict) else f
            if not key and info and isinstance(info, dict):
                key = info.get("target_filename") or info.get("key", "")

            item_fp = (
                f.get("filepath")
                or f.get("source_path")
                or (
                    (info.get("filepath") or info.get("source_path"))
                    if isinstance(info, dict)
                    else None
                )
            )
            if not item_fp and key and self.base_dir:
                item_fp = os.path.join(self.base_dir, str(key))

            stem = f.get("original") or f.get("current_name") or f.get("stem")
            ext = f.get("extension") or f.get("ext")
            if not stem or ext is None:
                base = os.path.basename(key or (item_fp or ""))
                base_stem, base_ext = os.path.splitext(base)
                if not stem:
                    stem = base_stem
                if ext is None:
                    ext = base_ext

            folder = (
                f.get("folder")
                or f.get("category")
                or (info.get("folder") if isinstance(info, dict) else None)
                or self.category
            )
            date = (
                f.get("date")
                or f.get("file_date")
                or (
                    (info.get("date") or info.get("file_date"))
                    if isinstance(info, dict)
                    else None
                )
                or resolve_disk_file_date(
                    str(item_fp) if item_fp else None,
                    base_dir=(
                        info.get("source_root")
                        if isinstance(info, dict) and info.get("source_root")
                        else self.base_dir
                    ),
                )
                or self.file_date
            )
            seq_val = f.get("seq", seq)
            fallback = f"{stem}{ext}" if ext else stem
            meta = {
                "original": stem,
                "stem": stem,
                "extension": ext,
                "category": folder,
                "date": date,
                "seq": seq_val,
                "base_dir": self.base_dir,
                "filepath": item_fp,
            }
            return meta, fallback, key, folder, info

        key_str = str(f)
        stem, ext = os.path.splitext(os.path.basename(key_str))
        meta = {
            "original": stem,
            "stem": stem,
            "extension": ext,
            "category": self.category,
            "date": self.file_date,
            "seq": seq,
            "base_dir": self.base_dir,
        }
        return meta, key_str, key_str, self.category, None

    def get_batch_previews(self, pattern_str: str) -> list[str]:
        """Calculate sequential seq numbering and preview token substitution for each checked file."""
        if not self.files:
            return []
        from app.core.pattern_formatter import PatternTokenFormatter

        previews = []
        for idx, f in enumerate(self.files):
            file_seq = self.seq + idx
            meta, fallback, _, _, _ = self._extract_file_metadata(f, file_seq)
            item_fp = meta.get("filepath")
            formatted = PatternTokenFormatter.format_pattern(
                pattern=pattern_str,
                metadata=meta,
                seq=file_seq,
                fallback_original=fallback,
                file_path=str(item_fp) if item_fp else None,
            )
            previews.append(formatted)
        return previews

    def _evaluate_preview(self, pattern_str: str) -> str:
        """Evaluate formatted pattern string for live preview."""
        if self.files:
            previews = self.get_batch_previews(pattern_str)
            if not previews:
                return ""
            if len(previews) == 1:
                return previews[0]
            if len(previews) <= 3:
                return ", ".join(previews)
            return f"{previews[0]}, {previews[1]}, ... (+{len(previews) - 2} more)"

        from app.core.pattern_formatter import PatternTokenFormatter

        metadata = {
            "original": self.current_name,
            "stem": self.current_name,
            "extension": self.extension,
            "category": self.category,
            "date": self.file_date,
            "seq": self.seq,
            "base_dir": self.base_dir,
        }
        fallback = (
            f"{self.current_name}{self.extension}"
            if self.extension
            else self.current_name
        )
        return PatternTokenFormatter.format_pattern(
            pattern=pattern_str,
            metadata=metadata,
            seq=self.seq,
            fallback_original=fallback,
            file_path=None if self.is_folder else self.source_path,
        )

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with ModalBox(classes="modal-box"):
            with Vertical(classes="modal-body"):
                yield Heading(
                    self.modal_title,
                    level=1,
                    classes="modal-title",
                    id="modal-title",
                )
                if self.files:
                    yield Label(
                        f"Batch renaming {len(self.files)} files. Sequential index starts at {self.seq:02d}.",
                        classes="modal-subtitle",
                    )
                elif self.extension:
                    yield Label(
                        f"Extension '{self.extension}' is locked unless modified in template",
                        classes="modal-subtitle",
                    )
                yield Label(
                    "Tokens: {date}, {category}, {original}, {extension}, {seq}",
                    classes="token-help",
                )
                inp = Input(
                    value=self.current_name,
                    placeholder="Enter new name or pattern (e.g. {date}_{category}_{original})...",
                    id="input-name",
                )
                inp.tooltip = "Enter new file or folder name or pattern template"
                yield inp

                yield Heading(
                    "Optional Destination Path Override (relative or absolute):",
                    level=2,
                )
                dest_inp = Input(
                    value=self.destination_override,
                    placeholder="e.g. /external/backup, D:\\Archive, or subcategory...",
                    id="input-destination",
                )
                dest_inp.tooltip = "Enter optional destination directory path override (absolute or relative)"
                yield dest_inp

                yield Label("", id="warning-collision", classes="modal-warning")

                yield Label(
                    f"Preview: {self._evaluate_preview(self.current_name)}",
                    id="label-preview",
                    classes="modal-preview",
                )
            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel rename action and close dialog"
                yield btn_cancel
                btn_confirm = Button("Rename", id="btn-confirm", variant="primary")
                btn_confirm.tooltip = "Confirm renaming action"
                yield btn_confirm

    def on_mount(self) -> None:
        """Focus input field on mount and emit screen reader announcement."""
        super().on_mount()
        self.query_one("#input-name", Input).focus()
        self.announce(
            f"Opened rename dialog for '{self.modal_title}'. Enter new name or pattern and press Enter or click Rename."
        )
        self._check_collision_and_update_ui()

    def _validate_path_security(self, path_str: str) -> str | None:
        """Validate path string for directory traversal and illegal characters."""
        if not path_str:
            return None
        if "\x00" in path_str:
            return "Null bytes are not permitted in paths."
        segments = re.split(r"[/\\]+", path_str)
        if ".." in segments:
            return "Directory traversal ('..') segments are not permitted."
        return None

    def _check_collision_and_update_ui(self) -> tuple[bool, str | None]:
        """Run live collision check and path validation across plan and filesystem.

        Returns (is_blocked, message).
        """
        try:
            name_inp = self.query_one("#input-name", Input)
            name_val = name_inp.value.strip()
        except Exception:
            return False, None

        try:
            dest_inp = self.query_one("#input-destination", Input)
            dest_val = dest_inp.value.strip()
        except Exception:
            dest_val = ""

        # Check directory traversal segments (..) and null bytes
        err = self._validate_path_security(name_val) or self._validate_path_security(
            dest_val
        )
        if err:
            msg = f"⚠️ Security Error: {err}"
            self._set_collision_warning(msg, is_error=True)
            if self._last_collision_state != msg:
                self.announce(msg)
                self._last_collision_state = msg
            return True, err

        # Check illegal characters in filename
        if self.files:
            batch_previews = self.get_batch_previews(name_val) if name_val else []
            for formatted_name in batch_previews:
                if any(c in formatted_name for c in '<>:"/\\|?*\x00'):
                    msg = "Filename contains illegal filesystem characters."
                    self._set_collision_warning(
                        f"⚠️ Validation Error: {msg}", is_error=True
                    )
                    return True, msg
        else:
            formatted = self._evaluate_preview(name_val) if name_val else ""
            if self.extension and formatted and not formatted.endswith(self.extension):
                formatted_name = f"{formatted}{self.extension}"
            else:
                formatted_name = formatted

            if not self.is_folder and formatted_name:
                if any(c in formatted_name for c in '<>:"/\\|?*\x00'):
                    msg = "Filename contains illegal filesystem characters."
                    self._set_collision_warning(
                        f"⚠️ Validation Error: {msg}", is_error=True
                    )
                    return True, msg

        if dest_val:
            s_check = re.sub(r"^[a-zA-Z]:", "", dest_val)
            if any(c in s_check for c in '<>"|?*\x00'):
                msg = "Destination path contains illegal characters."
                self._set_collision_warning(f"⚠️ Validation Error: {msg}", is_error=True)
                return True, msg

        collision_msg: str | None = None

        if self.is_folder:
            target_folder = dest_val if dest_val else formatted_name
            if target_folder and target_folder != self.current_name:
                if self.plan and target_folder in self.plan:
                    collision_msg = (
                        f"Folder '{target_folder}' already exists in plan. "
                        "Confirming will merge folder contents safely."
                    )
                elif self.base_dir:
                    disk_path = (
                        target_folder
                        if _is_absolute_path(target_folder)
                        else os.path.join(self.base_dir, target_folder)
                    )
                    if os.path.isdir(disk_path):
                        collision_msg = (
                            f"Target folder '{target_folder}' exists on disk. "
                            "Contents will merge safely."
                        )
        elif self.files:
            target_folder = dest_val if dest_val else self.category
            if batch_previews:
                seen_names = set()
                dup_name = None
                for nm in batch_previews:
                    if nm in seen_names:
                        dup_name = nm
                        break
                    seen_names.add(nm)
                if dup_name:
                    collision_msg = (
                        f"Collision detected: Multiple files resolve to duplicate name '{dup_name}'. "
                        "Names will be disambiguated upon renaming."
                    )
                elif self.plan:
                    folder_dict = self.plan.get(target_folder, {})
                    if isinstance(folder_dict, dict):
                        for fn in batch_previews:
                            if fn in folder_dict:
                                collision_msg = (
                                    f"Collision detected: '{fn}' already exists in "
                                    f"'{target_folder or 'root'}'. Overwrite will be blocked (names disambiguated)."
                                )
                                break
        else:
            target_folder = dest_val if dest_val else self.category
            if formatted_name:
                if self.plan:
                    folder_dict = self.plan.get(target_folder)
                    if isinstance(folder_dict, dict) and formatted_name in folder_dict:
                        existing_node = folder_dict[formatted_name]
                        if not (
                            isinstance(existing_node, dict)
                            and existing_node.get("filepath")
                            and self.source_path
                            and existing_node.get("filepath") == self.source_path
                        ):
                            collision_msg = (
                                f"Collision detected: '{formatted_name}' already exists in "
                                f"'{target_folder or 'root'}'. Overwrite will be blocked (names disambiguated)."
                            )

                if not collision_msg and (
                    self.base_dir
                    or (target_folder and _is_absolute_path(target_folder))
                ):
                    if target_folder and _is_absolute_path(target_folder):
                        dest_file_path = os.path.normpath(
                            os.path.join(target_folder, formatted_name)
                        )
                    elif self.base_dir:
                        dest_file_path = os.path.normpath(
                            os.path.join(self.base_dir, target_folder, formatted_name)
                        )
                    else:
                        dest_file_path = None

                    if dest_file_path and os.path.lexists(dest_file_path):
                        is_same = False
                        if self.source_path and os.path.lexists(self.source_path):
                            try:
                                is_same = os.path.samefile(
                                    dest_file_path, self.source_path
                                )
                            except OSError:
                                is_same = os.path.normcase(
                                    os.path.abspath(dest_file_path)
                                ) == os.path.normcase(os.path.abspath(self.source_path))
                        if not is_same:
                            collision_msg = (
                                f"Collision detected: File '{formatted_name}' exists on disk at destination. "
                                "Overwrite will be blocked (names disambiguated)."
                            )

        if collision_msg:
            full_warn = f"⚠️ {collision_msg}"
            self._set_collision_warning(full_warn, is_error=False)
            if self._last_collision_state != full_warn:
                self.announce(full_warn)
                self._last_collision_state = full_warn
            return False, collision_msg
        else:
            self._set_collision_warning("", is_error=False)
            self._last_collision_state = None
            return False, None

    @on(Input.Changed, "#input-name")
    def on_input_changed(self, event: Input.Changed) -> None:
        """Update live preview and collision status on input value change."""
        try:
            preview_str = self._evaluate_preview(event.value)
            lbl = self.query_one("#label-preview", Label)
            lbl.update(f"Preview: {preview_str}")
        except Exception:
            pass
        self._check_collision_and_update_ui()

    @on(Input.Changed, "#input-destination")
    def on_destination_changed(self, event: Input.Changed) -> None:
        """Update collision status on destination path override change."""
        self._check_collision_and_update_ui()

    @on(Button.Pressed, "#btn-confirm")
    def action_confirm(self) -> None:
        """Confirm renaming action."""
        is_blocked, err = self._check_collision_and_update_ui()
        if is_blocked:
            self.announce(f"Cannot confirm rename: {err}")
            return

        val = self.query_one("#input-name", Input).value.strip()
        dest_val = self.query_one("#input-destination", Input).value.strip()

        if self.files:
            if not val and not dest_val:
                self.announce("Cancelled rename action.")
                self.dismiss(None)
                return

            batch_previews = self.get_batch_previews(val)
            batch_items = []
            for idx, f in enumerate(self.files):
                file_seq = self.seq + idx
                _meta, fallback, key, folder, info = self._extract_file_metadata(
                    f, file_seq
                )
                formatted_name = (
                    batch_previews[idx] if idx < len(batch_previews) else fallback
                )
                target_folder = dest_val if dest_val else folder
                batch_items.append(
                    {
                        "file": f,
                        "key": key,
                        "old_name": key,
                        "new_name": formatted_name,
                        "folder": target_folder,
                        "old_folder": folder,
                        "info": info,
                    }
                )
            result = RenameResult(
                val,
                destination_override=dest_val if dest_val else None,
                batch_items=batch_items,
                is_batch=True,
            )
            dest_desc = f" with destination override '{dest_val}'" if dest_val else ""
            self.announce(
                f"Confirmed batch rename for {len(batch_items)} files{dest_desc}."
            )
            self.dismiss(result)
            return

        formatted = self._evaluate_preview(val) if val else ""

        if formatted:
            result = RenameResult(
                formatted, destination_override=dest_val if dest_val else None
            )
            dest_desc = f" with destination override '{dest_val}'" if dest_val else ""
            self.announce(f"Confirmed rename to '{formatted}'{dest_desc}.")
            self.dismiss(result)
        else:
            self.announce("Cancelled rename action.")
            self.dismiss(None)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel renaming action."""
        self.announce("Cancelled rename action.")
        self.dismiss(None)


class NewFolderModal(_SubmittableModalMixin, ResponsiveModalScreen[str | None]):
    """Modal dialog for creating a new folder node in the plan."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .button-row {
        min-height: 3;
    }
    .button-row Button {
        margin-left: 1;
    }
    Button:focus, Input:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with ModalBox(classes="modal-box"):
            with Vertical(classes="modal-body"):
                yield Heading(
                    "Create New Target Folder",
                    level=1,
                    classes="modal-title",
                    id="modal-title",
                )
                inp = Input(
                    placeholder="e.g. Financials, Contracts, Invoices",
                    id="input-folder-name",
                )
                inp.tooltip = "Enter new target folder category name"
                yield inp
            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel folder creation and close dialog"
                yield btn_cancel
                btn_confirm = Button("Create", id="btn-confirm", variant="primary")
                btn_confirm.tooltip = "Confirm new folder category creation"
                yield btn_confirm

    def on_mount(self) -> None:
        """Focus input field on mount and emit screen reader announcement."""
        super().on_mount()
        self.query_one("#input-folder-name", Input).focus()
        self.announce("Opened create new target folder dialog.")

    @on(Button.Pressed, "#btn-confirm")
    def action_confirm(self) -> None:
        """Confirm folder creation action."""
        val = self.query_one("#input-folder-name", Input).value.strip()
        if val:
            self.announce(f"Confirmed folder creation for '{val}'.")
        else:
            self.announce("Cancelled folder creation.")
        self.dismiss(val if val else None)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel folder creation action."""
        self.announce("Cancelled folder creation.")
        self.dismiss(None)


class ReassignModal(
    _CollisionWarningMixin,
    _SubmittableModalMixin,
    ResponsiveModalScreen[str | None],
):
    """Modal dialog for reassigning checked file nodes to a target category folder."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .modal-warning {
        color: $warning;
        text-style: bold;
        margin-top: 1;
        margin-bottom: 1;
    }
    .modal-error {
        color: $error;
        text-style: bold;
        margin-top: 1;
        margin-bottom: 1;
    }
    .button-row {
        min-height: 3;
    }
    .button-row Button {
        margin-left: 1;
    }
    Button:focus, Input:focus, Select:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(
        self,
        existing_folders: list[str],
        count: int = 1,
        plan: dict[str, Any] | None = None,
        base_dir: str | None = None,
        target_files: list[dict[str, Any]] | None = None,
        dest_dir: str | None = None,
        settings: Any = None,
    ):
        super().__init__()
        from app.core.scanner import discover_existing_folders

        disk_folders = discover_existing_folders(
            base_dir, dest_dir=dest_dir, settings=settings
        )
        merged: list[str] = []
        seen_lower: set[str] = set()
        for folder in list(existing_folders or []) + disk_folders:
            if not folder:
                continue
            key = folder.lower()
            if key not in seen_lower:
                seen_lower.add(key)
                merged.append(folder)
        self.existing_folders = merged
        self.count = count
        self.plan = plan
        self.base_dir = base_dir
        self.dest_dir = dest_dir
        self.settings = settings
        self.target_files = target_files or []
        self._last_collision_state: str | None = None

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with ModalBox(classes="modal-box"):
            with Vertical(classes="modal-body"):
                yield Heading(
                    f"Reassign {self.count} Selected File(s)",
                    level=1,
                    classes="modal-title",
                    id="modal-title",
                )
                yield Heading("Select Existing Target Folder:", level=2)
                options = [(f, f) for f in sorted(self.existing_folders)]
                if options:
                    yield Select(options, prompt="Choose folder...", id="select-folder")
                yield Heading(
                    "Or Enter Target Category / External Directory Path:", level=2
                )
                inp = Input(
                    placeholder="e.g. Drafts, /Volumes/Backup, or D:\\External\\Archive",
                    id="input-target-folder",
                )
                inp.tooltip = (
                    "Enter relative folder name or absolute external directory path"
                )
                yield inp
                yield Label("", id="warning-collision", classes="modal-warning")
            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel reassign dialog"
                yield btn_cancel
                btn_confirm = Button("Reassign", id="btn-confirm", variant="primary")
                btn_confirm.tooltip = "Confirm bulk reassignment"
                yield btn_confirm

    def on_mount(self) -> None:
        """Focus input field on mount and emit announcement."""
        super().on_mount()
        self.query_one("#input-target-folder", Input).focus()
        self.announce(f"Opened bulk reassign dialog for {self.count} items.")
        self._check_collision_and_update_ui()

    def _check_collision_and_update_ui(self) -> tuple[bool, str | None]:
        """Run live collision check and path validation across plan and filesystem."""
        try:
            inp = self.query_one("#input-target-folder", Input)
            target = inp.value.strip()
            if not target:
                sel = self.query_one("#select-folder", Select)
                if sel.value is not Select.BLANK and sel.value:
                    target = str(sel.value).strip()
        except Exception:
            return False, None

        if not target:
            self._set_collision_warning("", is_error=False)
            self._last_collision_state = None
            return False, None

        if "\x00" in target:
            msg = "Null bytes are not permitted in paths."
            self._set_collision_warning(f"⚠️ Security Error: {msg}", is_error=True)
            return True, msg

        segments = re.split(r"[/\\]+", target)
        if ".." in segments:
            msg = "Directory traversal ('..') segments are not permitted."
            self._set_collision_warning(f"⚠️ Security Error: {msg}", is_error=True)
            if self._last_collision_state != msg:
                self.announce(f"⚠️ Security Error: {msg}")
                self._last_collision_state = msg
            return True, msg

        s_check = re.sub(r"^[a-zA-Z]:", "", target)
        if any(c in s_check for c in '<>"|?*\x00'):
            msg = "Destination path contains illegal characters."
            self._set_collision_warning(f"⚠️ Validation Error: {msg}", is_error=True)
            return True, msg

        # Check collision across staging plan and filesystem
        collisions = []
        target_dict = (
            self.plan.get(target, {})
            if self.plan and isinstance(self.plan.get(target), dict)
            else {}
        )

        for fi in self.target_files:
            fn = fi.get("target_filename") or fi.get("key")
            src_fp = fi.get("filepath")
            if not fn:
                continue

            if fn in target_dict:
                existing_item = target_dict[fn]
                if (
                    isinstance(existing_item, dict)
                    and existing_item is not fi
                    and existing_item.get("filepath") != src_fp
                ):
                    collisions.append(fn)
            elif self.base_dir or _is_absolute_path(target):
                if _is_absolute_path(target):
                    dst_disk = os.path.normpath(os.path.join(target, fn))
                elif self.base_dir:
                    dst_disk = os.path.normpath(os.path.join(self.base_dir, target, fn))
                else:
                    dst_disk = None

                if dst_disk and os.path.lexists(dst_disk):
                    is_same = False
                    if src_fp and os.path.lexists(src_fp):
                        try:
                            is_same = os.path.samefile(dst_disk, src_fp)
                        except OSError:
                            is_same = os.path.normcase(
                                os.path.abspath(dst_disk)
                            ) == os.path.normcase(os.path.abspath(src_fp))
                    if not is_same:
                        collisions.append(fn)

        if collisions:
            preview_files = ", ".join(f"'{c}'" for c in collisions[:3])
            more = f" and {len(collisions) - 3} more" if len(collisions) > 3 else ""
            warn_msg = (
                f"Collision detected: File(s) {preview_files}{more} already exist in '{target}'. "
                "Accidental overwrites blocked (will disambiguate)."
            )
            full_warn = f"⚠️ {warn_msg}"
            self._set_collision_warning(full_warn, is_error=False)
            if self._last_collision_state != full_warn:
                self.announce(full_warn)
                self._last_collision_state = full_warn
            return False, warn_msg
        else:
            self._set_collision_warning("", is_error=False)
            self._last_collision_state = None
            return False, None

    @on(Input.Changed, "#input-target-folder")
    def on_input_changed(self, event: Input.Changed) -> None:
        """Update collision status on manual target folder input change."""
        self._check_collision_and_update_ui()

    @on(Select.Changed, "#select-folder")
    def on_select_changed(self, event: Select.Changed) -> None:
        """Update collision status on select folder change."""
        self._check_collision_and_update_ui()

    @on(Button.Pressed, "#btn-confirm")
    def action_confirm(self) -> None:
        """Confirm reassign action."""
        is_blocked, err = self._check_collision_and_update_ui()
        if is_blocked:
            self.announce(f"Cannot confirm reassignment: {err}")
            return

        val = self.query_one("#input-target-folder", Input).value.strip()
        if not val:
            try:
                sel = self.query_one("#select-folder", Select)
                if sel.value is not Select.BLANK and sel.value:
                    val = str(sel.value).strip()
            except Exception:
                pass
        if val:
            self.announce(f"Reassigning to target folder '{val}'.")
        else:
            self.announce("Cancelled bulk reassign.")
        self.dismiss(val if val else None)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel reassign action."""
        self.announce("Cancelled bulk reassign.")
        self.dismiss(None)


class DirectorySelectModal(
    _SubmittableModalMixin,
    ResponsiveModalScreen[str | list[str] | None],
):
    """Modal dialog for choosing target base directory or presets."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .error-banner {
        color: $error;
        text-style: bold;
    }
    .preset-row {
        width: 100%;
        layout: grid;
        grid-size: 3;
        grid-gutter: 0 1;
        margin-top: 1;
        margin-bottom: 1;
        height: auto;
        min-height: 3;
    }
    .preset-row Button {
        width: 100%;
        min-width: 0;
    }
    .sources-list {
        margin-top: 1;
        margin-bottom: 1;
        color: $text;
        border: solid $primary;
        padding: 0 1;
    }
    .source-actions-row {
        width: 100%;
        layout: grid;
        grid-size: 2;
        grid-gutter: 0 1;
        margin-bottom: 1;
        height: auto;
        min-height: 3;
    }
    .source-actions-row Button {
        width: 100%;
        min-width: 0;
    }
    .button-row {
        layout: grid;
        grid-size: 3;
        grid-gutter: 0 1;
        min-height: 3;
    }
    .button-row Button {
        width: 100%;
        min-width: 0;
    }
    Button:focus, Input:focus {
        border: heavy $accent;
        text-style: bold;
    }
    DirectorySelectModal {
        &.compact .preset-row {
            margin-top: 0;
            margin-bottom: 0;
            min-height: 1;
        }
        &.compact .preset-row Button,
        &.compact .source-actions-row Button {
            height: 1;
            min-height: 1;
            border: none;
        }
        &.compact .sources-list {
            margin-top: 0;
            margin-bottom: 0;
        }
        &.compact .source-actions-row {
            margin-bottom: 0;
            min-height: 1;
        }
        &.compact .button-row {
            grid-size: 3;
        }
        &.narrow .preset-row {
            grid-size: 2;
        }
        &.narrow .button-row {
            grid-size: 2;
        }
    }
    """

    def __init__(self, current_dir: str = "", source_dirs: list[str] | None = None):
        super().__init__()
        self.current_dir = current_dir
        self.added_via_btn = False
        if source_dirs:
            self.selected_sources = [
                d if str(d).startswith("/") else os.path.abspath(d)
                for d in source_dirs
                if d
            ]
        elif current_dir:
            self.selected_sources = [
                current_dir
                if str(current_dir).startswith("/")
                else os.path.abspath(current_dir)
            ]
        else:
            self.selected_sources = []

    @staticmethod
    def _validate_directory_path(path_str: str) -> str | None:
        """Validate if path_str exists, is a directory, and allows read access.

        Returns error message string if invalid, or None if valid.
        """
        if not path_str or not path_str.strip():
            return "Directory path cannot be empty."

        clean_path = path_str.strip()
        expanded = os.path.abspath(os.path.expanduser(clean_path))

        try:
            if not os.path.exists(expanded):
                return f"Target directory does not exist: {clean_path}"

            if not os.path.isdir(expanded):
                return f"Path is not a directory: {clean_path}"

            if not os.access(expanded, os.R_OK):
                return f"Permission denied for directory: {clean_path}"
        except Exception as exc:
            return f"Invalid directory path: {exc}"

        return None

    def _show_error(self, message: str) -> None:
        """Display inline error banner, announce message, and focus input."""
        try:
            lbl = self.query_one("#lbl-error", Static)
            lbl.update(message)
        except Exception:
            pass
        self.announce(message, priority="assertive")
        try:
            self.query_one("#input-dir", Input).focus()
        except Exception:
            pass

    @on(Input.Changed, "#input-dir")
    def on_input_changed(self, event: Input.Changed) -> None:
        """Clear error banner when user changes input text."""
        try:
            lbl = self.query_one("#lbl-error", Static)
            lbl.update("")
        except Exception:
            pass

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with ModalBox(classes="modal-box"):
            with Vertical(classes="modal-body"):
                yield Heading(
                    "Select Target Directory",
                    level=1,
                    classes="modal-title",
                    id="modal-title",
                )
                inp = Input(
                    value=self.current_dir,
                    placeholder="Enter absolute directory path...",
                    id="input-dir",
                )
                inp.tooltip = "Enter absolute target directory path to scan"
                yield inp
                yield Static("", id="lbl-error", classes="error-banner")
                yield Label("Selected Sources:", id="lbl-sources")
                yield Static(
                    self._format_sources_display(),
                    id="static-sources-list",
                    classes="sources-list",
                )
                with Horizontal(classes="source-actions-row"):
                    btn_add = Button("Add Path", id="btn-add-dir", variant="default")
                    btn_add.tooltip = "Add current path to multi-root source list"
                    yield btn_add
                    btn_clear = Button(
                        "Clear Sources", id="btn-clear-sources", variant="default"
                    )
                    btn_clear.tooltip = "Clear staged multi-root source directories"
                    yield btn_clear
                yield Label("Quick Presets:")
                with Horizontal(classes="preset-row"):
                    p_demo = Button(
                        "Demo Workspace", id="preset-demo", variant="default"
                    )
                    p_demo.tooltip = "Select sandbox demo workspace quick preset"
                    yield p_demo
                    p_dl = Button("Downloads", id="preset-downloads", variant="default")
                    p_dl.tooltip = "Select user Downloads folder quick preset"
                    yield p_dl
                    p_docs = Button(
                        "Documents", id="preset-documents", variant="default"
                    )
                    p_docs.tooltip = "Select user Documents folder quick preset"
                    yield p_docs
            with Horizontal(classes="button-row"):
                btn_browse = Button("Native Browse", id="btn-browse", variant="default")
                btn_browse.tooltip = "Launch native OS directory browser dialog"
                yield btn_browse
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel directory selection and close dialog"
                yield btn_cancel
                btn_confirm = Button("Select", id="btn-confirm", variant="primary")
                btn_confirm.tooltip = "Confirm directory selection"
                yield btn_confirm

    def _format_sources_display(self) -> str:
        if not self.selected_sources:
            return "(No source directories selected)"
        return "\n".join(f"• {d}" for d in self.selected_sources)

    def _update_sources_widget(self) -> None:
        try:
            st = self.query_one("#static-sources-list", Static)
            st.update(self._format_sources_display())
        except Exception:
            pass

    def on_mount(self) -> None:
        """Focus input field on mount and emit screen reader announcement."""
        super().on_mount()
        try:
            self.query_one("#input-dir", Input).focus()
        except Exception:
            return
        self.announce("Opened target directory selection dialog.")

    @on(Button.Pressed, "#btn-add-dir")
    def action_add_dir(self) -> None:
        """Add input directory path to selected source list."""
        val = self.query_one("#input-dir", Input).value.strip()
        if not val:
            return
        err = self._validate_directory_path(val)
        if err:
            self._show_error(err)
            return

        abs_p = os.path.abspath(os.path.expanduser(val))
        if abs_p not in self.selected_sources:
            self.selected_sources.append(abs_p)
            self.announce(f"Added directory path: {abs_p}")
            self._update_sources_widget()
        self.added_via_btn = True
        try:
            self.query_one("#lbl-error", Static).update("")
        except Exception:
            pass

    @on(Button.Pressed, "#btn-clear-sources")
    def action_clear_sources(self) -> None:
        """Clear staged source directories and reset multi-source add state."""
        self.selected_sources.clear()
        self.added_via_btn = False
        self._update_sources_widget()
        try:
            self.query_one("#lbl-error", Static).update("")
        except Exception:
            pass
        self.announce("Cleared selected source directories.")

    @on(Button.Pressed, "#preset-demo")
    def action_demo(self) -> None:
        """Select sandbox demo workspace preset."""
        from app.core.sample_corpus import generate_sample_corpus
        from app.ui.notifications import notify

        path = os.path.abspath("sandbox/demo_workspace")
        if not os.path.exists(path):
            try:
                os.makedirs(path, exist_ok=True)
                created_msg = f"Created demo workspace directory: {path}"
                self.announce(created_msg)
                notify(created_msg, type="info")
            except Exception as exc:
                err_msg = f"Failed to create directory '{path}': {exc}"
                self.announce(err_msg, priority="assertive")
                notify(err_msg, type="error")
                return

        try:
            generate_sample_corpus(path, overwrite=True)
            sample_msg = f"Generated sample corpus documents in '{path}'."
            self.announce(sample_msg)
            notify(sample_msg, type="info")
        except Exception as exc:
            logger.error(f"Failed to generate sample corpus in '{path}': {exc}")
            err_msg = f"Failed to generate sample corpus: {exc}"
            self.announce(err_msg, priority="assertive")
            notify(err_msg, type="error")

        self.added_via_btn = False
        self.selected_sources = [path]
        self._update_sources_widget()
        self.announce(f"Selected demo workspace preset: {path}")
        self.dismiss(path)

    @on(Button.Pressed, "#preset-downloads")
    def action_downloads(self) -> None:
        """Select user Downloads directory preset."""
        path = os.path.abspath(os.path.expanduser("~/Downloads"))
        err = self._validate_directory_path(path)
        if err:
            self._show_error(err)
            return
        self.added_via_btn = False
        self.selected_sources = [path]
        self._update_sources_widget()
        self.announce(f"Selected Downloads preset: {path}")
        self.dismiss(path)

    @on(Button.Pressed, "#preset-documents")
    def action_documents(self) -> None:
        """Select user Documents directory preset."""
        path = os.path.abspath(os.path.expanduser("~/Documents"))
        err = self._validate_directory_path(path)
        if err:
            self._show_error(err)
            return
        self.added_via_btn = False
        self.selected_sources = [path]
        self._update_sources_widget()
        self.announce(f"Selected Documents preset: {path}")
        self.dismiss(path)

    @on(Button.Pressed, "#btn-confirm")
    def action_confirm(self) -> None:
        """Confirm directory selection."""
        val = self.query_one("#input-dir", Input).value.strip()
        if val:
            err = self._validate_directory_path(val)
            if err:
                self._show_error(err)
                return

            abs_p = os.path.abspath(os.path.expanduser(val))
            if not self.added_via_btn:
                self.selected_sources = [abs_p]
                self._update_sources_widget()
                self.announce(f"Confirmed directory selection: '{abs_p}'")
                self.dismiss(abs_p)
                return

            if abs_p not in self.selected_sources:
                self.selected_sources.append(abs_p)

        if self.selected_sources:
            for source in self.selected_sources:
                err = self._validate_directory_path(source)
                if err:
                    self._show_error(err)
                    return

            self.announce(f"Confirmed directory selection: '{self.selected_sources}'")
            res = (
                self.selected_sources
                if len(self.selected_sources) > 1
                else self.selected_sources[0]
            )
            self.dismiss(res)
        else:
            self.announce("Cancelled directory selection.")
            self.dismiss(None)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel directory selection."""
        self.announce("Cancelled directory selection.")
        self.dismiss(None)

    @on(Button.Pressed, "#btn-browse")
    @work(thread=True)
    def action_native_browse(self) -> None:
        """Launch native OS directory browser dialog in background worker thread."""
        from app.core.native_dialog import NativeDialogError, select_native_directory

        curr_val = ""
        try:
            inp = self.query_one("#input-dir", Input)
            curr_val = inp.value.strip()
        except Exception:
            pass

        initial_dir = curr_val or self.current_dir

        try:
            chosen = select_native_directory(initial_dir)
            if chosen:
                self.app.call_from_thread(self._on_native_dir_selected, chosen)
        except NativeDialogError as exc:
            self.app.call_from_thread(self._on_native_dir_failed, str(exc))
        except Exception as exc:
            self.app.call_from_thread(self._on_native_dir_failed, str(exc))

    def _on_native_dir_selected(self, path: str) -> None:
        """Handle decoded UTF-8 path string from native folder browser."""
        abs_p = os.path.abspath(os.path.expanduser(path))
        try:
            inp = self.query_one("#input-dir", Input)
            inp.value = abs_p
        except Exception:
            pass
        if not self.added_via_btn:
            self.selected_sources = [abs_p]
            self._update_sources_widget()
        elif abs_p not in self.selected_sources:
            self.selected_sources.append(abs_p)
            self._update_sources_widget()
        self.announce(f"Selected native folder path: {abs_p}")

    def _on_native_dir_failed(self, reason: str) -> None:
        """Handle native folder dialog failure by falling back to manual path entry."""
        from app.ui.notifications import notify

        msg = "Native folder browser failed to launch. Please enter path manually."
        self.announce(msg, priority="assertive")
        notify(msg, type="warning")
        try:
            inp = self.query_one("#input-dir", Input)
            inp.focus()
        except Exception:
            pass


class ManualPathFallbackModal(ResponsiveModalScreen[str | None]):
    """Fallback modal dialog for manual directory path entry when native selection fails."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .modal-box {
        border: thick $warning;
        max-width: 70;
    }
    .button-row {
        min-height: 3;
    }
    .button-row Button {
        margin-left: 1;
    }
    """

    def __init__(self, current_dir: str = "", reason: str = ""):
        super().__init__()
        self.current_dir = current_dir
        self.reason = reason

    def compose(self) -> ComposeResult:
        """Compose modal fallback UI children."""
        with ModalBox(classes="modal-box"):
            with Vertical(classes="modal-body"):
                yield Heading("Manual Directory Entry", level=1, classes="modal-title")
                if self.reason:
                    yield Label(f"Native tool notice: {self.reason}", id="lbl-reason")
                yield Label("Please enter target directory path manually:")
                inp = Input(
                    value=self.current_dir,
                    placeholder="Enter absolute folder path...",
                    id="input-fallback-dir",
                )
                yield inp
            with Horizontal(classes="button-row"):
                btn_cancel = Button(
                    "Cancel", id="btn-cancel-fallback", variant="default"
                )
                yield btn_cancel
                btn_confirm = Button(
                    "Confirm", id="btn-confirm-fallback", variant="primary"
                )
                yield btn_confirm

    def on_mount(self) -> None:
        """Focus input field on mount and announce screen reader alert."""
        super().on_mount()
        self.query_one("#input-fallback-dir", Input).focus()
        self.announce("Opened manual directory entry fallback dialog.")

    @on(Button.Pressed, "#btn-confirm-fallback")
    @on(Input.Submitted, "#input-fallback-dir")
    def action_confirm(self) -> None:
        """Confirm manual path entry."""
        val = self.query_one("#input-fallback-dir", Input).value.strip()
        if val:
            abs_p = os.path.abspath(os.path.expanduser(val))
            self.announce(f"Confirmed manual directory entry: '{abs_p}'")
            self.dismiss(abs_p)
        else:
            self.announce("Cancelled manual directory entry.")
            self.dismiss(None)

    @on(Button.Pressed, "#btn-cancel-fallback")
    def action_cancel(self) -> None:
        """Cancel manual path entry."""
        self.announce("Cancelled manual directory entry.")
        self.dismiss(None)


def _format_file_size(size_bytes: int) -> str:
    """Format size in bytes to human readable string."""
    if size_bytes <= 0:
        return "0 B"
    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


class _BaseGroupSelectionModal(ResponsiveModalScreen[dict[str, str] | None]):
    """Shared base modal screen for per-group collision and duplicate review dialogs."""

    NARROW_WIDTH_BREAKPOINT = 60

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .modal-box {
        max-width: 90;
        min-width: 40;
    }
    .modal-subtitle {
        color: $text-muted;
        margin-bottom: 1;
    }
    .batch-bar {
        margin-top: 1;
        margin-bottom: 1;
        height: 3;
        align: left middle;
    }
    .batch-select {
        width: 1fr;
        margin-right: 1;
    }
    #btn-apply-all {
        min-width: 16;
    }
    .duplicate-card {
        background: $surface;
        border: solid $accent;
        padding: 1;
        margin-bottom: 1;
    }
    .card-title {
        text-style: bold;
        color: $accent;
    }
    .card-subtitle {
        color: $text-muted;
    }
    .source-item {
        color: $text;
        margin-left: 2;
    }
    .action-select {
        margin-top: 1;
        width: 100%;
    }
    #duplicate-groups-list {
        height: auto;
    }
    .button-row {
        min-height: 3;
    }
    .button-row Button {
        margin-left: 1;
    }
    Button:focus, Select:focus {
        border: heavy $accent;
        text-style: bold;
    }
    InteractiveCollisionResolverModal, DuplicateReviewModal {
        background: rgba(0, 0, 0, 0.65);
        &.compact .modal-subtitle {
            margin-bottom: 0;
        }
        &.compact .batch-bar {
            margin-top: 0;
            margin-bottom: 0;
            height: auto;
        }
        &.compact #btn-apply-all {
            height: 1;
            min-height: 1;
            border: none;
        }
        &.compact .duplicate-card {
            padding: 0 1;
            margin-bottom: 0;
        }
        &.compact .action-select {
            margin-top: 0;
        }
        &.compact .button-row {
            margin-top: 0;
            min-height: 1;
        }
        &.compact .button-row Button {
            height: 1;
            min-height: 1;
            border: none;
        }
        &.narrow .batch-bar {
            layout: vertical;
            height: auto;
        }
        &.narrow .batch-select {
            width: 100%;
            margin-right: 0;
        }
        &.narrow .button-row Button {
            min-width: 0;
            padding: 0 1;
        }
    }
    """

    DEFAULT_BATCH_ACTION: str = "rename"
    OPEN_ANNOUNCEMENT: str = ""
    APPLY_ALL_SCOPE_LABEL: str = "groups"
    CONFIRM_ANNOUNCEMENT: str = ""
    CANCEL_ANNOUNCEMENT: str = ""

    duplicate_groups: list[dict[str, Any]]
    user_selections: dict[str, str]

    def on_mount(self) -> None:
        """Focus initial widget on mount and announce to screen reader."""
        super().on_mount()
        try:
            self.query_one("#batch-action-select", Select).focus()
        except (LookupError, AttributeError):
            if self.duplicate_groups:
                first_gid = self.duplicate_groups[0]["group_id"]
                try:
                    self.query_one(f"#select-action-{first_gid}", Select).focus()
                except (LookupError, AttributeError):
                    with contextlib.suppress(LookupError, AttributeError):
                        self.query_one("#btn-confirm-duplicates", Button).focus()
            else:
                with contextlib.suppress(LookupError, AttributeError):
                    self.query_one("#btn-confirm-duplicates", Button).focus()
        if self.OPEN_ANNOUNCEMENT:
            self.announce(self.OPEN_ANNOUNCEMENT)

    @on(Button.Pressed, "#btn-apply-all")
    def action_apply_to_all(self) -> None:
        """Apply the selected batch action to all groups."""
        try:
            batch_sel = self.query_one("#batch-action-select", Select)
            batch_value = (
                str(batch_sel.value)
                if batch_sel.value is not None
                else self.DEFAULT_BATCH_ACTION
            )
        except (LookupError, AttributeError):
            batch_value = self.DEFAULT_BATCH_ACTION

        for group in self.duplicate_groups:
            gid = group["group_id"]
            self.user_selections[gid] = batch_value
            with contextlib.suppress(LookupError, AttributeError):
                sel_widget = self.query_one(f"#select-action-{gid}", Select)
                sel_widget.value = batch_value

        self.announce(
            f"Applied '{batch_value}' action to all {self.APPLY_ALL_SCOPE_LABEL}."
        )

    @on(Select.Changed)
    def on_select_changed(self, event: Select.Changed) -> None:
        """Record user selection updates for per-group Select controls."""
        if event.select.id and event.select.id.startswith("select-action-"):
            gid = event.select.id.replace("select-action-", "")
            if event.value is not None:
                self.user_selections[gid] = str(event.value)

    @on(Button.Pressed, "#btn-confirm-duplicates")
    def action_confirm(self) -> None:
        """Confirm selections and dismiss dialog."""
        if self.CONFIRM_ANNOUNCEMENT:
            self.announce(self.CONFIRM_ANNOUNCEMENT)
        self.dismiss(self.user_selections)

    @on(Button.Pressed, "#btn-cancel-duplicates")
    def action_cancel(self) -> None:
        """Cancel dialog and abort execution."""
        if self.CANCEL_ANNOUNCEMENT:
            self.announce(self.CANCEL_ANNOUNCEMENT)
        self.dismiss(None)


class InteractiveCollisionResolverModal(_BaseGroupSelectionModal):
    """Interactive per-file collision resolution modal before execution."""

    DEFAULT_BATCH_ACTION = "rename"
    OPEN_ANNOUNCEMENT = "Opened interactive collision resolver modal."
    APPLY_ALL_SCOPE_LABEL = "collision groups"
    CONFIRM_ANNOUNCEMENT = "Applied collision resolution selections."
    CANCEL_ANNOUNCEMENT = "Cancelled collision resolver dialog."

    RESOLVER_OPTIONS = [
        ("Rename (auto-number colliding files)", "rename"),
        ("Overwrite (replace target file)", "overwrite"),
        ("Skip (exclude colliding file)", "skip"),
        ("Keep Both (preserve destination & prefix source)", "keep_both"),
        ("Keep Primary File Only", "keep"),
    ]

    def __init__(
        self,
        duplicate_groups: list[dict[str, Any]] | None = None,
        collision_groups: list[dict[str, Any]] | None = None,
    ):
        super().__init__()
        groups = (
            collision_groups if collision_groups is not None else duplicate_groups
        ) or []
        self.collision_groups = groups
        self.duplicate_groups = groups
        valid_values = {opt[1] for opt in self.RESOLVER_OPTIONS}
        self.user_selections = {
            g["group_id"]: (
                str(g.get("default_action"))
                if g.get("default_action") in valid_values
                else "rename"
            )
            for g in self.collision_groups
        }

    def compose(self) -> ComposeResult:
        """Compose interactive collision resolution dialog widgets."""
        valid_values = {opt[1] for opt in self.RESOLVER_OPTIONS}
        with ModalBox(classes="modal-box"):
            with Vertical(classes="modal-body"):
                yield Heading(
                    "Interactive Collision Resolver",
                    level=1,
                    classes="modal-title",
                    id="modal-title",
                )
                yield Label(
                    "Distinct files share a proposed destination filename. Select per-target resolution actions:",
                    classes="modal-subtitle",
                )

                with Horizontal(classes="batch-bar", id="batch-action-bar"):
                    batch_sel = Select(
                        self.RESOLVER_OPTIONS,
                        value="rename",
                        id="batch-action-select",
                        classes="batch-select",
                        allow_blank=False,
                    )
                    batch_sel.tooltip = "Select batch collision action to apply to all colliding destination targets"
                    setattr(
                        batch_sel,
                        "aria_label",
                        "Batch resolution action for all destination collision groups",
                    )
                    yield batch_sel

                    btn_apply = Button("Apply to All", id="btn-apply-all")
                    btn_apply.tooltip = (
                        "Apply selected collision resolution action to all groups"
                    )
                    setattr(
                        btn_apply,
                        "aria_label",
                        "Apply selected collision resolution action to all groups",
                    )
                    yield btn_apply

                with Vertical(id="duplicate-groups-list"):
                    for group in self.collision_groups:
                        gid = group["group_id"]
                        tf_name = group.get("target_filename", "Collision")
                        t_dir = group.get("target_dir", "")
                        items = group.get("items", [])
                        raw_default = group.get("default_action", "rename")
                        default_action = (
                            str(raw_default)
                            if raw_default in valid_values
                            else "rename"
                        )

                        with Vertical(classes="duplicate-card", id=f"card-{gid}"):
                            yield Label(
                                f"Destination Target: {tf_name} ({t_dir})",
                                classes="card-title",
                            )
                            yield Label(
                                f"Colliding distinct source files ({len(items)}):",
                                classes="card-subtitle",
                            )
                            for item in items:
                                rel_src = item.get("relative_source") or item.get(
                                    "source_path", ""
                                )
                                src_p = item.get("source_path", "")
                                size_info = ""
                                if src_p and os.path.exists(src_p):
                                    try:
                                        sz = os.path.getsize(src_p)
                                        size_info = f" ({_format_file_size(sz)})"
                                    except OSError:
                                        pass
                                yield Label(
                                    f"• {rel_src}{size_info}", classes="source-item"
                                )

                            sel = Select(
                                self.RESOLVER_OPTIONS,
                                value=default_action,
                                id=f"select-action-{gid}",
                                classes="action-select",
                                allow_blank=False,
                            )
                            sel.tooltip = (
                                f"Select collision resolution action for {tf_name}"
                            )
                            setattr(
                                sel,
                                "aria_label",
                                f"Collision resolution action for {tf_name}",
                            )
                            yield sel

            with Horizontal(classes="button-row"):
                btn_confirm = Button(
                    "Apply & Execute Moves",
                    variant="primary",
                    id="btn-confirm-duplicates",
                )
                btn_confirm.tooltip = (
                    "Confirm collision resolution selections and continue execution"
                )
                setattr(
                    btn_confirm,
                    "aria_label",
                    "Confirm collision resolution selections and continue execution",
                )
                yield btn_confirm

                btn_cancel = Button("Cancel", id="btn-cancel-duplicates")
                btn_cancel.tooltip = "Cancel collision resolution and abort execution"
                setattr(
                    btn_cancel,
                    "aria_label",
                    "Cancel collision resolution and abort execution",
                )
                yield btn_cancel


class DuplicateReviewModal(_BaseGroupSelectionModal):
    """Dedicated review modal for exact byte-verified duplicate file clusters."""

    DEFAULT_BATCH_ACTION = "stage"
    OPEN_ANNOUNCEMENT = "Opened byte-verified duplicate review modal."
    APPLY_ALL_SCOPE_LABEL = "duplicate clusters"
    CONFIRM_ANNOUNCEMENT = "Applied duplicate cleanup selections."
    CANCEL_ANNOUNCEMENT = "Cancelled duplicate review dialog."

    DUPLICATE_OPTIONS = [
        ("Stage Duplicates for Undo (.sortify_duplicates)", "stage"),
        ("Move Duplicates to OS Trash (via staging)", "trash"),
        ("Keep Primary File Only (skip copies)", "keep"),
        ("Rename Copies (keep all with _1 suffix)", "rename"),
        ("Skip Cluster (leave all untouched)", "skip"),
    ]

    def __init__(
        self,
        duplicate_groups: list[dict[str, Any]] | None = None,
    ):
        super().__init__()
        self.duplicate_groups = duplicate_groups or []
        valid_values = {opt[1] for opt in self.DUPLICATE_OPTIONS}
        self.user_selections = {
            g["group_id"]: (
                str(g.get("default_action"))
                if g.get("default_action") in valid_values
                else "stage"
            )
            for g in self.duplicate_groups
        }

    def compose(self) -> ComposeResult:
        """Compose byte-verified duplicate cluster review dialog widgets."""
        valid_values = {opt[1] for opt in self.DUPLICATE_OPTIONS}
        initial_batch = "stage"
        if self.duplicate_groups:
            first_def = self.duplicate_groups[0].get("default_action")
            if first_def in valid_values:
                initial_batch = str(first_def)

        with ModalBox(classes="modal-box"):
            with Vertical(classes="modal-body"):
                yield Heading(
                    "Byte-Verified Duplicate Review",
                    level=1,
                    classes="modal-title",
                    id="modal-title",
                )
                yield Label(
                    "Exact byte-identical copies detected. Redundant copies move only after the surviving original is verified:",
                    classes="modal-subtitle",
                )

                with Horizontal(classes="batch-bar", id="batch-action-bar"):
                    batch_sel = Select(
                        self.DUPLICATE_OPTIONS,
                        value=initial_batch,
                        id="batch-action-select",
                        classes="batch-select",
                        allow_blank=False,
                    )
                    batch_sel.tooltip = "Select batch cleanup action to apply to all byte-identical duplicate clusters"
                    setattr(
                        batch_sel,
                        "aria_label",
                        "Batch cleanup action for all byte-identical duplicate clusters",
                    )
                    yield batch_sel

                    btn_apply = Button("Apply to All", id="btn-apply-all")
                    btn_apply.tooltip = (
                        "Apply selected duplicate cleanup action to all clusters"
                    )
                    setattr(
                        btn_apply,
                        "aria_label",
                        "Apply selected duplicate cleanup action to all clusters",
                    )
                    yield btn_apply

                with Vertical(id="duplicate-groups-list"):
                    for group in self.duplicate_groups:
                        gid = group["group_id"]
                        tf_name = group.get("target_filename", "Duplicate")
                        items = group.get("items", [])
                        raw_default = group.get("default_action", "stage")
                        default_action = (
                            str(raw_default) if raw_default in valid_values else "stage"
                        )
                        byte_size = group.get("byte_size")
                        sha_val = str(group.get("sha256") or "")
                        sel_mode = group.get("detect_original_by") or "first_seen"
                        meta_parts: list[str] = []
                        if isinstance(byte_size, int) and byte_size >= 0:
                            meta_parts.append(_format_file_size(byte_size))
                        if sha_val:
                            meta_parts.append(f"sha256:{sha_val[:12]}")
                        meta_suffix = (
                            f" ({', '.join(meta_parts)})" if meta_parts else ""
                        )

                        with Vertical(classes="duplicate-card", id=f"card-{gid}"):
                            yield Label(
                                f"Duplicate Cluster: {tf_name}{meta_suffix}",
                                classes="card-title",
                            )
                            yield Label(
                                f"Verified identical copies ({len(items)}) — original selector: {sel_mode}",
                                classes="card-subtitle",
                            )
                            if group.get("existing_file_on_disk") and group.get(
                                "primary_source_path"
                            ):
                                yield Label(
                                    f"• [Original on Disk] {group['primary_source_path']}",
                                    classes="source-item",
                                )
                            for idx, item in enumerate(items):
                                rel_src = item.get("relative_source") or item.get(
                                    "source_path", ""
                                )
                                src_p = item.get("source_path", "")
                                role_tag = (
                                    "[Original]"
                                    if (
                                        not group.get("existing_file_on_disk")
                                        and (item.get("is_primary") or idx == 0)
                                    )
                                    else "[Duplicate]"
                                )
                                size_info = ""
                                if isinstance(item.get("byte_size"), int):
                                    size_info = (
                                        f" ({_format_file_size(item['byte_size'])})"
                                    )
                                elif src_p and os.path.exists(src_p):
                                    try:
                                        sz = os.path.getsize(src_p)
                                        size_info = f" ({_format_file_size(sz)})"
                                    except OSError:
                                        pass
                                yield Label(
                                    f"• {role_tag} {rel_src}{size_info}",
                                    classes="source-item",
                                )

                            sel = Select(
                                self.DUPLICATE_OPTIONS,
                                value=default_action,
                                id=f"select-action-{gid}",
                                classes="action-select",
                                allow_blank=False,
                            )
                            sel.tooltip = (
                                f"Select duplicate cleanup action for {tf_name}"
                            )
                            setattr(
                                sel,
                                "aria_label",
                                f"Duplicate cleanup action for cluster {tf_name}",
                            )
                            yield sel

            with Horizontal(classes="button-row"):
                btn_confirm = Button(
                    "Confirm Duplicate Cleanup",
                    variant="primary",
                    id="btn-confirm-duplicates",
                )
                btn_confirm.tooltip = (
                    "Confirm duplicate cleanup actions and continue execution"
                )
                setattr(
                    btn_confirm,
                    "aria_label",
                    "Confirm duplicate cleanup actions and continue execution",
                )
                yield btn_confirm

                btn_cancel = Button("Cancel", id="btn-cancel-duplicates")
                btn_cancel.tooltip = "Cancel duplicate review and abort execution"
                setattr(
                    btn_cancel,
                    "aria_label",
                    "Cancel duplicate review and abort execution",
                )
                yield btn_cancel


DirectoryModal = DirectorySelectModal
