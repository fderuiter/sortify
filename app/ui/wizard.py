import os
import threading
import tkinter as tk
from pathlib import Path
import customtkinter as ctk
from huggingface_hub import snapshot_download
from app.config import get_app_dir

class SetupWizard(ctk.CTkToplevel):
    def __init__(self, parent, settings, on_complete):
        super().__init__(parent)
        self.settings = settings
        self.on_complete = on_complete
        self.title("Privacy & Data Setup")
        self.geometry("500x400")
        self.transient(parent)
        self.grab_set()
        
        # UI Elements
        self.title_label = ctk.CTkLabel(self, text="AI Features Setup", font=("Roboto", 20, "bold"))
        self.title_label.pack(pady=20)
        
        self.desc = ctk.CTkTextbox(self, width=450, height=150, wrap="word")
        self.desc.insert("1.0", "To use the Smart AutoSorter AI features, the application needs to download a small AI model (all-MiniLM-L6-v2) from Hugging Face, a third-party service. This requires a one-time network request and will consume approximately 80MB of bandwidth.\n\nYour privacy is important to us. The model will be stored locally in your configuration directory and all future processing will happen entirely offline on your machine. We will not send your files or data to any external server.")
        self.desc.configure(state="disabled")
        self.desc.pack(pady=10)
        
        self.progress = ctk.CTkProgressBar(self, width=400)
        self.progress.set(0)
        
        self.status = ctk.CTkLabel(self, text="")
        
        self.btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_frame.pack(pady=20)
        
        self.accept_btn = ctk.CTkButton(self.btn_frame, text="Accept & Download", command=self.accept, fg_color="green", hover_color="darkgreen")
        self.accept_btn.pack(side="left", padx=10)
        
        self.decline_btn = ctk.CTkButton(self.btn_frame, text="Decline (Offline Mode)", command=self.decline, fg_color="gray", hover_color="darkgray")
        self.decline_btn.pack(side="left", padx=10)
        
        self.protocol("WM_DELETE_WINDOW", self.decline)

    def accept(self):
        self.accept_btn.configure(state="disabled")
        self.decline_btn.configure(state="disabled")
        self.progress.pack(pady=10)
        self.progress.start()
        self.status.pack(pady=5)
        self.status.configure(text="Downloading model from Hugging Face...")
        
        threading.Thread(target=self.download_model, daemon=True).start()

    def download_model(self):
        try:
            model_dir = get_app_dir() / "model"
            snapshot_download(repo_id="sentence-transformers/all-MiniLM-L6-v2", local_dir=str(model_dir))
            self.settings.AI_CONSENT_GRANTED = True
            self.after(0, self.finish)
        except Exception as e:
            self.after(0, lambda: self.status.configure(text=f"Download failed: {str(e)}", text_color="red"))
            self.after(0, self.progress.stop)
            self.after(0, lambda: self.accept_btn.configure(state="normal"))
            self.after(0, lambda: self.decline_btn.configure(state="normal"))

    def decline(self):
        self.settings.AI_CONSENT_GRANTED = False
        self.finish()
        
    def finish(self):
        self.on_complete()
        self.destroy()
