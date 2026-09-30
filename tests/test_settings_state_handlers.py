"""Unit tests for settings state handlers, sliders, presets, policies, and UI component workflows in app/ui/settings.py."""

import re
from unittest.mock import MagicMock, patch

from app.config import AppSettings
from app.ui.settings import (
    ThreadSafeState,
    create_accessible_slider,
    get_shadowed_policies,
    render_validation_warning_banner,
    show_settings,
)


class MockUIElement:
    """Flexible mock for NiceGUI elements in unit tests."""

    def __init__(self, tag, *args, **kwargs):
        self.tag = tag
        self.args = args
        self.kwargs = kwargs
        self.value = kwargs.get("value")
        self.visible = True
        self.is_deleted = False
        self._props = {}
        self._raw_props = []
        self._classes = []
        self._text = args[0] if args and isinstance(args[0], str) else ""
        self.handlers = {}
        self.slots = {}
        self.timer = None

        if "on_click" in kwargs and kwargs["on_click"]:
            self.handlers["click"] = kwargs["on_click"]
        if "on_change" in kwargs and kwargs["on_change"]:
            self.handlers["change"] = kwargs["on_change"]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def classes(self, cls_str=""):
        self._classes.append(cls_str)
        return self

    def props(self, p_str=""):
        if p_str:
            self._raw_props.append(str(p_str))
            self._props["_last_props"] = str(p_str)
            matches = re.findall(r'([a-zA-Z0-9_-]+)(?:=(?:"([^"]*)"|\'([^\']*)\'|([^\s]+)))?', str(p_str))
            for k, val_double, val_single, val_unquoted in matches:
                val = val_double or val_single or val_unquoted or True
                self._props[k] = val
        return self

    def tooltip(self, tip=""):
        self._props["tooltip"] = tip
        return self

    def clear(self):
        pass

    def disable(self):
        self._props["disabled"] = True

    def enable(self):
        self._props["disabled"] = False

    def set_visibility(self, val):
        self.visible = bool(val)

    def set_value(self, val):
        self.value = val

    def set_text(self, txt):
        self._text = txt

    def on(self, event, handler):
        self.handlers[event] = handler
        return self

    def on_value_change(self, handler):
        self.handlers["value_change"] = handler
        return self

    def bind_text_from(self, *args, **kwargs):
        return self

    def open(self):
        pass

    def close(self):
        pass

    def cancel(self):
        pass

    def has_aria_label(self, label):
        aria_val = self._props.get("aria-label")
        if aria_val == label:
            return True
        for raw in self._raw_props:
            if f'aria-label="{label}"' in raw or f"aria-label='{label}'" in raw:
                return True
        return False


class MockUIHarness:
    """Interceptors for app.ui.settings.ui calls during show_settings."""

    def __init__(self):
        self.elements = []
        self.notifications = []
        self.timers = []

    def _create(self, tag, *args, **kwargs):
        el = MockUIElement(tag, *args, **kwargs)
        self.elements.append(el)
        return el

    def button(self, *args, **kwargs):
        return self._create("button", *args, **kwargs)

    def input(self, *args, **kwargs):
        return self._create("input", *args, **kwargs)

    def slider(self, *args, **kwargs):
        return self._create("slider", *args, **kwargs)

    def switch(self, *args, **kwargs):
        return self._create("switch", *args, **kwargs)

    def select(self, *args, **kwargs):
        return self._create("select", *args, **kwargs)

    def number(self, *args, **kwargs):
        return self._create("number", *args, **kwargs)

    def checkbox(self, *args, **kwargs):
        return self._create("checkbox", *args, **kwargs)

    def label(self, *args, **kwargs):
        return self._create("label", *args, **kwargs)

    def icon(self, *args, **kwargs):
        return self._create("icon", *args, **kwargs)

    def card(self, *args, **kwargs):
        return self._create("card", *args, **kwargs)

    def row(self, *args, **kwargs):
        return self._create("row", *args, **kwargs)

    def column(self, *args, **kwargs):
        return self._create("column", *args, **kwargs)

    def dialog(self, *args, **kwargs):
        return self._create("dialog", *args, **kwargs)

    def tabs(self, *args, **kwargs):
        return self._create("tabs", *args, **kwargs)

    def tab(self, *args, **kwargs):
        return self._create("tab", *args, **kwargs)

    def tab_panels(self, *args, **kwargs):
        return self._create("tab_panels", *args, **kwargs)

    def tab_panel(self, *args, **kwargs):
        return self._create("tab_panel", *args, **kwargs)

    def expansion(self, *args, **kwargs):
        return self._create("expansion", *args, **kwargs)

    def link(self, *args, **kwargs):
        return self._create("link", *args, **kwargs)

    def linear_progress(self, *args, **kwargs):
        return self._create("linear_progress", *args, **kwargs)

    def scroll_area(self, *args, **kwargs):
        return self._create("scroll_area", *args, **kwargs)

    def markdown(self, *args, **kwargs):
        return self._create("markdown", *args, **kwargs)

    def notify(self, message, type="info", **kwargs):
        self.notifications.append({"message": message, "type": type})

    def timer(self, interval, callback, **kwargs):
        t = MockUIElement("timer", interval, callback)
        t.callback = callback
        self.timers.append(t)
        return t

    def find_by_aria_label(self, label, tag=None):
        res = [e for e in self.elements if e.has_aria_label(label) and (tag is None or e.tag == tag)]
        return res[0] if res else None

    def find_all_by_aria_label(self, label, tag=None):
        return [e for e in self.elements if e.has_aria_label(label) and (tag is None or e.tag == tag)]

    def find_by_text(self, text, tag=None):
        res = [e for e in self.elements if text in e._text and (tag is None or e.tag == tag)]
        return res[0] if res else None


class EventObj:
    def __init__(self, sender, value):
        self.sender = sender
        self.value = value


# --- 1. ThreadSafeState Tests ---

def test_thread_safe_state():
    state = ThreadSafeState(a=1, b="test")
    assert state["a"] == 1
    assert state["b"] == "test"
    state["a"] = 100
    state["c"] = True
    assert state["a"] == 100
    assert state["c"] is True


# --- 2. Shadowed Policies Detection Tests ---

def test_shadowed_policies_detection():
    policies = [
        {"type": "keyword", "expression": "invoice", "priority": 30},
        {"type": "pattern", "expression": "inv", "priority": 20},
        {"type": "keyword", "expression": "invoice_2023", "priority": 10},
        {"type": "override", "expression": "exact_match", "priority": 25},
        {"type": "override", "expression": "exact_match_sub", "priority": 15},
        {"type": "keyword", "expression": "", "priority": 50},
    ]
    shadowed = get_shadowed_policies(policies)
    assert shadowed[0] is False
    assert shadowed[2] is True
    assert shadowed[4] is True


# --- 3. Slider Control & ARIA Attributes Tests ---

def test_accessible_slider_formatting_and_events():
    harness = MockUIHarness()

    with patch("app.ui.settings.ui", harness):
        # Value formatter callback
        s1 = create_accessible_slider(
            min=0, max=100, value=50, step=1,
            value_formatter=lambda v: f"Val:{v}"
        )
        assert s1._props["aria-valuetext"] == "Val:50"

        # Float step formatting
        s2 = create_accessible_slider(
            min=0.0, max=1.0, value=0.05, step=0.01
        )
        assert s2._props["aria-valuetext"] == "0.05"

        # Keydown event args variations
        s3 = create_accessible_slider(min=0, max=10, value=5, step=1)
        
        # Event as dict
        s3._handle_keydown({"key": "ArrowRight"})
        assert s3.value == 6

        # Event with args list of dict
        class EventWithArgsDict:
            args = [{"key": "ArrowLeft"}]
        s3._handle_keydown(EventWithArgsDict())
        assert s3.value == 5

        # Event with args string
        class EventWithArgsStr:
            args = "Home"
        s3._handle_keydown(EventWithArgsStr())
        assert s3.value == 0

        # Keydown clamping
        s3._handle_keydown("ArrowLeft")
        assert s3.value == 0
        s3._handle_keydown("End")
        assert s3.value == 10
        s3._handle_keydown("ArrowRight")
        assert s3.value == 10

        # Float step rounding
        s4 = create_accessible_slider(min=0.0, max=1.0, value=0.5, step=0.05)
        s4._handle_keydown("ArrowRight")
        assert s4.value == 0.55

        # PageUp / PageDown
        s3._handle_keydown("PageUp")
        s3._handle_keydown("PageDown")


# --- 4. Validation Warning Banner Tests ---

def test_validation_warning_banner():
    harness = MockUIHarness()
    settings = AppSettings()
    settings._has_validation_errors = True
    settings._validation_errors = [
        {"field": "PROTECTED_PATHS", "message": "invalid path traversal"},
        {"field": "KEYWORD_RULES", "message": "empty field required"},
        {"field": "MAX_WORKERS", "message": "out of range limits"},
    ]

    with patch("app.ui.settings.ui", harness):
        card = render_validation_warning_banner(settings)
        assert card.visible is True

        revalidate_btn = harness.find_by_text("Re-validate", tag="button")
        assert revalidate_btn is not None

        # Test re-validate returning True
        mock_reval = MagicMock(return_value=True)
        object.__setattr__(settings, "revalidate", mock_reval)
        revalidate_btn.handlers["click"]()
        assert any("re-validated successfully" in n["message"] for n in harness.notifications)

        # Test re-validate returning False
        mock_reval.return_value = False
        revalidate_btn.handlers["click"]()
        assert any("Validation errors remain" in n["message"] for n in harness.notifications)

        # Test offline guide modal - non-packaged and missing
        guide_btn = harness.find_by_text("View Guide (Offline)", tag="button")
        assert guide_btn is not None
        with patch("pathlib.Path.exists", return_value=False):
            guide_btn.handlers["click"]()

        # Test packaged path
        with patch("app.core.path_utils.is_packaged", return_value=True), patch("sys._MEIPASS", "/tmp/meipass", create=True):
            guide_btn.handlers["click"]()

        # Test deleted banner card refresh
        card.is_deleted = True
        timer = harness.timers[0]
        timer.callback()


# --- 5. Settings Show & General Tab Tests ---

def test_show_settings_general_tab(tmp_path):
    harness = MockUIHarness()
    parent_app = MagicMock()

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.PROTECTED_PATHS = ["/protected/dir1", "/protected/dir2"]
    settings.IGNORED_EXTENSIONS = [".tmp", ".bak"]

    with patch("app.ui.settings.ui", harness):
        show_settings(parent_app, settings)

        # Explorer integration
        switch_explorer = harness.find_by_aria_label("Explorer integration toggle", tag="switch")
        with patch("sys.platform", "linux"):
            switch_explorer.handlers["change"](EventObj(switch_explorer, True))
            assert any("only available on Windows" in n["message"] for n in harness.notifications)

        with patch("sys.platform", "win32"), patch("app.core.integration.register_context_menu") as mock_reg:
            switch_explorer.handlers["change"](EventObj(switch_explorer, True))
            mock_reg.assert_called_with(True)

            mock_reg.side_effect = RuntimeError("Reg error")
            switch_explorer.handlers["change"](EventObj(switch_explorer, True))

        # Cleanup empty directories
        switch_cleanup = harness.find_by_aria_label("Cleanup empty directories toggle", tag="switch")
        switch_cleanup.handlers["change"](EventObj(switch_cleanup, False))
        assert settings.CLEANUP_EMPTY_FOLDERS is False

        # Protected paths and ignored extensions delete buttons before re-rendering
        remove_btns = [e for e in harness.elements if e.tag == "button" and getattr(e, "_text", "") == "Remove"]
        assert len(remove_btns) >= 4
        # First 2 are protected paths, next 2 are ignored extensions
        remove_btns[0].handlers["click"]() # deletes /protected/dir1
        remove_btns[2].handlers["click"]() # deletes .tmp

        btn_add_prot = harness.find_by_aria_label("Add Protected Path Button", tag="button")
        input_prot = harness.find_by_aria_label("Add Protected Directory Path input", tag="input")
        input_prot.value = ""
        btn_add_prot.handlers["click"]()
        input_prot.value = "/protected/dir2"
        btn_add_prot.handlers["click"]()
        input_prot.value = "/protected/dir3"
        btn_add_prot.handlers["click"]()
        assert "/protected/dir3" in settings.PROTECTED_PATHS

        btn_clear_prot = harness.find_by_aria_label("Clear All Protected Paths Button", tag="button")
        btn_clear_prot.handlers["click"]()
        assert settings.PROTECTED_PATHS == []

        btn_add_ext = harness.find_by_aria_label("Add Ignored Extension Button", tag="button")
        input_ext = harness.find_by_aria_label("Add Ignored Extension input", tag="input")
        input_ext.value = "   "
        btn_add_ext.handlers["click"]()
        input_ext.value = "."
        btn_add_ext.handlers["click"]()
        input_ext.value = "log"
        btn_add_ext.handlers["click"]()
        assert ".log" in settings.IGNORED_EXTENSIONS

        # Max Depth & Folders change handlers
        num_depth = harness.find_by_aria_label("Max folder depth input", tag="number")
        num_folders = harness.find_by_aria_label("Max folders input", tag="number")

        num_depth.handlers["change"](EventObj(num_depth, 5))
        assert settings.MAX_DEPTH == 5

        num_folders.handlers["change"](EventObj(num_folders, 30))
        assert settings.MAX_FOLDERS == 30
        num_folders.handlers["change"](EventObj(num_folders, 9999))


# --- 6. Settings Sliders Tests ---

def test_show_settings_all_sliders(tmp_path):
    harness = MockUIHarness()
    parent_app = MagicMock()
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))

    with patch("app.ui.settings.ui", harness):
        show_settings(parent_app, settings)

        sliders = [e for e in harness.elements if e.tag == "slider"]
        assert len(sliders) == 9

        for s in sliders:
            aria_label = s.kwargs.get("aria_label") or s._props.get("aria-label") or ""
            handler = s.handlers.get("change")
            if handler:
                if "Worker Concurrency Limit" in aria_label and "Audio" not in aria_label:
                    handler(EventObj(s, 8))
                    assert settings.MAX_WORKERS == 8
                    handler(EventObj(s, 8))
                    handler(EventObj(s, -100))
                elif "Audio Worker Concurrency Limit" in aria_label:
                    handler(EventObj(s, 4))
                    assert settings.AUDIO_MAX_WORKERS == 4
                    handler(EventObj(s, 4))
                    handler(EventObj(s, -100))
                elif "Visual Layout Timeout" in aria_label:
                    handler(EventObj(s, 60))
                    assert settings.VISUAL_TIMEOUT == 60
                    handler(EventObj(s, 60))
                    handler(EventObj(s, -100))
                elif "Min Debounce Delay" in aria_label:
                    handler(EventObj(s, 1.5))
                    assert settings.DEBOUNCE_DELAY == 1.5
                    handler(EventObj(s, 15.0))
                    handler(EventObj(s, -100.0))
                elif "Max Debounce Delay" in aria_label:
                    handler(EventObj(s, 5.0))
                    assert settings.MAX_DEBOUNCE_DELAY == 5.0
                    handler(EventObj(s, 0.1))
                    handler(EventObj(s, -100.0))
                elif "ML Thread Count" in aria_label:
                    handler(EventObj(s, 4))
                    assert settings.MODEL_THREADS == 4
                    handler(EventObj(s, 4))
                    handler(EventObj(s, -100))
                elif "Image Max Dimension" in aria_label:
                    handler(EventObj(s, 2000))
                    assert settings.IMAGE_MAX_DIMENSION == 2000
                    handler(EventObj(s, 2000))
                    handler(EventObj(s, -100))
                elif "Image Skip Threshold" in aria_label:
                    handler(EventObj(s, 500))
                    assert settings.IMAGE_SKIP_THRESHOLD == 500
                    handler(EventObj(s, 500))
                    handler(EventObj(s, -100))
                elif "Coherence Threshold" in aria_label:
                    handler(EventObj(s, 0.75))
                    assert abs(settings.COHERENCE_THRESHOLD - 0.75) < 1e-4
                    handler(EventObj(s, 0.75))
                    handler(EventObj(s, -100.0))


# --- 7. Settings AI Tab Tests ---

def test_show_settings_ai_tab_full(tmp_path):
    harness = MockUIHarness()
    parent_app = MagicMock()
    parent_app.update_ai_warning = MagicMock()

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))

    with patch("app.ui.settings.ui", harness):
        with patch("app.core.verifier.check_ai_status", return_value=(False, "Models missing")):
            show_settings(parent_app, settings)

        btn_reset_cache = harness.find_by_aria_label("Reset Model Cache Button", tag="button")

        with patch("app.core.shared_registry.SharedModelRegistry.get_instance") as mock_reg, \
             patch("app.core.downloader.DownloadManager.get_instance") as mock_dm:
            dm_inst = MagicMock()
            mock_dm.return_value = dm_inst
            btn_reset_cache.handlers["click"]()

            on_done_cb = dm_inst.delete_model_async.call_args.kwargs["on_done"]
            on_done_cb(True, None)
            on_done_cb(False, "Error deleting model")

        sel_vision = harness.find_by_aria_label("Vision Extraction Engine Select", tag="select")
        sel_vision.handlers["change"](EventObj(sel_vision, "florence-2"))
        assert settings.VISION_ENGINE == "florence-2"
        sel_vision.handlers["change"](EventObj(sel_vision, "invalid_engine"))

        input_proxy = harness.find_by_aria_label("Proxy Input", tag="input")
        btn_save_proxy = harness.find_by_aria_label("Save Proxy Settings Button", tag="button")
        input_proxy.value = "http://proxy.internal:8080"
        btn_save_proxy.handlers["click"]()
        assert settings.PROXY == "http://proxy.internal:8080"

        btn_dl = harness.find_by_aria_label("Download AI Model Button", tag="button")
        with patch("app.core.downloader.DownloadManager.get_instance") as mock_dm:
            dm_inst = MagicMock()
            mock_dm.return_value = dm_inst
            btn_dl.handlers["click"]()
            dm_inst.start_download.side_effect = RuntimeError("DL Error")
            btn_dl.handlers["click"]()

        sync_timer = harness.timers[0]
        with patch("app.core.downloader.DownloadManager.get_instance") as mock_dm, \
             patch("app.core.verifier.check_ai_status", return_value=(True, "")):
            dm_inst = MagicMock()
            dm_inst.state = {
                "is_downloading": True,
                "progress": 0.5,
                "status_text": "Downloading...",
                "success": False,
                "error": None
            }
            mock_dm.return_value = dm_inst

            sync_timer.callback()

            dm_inst.state["is_downloading"] = False
            dm_inst.state["success"] = True
            sync_timer.callback()

            dm_inst.state["success"] = False
            dm_inst.state["error"] = "Network Error"
            sync_timer.callback()

        sw_ocr_gpu = harness.find_by_aria_label("OCR GPU acceleration toggle", tag="switch")
        sw_audio_gpu = harness.find_by_aria_label("Audio GPU acceleration toggle", tag="switch")

        sw_ocr_gpu.handlers["change"](EventObj(sw_ocr_gpu, True))
        assert settings.OCR_GPU_ENABLED is True

        sw_audio_gpu.handlers["change"](EventObj(sw_audio_gpu, True))
        assert settings.AUDIO_GPU_ENABLED is True

        input_ocr = harness.find_by_aria_label("OCR target languages input", tag="input")
        btn_ocr = harness.find_by_aria_label("Save OCR Languages Button", tag="button")

        input_ocr.value = "en,de"
        btn_ocr.handlers["click"]()
        assert settings.OCR_LANGUAGES == "en,de"

        input_ocr.value = None
        btn_ocr.handlers["click"]()


# --- 8. Settings Presets & Stopwords Tests ---

def test_show_settings_presets_and_stopwords(tmp_path):
    harness = MockUIHarness()
    parent_app = MagicMock()
    parent_app.app_session = MagicMock()
    parent_app.app_session.analyzer = MagicMock()
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.STOP_WORDS = {"alpha", "beta"}

    with patch("app.ui.settings.ui", harness):
        show_settings(parent_app, settings)

        btn_german = harness.find_by_text("German", tag="button")
        btn_french = harness.find_by_text("French", tag="button")
        btn_spanish = harness.find_by_text("Spanish", tag="button")

        btn_german.handlers["click"]()
        assert "und" in settings.STOP_WORDS

        btn_french.handlers["click"]()
        assert "les" in settings.STOP_WORDS

        btn_spanish.handlers["click"]()
        assert "por" in settings.STOP_WORDS

        btn_german.handlers["click"]()
        assert any("already in the list" in n["message"] for n in harness.notifications)

        close_btns = [e for e in harness.elements if e.tag == "button" and e.kwargs.get("icon") == "close"]
        if close_btns:
            close_btns[0].handlers["click"]()

        input_stop = harness.find_by_aria_label("Add Stop Word input", tag="input")
        btn_add_stop = harness.find_by_aria_label("Add Stop Word Button", tag="button")

        input_stop.value = "!!!"
        btn_add_stop.handlers["click"]()

        input_stop.value = "custom_one, custom_two!"
        btn_add_stop.handlers["click"]()
        assert "customone" in settings.STOP_WORDS or "custom" in settings.STOP_WORDS


# --- 9. Settings Keyword Rules Tests ---

def test_show_settings_keyword_rules(tmp_path):
    harness = MockUIHarness()
    parent_app = MagicMock()
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.KEYWORD_RULES = {"invoice": "Invoices", "receipt": "Receipts"}

    with patch("app.ui.settings.ui", harness):
        show_settings(parent_app, settings)

        del_rule_btns = [e for e in harness.elements if e.tag == "button" and getattr(e, "_text", "") == "Delete"]
        if del_rule_btns:
            del_rule_btns[0].handlers["click"]()

        input_kw = harness.find_by_aria_label("Keyword input", tag="input")
        input_target = harness.find_by_aria_label("Target Path input", tag="input")
        btn_add_rule = harness.find_by_aria_label("Add Rule Button", tag="button")

        input_kw.value = ""
        input_target.value = "Path"
        btn_add_rule.handlers["click"]()

        input_kw.value = "bill"
        input_target.value = "../invalid/traversal"
        btn_add_rule.handlers["click"]()
        assert any("Invalid target path" in n["message"] for n in harness.notifications)

        input_kw.value = "bill"
        input_target.value = "Finances/Bills"
        btn_add_rule.handlers["click"]()
        assert settings.KEYWORD_RULES["bill"] == "Finances/Bills"


# --- 10. Settings Learned Rules Tests ---

def test_show_settings_learned_rules(tmp_path):
    harness = MockUIHarness()
    parent_app = MagicMock()
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.LEARNED_RULES = {"k1": "Path1", "k2": "Path2"}

    with patch("app.ui.settings.ui", harness):
        show_settings(parent_app, settings)

        search_inp = harness.find_by_aria_label("Search learned rules", tag="input")
        if search_inp and "change" in search_inp.handlers:
            search_inp.handlers["change"](EventObj(search_inp, "k1"))

        kw_inp = harness.find_by_aria_label("Keyword pattern input for k1", tag="input")
        if kw_inp:
            kw_handler = kw_inp.handlers.get("change") or kw_inp.handlers.get("value_change")
            if kw_handler:
                kw_inp.value = "k1"
                kw_handler(EventObj(kw_inp, "k1"))
                kw_inp.value = ""
                kw_handler(EventObj(kw_inp, ""))
                kw_inp.value = "k1_new"
                kw_handler(EventObj(kw_inp, "k1_new"))

        path_inp = harness.find_by_aria_label("Destination path input for k2", tag="input")
        if path_inp:
            path_handler = path_inp.handlers.get("change") or path_inp.handlers.get("value_change")
            if path_handler:
                path_inp.value = "Path2"
                path_handler(EventObj(path_inp, "Path2"))
                path_inp.value = ""
                path_handler(EventObj(path_inp, ""))
                path_inp.value = "../invalid/path"
                path_handler(EventObj(path_inp, "../invalid/path"))
                path_inp.value = "Path2_New"
                path_handler(EventObj(path_inp, "Path2_New"))

        del_btn = harness.find_by_aria_label("Delete learned rule for k2", tag="button")
        if del_btn and "click" in del_btn.handlers:
            del_btn.handlers["click"]()


# --- 11. Settings Policies Tests ---

def test_show_settings_policies(tmp_path):
    harness = MockUIHarness()
    parent_app = MagicMock()
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.POLICIES = [
        {"type": "keyword", "expression": "p1", "target_path": "Path1", "priority": 20, "halting": False},
        {"type": "pattern", "expression": "p1_sub", "target_path": "Path1Sub", "priority": 10, "halting": False},
    ]

    with patch("app.ui.settings.ui", harness):
        show_settings(parent_app, settings)

        info_icons = [e for e in harness.elements if e.tag == "icon" and getattr(e, "args", [None])[0] == "help_outline"]
        if info_icons and "click" in info_icons[0].handlers:
            info_icons[0].handlers["click"]()

        halt_chk = harness.find_by_aria_label("Halt toggle checkbox", tag="checkbox")
        if halt_chk and "change" in halt_chk.handlers:
            halt_chk.handlers["change"](EventObj(halt_chk, True))

        up_btns = [e for e in harness.elements if e.tag == "button" and e.kwargs.get("icon") == "arrow_upward"]
        down_btns = [e for e in harness.elements if e.tag == "button" and e.kwargs.get("icon") == "arrow_downward"]
        if down_btns:
            down_btns[0].handlers["click"]()
        if up_btns:
            up_btns[-1].handlers["click"]()

        del_policy_btns = [e for e in harness.elements if e.tag == "button" and e.kwargs.get("icon") == "delete" and e.has_aria_label("Delete Policy Button")]
        if del_policy_btns:
            del_policy_btns[0].handlers["click"]()

        p_type = harness.find_by_aria_label("Type", tag="select") or harness.elements[0]
        p_expr = harness.find_by_aria_label("Policy Expression input", tag="input")
        p_target = harness.find_by_aria_label("Policy Target Path input", tag="input")
        p_priority = harness.find_by_aria_label("Priority", tag="number") or [e for e in harness.elements if e.tag == "number"][-1]
        btn_add_policy = harness.find_by_aria_label("Add Policy Button", tag="button")

        # Invalid type
        p_type.value = "invalid_type"
        btn_add_policy.handlers["click"]()
        p_type.value = "keyword"

        # Missing expression
        p_expr.value = ""
        p_target.value = "Path"
        btn_add_policy.handlers["click"]()

        # Missing target path
        p_expr.value = "expr1"
        p_target.value = ""
        btn_add_policy.handlers["click"]()

        # Missing priority
        p_expr.value = "expr1"
        p_target.value = "Path"
        p_priority.value = None
        btn_add_policy.handlers["click"]()

        # Non-integer priority
        p_priority.value = "not_a_number"
        btn_add_policy.handlers["click"]()
        p_priority.value = 10

        # Invalid target path
        p_expr.value = "test_expr"
        p_target.value = "../invalid/path"
        btn_add_policy.handlers["click"]()

        # Valid policy
        p_expr.value = "valid_expr"
        p_target.value = "Valid/Path"
        btn_add_policy.handlers["click"]()


# --- 12. Settings Dismiss Lifecycle Tests ---

def test_show_settings_dialog_dismiss(tmp_path):
    harness = MockUIHarness()
    parent_app = MagicMock()
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))

    with patch("app.ui.settings.ui", harness):
        show_settings(parent_app, settings)

        dialog = [e for e in harness.elements if e.tag == "dialog"][0]
        dismiss_handler = dialog.handlers.get("dismiss")
        assert dismiss_handler is not None
        dismiss_handler()
