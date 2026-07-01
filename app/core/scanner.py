"""Directory scanning utility."""

import os


def get_files_recursively(base: str, rel_path: str = "") -> list:
    """Recursively list all files in a directory deterministically."""
    files = []
    try:
        # Sorting the scanned entries ensures deterministic file discovery
        with os.scandir(os.path.join(base, rel_path)) as entries:
            sorted_entries = sorted(entries, key=lambda e: e.name)
            for entry in sorted_entries:
                if entry.name.startswith("."):
                    continue
                entry_rel_path = (
                    os.path.join(rel_path, entry.name) if rel_path else entry.name
                )
                if entry.is_dir():
                    files.extend(get_files_recursively(base, entry_rel_path))
                else:
                    files.append(entry_rel_path)
    except Exception:
        pass
    
    if rel_path == "":
        return sorted(files)
    return files
