"""Helper module for web-native directory selection using NiceGUI."""

import asyncio
import logging
import os
from typing import Callable, List, Optional

try:
    from nicegui import ui
except ImportError:
    from unittest.mock import MagicMock
    ui = MagicMock()

from app.ui.tokens import TOKENS

logger = logging.getLogger(__name__)

# Standardized responsive fluid layout helpers re-exported from tokens
STANDARD_DIALOG_CARD_MD = TOKENS.COMPONENTS.DIALOG_CARD_MD
STANDARD_DIALOG_CARD_LG = TOKENS.COMPONENTS.DIALOG_CARD_LG
STANDARD_DIALOG_CARD_XL = TOKENS.COMPONENTS.DIALOG_CARD_XL


def get_dialog_card_classes(size="md", extra=""):
    """Get standardized, fluid-width layout classes for dialog cards to ensure responsiveness."""
    classes = {
        "md": TOKENS.COMPONENTS.DIALOG_CARD_MD,
        "lg": TOKENS.COMPONENTS.DIALOG_CARD_LG,
        "xl": TOKENS.COMPONENTS.DIALOG_CARD_XL,
    }
    base = classes.get(size, TOKENS.COMPONENTS.DIALOG_CARD_MD)
    if extra:
        return f"{base} {extra}"
    return base


def scan_subdirectories(path: str) -> List[dict]:
    """Safely inspect directory structure non-blockingly via os.scandir.

    Returns a list of subdirectories. Ignores files and hidden directories starting with '.'.
    Handles PermissionError, FileNotFoundError, and OSError gracefully.
    """
    entries = []
    if not path or not os.path.exists(path) or not os.path.isdir(path):
        return entries

    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        entries.append(
                            {
                                "name": entry.name,
                                "path": entry.path,
                            }
                        )
                except (PermissionError, OSError):
                    continue
    except (PermissionError, FileNotFoundError, OSError) as e:
        logger.warning(f"Unable to scan directory '{path}': {e}")
        return []

    entries.sort(key=lambda x: x["name"].lower())
    return entries


async def scan_subdirectories_async(path: str) -> List[dict]:
    """Asynchronously scan subdirectories using threadpool execution to avoid blocking the event loop."""
    return await asyncio.to_thread(scan_subdirectories, path)


def find_tree_node(target_id: str, nodes: List[dict]) -> Optional[dict]:
    """Recursively search for a node by ID in the tree node structure."""
    for n in nodes:
        if n.get("id") == target_id:
            return n
        if n.get("children"):
            res = find_tree_node(target_id, n["children"])
            if res:
                return res
    return None


def ask_directory_async(
    parent=None,
    title="Select Directory",
    callback: Optional[Callable[[str], None]] = None,
    disable_ui_callback: Optional[Callable[[], None]] = None,
    enable_ui_callback: Optional[Callable[[], None]] = None,
    initial_dir: Optional[str] = None,
):
    """Launch a client-rendered, web-native NiceGUI directory tree browser modal dialog.

    Eliminates server-side OS desktop subprocess calls (osascript, powershell, zenity, kdialog).
    Supports both callback and await/async return patterns.
    """
    if disable_ui_callback:
        try:
            disable_ui_callback()
        except Exception as e:
            logger.warning(f"Error executing disable_ui_callback: {e}")

    # Resolve starting directory
    start_dir = ""
    if initial_dir and os.path.isdir(str(initial_dir)):
        start_dir = os.path.abspath(str(initial_dir))
    else:
        try:
            start_dir = os.path.abspath(os.path.expanduser("~"))
            if not os.path.exists(start_dir):
                start_dir = os.path.abspath(os.getcwd())
        except Exception:
            start_dir = os.path.abspath(os.getcwd())

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()

    fut = loop.create_future()

    def _finish(selected_path: str):
        if enable_ui_callback:
            try:
                enable_ui_callback()
            except Exception as e:
                logger.warning(f"Error executing enable_ui_callback: {e}")
        if callback:
            try:
                callback(selected_path)
            except Exception as e:
                logger.warning(f"Error executing directory callback: {e}")
        if not fut.done():
            fut.set_result(selected_path)

    # In mock or headless environments without active NiceGUI rendering context or browser WebSocket
    from unittest.mock import MagicMock

    is_interactive = False
    try:
        from nicegui import context

        is_interactive = getattr(context.client, "has_socket_connection", False)
    except Exception:
        is_interactive = False

    if isinstance(ui, MagicMock) or not is_interactive:
        logger.info(
            f"Headless execution without active browser WebSocket connection. Using directory: '{start_dir}'"
        )
        _finish(start_dir)
        return fut

    try:
        # Build NiceGUI modal dialog
        with ui.dialog() as dialog, ui.card().classes(get_dialog_card_classes("lg")):
            dialog.props("persistent")

            # Title & Header
            with ui.row().classes("w-full items-center justify-between border-b pb-2 mb-2"):
                ui.label(title).classes("text-lg font-bold text-gray-800")
                ui.button(
                    icon="close", on_click=lambda: _close_dialog("")
                ).props("flat round dense aria-label='Close Directory Dialog'")

            # Path input & Navigation
            with ui.row().classes("w-full items-center gap-2 mb-2"):
                path_input = (
                    ui.input(
                        label="Directory Path",
                        value=start_dir,
                        placeholder="Enter or paste folder path...",
                    )
                    .classes("flex-grow")
                    .props('outlined dense aria-label="Directory Path Input"')
                )

                def _go_to_parent():
                    curr = path_input.value.strip()
                    if curr:
                        parent_dir = os.path.dirname(os.path.abspath(curr))
                        if os.path.isdir(parent_dir) and parent_dir != curr:
                            path_input.set_value(parent_dir)
                            _refresh_tree_for_path(parent_dir)

                ui.button(
                    icon="arrow_upward", on_click=_go_to_parent
                ).props("outline dense aria-label='Parent Folder Button'").tooltip("Parent Folder")

            # Warning / Status message label
            warning_label = ui.label("").classes(
                "text-xs text-red-500 font-semibold hidden mb-2"
            )

            # Tree component initialization
            loaded_paths = set()
            root_name = os.path.basename(start_dir) or start_dir
            nodes = [
                {
                    "id": start_dir,
                    "label": root_name,
                    "children": [
                        {"id": os.path.join(start_dir, "__placeholder__"), "label": "..."}
                    ],
                }
            ]

            async def _load_children_for_node(node_id: str):
                if node_id in loaded_paths:
                    return
                node = find_tree_node(node_id, nodes)
                if not node:
                    return
                subdirs = await scan_subdirectories_async(node_id)
                loaded_paths.add(node_id)
                new_children = []
                for sub in subdirs:
                    new_children.append(
                        {
                            "id": sub["path"],
                            "label": sub["name"],
                            "children": [
                                {
                                    "id": os.path.join(sub["path"], "__placeholder__"),
                                    "label": "...",
                                }
                            ],
                        }
                    )
                node["children"] = new_children
                try:
                    tree.update()
                except Exception:
                    pass

            async def _on_tree_select(e):
                selected_id = e.value
                if selected_id and not str(selected_id).endswith("__placeholder__"):
                    path_input.set_value(selected_id)
                    warning_label.set_visibility(False)

            async def _on_tree_expand(e):
                expanded_list = e.value if isinstance(e.value, list) else [e.value]
                for node_id in expanded_list:
                    if (
                        node_id
                        and not str(node_id).endswith("__placeholder__")
                        and node_id not in loaded_paths
                    ):
                        await _load_children_for_node(node_id)

            tree = ui.tree(
                nodes,
                node_key="id",
                label_key="label",
                on_select=_on_tree_select,
                on_expand=_on_tree_expand,
            ).classes("w-full max-h-80 min-h-32 overflow-y-auto border rounded p-2 bg-gray-50")

            def _refresh_tree_for_path(target_path: str):
                if not os.path.isdir(target_path):
                    return
                loaded_paths.clear()
                t_name = os.path.basename(target_path) or target_path
                nodes.clear()
                nodes.append(
                    {
                        "id": target_path,
                        "label": t_name,
                        "children": [
                            {
                                "id": os.path.join(target_path, "__placeholder__"),
                                "label": "...",
                            }
                        ],
                    }
                )
                try:
                    tree.update()
                except Exception:
                    pass
                if loop.is_running():
                    asyncio.create_task(_load_children_for_node(target_path))

            # Trigger async loading of root children
            if loop.is_running():
                asyncio.create_task(_load_children_for_node(start_dir))

            def _close_dialog(selected_path: str):
                try:
                    dialog.close()
                except Exception:
                    pass
                _finish(selected_path)

            def _on_confirm():
                val = path_input.value.strip()
                if not val:
                    warning_label.set_text("Please enter or select a directory path.")
                    warning_label.set_visibility(True)
                    try:
                        ui.notify("Please specify or select a directory path.", type="warning")
                    except Exception:
                        pass
                    return

                abs_val = os.path.abspath(val)
                if os.path.exists(abs_val) and os.path.isdir(abs_val):
                    warning_label.set_visibility(False)
                    _close_dialog(abs_val)
                else:
                    warning_label.set_text("Selected path does not exist or is not a directory.")
                    warning_label.set_visibility(True)
                    try:
                        ui.notify(f"Invalid directory path: '{val}'", type="warning")
                    except Exception:
                        pass

            # Footer action buttons
            with ui.row().classes("w-full justify-end gap-2 mt-4"):
                ui.button("Cancel", on_click=lambda: _close_dialog("")).props(
                    "flat color=primary aria-label='Cancel Directory Selection'"
                )
                ui.button("Select Directory", on_click=_on_confirm).props(
                    "color=primary aria-label='Confirm Directory Selection'"
                )

        dialog.open()
    except Exception as ex:
        logger.error(f"Error initializing web-native directory dialog: {ex}")
        _finish(start_dir)

    return fut
