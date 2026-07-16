"""Setup wizard module for AI consent and downloading."""

import threading

import customtkinter as ctk
from huggingface_hub import snapshot_download

from app.config import get_app_dir


class SetupWizard(ctk.CTkToplevel):
    """Setup wizard window for downloading the AI model."""

    def __init__(self, parent, settings, on_complete):
        super().__init__(parent)
        self.settings = settings
        self.on_complete = on_complete
        self.title("Privacy & Data Setup")
        self.geometry("500x400")
        self.transient(parent)
        self.grab_set()

        # UI Elements
        self.title_label = ctk.CTkLabel(
            self, text="AI Features Setup", font=("Roboto", 20, "bold")
        )
        self.title_label.pack(pady=20)

        self.desc = ctk.CTkTextbox(self, width=450, height=150, wrap="word")
        self.desc.insert(
            "1.0",
            "To use the Smart AutoSorter AI features, the application needs to download a small AI model (all-MiniLM-L6-v2) from Hugging Face, a third-party service. This requires a one-time network request and will consume approximately 80MB of bandwidth.\n\nYour privacy is important to us. The model will be stored locally in your configuration directory and all future processing will happen entirely offline on your machine. We will not send your files or data to any external server.",
        )
        self.desc.configure(state="disabled")
        self.desc.pack(pady=10)

        self.progress = ctk.CTkProgressBar(self, width=400)
        self.progress.set(0)

        self.status = ctk.CTkLabel(self, text="")

        self.btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_frame.pack(pady=20)

        self.accept_btn = ctk.CTkButton(
            self.btn_frame,
            text="Accept & Download",
            command=self.accept,
            fg_color="green",
            hover_color="darkgreen",
        )
        self.accept_btn.pack(side="left", padx=10)

        self.decline_btn = ctk.CTkButton(
            self.btn_frame,
            text="Decline (Offline Mode)",
            command=self.decline,
            fg_color="gray",
            hover_color="darkgray",
        )
        self.decline_btn.pack(side="left", padx=10)

        self.help_btn = ctk.CTkButton(
            self.btn_frame,
            text="Help",
            command=self.open_help,
            fg_color="transparent",
            border_width=1,
            text_color=("black", "white"),
        )
        self.help_btn.pack(side="left", padx=10)

        self.protocol("WM_DELETE_WINDOW", self.decline)

    def open_help(self):
        """Open the local user guide in the default browser."""
        import os
        import webbrowser
        from pathlib import Path

        docs_path = (
            Path(os.path.abspath(__file__)).parent.parent.parent
            / "docs"
            / "user_guide.md"
        )
        webbrowser.open(docs_path.as_uri())

    def accept(self):
        """Handle accept action."""
        self.accept_btn.configure(state="disabled")
        self.decline_btn.configure(state="disabled")
        self.progress.pack(pady=10)
        self.progress.start()
        self.status.pack(pady=5)
        self.status.configure(text="Downloading model from Hugging Face...")

        threading.Thread(target=self.download_model, daemon=True).start()

    def download_model(self):
        """Download the model in a background thread."""
        try:
            import shutil
            model_dir = get_app_dir() / "model"
            if model_dir.exists():
                shutil.rmtree(model_dir)
            snapshot_download(
                repo_id="sentence-transformers/all-MiniLM-L6-v2",
                local_dir=str(model_dir),
            )
            self.settings.AI_CONSENT_GRANTED = True
            self.after(0, self.finish)
        except Exception as e:
            err_msg = str(e)
            self.after(
                0,
                lambda msg=err_msg: self.status.configure(
                    text=f"Download failed: {msg}", text_color="red"
                ),
            )
            self.after(0, self.progress.stop)
            self.after(0, lambda: self.accept_btn.configure(state="normal"))
            self.after(0, lambda: self.decline_btn.configure(state="normal"))

    def decline(self):
        """Handle decline action."""
        self.settings.AI_CONSENT_GRANTED = False
        self.finish()

    def finish(self):
        """Complete the wizard and close."""
        self.on_complete()
        self.destroy()
