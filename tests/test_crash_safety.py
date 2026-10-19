"""Crash-safety property test for sorting and undo (ADR 0005, ADR 0006).

Each case builds a folder of related, unrelated and hidden files, runs a real
``smart-autosorter sort`` in a child process, and kills that process with
``os._exit`` right before or right after its k-th file move, which is where a
power cut or ``kill -9`` would leave the mover. It then runs a real
``smart-autosorter undo --latest`` and checks two properties:

1. Nothing is lost: right after the crash, every original file's bytes still
   exist somewhere in the folder.
2. Undo restores the folder: afterwards every original file is back at its
   own path with identical bytes, and nothing extra is left behind.

The crash hook lives only in the child's wrapper script below; production code
has no test branches for it.
"""

from __future__ import annotations

import hashlib
import os
import random
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[1]

# Runs the real CLI, but dies with os._exit (no finally blocks, no atexit, no
# flushes) around the CRASH_AT-th call to the mover's no-clobber move.
CRASHING_CLI = """
import os, sys, threading
crash_at = int(os.environ.get("CRASH_AT", "0"))
crash_mode = os.environ.get("CRASH_MODE", "after")
if crash_at:
    import app.core.resilient_file_ops as rfo
    real_move = rfo.atomic_move_non_clobber
    lock = threading.Lock()
    calls = [0]

    def crashing_move(src, dst):
        with lock:
            calls[0] += 1
            n = calls[0]
        if crash_mode == "before" and n == crash_at:
            os._exit(137)
        real_move(src, dst)
        if crash_mode == "after" and n == crash_at:
            os._exit(137)

    rfo.atomic_move_non_clobber = crashing_move
sys.argv = ["smart-autosorter", *sys.argv[1:]]
from app.main import main
sys.exit(main())
"""

# A crash after a file has moved but before its ledger row is written leaves
# that file in its new folder, and undo still reports success. The single move
# ledger in ADR 0006 (#714) fixes this; strict xfail turns into a failure the
# day it does, so whoever lands the fix removes these marks.
UNRESTORED_REASON = (
    "crash between a move and its ledger update leaves the file unrestored "
    "while undo reports success (ADR 0006, #714)"
)
UNRESTORED_AFTER_CRASH = pytest.mark.xfail(strict=True, reason=UNRESTORED_REASON)
# Moves run on several workers, so a crash before the k-th move can still catch
# another worker between its move and its ledger update. That race hits some
# runs and not others, so this mark can't be strict.
UNRESTORED_SOMETIMES = pytest.mark.xfail(strict=False, reason=UNRESTORED_REASON)

TOPICS = {
    "fin": "quarterly revenue budget invoice tax ledger profit expense balance fiscal",
    "tech": "python kubernetes docker server deployment api database backend cluster",
    "bio": "protein cell genome enzyme dna rna mutation organism tissue biology",
}


def _make_corpus(root: Path, seed: int, n_files: int = 36) -> None:
    rng = random.Random(seed)
    for i in range(n_files):
        topic = rng.choice(sorted(TOPICS))
        folder = root / rng.choice(["", "inbox", "inbox/old", "misc"])
        folder.mkdir(parents=True, exist_ok=True)
        words = TOPICS[topic].split()
        text = " ".join(rng.choice(words) for _ in range(40)) + f" #{i}"
        # Reused names across folders make the mover rename on collision.
        name = rng.choice([f"{topic}{i}.txt", f"notes{i % 5}.txt"])
        path = folder / name
        if path.exists():
            path = folder / f"{i}_{name}"
        path.write_text(text, encoding="utf-8")
    # Files a sort must never touch or lose.
    (root / ".hidden").write_text("hidden", encoding="utf-8")
    (root / ".config").mkdir()
    (root / ".config" / "settings.ini").write_text("[a]\nb=1\n", encoding="utf-8")
    (root / "same-bytes-1.txt").write_text("identical bytes", encoding="utf-8")
    (root / "misc" / "same-bytes-2.txt").parent.mkdir(exist_ok=True)
    (root / "misc" / "same-bytes-2.txt").write_text("identical bytes", encoding="utf-8")


def _snapshot(root: Path) -> dict[str, str]:
    files = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            files[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return files


def _cli(work: Path, *args: str, crash_at: int = 0, crash_mode: str = "after"):
    env = {
        **os.environ,
        "AUTOSORTER_APP_DIR": str(work / "app"),
        "HOME": str(work / "home"),
        "CRASH_AT": str(crash_at),
        "CRASH_MODE": crash_mode,
    }
    return subprocess.run(
        [sys.executable, "-c", CRASHING_CLI, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.parametrize(
    ("seed", "crash_mode", "crash_at"),
    [
        (1, "before", 1),
        (2, "after", 1),
        pytest.param(3, "before", 17, marks=UNRESTORED_SOMETIMES),
        pytest.param(4, "after", 17, marks=UNRESTORED_AFTER_CRASH),
        pytest.param(5, "after", 30, marks=UNRESTORED_AFTER_CRASH),
        (6, "after", 0),  # no crash: a full sort, then undo
    ],
)
def test_crash_during_sort_then_undo_restores_every_file(
    tmp_path, seed, crash_mode, crash_at
):
    folder = tmp_path / "folder"
    folder.mkdir()
    (tmp_path / "home").mkdir()
    _make_corpus(folder, seed)
    before = _snapshot(folder)

    sort = _cli(
        tmp_path,
        "sort",
        str(folder),
        "--accept-ai-consent",
        "--non-interactive",
        crash_at=crash_at,
        crash_mode=crash_mode,
    )
    assert sort.returncode in (0, 137), sort.stdout + sort.stderr
    after_crash = _snapshot(folder)
    if crash_at:
        assert sort.returncode == 137, "the crash point was never reached"
        assert after_crash != before or crash_mode == "before" and crash_at == 1

    missing = Counter(before.values()) - Counter(after_crash.values())
    assert not missing, f"bytes lost by the crash itself: {sorted(missing)}"

    undo = _cli(tmp_path, "undo", "--latest")
    after_undo = _snapshot(folder)
    not_restored = sorted(p for p, h in before.items() if after_undo.get(p) != h)
    left_behind = sorted(set(after_undo) - set(before))
    assert not not_restored and not left_behind, (
        f"undo exit {undo.returncode}: not restored {not_restored}, "
        f"left behind {left_behind}\n{undo.stdout[-2000:]}{undo.stderr[-2000:]}"
    )
