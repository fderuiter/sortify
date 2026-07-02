import os
import glob

def generate_api_docs():
    app_dir = "app"
    output_file = os.path.join("docs", "api_reference.md")
    
    with open(output_file, "w") as f:
        f.write("# API Reference\n\n")
        f.write("This document is automatically generated. Do not edit manually.\n\n")
        
        # Find all python files
        py_files = glob.glob(os.path.join(app_dir, "**", "*.py"), recursive=True)
        py_files = [p for p in py_files if not p.endswith("__init__.py")]
        py_files.sort()
        
        for file_path in py_files:
            # Convert app/core/extractor.py to app.core.extractor
            module_name = file_path.replace(os.sep, ".").removesuffix(".py")
            f.write(f"## `{module_name}`\n\n")
            f.write(f"::: {module_name}\n\n")

if __name__ == "__main__":
    generate_api_docs()
