# ruff: noqa: D101,D102
"""Setup wizard module for AI consent and downloading."""

import math
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import customtkinter as ctk

from app.config import get_app_dir

FILES_TO_DOWNLOAD = [
    "config.json",
    "tokenizer_config.json",
    "tokenizer.json",
    "vocab.txt",
    "special_tokens_map.json",
    "sentence_bert_config.json",
    "modules.json",
    "data_config.json",
    "config_sentence_transformers.json",
    "README.md",
    "1_Pooling/config.json",
    "model.safetensors",
]

class Downloader:
    """A background downloader class for fetching models from Hugging Face."""

    def __init__(self, repo_id, files, dest_dir, on_progress, on_complete, on_error):
        self.repo_id = repo_id
        self.files = files
        self.dest_dir = Path(dest_dir)
        self.on_progress = on_progress
        self.on_complete = on_complete
        self.on_error = on_error
        
        self.lock = threading.Lock()
        self.is_paused = False
        self.is_cancelled = False
        self.current_response = None
        self.thread = None

    def start(self):
        """Start or resume the download in a background thread."""
        if self.thread and self.thread.is_alive():
            self.thread.join()
        with self.lock:
            self.is_paused = False
            self.is_cancelled = False
            self.thread = threading.Thread(target=self._download_loop, daemon=True)
            self.thread.start()

    def pause(self):
        """Pause the ongoing download by closing the active connection."""
        with self.lock:
            self.is_paused = True
            if self.current_response:
                try:
                    self.current_response.close()
                except Exception:
                    pass

    def resume(self):
        """Resume the paused download."""
        self.start()

    def cancel(self):
        """Cancel the ongoing download completely."""
        with self.lock:
            self.is_cancelled = True
            if self.current_response:
                try:
                    self.current_response.close()
                except Exception:
                    pass

    def _check_cancel_pause(self):
        with self.lock:
            return self.is_cancelled or self.is_paused

    def _download_loop(self):
        try:
            total_bytes = 0
            file_sizes = {}
            for file in self.files:
                if self._check_cancel_pause():
                    return
                
                url = f"https://huggingface.co/{self.repo_id}/resolve/main/{file}"
                req = urllib.request.Request(url, method="HEAD")
                try:
                    resp = urllib.request.urlopen(req, timeout=5)
                    size = int(resp.headers.get("Content-Length", 0))
                    file_sizes[file] = size
                    total_bytes += size
                except Exception:
                    file_sizes[file] = 0
            
            bytes_downloaded = 0
            for file in self.files:
                dest_path = self.dest_dir / file
                part_path = dest_path.with_suffix(dest_path.suffix + ".part")
                if dest_path.exists():
                    bytes_downloaded += dest_path.stat().st_size
                elif part_path.exists():
                    bytes_downloaded += part_path.stat().st_size
            
            start_time = time.time()
            last_update = start_time
            bytes_since_update = 0
            
            for file in self.files:
                if self._check_cancel_pause():
                    return
                
                dest_path = self.dest_dir / file
                if dest_path.exists():
                    continue
                    
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                part_path = dest_path.with_suffix(dest_path.suffix + ".part")
                
                downloaded_file_bytes = 0
                if part_path.exists():
                    downloaded_file_bytes = part_path.stat().st_size
                
                url = f"https://huggingface.co/{self.repo_id}/resolve/main/{file}"
                req = urllib.request.Request(url)
                if downloaded_file_bytes > 0:
                    req.add_header("Range", f"bytes={downloaded_file_bytes}-")
                
                try:
                    response = urllib.request.urlopen(req, timeout=10)
                except urllib.error.HTTPError as e:
                    if e.code == 416:
                        part_path.unlink()
                        bytes_downloaded -= downloaded_file_bytes
                        downloaded_file_bytes = 0
                        req = urllib.request.Request(url)
                        response = urllib.request.urlopen(req, timeout=10)
                    else:
                        raise
                        
                with self.lock:
                    if self.is_paused or self.is_cancelled:
                        response.close()
                        return
                    self.current_response = response
                
                mode = "ab" if response.status == 206 else "wb"
                if mode == "wb":
                    bytes_downloaded -= downloaded_file_bytes
                    downloaded_file_bytes = 0
                
                try:
                    with open(part_path, mode) as f:
                        while True:
                            with self.lock:
                                if self.is_paused or self.is_cancelled:
                                    break
                            
                            try:
                                chunk = response.read(65536)
                            except Exception:
                                break
                                
                            if not chunk:
                                break
                            
                            f.write(chunk)
                            bytes_downloaded += len(chunk)
                            bytes_since_update += len(chunk)
                            
                            now = time.time()
                            if now - last_update >= 1.0:
                                dt = now - last_update
                                speed = bytes_since_update / dt
                                eta = (total_bytes - bytes_downloaded) / speed if speed > 0 else 0
                                percent = (bytes_downloaded / total_bytes) if total_bytes > 0 else 0
                                
                                self.on_progress(percent, speed, eta)
                                
                                last_update = now
                                bytes_since_update = 0
                finally:
                    with self.lock:
                        self.current_response = None
                        response.close()
                
                with self.lock:
                    is_canc = self.is_cancelled
                    is_paus = self.is_paused
                    
                if is_canc:
                    if part_path.exists():
                        part_path.unlink()
                    return
                if is_paus:
                    return
                
                part_path.rename(dest_path)
            
            with self.lock:
                if not self.is_cancelled and not self.is_paused:
                    self.on_progress(1.0, 0, 0)
                    self.on_complete()

        except Exception as e:
            with self.lock:
                if not self.is_cancelled:
                    self.on_error(str(e))

class SetupWizard(ctk.CTkToplevel):
    """Setup wizard window for downloading the AI model."""

    def __init__(self, parent, settings, on_complete):
        super().__init__(parent)
        self.settings = settings
        self.on_complete_callback = on_complete
        self.title("Privacy & Data Setup")
        self.geometry("500x400")
        self.transient(parent)
        self.grab_set()
        self.downloader = None

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
        
        self.pause_btn = ctk.CTkButton(
            self.btn_frame,
            text="Pause",
            command=self.pause_download,
            fg_color="orange",
            hover_color="darkorange",
        )
        self.cancel_btn = ctk.CTkButton(
            self.btn_frame,
            text="Cancel",
            command=self.cancel_download,
            fg_color="red",
            hover_color="darkred",
        )
        self.resume_btn = ctk.CTkButton(
            self.btn_frame,
            text="Resume",
            command=self.resume_download,
            fg_color="green",
            hover_color="darkgreen",
        )

        self.protocol("WM_DELETE_WINDOW", self.on_closing)

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

    def format_size(self, size_bytes):
        """Format bytes into a human-readable size."""
        if size_bytes == 0:
            return "0B"
        size_name = ("B", "KB", "MB", "GB", "TB")
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {size_name[i]}"

    def update_progress(self, percent, speed, eta):
        """Update the UI progress indicator."""
        speed_str = self.format_size(speed) + "/s"
        eta_str = f"{int(eta)}s remaining"
        text = f"Downloading... {int(percent*100)}% ({speed_str} - {eta_str})"
        
        self.after(0, lambda: self.progress.set(percent))
        self.after(0, lambda: self.status.configure(text=text, text_color="white"))

    def download_complete(self):
        """Handle download completion."""
        self.settings.AI_CONSENT_GRANTED = True
        self.after(0, self.finish)

    def download_error(self, err_msg):
        """Handle download errors."""
        self.after(0, lambda msg=err_msg: self.status.configure(
            text=f"Download failed: {msg}", text_color="red"
        ))
        self.after(0, lambda: self.pause_btn.pack_forget())
        self.after(0, lambda: self.cancel_btn.pack_forget())
        self.after(0, lambda: self.resume_btn.pack_forget())
        self.after(0, lambda: self.accept_btn.pack(side="left", padx=10))
        self.after(0, lambda: self.decline_btn.pack(side="left", padx=10))

    def accept(self):
        """Handle accept action."""
        self.accept_btn.pack_forget()
        self.decline_btn.pack_forget()
        self.help_btn.pack_forget()
        
        self.pause_btn.pack(side="left", padx=10)
        self.cancel_btn.pack(side="left", padx=10)
        self.help_btn.pack(side="left", padx=10)
        
        self.progress.pack(pady=10)
        self.status.pack(pady=5)
        self.status.configure(text="Starting download...", text_color="white")

        import shutil
        model_dir = get_app_dir() / "model"
        
        if model_dir.exists():
            shutil.rmtree(model_dir)
            
        if not self.downloader:
            self.downloader = Downloader(
                repo_id="sentence-transformers/all-MiniLM-L6-v2",
                files=FILES_TO_DOWNLOAD,
                dest_dir=model_dir,
                on_progress=self.update_progress,
                on_complete=self.download_complete,
                on_error=self.download_error
            )
        self.downloader.start()
        
    def pause_download(self):
        """Pause the active download."""
        if self.downloader:
            self.downloader.pause()
            self.status.configure(text="Download paused. Network utilization is 0.", text_color="orange")
            self.pause_btn.pack_forget()
            self.resume_btn.pack(side="left", padx=10, before=self.cancel_btn)
            
    def resume_download(self):
        """Resume a paused download."""
        if self.downloader:
            self.resume_btn.pack_forget()
            self.pause_btn.pack(side="left", padx=10, before=self.cancel_btn)
            self.status.configure(text="Resuming download...", text_color="white")
            self.downloader.resume()

    def cancel_download(self):
        """Cancel the download and reset UI state."""
        if self.downloader:
            self.downloader.cancel()
            if self.downloader.thread and self.downloader.thread.is_alive():
                self.downloader.thread.join(timeout=3.0)
                
        self.pause_btn.pack_forget()
        self.resume_btn.pack_forget()
        self.cancel_btn.pack_forget()
        self.progress.pack_forget()
        self.progress.set(0)
        self.status.configure(text="Download cancelled.", text_color="red")
        
        self.help_btn.pack_forget()
        self.accept_btn.pack(side="left", padx=10)
        self.decline_btn.pack(side="left", padx=10)
        self.help_btn.pack(side="left", padx=10)
        self.downloader = None

    def on_closing(self):
        """Handle window close event."""
        if self.downloader:
            self.downloader.cancel()
            if self.downloader.thread and self.downloader.thread.is_alive():
                self.downloader.thread.join(timeout=3.0)
        self.decline()

    def decline(self):
        """Handle decline action."""
        self.settings.AI_CONSENT_GRANTED = False
        self.finish()

    def finish(self):
        """Complete the wizard and close."""
        self.on_complete_callback()
        self.destroy()
