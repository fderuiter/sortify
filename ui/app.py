# ui/app.py
import os
import time
import threading
import customtkinter as ctk
from tkinter import filedialog
from config import MAX_FOLDERS
from core.extractor import build_corpus
from core.analyzer import generate_sorting_plan
from core.mover import execute_moves

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

class AutoSorterApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Smart AutoSorter AI Pro")
        self.geometry("750x600")
        
        self.base_dir = None
        self.plan = None
        
        # Benchmarking / Progress Metrics
        self.total_files = 0
        self.completed_files = 0
        self.start_time = 0
        
        # --- UI Build ---
        self.title_label = ctk.CTkLabel(self, text="AI File Organizer Pro", font=("Roboto", 24, "bold"))
        self.title_label.pack(pady=15)
        
        self.select_btn = ctk.CTkButton(self, text="Select Directory to Sort", command=self.select_directory)
        self.select_btn.pack(pady=10)
        
        self.status_label = ctk.CTkLabel(self, text="Waiting for directory...", text_color="gray", font=("Roboto", 13))
        self.status_label.pack(pady=5)
        
        # Progress Bar Layout
        self.progress_bar = ctk.CTkProgressBar(self, width=500)
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=10)
        
        self.meta_label = ctk.CTkLabel(self, text="", font=("Roboto", 12, "italic"), text_color="cyan")
        self.meta_label.pack(pady=2)
        
        # Display Preview Box
        self.textbox = ctk.CTkTextbox(self, width=650, height=250, state="disabled")
        self.textbox.pack(pady=10)
        
        self.execute_btn = ctk.CTkButton(self, text="Approve & Execute Sort", 
                                         command=self.execute_sort, 
                                         fg_color="green", hover_color="darkgreen", state="disabled")
        self.execute_btn.pack(pady=15)

    def select_directory(self):
        self.base_dir = filedialog.askdirectory(title="Select Directory")
        if self.base_dir:
            items_to_sort = [f for f in os.listdir(self.base_dir) if not f.startswith('.')]
            self.total_files = len(items_to_sort)
            
            if self.total_files == 0:
                self.status_label.configure(text="Selected directory is empty.", text_color="red")
                return
                
            self.completed_files = 0
            self.progress_bar.set(0)
            self.select_btn.configure(state="disabled")
            self.execute_btn.configure(state="disabled")
            
            self.status_label.configure(text="Initializing scanning threads...", text_color="white")
            self.start_time = time.time()
            
            # FIRE BACKGROUND THREAD: Keeps UI interactive and moving fluidly
            threading.Thread(target=self.pipeline_worker, args=(items_to_sort,), daemon=True).start()

    def item_completed_callback(self):
        """Thread-safe counter tracking execution velocity and calculating remaining time."""
        self.completed_files += 1
        progress_percentage = self.completed_files / self.total_files
        self.progress_bar.set(progress_percentage)
        
        # Metrics Calculations
        elapsed_time = time.time() - self.start_time
        files_per_second = self.completed_files / elapsed_time if elapsed_time > 0 else 0
        remaining_files = self.total_files - self.completed_files
        
        # Calculate Estimated Time Remaining (ETA)
        eta = remaining_files / files_per_second if files_per_second > 0 else 0
        
        # Push stats updates smoothly to UI
        self.meta_label.configure(
            text=f"Processed: {self.completed_files}/{self.total_files} items | Speed: {files_per_second:.1f} files/sec | ETA: {int(eta)}s remaining"
        )

    def pipeline_worker(self, items_to_sort):
        """Runs the data collection and ML algorithm inside a background environment."""
        # 1. Asynchronous Text Extraction
        corpus = build_corpus(self.base_dir, items_to_sort, self.item_completed_callback)
        
        # 2. Transition Status to Processing Phase
        self.status_label.configure(text="Data compiled. Modeling semantic themes...", text_color="yellow")
        
        # 3. Process Topic Clustering 
        self.plan = generate_sorting_plan(corpus, MAX_FOLDERS)
        
        # 4. Render Layout Proposal Tree
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")
        
        for folder, files in self.plan.items():
            self.textbox.insert("end", f"📂 [{folder}] ({len(files)} items)\n")
            for f in files[:3]:
                self.textbox.insert("end", f"   ├── {f}\n")
            if len(files) > 3:
                self.textbox.insert("end", f"   └── ...and {len(files) - 3} more.\n")
            self.textbox.insert("end", "\n")
            
        self.textbox.configure(state="disabled")
        
        # Reset Interaction States on UI
        self.status_label.configure(text="AI Plan ready for review.", text_color="green")
        self.execute_btn.configure(state="normal")
        self.select_btn.configure(state="normal")

    def execute_sort(self):
        if self.plan and self.base_dir:
            self.status_label.configure(text="Moving files into position...", text_color="white")
            self.execute_btn.configure(state="disabled")
            
            # Execute physical operations safely
            execute_moves(self.base_dir, self.plan)
            
            self.status_label.configure(text="Sorting complete! Check autosorter.log for any skipped/locked files.", text_color="green")
            self.meta_label.configure(text="")

def run_app():
    app = AutoSorterApp()
    app.mainloop()