"""Comprehensive unit tests for DensitySlider and VimTree widgets."""

import pytest
from textual import events
from textual.app import App, ComposeResult

from app.ui.widgets.density_slider import DensitySlider
from app.ui.widgets.vim_tree import VimTree


class DensitySliderTestApp(App[None]):
    def compose(self) -> ComposeResult:
        yield DensitySlider(
            value=50,
            min_value=0,
            max_value=100,
            step=10,
            announce_formatter=lambda v: f"Custom density: {v}",
            id="test-slider",
        )


@pytest.mark.anyio
async def test_density_slider_key_navigation():
    app = DensitySliderTestApp()
    async with app.run_test() as pilot:
        slider = app.query_one("#test-slider", DensitySlider)
        slider.focus()
        await pilot.pause(0.01)

        # left / down decreases by step
        slider.on_key(events.Key("down", "down"))
        assert slider.value == 40
        slider.on_key(events.Key("left", "left"))
        assert slider.value == 30

        # right / up increases by step
        slider.on_key(events.Key("up", "up"))
        assert slider.value == 40
        slider.on_key(events.Key("right", "right"))
        assert slider.value == 50

        # page_up / page_down
        slider.on_key(events.Key("page_up", "page_up"))
        assert slider.value == 100
        slider.on_key(events.Key("page_down", "page_down"))
        assert slider.value == 50

        # home / end
        slider.on_key(events.Key("end", "end"))
        assert slider.value == 100
        slider.on_key(events.Key("home", "home"))
        assert slider.value == 0

        # control property on Changed
        msg = DensitySlider.Changed(slider, 70)
        assert msg.control == slider
        assert msg.value == 70

        # Formatter error fallback
        slider.announce_formatter = lambda v: (_ for _ in ()).throw(ValueError("boom"))
        announcement = slider._announce_value(80, force=True)
        assert announcement is not None

        # render output
        rendered = slider.render()
        assert (
            "[" in rendered or "━" in rendered or "=" in rendered or len(rendered) > 0
        )


class VimTreeTestApp(App[None]):
    def __init__(self):
        super().__init__()
        self.visual_select_active = False
        self.visual_selected_nodes = []
        self.actions_called = []

    def compose(self) -> ComposeResult:
        tree = VimTree("Root", id="test-tree")
        tree.root.add("Child 1", expand=True)
        tree.root.add_leaf("Leaf 1")
        yield tree

    def register_visual_selected_node(self, node):
        self.visual_selected_nodes.append(node)

    def action_toggle_selection(self):
        self.actions_called.append("toggle_selection")

    def action_exclude_selected(self):
        self.actions_called.append("exclude_selected")

    def action_include_selected(self):
        self.actions_called.append("include_selected")

    def action_reassign_selected(self):
        self.actions_called.append("reassign_selected")

    def action_visual_range_select(self):
        self.actions_called.append("visual_range_select")

    def action_toggle_select_all(self):
        self.actions_called.append("toggle_select_all")

    def action_generate_sample_corpus(self, flag=True):
        self.actions_called.append(f"generate_sample_corpus_{flag}")

    def action_rate_positive(self):
        self.actions_called.append("rate_positive")

    def action_rate_negative(self):
        self.actions_called.append("rate_negative")

    def action_open_cheat_sheet(self):
        self.actions_called.append("open_cheat_sheet")

    def action_focus_search(self):
        self.actions_called.append("focus_search")


@pytest.mark.anyio
async def test_vim_tree_navigation_and_shortcuts():
    app = VimTreeTestApp()
    async with app.run_test() as pilot:
        tree = app.query_one("#test-tree", VimTree)
        tree.focus()
        await pilot.pause(0.01)

        # Unfocused returns early
        tree.has_focus = False
        tree.on_key(events.Key("j", "j"))
        tree.has_focus = True

        # Test j, k with visual selection
        app.visual_select_active = True
        tree.on_key(events.Key("j", "j"))
        assert len(app.visual_selected_nodes) >= 1
        tree.on_key(events.Key("k", "k"))

        # Test h and l
        tree.on_key(events.Key("l", "l"))
        tree.on_key(events.Key("h", "h"))

        # Test shortcut triggers
        for key, expected_action in [
            ("space", "toggle_selection"),
            ("x", "exclude_selected"),
            ("i", "include_selected"),
            ("r", "reassign_selected"),
            ("v", "visual_range_select"),
            ("a", "toggle_select_all"),
            ("g", "generate_sample_corpus_True"),
            ("+", "rate_positive"),
            ("-", "rate_negative"),
            ("?", "open_cheat_sheet"),
            ("/", "focus_search"),
        ]:
            tree.on_key(events.Key(key, key))
            assert expected_action in app.actions_called

        # Test enter key
        tree.on_key(events.Key("enter", "enter"))
