"""Tree widget with vim-style key navigation."""

from textual import events
from textual.widgets import Tree


class VimTree(Tree):
    """Tree control with native vim motion navigation (h, j, k, l)."""

    def on_key(self, event: events.Key) -> None:
        """Handle key events for native vim tree navigation."""
        if not self.has_focus:
            return

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
            if hasattr(self.app, "action_toggle_selection"):
                self.app.action_toggle_selection()
            event.stop()
            event.prevent_default()
        elif event.key in ("x", "X"):
            if hasattr(self.app, "action_exclude_selected"):
                self.app.action_exclude_selected()
            event.stop()
            event.prevent_default()
        elif event.key in ("i", "I"):
            if hasattr(self.app, "action_include_selected"):
                self.app.action_include_selected()
            event.stop()
            event.prevent_default()
        elif event.key in ("r", "R"):
            if hasattr(self.app, "action_reassign_selected"):
                self.app.action_reassign_selected()
            event.stop()
            event.prevent_default()
        elif event.key in ("v", "V"):
            if hasattr(self.app, "action_visual_range_select"):
                self.app.action_visual_range_select()
            event.stop()
            event.prevent_default()
        elif event.key in ("a", "A"):
            if hasattr(self.app, "action_toggle_select_all"):
                self.app.action_toggle_select_all()
            event.stop()
            event.prevent_default()
        elif event.key in ("g", "G", "shift+g"):
            if hasattr(self.app, "action_generate_sample_corpus"):
                self.app.action_generate_sample_corpus()
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
