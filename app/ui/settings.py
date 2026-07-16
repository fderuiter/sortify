"""Settings view and widgets for application configuration."""

from tkinter import filedialog

import customtkinter as ctk


class KeywordRoutingWidget(ctk.CTkFrame):
    """Widget to manage static keyword-to-directory routing rules."""

    def __init__(self, master, settings, on_change=None, **kwargs):
        super().__init__(master, **kwargs)
        self.settings = settings
        self.rules = getattr(self.settings, "KEYWORD_RULES", {}).copy()
        self.on_change = on_change
        
        title = ctk.CTkLabel(self, text="Static Keyword Routing Rules", font=("Roboto", 16, "bold"))
        title.pack(anchor="w", padx=10, pady=(10, 5))
        
        desc = ctk.CTkLabel(self, text="Map specific filename keywords to target directories.", text_color="gray")
        desc.pack(anchor="w", padx=10, pady=(0, 10))
        
        self.add_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.add_frame.pack(fill="x", padx=10, pady=(0, 10))
        
        self.kw_entry = ctk.CTkEntry(self.add_frame, placeholder_text="Keyword (e.g. invoice)")
        self.kw_entry.pack(side="left", padx=(0, 10), fill="x", expand=True)
        
        self.path_entry = ctk.CTkEntry(self.add_frame, placeholder_text="Target Directory")
        self.path_entry.pack(side="left", padx=(0, 10), fill="x", expand=True)
        
        self.browse_btn = ctk.CTkButton(self.add_frame, text="Browse", width=60, command=self._browse_dir)
        self.browse_btn.pack(side="left", padx=(0, 10))
        
        self.add_btn = ctk.CTkButton(self.add_frame, text="Add Rule", width=80, command=self._add_rule)
        self.add_btn.pack(side="left")
        
        self.error_label = ctk.CTkLabel(self, text="", text_color="red")
        self.error_label.pack(anchor="w", padx=10)
        
        self.scroll_frame = ctk.CTkScrollableFrame(self)
        self.scroll_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        
        self._render_rules()

    def _browse_dir(self):
        from app.ui.dialog_helper import ask_directory_async

        def disable_ui():
            self.browse_btn.configure(state="disabled")

        def enable_ui():
            self.browse_btn.configure(state="normal")

        def on_selected(directory):
            if directory:
                self.path_entry.delete(0, "end")
                self.path_entry.insert(0, directory)

        ask_directory_async(self, "Select Target Directory", on_selected, disable_ui, enable_ui)
            
    def _add_rule(self, event=None):
        kw = self.kw_entry.get().strip()
        path = self.path_entry.get().strip()
        
        if not kw or not path:
            self.error_label.configure(text="Keyword and Target Directory cannot be empty.")
            return
            
        kw_lower = kw.lower()
        existing_lower = {k.lower(): k for k in self.rules.keys()}
        if kw_lower in existing_lower:
            self.error_label.configure(text="A rule for this keyword already exists.")
            return
            
        self.error_label.configure(text="")
        self.rules[kw] = path
        
        self.kw_entry.delete(0, "end")
        self.path_entry.delete(0, "end")
        self._save_and_render()
        
    def _remove_rule(self, kw):
        if kw in self.rules:
            del self.rules[kw]
            self._save_and_render()
            
    def _save_and_render(self):
        self.settings.KEYWORD_RULES = self.rules.copy()
        if self.on_change:
            self.on_change()
        self._render_rules()
        
    def _render_rules(self):
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()
            
        for i, (kw, path) in enumerate(self.rules.items()):
            frame = ctk.CTkFrame(self.scroll_frame, fg_color=("gray75", "gray30"), corner_radius=5)
            frame.pack(fill="x", pady=2, padx=2)
            
            lbl_kw = ctk.CTkLabel(frame, text=kw, font=("Roboto", 12, "bold"), width=120, anchor="w")
            lbl_kw.pack(side="left", padx=10, pady=5)
            
            lbl_path = ctk.CTkLabel(frame, text=path, font=("Roboto", 12))
            lbl_path.pack(side="left", padx=10, pady=5, fill="x", expand=True)
            
            btn_del = ctk.CTkButton(
                frame, text="×", width=30, height=30, fg_color="transparent", 
                text_color=("black", "white"), hover_color="#c9302c", 
                command=lambda k=kw: self._remove_rule(k)
            )
            btn_del.pack(side="right", padx=5, pady=5)


class TokenWidget(ctk.CTkFrame):
    """A widget for managing and displaying unique tokenized text strings."""

    def __init__(self, master, tokens, on_change=None, **kwargs):
        super().__init__(master, **kwargs)
        self.tokens = set(tokens)
        self.on_change = on_change

        self.entry = ctk.CTkEntry(
            self,
            placeholder_text="Type word and press Enter to add to exclusion list...",
        )
        self.entry.pack(fill="x", padx=10, pady=10)
        self.entry.bind("<Return>", self._add_token)

        self.scroll_frame = ctk.CTkScrollableFrame(self)
        self.scroll_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self._render_tokens()

    def _add_token(self, event):
        word = self.entry.get().strip().lower()
        if word and word not in self.tokens:
            self.tokens.add(word)
            self.entry.delete(0, "end")
            self._render_tokens()
            self._notify_change()

    def _remove_token(self, word):
        if word in self.tokens:
            self.tokens.remove(word)
            self._render_tokens()
            self._notify_change()

    def _notify_change(self):
        if self.on_change:
            self.on_change(self.tokens)

    def _render_tokens(self):
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()

        col_count = 5
        for i in range(col_count):
            self.scroll_frame.grid_columnconfigure(i, weight=1)

        for i, word in enumerate(sorted(self.tokens)):
            row = i // col_count
            col = i % col_count

            frame = ctk.CTkFrame(
                self.scroll_frame, fg_color=("gray75", "gray30"), corner_radius=15
            )
            frame.grid(row=row, column=col, padx=5, pady=5, sticky="ew")

            lbl = ctk.CTkLabel(frame, text=word, font=("Roboto", 12))
            lbl.pack(side="left", padx=(10, 5), pady=2)

            btn = ctk.CTkButton(
                frame,
                text="×",
                width=20,
                height=20,
                fg_color="transparent",
                text_color=("black", "white"),
                hover_color="#c9302c",
                command=lambda w=word: self._remove_token(w),
            )
            btn.pack(side="right", padx=(0, 5), pady=2)


class SettingsView(ctk.CTkFrame):
    """The main settings view for user configuration."""

    def __init__(self, master, settings, on_back, on_settings_changed, **kwargs):
        super().__init__(master, **kwargs)
        self.settings = settings
        self.on_back = on_back

        # Header
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.pack(fill="x", pady=10, padx=20)

        back_btn = ctk.CTkButton(
            header_frame, text="← Back", width=60, command=self.on_back
        )
        back_btn.pack(side="left")

        title = ctk.CTkLabel(
            header_frame, text="Settings - Exclusion List", font=("Roboto", 20, "bold")
        )
        title.pack(side="left", padx=20)

        # Content
        desc = ctk.CTkLabel(
            self,
            text="Manage words ignored by the AI sorting engine (e.g. 'the', 'and', file extensions).",
            text_color="gray",
        )
        desc.pack(padx=20, pady=(0, 10), anchor="w")

        # Cleanup Section
        self.cleanup_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.cleanup_frame.pack(fill="x", padx=20, pady=(0, 10))

        cleanup_title = ctk.CTkLabel(
            self.cleanup_frame, text="File Operations", font=("Roboto", 16, "bold")
        )
        cleanup_title.pack(anchor="w", pady=(0, 5))

        self.cleanup_switch = ctk.CTkSwitch(
            self.cleanup_frame,
            text="Cleanup Empty Folders",
            command=self._on_cleanup_toggled,
        )
        self.cleanup_switch.pack(anchor="w")
        if getattr(self.settings, "CLEANUP_EMPTY_FOLDERS", True):
            self.cleanup_switch.select()
        else:
            self.cleanup_switch.deselect()

        # Privacy Section
        self.privacy_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.privacy_frame.pack(fill="x", padx=20, pady=(0, 10))

        privacy_title = ctk.CTkLabel(
            self.privacy_frame,
            text="AI Features & Privacy",
            font=("Roboto", 16, "bold"),
        )
        privacy_title.pack(anchor="w", pady=(0, 5))

        self.ai_status_label = ctk.CTkLabel(
            self.privacy_frame, text="Checking AI model status...", text_color="gray"
        )
        self.ai_status_label.pack(anchor="w")

        self.ai_btn_frame = ctk.CTkFrame(self.privacy_frame, fg_color="transparent")
        self.ai_btn_frame.pack(anchor="w", pady=(5, 10))

        self.ai_btn = ctk.CTkButton(
            self.ai_btn_frame, text="Download AI Model", command=self.download_ai_model
        )
        self.ai_btn.pack(side="left")

        self.help_btn = ctk.CTkButton(
            self.ai_btn_frame,
            text="Troubleshooting",
            command=self.open_troubleshooting,
            fg_color="transparent",
            border_width=1,
            text_color=("black", "white"),
        )
        self.help_btn.pack(side="left", padx=(10, 0))

        self.update_ai_status()

        self.kw_widget = KeywordRoutingWidget(
            self, self.settings, on_change=lambda: on_settings_changed(self.settings.STOP_WORDS) if on_settings_changed else None
        )
        self.kw_widget.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        self.token_widget = TokenWidget(
            self, self.settings.STOP_WORDS, on_change=on_settings_changed
        )
        self.token_widget.pack(fill="both", expand=True, padx=20, pady=(0, 20))

    def _on_cleanup_toggled(self):
        self.settings.CLEANUP_EMPTY_FOLDERS = bool(self.cleanup_switch.get())

    def update_ai_status(self):
        """Update the AI model status UI based on model presence."""
        from app.config import get_app_dir

        model_dir = get_app_dir() / "model"
        if (model_dir / "config.json").exists():
            self.ai_status_label.configure(
                text="AI Model: Downloaded & Ready", text_color="green"
            )
            self.ai_btn.configure(state="disabled", text="Model Installed")
        else:
            self.ai_status_label.configure(
                text="AI Model: Not Downloaded (Offline Mode Active)",
                text_color="orange",
            )
            self.ai_btn.configure(state="normal", text="Download AI Model")

    def open_troubleshooting(self):
        """Open the local troubleshooting guide."""
        import os
        import webbrowser
        from pathlib import Path

        docs_path = (
            Path(os.path.abspath(__file__)).parent.parent.parent
            / "docs"
            / "troubleshooting.md"
        )
        webbrowser.open(docs_path.as_uri())

    def download_ai_model(self):
        """Launch the setup wizard to download the AI model."""
        from app.ui.wizard import SetupWizard

        def on_complete():
            self.update_ai_status()

        SetupWizard(self, self.settings, on_complete)
