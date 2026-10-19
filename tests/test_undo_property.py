"""Property: sorting a folder and then undoing the sort gives back the same folder.

Hypothesis picks the folder: nested sources, reused names that force
collision renames, duplicate bytes and empty files. Each example runs the
real ``execute_moves`` in-process, then ``HistoryManager.rollback`` (what
``smart-autosorter undo`` calls), and compares a hash of every user file
before and after. The slow crash test in ``test_crash_safety.py`` covers
undo after a killed sort; this one covers the clean path, fast enough for
every run.
"""

import hashlib
import os
import tempfile

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.mover import execute_moves


class KeepBothSettings:
    CONFLICT_POLICY = "rename"


# Few names and folders, so collisions and shared parents come up often.
_DIRS = st.lists(st.sampled_from(["a", "b", "deep"]), max_size=2)
_NAMES = st.sampled_from(["report.txt", "photo.jpg", "notes.md", "data"])
_CONTENTS = st.sampled_from([b"", b"same bytes", b"other", b"\x00\xffbinary"])
_CATEGORIES = st.sampled_from(["Finance", "Photos", "Docs"])

_FILES = st.lists(
    st.tuples(_DIRS, _NAMES, _CONTENTS, _CATEGORIES),
    min_size=1,
    max_size=8,
    unique_by=lambda f: (tuple(f[0]), f[1]),
)


def _manifest(base_dir):
    """Map every user file's relative path to a hash of its bytes."""
    out = {}
    for root, dirs, files in os.walk(base_dir):
        # The app keeps its own bookkeeping in dot-folders and dot-files.
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, base_dir).replace(os.sep, "/")
            with open(path, "rb") as fh:
                out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _all_bytes(manifest):
    return sorted(manifest.values())


@settings(
    max_examples=25,
    deadline=None,
    derandomize=True,
    # Each example sorts a fresh folder made below, so sharing the
    # fixture's database and history across examples is intended.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(files=_FILES)
def test_sort_then_undo_restores_every_file(test_history_env, files):
    tmp_root, db, _cache, history_manager, _worker = test_history_env
    base_dir = tempfile.mkdtemp(dir=tmp_root)

    plan = {}
    for i, (dirs, name, content, category) in enumerate(files):
        rel = "/".join(["inbox", *dirs, name])
        path = os.path.join(base_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        plan.setdefault(category, {})[f"{i}-{name}"] = {
            "__type__": "file",
            "relative_source": f"../{rel}",
            "target_filename": name,
        }
    before = _manifest(base_dir)

    execute_moves(base_dir, plan, db, history_manager, KeepBothSettings())
    sorted_state = _manifest(base_dir)
    # The sort itself keeps every byte, collision renames included.
    assert _all_bytes(sorted_state) == _all_bytes(before)
    assert not any(p.startswith("inbox/") for p in sorted_state)

    # Sessions from earlier examples share this history, so pick ours by folder.
    (session_id,) = [
        s["session_id"]
        for s in history_manager.get_sessions()
        if os.path.normpath(s["base_dir"]) == os.path.normpath(base_dir)
    ]
    history_manager.rollback(session_id)

    assert _manifest(base_dir) == before
