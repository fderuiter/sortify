"""Utility functions for handling paths and sanitizing filenames."""

import re

RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}


def sanitize_name(name: str) -> str:
    """Sanitize a file or folder name for Windows.

    Strips illegal characters and appends _safe to reserved names.
    """
    if not name:
        return name

    # Replace illegal characters with underscore (or just strip them)
    # The requirement says "strip illegal path characters" but in the example:
    # "Data: Archives" -> "Data_ Archives" so we should replace `:` with `_`.
    # Let's replace `< > : " / \\ | ? *` with `_`.
    safe_name = re.sub(r'[<>:"/\\|?*]', "_", name)

    # Strip trailing periods and spaces (also problematic on Windows)
    safe_name = safe_name.rstrip(". ")

    # Check if the name matches a reserved name (case-insensitive, optionally with an extension)
    upper_name = safe_name.upper()
    base_name = upper_name.split(".")[0]

    if base_name in RESERVED_NAMES:
        # Need to append _safe suffix. For "CON" -> "CON_safe".
        # If there's an extension, e.g. "CON.txt" -> "CON_safe.txt"?
        # The scenario says "CON" -> "CON_safe".

        # Let's preserve the original casing and just append _safe to the base name
        parts = safe_name.split(".")
        parts[0] = parts[0] + "_safe"
        safe_name = ".".join(parts)

    if not safe_name:
        safe_name = "Unnamed_safe"

    return safe_name


def is_valid_name(name: str) -> bool:
    """Check if a file or folder name is valid for Windows."""
    if not name:
        return False

    if re.search(r'[<>:"/\\|?*]', name):
        return False

    if name != name.rstrip(". "):
        return False

    base_name = name.upper().split(".")[0]
    if base_name in RESERVED_NAMES:
        return False

    return True


def is_non_local_path(path: str) -> bool:
    """Identify if a path resides on a network share, external, or removable drive.

    This check is completely local and does not make any network requests.
    """
    if not path:
        return False

    import os
    import sys

    # 1. Clean and check UNC Paths (Windows and Unix-like UNC representations)
    p = str(path).strip()
    if p.startswith(r"\\") or p.startswith("//"):
        return True

    # 2. Windows Drive Types
    if sys.platform == "win32":
        try:
            import ctypes
            drive, _ = os.path.splitdrive(os.path.abspath(p))
            if drive:
                drive_root = drive + "\\"
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(drive_root)
                # DRIVE_REMOVABLE = 2, DRIVE_REMOTE = 4, DRIVE_CDROM = 5
                if drive_type in (2, 4, 5):
                    return True
        except Exception:
            pass

    # 3. Unix Mount Points (macOS / Linux)
    norm_p = os.path.normpath(os.path.abspath(p))
    if (
        norm_p.startswith("/Volumes/")
        or norm_p.startswith("/media/")
        or norm_p.startswith("/mnt/")
    ):
        return True

    # 4. Check /proc/mounts on Linux for non-local filesystem types
    if sys.platform.startswith("linux"):
        try:
            # Walk up to find the mount point
            current = norm_p
            mount_point = None
            while current and current != "/":
                if os.path.ismount(current):
                    mount_point = current
                    break
                parent = os.path.dirname(current)
                if parent == current:
                    break
                current = parent
            if not mount_point:
                mount_point = "/"

            if os.path.exists("/proc/mounts"):
                non_local_fs = {
                    "nfs",
                    "nfs4",
                    "cifs",
                    "smbfs",
                    "sshfs",
                    "davfs",
                    "vboxsf",
                    "fuse.sshfs",
                    "fuse.rclone",
                    "afs",
                    "vfat",
                    "exfat",
                    "msdos",
                    "iso9660",
                    "udf",
                }
                with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) >= 3:
                            mount_p, fs_type = parts[1], parts[2]
                            if mount_p == mount_point:
                                if fs_type.lower() in non_local_fs:
                                    return True
        except Exception:
            pass

    return False

