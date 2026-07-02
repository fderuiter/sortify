"""Link management module."""

import logging
import os

try:
    import pylnk3
except ImportError:
    pylnk3 = None


class LinkManager:
    """Manager for symbolic and shortcut links."""

    _registry = {}

    @classmethod
    def register_link(cls, base_dir: str, rel_path: str):
        """Identify and store the link's target if it's a symlink or .lnk file."""
        full_path = os.path.join(base_dir, rel_path)
        if os.path.islink(full_path):
            try:
                target = os.readlink(full_path)
                cls._registry[full_path] = {"type": "symlink", "target": target}
            except OSError as e:
                logging.error(
                    f"Failed to read symlink for {full_path}: {e}", exc_info=True
                )
        elif full_path.lower().endswith(".lnk"):
            if pylnk3:
                try:
                    lnk = pylnk3.parse(full_path)
                    target = lnk.path
                    if target:
                        cls._registry[full_path] = {"type": "lnk", "target": target}
                except Exception as e:
                    logging.error(
                        f"Failed to parse Windows shortcut {full_path}: {e}",
                        exc_info=True,
                    )

    @classmethod
    def get_link_info(cls, full_path: str):
        """Retrieve link info for a given full path."""
        return cls._registry.get(full_path)

    @classmethod
    def clear(cls):
        """Clear the registry of stored link infos."""
        cls._registry.clear()
