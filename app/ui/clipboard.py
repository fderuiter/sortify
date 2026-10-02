"""Dual-Backend Hybrid Clipboard Service for Textual TUI.

Provides multi-backend clipboard copying utilizing Textual driver capabilities,
ANSI OSC 52 escape sequences with multiplexer passthrough support (tmux / screen),
and host OS platform binaries.
"""

import base64
import logging
import os
import shutil
import subprocess
import sys
from typing import Any, Optional

logger = logging.getLogger(__name__)


def generate_osc52_sequence(text: str) -> str:
    """Generate ANSI OSC 52 escape sequence with multiplexer passthrough wrapping.

    Handles standard terminal OSC 52 along with DCS passthrough wrappers for
    tmux and GNU screen terminal multiplexers over SSH or local PTY sessions.
    """
    encoded_bytes = base64.b64encode(text.encode("utf-8"))
    payload = encoded_bytes.decode("ascii")
    osc52_base = f"\x1b]52;c;{payload}\x07"

    term = os.environ.get("TERM", "").lower()
    tmux = os.environ.get("TMUX")
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    sty = os.environ.get("STY")

    is_tmux = bool(tmux) or "tmux" in term or term_program == "tmux"
    is_screen = ("screen" in term or bool(sty)) and not is_tmux

    if is_tmux:
        # tmux DCS passthrough: ESC characters inside payload must be doubled (\x1b\x1b)
        escaped_osc52 = osc52_base.replace("\x1b", "\x1b\x1b")
        return f"\x1bPtmux;{escaped_osc52}\x1b\\"
    elif is_screen:
        # screen DCS passthrough: \x1bP\x1b]52;c;{payload}\x07\x1b\
        return f"\x1bP\x1b]52;c;{payload}\x07\x1b\\"
    else:
        return osc52_base


def _emit_osc52_to_stdout(sequence: str) -> bool:
    """Write OSC 52 escape sequence directly to standard output stream."""
    try:
        out_stream = getattr(sys, "__stdout__", None) or sys.stdout
        if out_stream and hasattr(out_stream, "write"):
            out_stream.write(sequence)
            if hasattr(out_stream, "flush"):
                out_stream.flush()
            return True
    except Exception as exc:
        logger.debug(f"OSC 52 emission error: {exc}")
    return False


def _copy_via_platform_binary(text: str) -> bool:
    """Attempt copying text via host platform native clipboard executable."""
    try:
        if sys.platform == "darwin":
            pbcopy_bin = shutil.which("pbcopy")
            if pbcopy_bin:
                res = subprocess.run(
                    [pbcopy_bin],
                    input=text.encode("utf-8"),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1.0,
                    check=False,
                )
                return res.returncode == 0
        elif sys.platform == "win32":
            clip_bin = shutil.which("clip.exe") or shutil.which("clip")
            if clip_bin:
                res = subprocess.run(
                    [clip_bin],
                    input=text.encode("utf-16le" if sys.platform == "win32" else "utf-8"),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1.0,
                    check=False,
                )
                return res.returncode == 0
        else:
            # Linux / Unix
            for tool in [
                ["wl-copy"],
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
            ]:
                binary = shutil.which(tool[0])
                if binary:
                    cmd = [binary] + tool[1:]
                    res = subprocess.run(
                        cmd,
                        input=text.encode("utf-8"),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=1.0,
                        check=False,
                    )
                    if res.returncode == 0:
                        return True
    except Exception as exc:
        logger.debug(f"Platform binary clipboard copy error: {exc}")
    return False


class ClipboardService:
    """Hybrid Clipboard Dispatcher service supporting Textual driver, OSC 52, and platform binaries."""

    def __init__(self, app: Optional[Any] = None):
        self.app = app

    def copy(self, text: str) -> bool:
        """Copy payload text to clipboard using available hybrid dispatch backends.

        Runs synchronously without blocking the event loop or raising unhandled exceptions.
        Returns True if at least one dispatch mechanism succeeded.
        """
        if not text:
            return False

        success = False

        # 1. Textual Driver / App copy method
        if (
            self.app is not None
            and hasattr(self.app, "copy_to_clipboard")
            and callable(self.app.copy_to_clipboard)
        ):
            try:
                self.app.copy_to_clipboard(text)
                success = True
            except Exception as exc:
                logger.debug(f"Textual app copy_to_clipboard failed: {exc}")

        # 2. ANSI OSC 52 sequence emission
        try:
            seq = generate_osc52_sequence(text)
            if _emit_osc52_to_stdout(seq):
                success = True
        except Exception as exc:
            logger.debug(f"OSC 52 sequence dispatch failed: {exc}")

        # 3. Host platform native binaries fallback
        try:
            if _copy_via_platform_binary(text):
                success = True
        except Exception as exc:
            logger.debug(f"Platform binary dispatch failed: {exc}")

        return success
