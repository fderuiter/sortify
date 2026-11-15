"""Focused collisions dialogs, re-exported by edit for compatibility."""

import logging
import os
from typing import Any, Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Select

from app.ui.a11y import A11yMixin
from app.ui.heading import Heading
from app.ui.layout import ResponsiveLayoutMixin

logger = logging.getLogger(__name__)


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


class InteractiveCollisionResolverModal(
    ResponsiveLayoutMixin, A11yMixin, ModalScreen[Optional[dict[str, str]]]
):
    """Interactive per-file collision resolution modal before execution."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    InteractiveCollisionResolverModal, DuplicateReviewModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }
    .modal-box {
        padding: 1 2;
        background: $panel;
        border: thick $primary;
        width: 90%;
        max-width: 90;
        min-width: 40;
        height: auto;
        max-height: 90%;
        overflow-y: auto;
    }
    .narrow .modal-box {
        padding: 0 1;
        width: 95%;
    }
    .modal-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
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
    .narrow .batch-bar {
        height: auto;
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
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    Button:focus, Select:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    RESOLVER_OPTIONS = [
        ("Rename (auto-number duplicates)", "rename"),
        ("Overwrite (replace target file)", "overwrite"),
        ("Skip (exclude colliding file)", "skip"),
        ("Keep Both (preserve destination & prefix source)", "keep_both"),
        ("Keep Primary File Only", "keep"),
    ]

    def __init__(
        self,
        duplicate_groups: Optional[list[dict[str, Any]]] = None,
    ):
        super().__init__()
        self.duplicate_groups = duplicate_groups or []
        self.user_selections: dict[str, str] = {
            g["group_id"]: g.get("default_action", "rename")
            for g in self.duplicate_groups
        }

    def compose(self) -> ComposeResult:
        """Compose interactive collision resolution dialog widgets."""
        with Vertical(classes="modal-box"):
            yield Heading(
                "Interactive Collision Resolver",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            yield Label(
                "Review destination target collisions and select per-file resolution actions:",
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
                batch_sel.tooltip = (
                    "Select batch deduplication action to apply to all collision groups"
                )
                batch_sel.aria_label = (
                    "Batch resolution action for all duplicate clusters"
                )
                yield batch_sel

                btn_apply = Button("Apply to All", id="btn-apply-all")
                btn_apply.tooltip = (
                    "Apply selected resolution action to all duplicate clusters"
                )
                btn_apply.aria_label = (
                    "Apply selected resolution action to all duplicate clusters"
                )
                yield btn_apply

            with Vertical(id="duplicate-groups-list"):
                for group in self.duplicate_groups:
                    gid = group["group_id"]
                    tf_name = group.get("target_filename", "Collision")
                    t_dir = group.get("target_dir", "")
                    items = group.get("items", [])
                    default_action = group.get("default_action", "rename")

                    with Vertical(classes="duplicate-card", id=f"card-{gid}"):
                        yield Label(
                            f"Destination Target: {tf_name} ({t_dir})",
                            classes="card-title",
                        )
                        yield Label(
                            f"Colliding source files ({len(items)}):",
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
                        sel.aria_label = (
                            f"Resolution action for duplicate group {tf_name}"
                        )
                        yield sel

            with Horizontal(classes="button-row"):
                yield Button(
                    "Apply & Execute Moves",
                    variant="primary",
                    id="btn-confirm-duplicates",
                )
                yield Button("Cancel", id="btn-cancel-duplicates")

    def _update_layout(self, width: int) -> None:
        """Update responsiveness based on modal container width."""
        if width < 60:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_mount(self) -> None:
        """Focus initial widget on mount and announce to screen reader."""
        self._update_layout(self.size.width)
        try:
            self.query_one("#batch-action-select", Select).focus()
        except Exception:
            if self.duplicate_groups:
                first_gid = self.duplicate_groups[0]["group_id"]
                try:
                    self.query_one(f"#select-action-{first_gid}", Select).focus()
                except Exception:
                    try:
                        self.query_one("#btn-confirm-duplicates", Button).focus()
                    except Exception:
                        pass
            else:
                try:
                    self.query_one("#btn-confirm-duplicates", Button).focus()
                except Exception:
                    pass
        self.announce("Opened interactive collision resolver modal.")

    @on(Button.Pressed, "#btn-apply-all")
    def action_apply_to_all(self) -> None:
        """Apply the selected batch action to all collision groups."""
        try:
            batch_sel = self.query_one("#batch-action-select", Select)
            batch_value = (
                str(batch_sel.value) if batch_sel.value is not None else "rename"
            )
        except Exception:
            batch_value = "rename"

        for group in self.duplicate_groups:
            gid = group["group_id"]
            self.user_selections[gid] = batch_value
            try:
                sel_widget = self.query_one(f"#select-action-{gid}", Select)
                sel_widget.value = batch_value
            except Exception:
                pass

        self.announce(f"Applied '{batch_value}' action to all duplicate groups.")

    @on(Select.Changed)
    def on_select_changed(self, event: Select.Changed) -> None:
        """Record user selection updates for duplicate groups."""
        if event.select.id and event.select.id.startswith("select-action-"):
            gid = event.select.id.replace("select-action-", "")
            if event.value is not None:
                self.user_selections[gid] = str(event.value)

    @on(Button.Pressed, "#btn-confirm-duplicates")
    def action_confirm(self) -> None:
        """Confirm deduplication selections and dismiss dialog."""
        self.announce("Applied duplicate resolution selections.")
        self.dismiss(self.user_selections)

    @on(Button.Pressed, "#btn-cancel-duplicates")
    def action_cancel(self) -> None:
        """Cancel duplicate review and abort execution."""
        self.announce("Cancelled duplicate review dialog.")
        self.dismiss(None)
