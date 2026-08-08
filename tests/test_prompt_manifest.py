import json

import pytest

from scripts.prompt_manifest import (
    compute_sha256,
    generate,
    parse_and_validate_prompt,
    verify,
)


def test_parse_valid_frontmatter(tmp_path):
    """Test parsing a valid prompt markdown file with full frontmatter."""
    content = """---
model: gpt-4
temperature: 0.7
---
This is the prompt body.
With multiple lines.
"""
    file_path = tmp_path / "valid_agent.md"
    content = content.replace("\r\n", "\n")
    file_path.write_text(content, encoding="utf-8", newline="\n")

    frontmatter, body = parse_and_validate_prompt(file_path)

    assert frontmatter == {"model": "gpt-4", "temperature": 0.7}
    assert body == "This is the prompt body.\nWith multiple lines.\n"


def test_parse_valid_frontmatter_partial(tmp_path):
    """Test parsing a valid prompt with only model or only temperature."""
    # Only model
    file_path_1 = tmp_path / "model_only.md"
    file_path_1.write_text(
        "---\nmodel: gpt-3.5-turbo\n---\nBody text.", encoding="utf-8", newline="\n"
    )
    fm1, body1 = parse_and_validate_prompt(file_path_1)
    assert fm1 == {"model": "gpt-3.5-turbo"}
    assert body1 == "Body text."

    # Only temperature (float)
    file_path_2 = tmp_path / "temp_float.md"
    file_path_2.write_text(
        "---\ntemperature: 1.5\n---\nBody text.", encoding="utf-8", newline="\n"
    )
    fm2, body2 = parse_and_validate_prompt(file_path_2)
    assert fm2 == {"temperature": 1.5}
    assert body2 == "Body text."

    # Only temperature (int)
    file_path_3 = tmp_path / "temp_int.md"
    file_path_3.write_text(
        "---\ntemperature: 1\n---\nBody text.", encoding="utf-8", newline="\n"
    )
    fm3, body3 = parse_and_validate_prompt(file_path_3)
    assert fm3 == {"temperature": 1}
    assert body3 == "Body text."


def test_parse_empty_frontmatter(tmp_path):
    """Test parsing a prompt with empty frontmatter block."""
    file_path = tmp_path / "empty_fm.md"
    file_path.write_text("---\n---\nBody text.", encoding="utf-8", newline="\n")
    fm, body = parse_and_validate_prompt(file_path)
    assert fm == {}
    assert body == "Body text."


def test_parse_invalid_key(tmp_path):
    """Test that invalid frontmatter keys raise a ValueError."""
    content = """---
model: gpt-4
temperature: 0.7
invalid_key: true
---
Body text.
"""
    file_path = tmp_path / "invalid_key.md"
    content = content.replace("\r\n", "\n")
    file_path.write_text(content, encoding="utf-8", newline="\n")

    with pytest.raises(ValueError) as excinfo:
        parse_and_validate_prompt(file_path)
    assert "Invalid schema key 'invalid_key'" in str(excinfo.value)


def test_parse_invalid_model_type(tmp_path):
    """Test that invalid type for model (not string) raises a TypeError."""
    content = """---
model: 12345
---
Body text.
"""
    file_path = tmp_path / "invalid_model.md"
    content = content.replace("\r\n", "\n")
    file_path.write_text(content, encoding="utf-8", newline="\n")

    with pytest.raises(TypeError) as excinfo:
        parse_and_validate_prompt(file_path)
    assert "Invalid type for key 'model'" in str(excinfo.value)


def test_parse_invalid_temp_type(tmp_path):
    """Test that invalid type for temperature (e.g. string or boolean) raises a TypeError."""
    # String
    file_path_1 = tmp_path / "invalid_temp_str.md"
    file_path_1.write_text(
        "---\ntemperature: '0.7'\n---\nBody.", encoding="utf-8", newline="\n"
    )
    with pytest.raises(TypeError) as excinfo:
        parse_and_validate_prompt(file_path_1)
    assert "Invalid type for key 'temperature'" in str(excinfo.value)

    # Boolean (since True is an instance of int in Python, check explicitly)
    file_path_2 = tmp_path / "invalid_temp_bool.md"
    file_path_2.write_text(
        "---\ntemperature: true\n---\nBody.", encoding="utf-8", newline="\n"
    )
    with pytest.raises(TypeError) as excinfo:
        parse_and_validate_prompt(file_path_2)
    assert "Invalid type for key 'temperature'" in str(excinfo.value)


def test_parse_out_of_bounds_temperature(tmp_path):
    """Test that temperature values out of [0.0, 2.0] bounds raise a ValueError."""
    # Too low
    file_path_1 = tmp_path / "temp_too_low.md"
    file_path_1.write_text(
        "---\ntemperature: -0.1\n---\nBody.", encoding="utf-8", newline="\n"
    )
    with pytest.raises(ValueError) as excinfo:
        parse_and_validate_prompt(file_path_1)
    assert "out of bounds [0.0, 2.0]" in str(excinfo.value)

    # Too high
    file_path_2 = tmp_path / "temp_too_high.md"
    file_path_2.write_text(
        "---\ntemperature: 2.1\n---\nBody.", encoding="utf-8", newline="\n"
    )
    with pytest.raises(ValueError) as excinfo:
        parse_and_validate_prompt(file_path_2)
    assert "out of bounds [0.0, 2.0]" in str(excinfo.value)


def test_parse_backward_compatibility_no_frontmatter(tmp_path):
    """Test backward compatibility where a file has no frontmatter block."""
    content = """# Title
This is standard markdown.
No frontmatter at all.
"""
    file_path = tmp_path / "legacy.md"
    content = content.replace("\r\n", "\n")
    file_path.write_text(content, encoding="utf-8", newline="\n")

    frontmatter, body = parse_and_validate_prompt(file_path)
    assert frontmatter == {}
    assert body == content


def test_parse_backward_compatibility_unclosed_frontmatter(tmp_path):
    """Test backward compatibility where a file starts with '---' but has no closing '---'."""
    content = """---
model: gpt-4
temperature: 0.7
# Oops forgot to close frontmatter
This is standard markdown.
"""
    file_path = tmp_path / "unclosed.md"
    content = content.replace("\r\n", "\n")
    file_path.write_text(content, encoding="utf-8", newline="\n")

    frontmatter, body = parse_and_validate_prompt(file_path)
    assert frontmatter == {}
    assert body == content


def test_hash_frontmatter_decoupling(tmp_path):
    """Test that changing only frontmatter metadata parameters does not alter the generated hash,
    but changing body text does.
    """
    content_v1 = """---
model: gpt-4
temperature: 0.5
---
Hello World
"""
    file_path = tmp_path / "agent.md"
    content_v1 = content_v1.replace("\r\n", "\n")
    file_path.write_text(content_v1, encoding="utf-8", newline="\n")
    hash_v1 = compute_sha256(file_path)

    # Change only frontmatter parameters
    content_v2 = """---
model: claude-3-opus
temperature: 1.2
---
Hello World
"""
    content_v2 = content_v2.replace("\r\n", "\n")
    file_path.write_text(content_v2, encoding="utf-8", newline="\n")
    hash_v2 = compute_sha256(file_path)

    # Hashes must be identical
    assert hash_v1 == hash_v2

    # Change body text slightly (even 1 char)
    content_v3 = """---
model: claude-3-opus
temperature: 1.2
---
Hello World!
"""
    content_v3 = content_v3.replace("\r\n", "\n")
    file_path.write_text(content_v3, encoding="utf-8", newline="\n")
    hash_v3 = compute_sha256(file_path)

    # Hash must be different
    assert hash_v1 != hash_v3


def test_hash_line_ending_normalization(tmp_path):
    """Test that line endings (LF vs CRLF) in body are normalized to LF before hashing."""
    content_lf = "---\nmodel: gpt-4\n---\nLine 1\nLine 2\nLine 3\n"
    content_crlf = "---\r\nmodel: gpt-4\r\n---\r\nLine 1\r\nLine 2\r\nLine 3\r\n"

    file_lf = tmp_path / "lf.md"
    file_lf.write_text(content_lf, encoding="utf-8", newline="")

    file_crlf = tmp_path / "crlf.md"
    file_crlf.write_text(content_crlf, encoding="utf-8", newline="")

    assert compute_sha256(file_lf) == compute_sha256(file_crlf)


def test_parse_utf8_bom(tmp_path):
    """Test that files starting with a UTF-8 BOM are decoded correctly."""
    content = "\ufeff---\nmodel: gpt-4\ntemperature: 0.7\n---\nThis is the prompt body."
    file_path = tmp_path / "bom_agent.md"
    file_path.write_text(content, encoding="utf-8", newline="\n")

    frontmatter, body = parse_and_validate_prompt(file_path)
    assert frontmatter == {"model": "gpt-4", "temperature": 0.7}
    assert body == "This is the prompt body."


def test_end_to_end_generate_and_verify(tmp_path, monkeypatch):
    """Test full generate and verify cycle under happy path and failure paths."""
    # Setup temporary agents dir and manifest path
    agents_dir = tmp_path / "AGENTS"
    agents_dir.mkdir()
    manifest_path = agents_dir / "manifest.json"

    monkeypatch.setattr("scripts.prompt_manifest.AGENTS_DIR", agents_dir)
    monkeypatch.setattr("scripts.prompt_manifest.MANIFEST_PATH", manifest_path)

    # Create two agent prompts
    agent1 = agents_dir / "agent1.md"
    agent1.write_text(
        "---\nmodel: gpt-4\ntemperature: 0.5\n---\nHello from Agent 1\n",
        encoding="utf-8",
        newline="\n",
    )

    agent2 = agents_dir / "agent2.md"
    agent2.write_text(
        "---\nmodel: gpt-3.5-turbo\ntemperature: 1.0\n---\nHello from Agent 2\n",
        encoding="utf-8",
        newline="\n",
    )

    # Generate manifest
    generate()

    assert manifest_path.exists()
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest_data = json.load(f)

    assert "agent1.md" in manifest_data
    assert "agent2.md" in manifest_data

    # Verify happy path
    verify()  # Should complete without error/exit

    # Update only frontmatter, verification should still succeed!
    agent1.write_text(
        "---\nmodel: gpt-4-turbo\ntemperature: 1.5\n---\nHello from Agent 1\n",
        encoding="utf-8",
        newline="\n",
    )
    verify()  # Should succeed!

    # Update body of agent1, verification should fail
    agent1.write_text(
        "---\nmodel: gpt-4-turbo\ntemperature: 1.5\n---\nHello from Agent 1 - Modified!\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(SystemExit) as excinfo:
        verify()
    assert excinfo.value.code == 1


def test_verify_with_deleted_file(tmp_path, monkeypatch):
    """Test that verification fails when a file in manifest is deleted."""
    agents_dir = tmp_path / "AGENTS"
    agents_dir.mkdir()
    manifest_path = agents_dir / "manifest.json"

    monkeypatch.setattr("scripts.prompt_manifest.AGENTS_DIR", agents_dir)
    monkeypatch.setattr("scripts.prompt_manifest.MANIFEST_PATH", manifest_path)

    agent1 = agents_dir / "agent1.md"
    agent1.write_text("Hello 1", encoding="utf-8", newline="\n")

    generate()
    verify()  # Passes

    # Delete agent1.md
    agent1.unlink()

    with pytest.raises(SystemExit) as excinfo:
        verify()
    assert excinfo.value.code == 1


def test_verify_with_missing_manifest_file(tmp_path, monkeypatch):
    """Test that verification fails when a new file is not in the manifest."""
    agents_dir = tmp_path / "AGENTS"
    agents_dir.mkdir()
    manifest_path = agents_dir / "manifest.json"

    monkeypatch.setattr("scripts.prompt_manifest.AGENTS_DIR", agents_dir)
    monkeypatch.setattr("scripts.prompt_manifest.MANIFEST_PATH", manifest_path)

    agent1 = agents_dir / "agent1.md"
    agent1.write_text("Hello 1", encoding="utf-8", newline="\n")

    generate()

    # Create agent2.md after generation
    agent2 = agents_dir / "agent2.md"
    agent2.write_text("Hello 2", encoding="utf-8", newline="\n")

    with pytest.raises(SystemExit) as excinfo:
        verify()
    assert excinfo.value.code == 1


def test_main_cli(tmp_path, monkeypatch):
    """Test main entry point parsing and delegation."""
    from scripts.prompt_manifest import main

    agents_dir = tmp_path / "AGENTS"
    agents_dir.mkdir()
    manifest_path = agents_dir / "manifest.json"

    monkeypatch.setattr("scripts.prompt_manifest.AGENTS_DIR", agents_dir)
    monkeypatch.setattr("scripts.prompt_manifest.MANIFEST_PATH", manifest_path)

    agent1 = agents_dir / "agent1.md"
    agent1.write_text("Hello 1", encoding="utf-8", newline="\n")

    # Mock command line arguments for generate
    monkeypatch.setattr("sys.argv", ["prompt_manifest.py", "generate"])
    main()

    assert manifest_path.exists()

    # Mock command line arguments for verify
    monkeypatch.setattr("sys.argv", ["prompt_manifest.py", "verify"])
    main()  # Should succeed without throwing SystemExit or exception
