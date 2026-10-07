"""Unit tests for sidecar tag manager module app/core/sidecar_tags.py."""

import json

from app.core.sidecar_tags import (
    get_sidecar_path,
    load_all_sidecar_tags_for_dir,
    load_sidecar_tags,
    parse_tags_input,
    save_sidecar_tags,
)


def test_parse_tags_input():
    assert parse_tags_input("Reviewed 2026, Tax, Urgent") == [
        "Reviewed 2026",
        "Tax",
        "Urgent",
    ]
    assert parse_tags_input("  Tag 1 , Tag 2 , Tag 1 ") == ["Tag 1", "Tag 2"]
    assert parse_tags_input(["Tag 1 ", " Tag 2", ""]) == ["Tag 1", "Tag 2"]
    assert parse_tags_input("") == []


def test_get_sidecar_path(tmp_path):
    doc_path = tmp_path / "invoice.pdf"
    doc_path.touch()
    sidecar_path = get_sidecar_path(doc_path)
    assert sidecar_path == tmp_path / ".sortify_tags.json"


def test_load_and_save_sidecar_tags(tmp_path):
    doc_path = tmp_path / "tax_2026.pdf"
    doc_path.touch()

    # Initially no tags
    assert load_sidecar_tags(doc_path) == []

    # Save tags
    tags = ["Reviewed 2026", "Tax"]
    save_sidecar_tags(doc_path, tags)

    # Verify loaded tags
    assert load_sidecar_tags(doc_path) == tags

    # Verify sidecar file content on disk
    sidecar_file = tmp_path / ".sortify_tags.json"
    assert sidecar_file.exists()
    with open(sidecar_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data == {"tax_2026.pdf": ["Reviewed 2026", "Tax"]}


def test_load_different_formats(tmp_path):
    sidecar_file = tmp_path / ".sortify_tags.json"
    content = {
        "doc1.pdf": ["TagA", "TagB"],
        "doc2.pdf": {"tags": ["TagC", "TagD"]},
        "doc3.pdf": "SingleTag",
    }
    with open(sidecar_file, "w", encoding="utf-8") as f:
        json.dump(content, f)

    assert load_sidecar_tags(tmp_path / "doc1.pdf") == ["TagA", "TagB"]
    assert load_sidecar_tags(tmp_path / "doc2.pdf") == ["TagC", "TagD"]
    assert load_sidecar_tags(tmp_path / "doc3.pdf") == ["SingleTag"]

    all_tags = load_all_sidecar_tags_for_dir(tmp_path)
    assert all_tags["doc1.pdf"] == ["TagA", "TagB"]
    assert all_tags["doc2.pdf"] == ["TagC", "TagD"]
    assert all_tags["doc3.pdf"] == ["SingleTag"]


def test_clear_tags(tmp_path):
    doc_path = tmp_path / "doc.pdf"
    doc_path.touch()

    save_sidecar_tags(doc_path, ["TempTag"])
    assert load_sidecar_tags(doc_path) == ["TempTag"]

    # Clear tags
    save_sidecar_tags(doc_path, [])
    assert load_sidecar_tags(doc_path) == []

    sidecar_file = tmp_path / ".sortify_tags.json"
    with open(sidecar_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "doc.pdf" not in data
