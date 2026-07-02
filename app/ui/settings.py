"""Settings view and widgets for application configuration."""

import customtkinter as ctk


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

        self.token_widget = TokenWidget(
            self, self.settings.STOP_WORDS, on_change=on_settings_changed
        )
        self.token_widget.pack(fill="both", expand=True, padx=20, pady=(0, 20))
