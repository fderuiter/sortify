# ui/app.py
import os
import customtkinter as ctk
from tkinter import filedialog
from config import MAX_FOLDERS
from core.extractor import build_corpus
from core.analyzer import generate_sorting_plan
from core.mover import execute_moves

# Setup Modern Theme
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

class AutoSorterApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Smart AutoSorter AI")
        self.geometry("700x550")
        
        self.base_dir = None
        self.plan = None
        
        # UI Elements
        self.title_label = ctk.CTkLabel(self, text="AI File Organizer", font=("Roboto", 24, "bold"))
        self.title_label.pack(pady=20)
        
        self.select_btn = ctk.CTkButton(self, text="Select Directory to Sort", command=self.select_directory)
        self.select_btn.pack(pady=10)
        
        self.status_label = ctk.CTkLabel(self, text="Waiting for directory...", text_color="gray")
        self.status_label.pack(pady=5)
        
        # Textbox for plan preview
        self.textbox = ctk.CTkTextbox(self, width=600, height=300, state="disabled")
        self.textbox.pack(pady=10)
        
        self.execute_btn = ctk.CTkButton(self, text="Approve & Execute Sort", 
                                         command=self.execute_sort, 
                                         fg_color="green", hover_color="darkgreen", state="disabled")
        self.execute_btn.pack(pady=10)

    def select_directory(self):
        self.base_dir = filedialog.askdirectory(title="Select Directory")
        if self.base_dir:
            self.status_label.configure(text=f"Selected: {self.base_dir}\nScanning and building AI plan...", text_color="white")
            self.update() # Force UI refresh
            self.generate_plan()

    def generate_plan(self):
        items_to_sort = [f for f in os.listdir(self.base_dir) if not f.startswith('.')]
        if not items_to_sort:
            self.status_label.configure(text="Directory is empty.", text_color="red")
            return
            
        # 1. Extract
        corpus = build_corpus(self.base_dir, items_to_sort)
        
        # 2. Analyze
        self.plan = generate_sorting_plan(corpus, MAX_FOLDERS)
        
        # 3. Display Proposal
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
        
        # Enable Execution
        self.status_label.configure(text="Review the AI plan below. Click Execute to move files.", text_color="green")
        self.execute_btn.configure(state="normal")

    def execute_sort(self):
        if self.plan and self.base_dir:
            self.status_label.configure(text="Moving files...", text_color="white")
            self.update()
            
            execute_moves(self.base_dir, self.plan)
            
            self.status_label.configure(text="Success! Files have been sorted.", text_color="green")
            self.execute_btn.configure(state="disabled")
            self.select_btn.configure(text="Sort Another Directory")

def run_app():
    app = AutoSorterApp()
    app.mainloop()