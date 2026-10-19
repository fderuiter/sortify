"""Guard the committed SQLCipher binaries against placeholders and stale hashes."""

import hashlib
import json
from pathlib import Path

import pytest

BINARIES_ROOT = Path(__file__).resolve().parent.parent / "app" / "binaries"
NATIVE_SUFFIXES = (".so", ".pyd", ".dylib", ".dll")
# ELF, Mach-O (32/64-bit, both byte orders, universal) and PE/COFF headers.
NATIVE_MAGIC = (
    b"\x7fELF",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"MZ",
)


def _manifest_entries():
    manifest = json.loads((BINARIES_ROOT / "manifest.json").read_text("utf-8"))
    return [
        (platform, rel_path, expected)
        for platform, files in manifest.items()
        for rel_path, expected in files.items()
    ]


@pytest.mark.parametrize(("platform", "rel_path", "expected"), _manifest_entries())
def test_manifest_entry_matches_committed_file(platform, rel_path, expected):
    path = BINARIES_ROOT / platform / rel_path
    assert path.is_file(), f"manifest lists {platform}/{rel_path} but it is missing"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected


@pytest.mark.parametrize(
    "path",
    sorted(
        p
        for p in BINARIES_ROOT.rglob("*")
        if p.is_file() and p.suffix in NATIVE_SUFFIXES
    ),
    ids=lambda p: p.relative_to(BINARIES_ROOT).as_posix(),
)
def test_committed_native_libraries_are_real_binaries(path):
    with open(path, "rb") as f:
        header = f.read(4)
    assert header.startswith(NATIVE_MAGIC), (
        f"{path.relative_to(BINARIES_ROOT)} is not a native library; "
        "do not commit placeholders, let scripts/build.py vendor the real file"
    )
