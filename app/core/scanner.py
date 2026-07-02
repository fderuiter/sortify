"""Directory scanning utility."""

from pathlib import Path

from app.core.link_manager import LinkManager


def get_files_recursively(base: str | Path, rel_path: str | Path = "") -> list:
    """Recursively list all files in a directory deterministically."""
    files = []
    
    if str(rel_path) == "":
        LinkManager.clear()

    base_path = Path(base)
    target_path = base_path / rel_path

    try:
        # Sorting the scanned entries ensures deterministic file discovery
        sorted_entries = sorted(target_path.iterdir(), key=lambda e: e.name)
        for entry in sorted_entries:
            if entry.name.startswith("."):
                continue
            entry_rel_path = entry.relative_to(base_path).as_posix()
            
            # Check for symlink or .lnk file
            if entry.is_symlink() or entry.name.lower().endswith(".lnk"):
                LinkManager.register_link(base_path.as_posix(), entry_rel_path)
                files.append(entry_rel_path)
            elif entry.is_dir():
                files.extend(get_files_recursively(base, entry_rel_path))
            else:
                files.append(entry_rel_path)
    except Exception:
        pass

    if str(rel_path) == "":
        return sorted(files)
    return files
