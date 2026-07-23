"""Verification engine for proactive move validation."""

import os


def is_ml_available() -> bool:
    """Check if heavy machine learning dependencies (torch, easyocr) are available."""
    try:
        import torch
        import easyocr
        return True
    except ImportError:
        return False


class VerificationEngine:
    """Engine to verify file operations before execution."""

    @staticmethod
    def get_moves(base_dir: str, plan: dict, current_dest: str = "") -> list:
        """Get a flat list of moves from the plan."""
        moves = []
        for key, content in plan.items():
            if content is None or (
                isinstance(content, dict)
                and content.get("__type__") in ("file", "directory")
            ):
                if isinstance(content, dict) and content.get("__type__") == "directory":
                    continue

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
