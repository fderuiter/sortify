"""Verification engine for proactive move validation."""

import os
import platform
import shutil


class VerificationEngine:
    """Engine to verify file operations before execution."""

    @staticmethod
    def get_moves(base_dir: str, plan: dict, current_dest: str = "") -> list:
        """Get a flat list of moves from the plan."""
        moves = []
        for key, content in plan.items():
            if content is None:
                source_path = os.path.join(base_dir, key)
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
        if not os.path.exists(filepath):
            return False
        try:
            with open(filepath, "a"):
                pass
            return True
        except IOError:
            return False
        except PermissionError:
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
                except Exception:
                    pass

        is_windows = platform.system() == "Windows"
        for rel_src, src, dst in moves:
            if rel_src in errors:
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
