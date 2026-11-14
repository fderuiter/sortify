"""Tree widget with vim-style key navigation."""

from textual import events
from textual.widgets import Tree


class VimTree(Tree):
    """Tree control with native vim motion navigation (h, j, k, l)."""

    def _character_shortcuts_enabled(self) -> bool:
        """Check whether single-character keyboard shortcuts are enabled."""
        settings = getattr(getattr(self, "app", None), "settings", None)
        if settings is None:
            return True
        return bool(getattr(settings, "ENABLE_CHARACTER_KEY_SHORTCUTS", True))

    def on_key(self, event: events.Key) -> None:
        """Handle key events for native vim tree navigation."""
        if not self.has_focus:
            return

        char_shortcuts_enabled = self._character_shortcuts_enabled()

        if event.key == "j":
            self.action_cursor_down()
            if getattr(self.app, "visual_select_active", False) and self.cursor_node:
                self.app.register_visual_selected_node(self.cursor_node)
            event.stop()
            event.prevent_default()
        elif event.key == "k":
            self.action_cursor_up()
            if getattr(self.app, "visual_select_active", False) and self.cursor_node:
                self.app.register_visual_selected_node(self.cursor_node)
            event.stop()
            event.prevent_default()
        elif event.key == "h":
            node = self.cursor_node
            if node:
                if node.is_expanded and node.children:
                    node.collapse()
                else:
                    self.action_cursor_parent()
            event.stop()
            event.prevent_default()
        elif event.key == "l":
            node = self.cursor_node
            if node:
                if not node.is_expanded and (node.children or node.allow_expand):
                    node.expand()
                elif node.is_expanded and node.children:
                    self.action_cursor_down()
            event.stop()
            event.prevent_default()
        elif event.key in ("space", " "):
            action = getattr(self.app, "action_toggle_selection", None)
            if char_shortcuts_enabled and callable(action):
                action()
            event.stop()
            event.prevent_default()
        elif event.key in ("x", "X"):
            action = getattr(self.app, "action_exclude_selected", None)
            if char_shortcuts_enabled and callable(action):
                action()
            event.stop()
            event.prevent_default()
        elif event.key in ("i", "I"):
            action = getattr(self.app, "action_include_selected", None)
            if char_shortcuts_enabled and callable(action):
                action()
            event.stop()
            event.prevent_default()
        elif event.key in ("r", "R"):
            action = getattr(self.app, "action_reassign_selected", None)
            if char_shortcuts_enabled and callable(action):
                action()
            event.stop()
            event.prevent_default()
        elif event.key in ("v", "V"):
            action = getattr(self.app, "action_visual_range_select", None)
            if char_shortcuts_enabled and callable(action):
                action()
            event.stop()
            event.prevent_default()
        elif event.key in ("a", "A"):
            action = getattr(self.app, "action_toggle_select_all", None)
            if char_shortcuts_enabled and callable(action):
                action()
            event.stop()
            event.prevent_default()
        elif event.key in ("g", "G", "shift+g"):
            action = getattr(self.app, "action_generate_sample_corpus", None)
            if char_shortcuts_enabled and callable(action):
                action(True)
            event.stop()
            event.prevent_default()
        elif event.key in ("plus", "+"):
            action = getattr(self.app, "action_rate_positive", None)
            if char_shortcuts_enabled and callable(action):
                action()
            event.stop()
            event.prevent_default()
        elif event.key in ("minus", "-"):
            action = getattr(self.app, "action_rate_negative", None)
            if char_shortcuts_enabled and callable(action):
                action()
            event.stop()
            event.prevent_default()
        elif event.key in ("question_mark", "?"):
            action = getattr(self.app, "action_open_cheat_sheet", None)
            if char_shortcuts_enabled and callable(action):
                action()
            event.stop()
            event.prevent_default()
        elif event.key in ("/", "slash") or getattr(event, "character", None) == "/":
            action = getattr(self.app, "action_focus_search", None)
            if char_shortcuts_enabled and callable(action):
                action()
            event.stop()
            event.prevent_default()
        elif event.key in ("enter", "return"):
            node = self.cursor_node
            if node:
                if node.children or node.allow_expand:
                    node.toggle()
                else:
                    self.select_node(node)
            event.stop()
            event.prevent_default()
