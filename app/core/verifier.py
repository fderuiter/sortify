"""Verification engine for proactive move validation."""

import os
import platform
import shutil
from pathlib import Path


class VerificationEngine:
    """Engine to verify file operations before execution."""

    @staticmethod
    def get_moves(base_dir: str | Path, plan: dict, current_dest: str | Path = "") -> list:
        """Get a flat list of moves from the plan."""
        base_path = Path(base_dir)
        dest_path = Path(current_dest)
        
        moves = []
        for key, content in plan.items():
            if content is None or (
                isinstance(content, dict) and content.get("__type__") == "file"
            ):
                source_path = base_path / key

                if isinstance(content, dict) and "target_filename" in content:
                    filename = content["target_filename"]
                else:
                    filename = Path(key).name

                target_dir = base_path / dest_path
                dest_file_path = target_dir / filename
                moves.append((key, source_path.as_posix(), dest_file_path.as_posix()))
            else:
                moves.extend(
                    VerificationEngine.get_moves(
                        base_path, content, dest_path / key
                    )
                )
        return moves

    def _get_volume(self, path: str | Path) -> str:
        curr = Path(path).resolve()
        if platform.system() == "Windows":
            return curr.drive + "\\"
        else:
            while not curr.exists():
                parent = curr.parent
                if parent == curr:
                    break
                curr = parent
            while not curr.is_mount():
                parent = curr.parent
                if parent == curr:
                    break
                curr = parent
            return curr.as_posix()

    def _is_file_accessible(self, filepath: str | Path) -> bool:
        path = Path(filepath)
        if not path.exists():
            return False
        try:
            with open(path, "a"):
                pass
            return True
        except IOError:
            return False
        except PermissionError:
            return False

    def verify_plan(self, base_dir: str | Path, plan: dict) -> dict:
        """Verify the execution plan against constraints."""
        errors = {}
        moves = self.get_moves(base_dir, plan)

        volumes = {}
        for rel_src, src, dst in moves:
            src_vol = self._get_volume(src)
            dst_vol = self._get_volume(dst)

            src_path = Path(src)
            size = src_path.stat().st_size if src_path.exists() else 0

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
                if len(Path(dst).name) > 255:
                    errors[rel_src] = "Filename exceeds 255 characters"
                elif len(dst) > 4096:
                    errors[rel_src] = "Path exceeds 4096 characters"

            if rel_src not in errors and not self._is_file_accessible(src):
                errors[rel_src] = "File is locked or inaccessible"

        return errors
