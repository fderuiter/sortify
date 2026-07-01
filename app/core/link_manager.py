"""Link management module."""
import os
import sys

try:
    import pylnk3
except ImportError:
    pylnk3 = None


class LinkManager:
    _registry = {}

    @classmethod
    def register_link(cls, base_dir: str, rel_path: str):
        """Identify and store the link's target if it's a symlink or .lnk file."""
        full_path = os.path.join(base_dir, rel_path)
        if os.path.islink(full_path):
            try:
                target = os.readlink(full_path)
                cls._registry[full_path] = {"type": "symlink", "target": target}
            except OSError:
                pass
        elif full_path.lower().endswith(".lnk"):
            if pylnk3:
                try:
                    lnk = pylnk3.parse(full_path)
                    target = lnk.path
                    if target:
                        cls._registry[full_path] = {"type": "lnk", "target": target}
                except Exception:
                    pass

    @classmethod
    def get_link_info(cls, full_path: str):
        return cls._registry.get(full_path)

    @classmethod
    def clear(cls):
        cls._registry.clear()
