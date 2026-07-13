"""Verification engine for proactive move validation."""

import logging
import os
import platform
import shutil

from app.core.link_manager import LinkManager


class VerificationEngine:
    """Engine to verify file operations before execution."""

    @staticmethod
    def get_moves(base_dir: str, plan: dict, current_dest: str = "") -> list:
        """Get a flat list of moves from the plan."""
        moves = []
        for key, content in plan.items():
            if content is None or (
                isinstance(content, dict) and content.get("__type__") == "file"
            ):
                source_path = os.path.join(base_dir, key)

                if isinstance(content, dict) and "target_filename" in content:
                    filename = content["target_filename"]
                else:
                    filename = os.path.basename(key)

                dest_dir = os.path.join(base_dir, current_dest)
                dest_path = os.path.join(dest_dir, filename)
                moves.append((key, source_path, dest_path))
            else:
                moves.extend(
                    VerificationEngine.get_moves(
                        base_dir, content, os.path.join(current_dest, key)
                    )
                )
        return moves

    @staticmethod
    def is_cloud_path(path: str) -> bool:
        """Check if the given path resides within a cloud-synced folder."""
        norm_path = os.path.normpath(os.path.abspath(path)).lower()
        sys_platform = platform.system()

        if sys_platform == "Darwin":
            if "library/mobile documents" in norm_path or "com~apple~clouddocs" in norm_path:
                return True
        elif sys_platform == "Windows":
            env_vars = ["OneDrive", "OneDriveConsumer", "OneDriveCommercial"]
            for var in env_vars:
                env_val = os.environ.get(var)
                if env_val:
                    env_val_norm = os.path.normpath(os.path.abspath(env_val)).lower()
                    if norm_path.startswith(env_val_norm + os.sep) or norm_path == env_val_norm:
                        return True
            # Fallback checks
            if "\\onedrive\\" in norm_path or norm_path.endswith("\\onedrive"):
                return True
        return False

    def has_cloud_targets(self, base_dir: str, plan: dict) -> bool:
        """Check if any target path in the plan is a cloud-synced path."""
        moves = self.get_moves(base_dir, plan)
        for _, _, dst in moves:
            if self.is_cloud_path(dst):
                return True
        return False

    def _get_volume(self, path: str) -> str:
        curr = os.path.abspath(path)
        if platform.system() == "Windows":
            return os.path.splitdrive(curr)[0] + "\\"
        else:
            while not os.path.exists(curr):
                parent = os.path.dirname(curr)
                if parent == curr:
                    break
                curr = parent
            while not os.path.ismount(curr):
                parent = os.path.dirname(curr)
                if parent == curr:
                    break
                curr = parent
            return curr

    def _is_file_accessible(self, filepath: str) -> bool:
        if not os.path.lexists(filepath):
            return False

        # Don't try to open symlinks for appending
        if os.path.islink(filepath):
            return os.access(filepath, os.R_OK)

        try:
            with open(filepath, "a"):
                pass
            return True
        except IOError:
            return False
        except PermissionError:
            return False

    def _check_symlink_privilege(self, test_dir: str) -> bool:
        """Test if the OS allows creating symbolic links."""
        test_src = os.path.join(test_dir, ".test_symlink_src")
        test_dst = os.path.join(test_dir, ".test_symlink_dst")
        try:
            with open(test_src, "w") as f:
                f.write("test")
            os.symlink(test_src, test_dst)
            os.remove(test_dst)
            os.remove(test_src)
            return True
        except Exception:
            if os.path.exists(test_src):
                os.remove(test_src)
            return False

    def verify_plan(self, base_dir: str, plan: dict) -> dict:
        """Verify the execution plan against constraints."""
        errors = {}
        moves = self.get_moves(base_dir, plan)

        volumes = {}
        for rel_src, src, dst in moves:
            src_vol = self._get_volume(src)
            dst_vol = self._get_volume(dst)

            size = os.path.getsize(src) if os.path.exists(src) else 0

            if src_vol not in volumes:
                volumes[src_vol] = 0
            if dst_vol not in volumes:
                volumes[dst_vol] = 0

            if src_vol != dst_vol:
                volumes[src_vol] -= size
                volumes[dst_vol] += size

        for vol, net_change in volumes.items():
            if net_change > 0:
                try:
                    usage = shutil.disk_usage(vol)
                    if usage.free < net_change:
                        for rel_src, src, dst in moves:
                            if (
                                self._get_volume(dst) == vol
                                and self._get_volume(src) != vol
                            ):
                                errors[rel_src] = "Insufficient disk space"
                except Exception as e:
                    logging.error(
                        f"Failed to check disk space for volume {vol}: {e}",
                        exc_info=True,
                    )

        is_windows = platform.system() == "Windows"

        # Check if we have symlink capabilities if we are moving any symlinks
        symlink_privilege = None

        for rel_src, src, dst in moves:
            if rel_src in errors:
                continue

            link_info = LinkManager.get_link_info(src)
            if link_info and link_info["type"] == "symlink":
                if symlink_privilege is None:
                    symlink_privilege = self._check_symlink_privilege(base_dir)
                if not symlink_privilege:
                    errors[rel_src] = (
                        "Operating system blocks link modification due to permission constraints"
                    )
                    continue

            if is_windows:
                if len(dst) >= 260:
                    errors[rel_src] = "Path exceeds 260 characters"
            else:
                if len(os.path.basename(dst)) > 255:
                    errors[rel_src] = "Filename exceeds 255 characters"
                elif len(dst) > 4096:
                    errors[rel_src] = "Path exceeds 4096 characters"

            if rel_src not in errors and not self._is_file_accessible(src):
                errors[rel_src] = "File is locked or inaccessible"

        return errors
