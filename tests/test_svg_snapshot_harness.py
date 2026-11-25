"""Unit tests for the TUI SVG snapshot sanitization and comparison harness."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.svg_snapshot_harness import sanitize_svg, verify_svg_snapshot


def test_verify_svg_snapshot_unlinks_stale_actual_on_update_and_clean_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify mismatch writes .svg.actual and UPDATE_SNAPSHOTS=1 or clean match unlinks it."""
    import tests.test_visual_snapshots as vs

    monkeypatch.setattr(vs, "SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.delenv("UPDATE_SNAPSHOTS", raising=False)

    snap_id = "synthetic_modal"
    svg_path = tmp_path / f"{snap_id}.svg"
    actual_path = tmp_path / f"{snap_id}.svg.actual"

    svg_v1 = "<svg><text>v1</text></svg>\n"
    svg_v2 = "<svg><text>v2</text></svg>\n"
    svg_path.write_text(svg_v1, encoding="utf-8")

    # Mismatch writes .svg.actual and fails
    with pytest.raises(AssertionError, match="SVG visual snapshot mismatch"):
        vs.assert_svg_snapshot(snap_id, svg_v2)
    assert actual_path.is_file()
    assert actual_path.read_text(encoding="utf-8") == svg_v2

    # UPDATE_SNAPSHOTS=1 rewrites baseline and removes stale .svg.actual
    monkeypatch.setenv("UPDATE_SNAPSHOTS", "1")
    vs.assert_svg_snapshot(snap_id, svg_v2)
    assert svg_path.read_text(encoding="utf-8") == svg_v2
    assert not actual_path.exists()

    # Clean match also removes any leftover .svg.actual
    monkeypatch.delenv("UPDATE_SNAPSHOTS", raising=False)
    actual_path.write_text(svg_v1, encoding="utf-8")
    assert actual_path.is_file()
    vs.assert_svg_snapshot(snap_id, svg_v2)
    assert not actual_path.exists()


def test_verify_svg_snapshot_first_time_generation_and_crlf_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify first-time baseline creation fails until verified and CRLF line endings normalize cleanly."""
    monkeypatch.delenv("UPDATE_SNAPSHOTS", raising=False)

    snap_id = "new_dialog"
    svg_path = tmp_path / f"{snap_id}.svg"
    stale_actual = tmp_path / f"{snap_id}.svg.actual"
    stale_actual.write_text("<svg>stale</svg>\n", encoding="utf-8")

    raw_crlf_svg = (
        '<svg id="terminal-abc123-root">\r\n'
        "  <text>12:34:56 C:\\dummy\\folder\\doc.pdf</text>\r\n"
        "</svg>\r\n"
    )

    with pytest.raises(pytest.fail.Exception, match="generated for the first time"):
        verify_svg_snapshot(snap_id, raw_crlf_svg, snapshot_dir=tmp_path)

    assert svg_path.is_file()
    assert not stale_actual.exists()
    stored = svg_path.read_text(encoding="utf-8")
    assert "\r" not in stored
    assert "terminal-test-root" in stored
    assert "00:00:00 /dummy/folder/doc.pdf" in stored

    # Subsequent comparison with CRLF baseline on disk passes cleanly
    svg_path.write_bytes(stored.replace("\n", "\r\n").encode("utf-8"))
    verify_svg_snapshot(snap_id, sanitize_svg(raw_crlf_svg), snapshot_dir=tmp_path)


def test_sanitize_svg_normalizes_windows_dummy_drive_text_length_and_row_rects() -> (
    None
):
    """Verify stripping Windows drive prefixes on /dummy paths updates textLength and row rects."""
    posix_svg = (
        '<svg id="terminal-111-root">\n'
        '  <clipPath id="terminal-111-line-9"><rect x="0" y="221.1" width="1220" height="24.65"/></clipPath>\n'
        '  <rect fill="#242f38" x="170.8" y="221.1" width="207.4" height="24.65" shape-rendering="crispEdges"/>'
        '<rect fill="#242f38" x="378.2" y="221.1" width="671" height="24.65" shape-rendering="crispEdges"/>\n'
        '  <text class="terminal-111-r1" x="170.8" y="239.6" textLength="207.4" clip-path="url(#terminal-111-line-9)">•&#160;/dummy/projects</text>\n'
        "</svg>\n"
    )
    windows_svg = (
        '<svg id="terminal-222-root">\n'
        '  <clipPath id="terminal-222-line-9"><rect x="0" y="221.1" width="1220" height="24.65"/></clipPath>\n'
        '  <rect fill="#242f38" x="170.8" y="221.1" width="231.8" height="24.65" shape-rendering="crispEdges"/>'
        '<rect fill="#242f38" x="402.6" y="221.1" width="646.6" height="24.65" shape-rendering="crispEdges"/>\n'
        '  <text class="terminal-222-r1" x="170.8" y="239.6" textLength="231.8" clip-path="url(#terminal-222-line-9)">•&#160;D:\\dummy\\projects</text>\n'
        "</svg>\n"
    )
    assert sanitize_svg(windows_svg) == sanitize_svg(posix_svg)
