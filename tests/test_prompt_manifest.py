import json
import re
from pathlib import Path

import pytest
import yaml

from scripts.prompt_manifest import (
    PINNED_PUBLIC_KEY_HEX,
    compute_sha256,
    generate,
    generate_keypair,
    keygen,
    parse_and_validate_prompt,
    serialize_manifest_payload,
    sign_manifest_hashes,
    verify,
    verify_manifest_signature,
)


@pytest.fixture
def ed25519_keypair() -> tuple[str, str]:
    """Generate an ephemeral Ed25519 keypair dynamically per test."""
    return generate_keypair()


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


def test_keygen_generates_valid_ed25519_keypair(capsys):
    """Test keygen generates distinct valid 32-byte hex Ed25519 keypairs and prints CLI env lines."""
    priv1, pub1 = keygen()
    captured = capsys.readouterr()

    assert len(priv1) == 64
    assert len(pub1) == 64
    assert bytes.fromhex(priv1) != bytes.fromhex(pub1)
    assert f"PROMPT_MANIFEST_PRIVATE_KEY={priv1}" in captured.out
    assert f"PINNED_PUBLIC_KEY_HEX={pub1}" in captured.out

    priv2, pub2 = generate_keypair()
    assert priv1 != priv2
    assert pub1 != pub2

    hashes = {"agent.md": "a" * 64}
    sig = sign_manifest_hashes(hashes, priv1)
    assert len(sig) == 128
    assert verify_manifest_signature(hashes, sig, pub1) is True
    assert verify_manifest_signature(hashes, sig, pub2) is False


def test_end_to_end_generate_and_verify(tmp_path, monkeypatch, ed25519_keypair):
    """Test full generate and verify cycle under happy path and failure paths."""
    private_key_hex, public_key_hex = ed25519_keypair

    agents_dir = tmp_path / "AGENTS"
    agents_dir.mkdir()
    manifest_path = agents_dir / "manifest.json"

    monkeypatch.setattr("scripts.prompt_manifest.AGENTS_DIR", agents_dir)
    monkeypatch.setattr("scripts.prompt_manifest.MANIFEST_PATH", manifest_path)
    monkeypatch.setattr("scripts.prompt_manifest.PINNED_PUBLIC_KEY_HEX", public_key_hex)
    monkeypatch.setenv("PROMPT_MANIFEST_PRIVATE_KEY", private_key_hex)

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

    assert "_signature" in manifest_data
    assert len(manifest_data["_signature"]) == 128
    assert "agent1.md" in manifest_data
    assert "agent2.md" in manifest_data

    # Verify happy path without needing private key in environment
    monkeypatch.delenv("PROMPT_MANIFEST_PRIVATE_KEY", raising=False)
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


def test_verify_with_deleted_file(tmp_path, monkeypatch, ed25519_keypair):
    """Test that verification fails when a file in manifest is deleted."""
    private_key_hex, public_key_hex = ed25519_keypair
    agents_dir = tmp_path / "AGENTS"
    agents_dir.mkdir()
    manifest_path = agents_dir / "manifest.json"

    monkeypatch.setattr("scripts.prompt_manifest.AGENTS_DIR", agents_dir)
    monkeypatch.setattr("scripts.prompt_manifest.MANIFEST_PATH", manifest_path)
    monkeypatch.setattr("scripts.prompt_manifest.PINNED_PUBLIC_KEY_HEX", public_key_hex)
    monkeypatch.setenv("PROMPT_MANIFEST_PRIVATE_KEY", private_key_hex)

    agent1 = agents_dir / "agent1.md"
    agent1.write_text("Hello 1", encoding="utf-8", newline="\n")

    generate()
    verify()  # Passes

    # Delete agent1.md
    agent1.unlink()

    with pytest.raises(SystemExit) as excinfo:
        verify()
    assert excinfo.value.code == 1


def test_verify_with_missing_manifest_file(tmp_path, monkeypatch, ed25519_keypair):
    """Test that verification fails when a new file is not in the manifest."""
    private_key_hex, public_key_hex = ed25519_keypair
    agents_dir = tmp_path / "AGENTS"
    agents_dir.mkdir()
    manifest_path = agents_dir / "manifest.json"

    monkeypatch.setattr("scripts.prompt_manifest.AGENTS_DIR", agents_dir)
    monkeypatch.setattr("scripts.prompt_manifest.MANIFEST_PATH", manifest_path)
    monkeypatch.setattr("scripts.prompt_manifest.PINNED_PUBLIC_KEY_HEX", public_key_hex)
    monkeypatch.setenv("PROMPT_MANIFEST_PRIVATE_KEY", private_key_hex)

    agent1 = agents_dir / "agent1.md"
    agent1.write_text("Hello 1", encoding="utf-8", newline="\n")

    generate()

    # Create agent2.md after generation
    agent2 = agents_dir / "agent2.md"
    agent2.write_text("Hello 2", encoding="utf-8", newline="\n")

    with pytest.raises(SystemExit) as excinfo:
        verify()
    assert excinfo.value.code == 1


def test_main_cli(tmp_path, monkeypatch, capsys):
    """Test main CLI entry point for keygen, generate, and verify."""
    from scripts.prompt_manifest import main

    agents_dir = tmp_path / "AGENTS"
    agents_dir.mkdir()
    manifest_path = agents_dir / "manifest.json"

    monkeypatch.setattr("scripts.prompt_manifest.AGENTS_DIR", agents_dir)
    monkeypatch.setattr("scripts.prompt_manifest.MANIFEST_PATH", manifest_path)
    monkeypatch.delenv("PROMPT_MANIFEST_PRIVATE_KEY", raising=False)

    # CLI keygen
    monkeypatch.setattr("sys.argv", ["prompt_manifest.py", "keygen"])
    main()
    keygen_out = capsys.readouterr().out
    kv = dict(
        line.split("=", 1) for line in keygen_out.strip().splitlines() if "=" in line
    )
    private_key_hex = kv["PROMPT_MANIFEST_PRIVATE_KEY"]
    public_key_hex = kv["PINNED_PUBLIC_KEY_HEX"]

    agent1 = agents_dir / "agent1.md"
    agent1.write_text("Hello 1", encoding="utf-8", newline="\n")

    # CLI generate with --private-key
    monkeypatch.setattr(
        "sys.argv",
        ["prompt_manifest.py", "generate", "--private-key", private_key_hex],
    )
    main()

    assert manifest_path.exists()
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest_data = json.load(f)
    assert "_signature" in manifest_data

    # CLI verify with --public-key and --strict
    monkeypatch.setattr(
        "sys.argv",
        ["prompt_manifest.py", "verify", "--public-key", public_key_hex, "--strict"],
    )
    main()

    # CLI verify using PINNED_PUBLIC_KEY_HEX without CLI key argument
    monkeypatch.setattr("scripts.prompt_manifest.PINNED_PUBLIC_KEY_HEX", public_key_hex)
    monkeypatch.setattr("sys.argv", ["prompt_manifest.py", "verify", "--strict"])
    main()


def test_sign_manifest_hashes_deterministic_sorting(ed25519_keypair):
    """Test that Ed25519 signing sorts filenames alphabetically and is deterministic."""
    private_key_hex, public_key_hex = ed25519_keypair
    hashes_order_1 = {
        "b_agent.md": "hash_b",
        "a_agent.md": "hash_a",
        "c_agent.md": "hash_c",
    }
    hashes_order_2 = {
        "c_agent.md": "hash_c",
        "a_agent.md": "hash_a",
        "b_agent.md": "hash_b",
    }
    # Including an existing _signature in the dict should not affect computation
    hashes_with_sig = {
        "_signature": "old_sig",
        "a_agent.md": "hash_a",
        "b_agent.md": "hash_b",
        "c_agent.md": "hash_c",
    }

    sig1 = sign_manifest_hashes(hashes_order_1, private_key_hex)
    sig2 = sign_manifest_hashes(hashes_order_2, private_key_hex)
    sig3 = sign_manifest_hashes(hashes_with_sig, private_key_hex)

    assert sig1 == sig2
    assert sig1 == sig3
    assert verify_manifest_signature(hashes_order_1, sig1, public_key_hex) is True


def test_serialize_manifest_payload_line_ending_normalization(ed25519_keypair):
    """Test that CRLF line endings normalize to LF and Ed25519 produces 128-char hex signatures."""
    private_key_hex, public_key_hex = ed25519_keypair
    hashes = {"agent.md": "hash123\r\n"}
    payload = serialize_manifest_payload(hashes)
    assert b"\r" not in payload
    sig = sign_manifest_hashes({"agent.md": "hash123"}, private_key_hex)
    assert isinstance(sig, str)
    assert len(sig) == 128  # 64-byte Ed25519 signature hex length
    assert verify_manifest_signature({"agent.md": "hash123"}, sig, public_key_hex)


def test_generate_fails_without_private_key(tmp_path, monkeypatch):
    """Test that generate fails with exit code 1 if PROMPT_MANIFEST_PRIVATE_KEY is missing."""
    agents_dir = tmp_path / "AGENTS"
    agents_dir.mkdir()
    manifest_path = agents_dir / "manifest.json"

    monkeypatch.setattr("scripts.prompt_manifest.AGENTS_DIR", agents_dir)
    monkeypatch.setattr("scripts.prompt_manifest.MANIFEST_PATH", manifest_path)
    monkeypatch.delenv("PROMPT_MANIFEST_PRIVATE_KEY", raising=False)

    agent1 = agents_dir / "agent1.md"
    agent1.write_text("Hello 1", encoding="utf-8", newline="\n")

    with pytest.raises(SystemExit) as excinfo:
        generate()
    assert excinfo.value.code == 1
    assert not manifest_path.exists()


def test_generate_fails_with_malformed_private_key_without_leaking(
    tmp_path, monkeypatch, capsys
):
    """Test that malformed private keys fail cleanly without echoing key material."""
    agents_dir = tmp_path / "AGENTS"
    agents_dir.mkdir()
    manifest_path = agents_dir / "manifest.json"

    monkeypatch.setattr("scripts.prompt_manifest.AGENTS_DIR", agents_dir)
    monkeypatch.setattr("scripts.prompt_manifest.MANIFEST_PATH", manifest_path)

    agent1 = agents_dir / "agent1.md"
    agent1.write_text("Hello 1", encoding="utf-8", newline="\n")

    bad_input = "not-valid-hex-" + ("z" * 20)
    with pytest.raises(SystemExit) as excinfo:
        generate(private_key=bad_input)
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert bad_input not in captured.err
    assert bad_input not in captured.out
    assert not manifest_path.exists()


def test_verify_fails_with_tampered_signature(tmp_path, monkeypatch, ed25519_keypair):
    """Test that verification fails when _signature is tampered, malformed, or non-string."""
    private_key_hex, public_key_hex = ed25519_keypair
    agents_dir = tmp_path / "AGENTS"
    agents_dir.mkdir()
    manifest_path = agents_dir / "manifest.json"

    monkeypatch.setattr("scripts.prompt_manifest.AGENTS_DIR", agents_dir)
    monkeypatch.setattr("scripts.prompt_manifest.MANIFEST_PATH", manifest_path)
    monkeypatch.setattr("scripts.prompt_manifest.PINNED_PUBLIC_KEY_HEX", public_key_hex)

    agent1 = agents_dir / "agent1.md"
    agent1.write_text("Hello 1", encoding="utf-8", newline="\n")

    generate(private_key=private_key_hex)
    with open(manifest_path, "r", encoding="utf-8") as f:
        valid_manifest = json.load(f)

    # Flip one nibble in valid 128-char hex signature
    orig_sig = valid_manifest["_signature"]
    flipped_char = "0" if orig_sig[0] != "0" else "1"
    tampered_sig = flipped_char + orig_sig[1:]

    for bad_sig in (tampered_sig, "not_hex_signature", 12345):
        manifest_data = dict(valid_manifest)
        manifest_data["_signature"] = bad_sig
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=4)

        with pytest.raises(SystemExit) as excinfo:
            verify()
        assert excinfo.value.code == 1


def test_verify_fails_with_tampered_prompt_and_manifest_hash(
    tmp_path, monkeypatch, ed25519_keypair
):
    """Test security threat model: attacker alters prompt AND updates manifest hash
    (either keeping original signature or re-signing with an unpinned keypair).
    """
    maintainer_priv, maintainer_pub = ed25519_keypair
    attacker_priv, _ = generate_keypair()

    agents_dir = tmp_path / "AGENTS"
    agents_dir.mkdir()
    manifest_path = agents_dir / "manifest.json"

    monkeypatch.setattr("scripts.prompt_manifest.AGENTS_DIR", agents_dir)
    monkeypatch.setattr("scripts.prompt_manifest.MANIFEST_PATH", manifest_path)
    monkeypatch.setattr("scripts.prompt_manifest.PINNED_PUBLIC_KEY_HEX", maintainer_pub)

    agent1 = agents_dir / "agent1.md"
    agent1.write_text("Original prompt text", encoding="utf-8", newline="\n")

    generate(private_key=maintainer_priv)

    # Case 1: Contributor tampers with prompt and updates SHA-256 hash without re-signing
    agent1.write_text(
        "Tampered prompt text by PR author", encoding="utf-8", newline="\n"
    )
    new_hash = compute_sha256(agent1)

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest_data = json.load(f)
    manifest_data["agent1.md"] = new_hash
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=4)

    with pytest.raises(SystemExit) as excinfo:
        verify()
    assert excinfo.value.code == 1

    # Case 2: Contributor re-signs manifest with their own Ed25519 keypair (not pinned)
    generate(private_key=attacker_priv)
    with pytest.raises(SystemExit) as excinfo:
        verify()
    assert excinfo.value.code == 1


def test_verify_fails_with_missing_signature_unconditionally(
    tmp_path, monkeypatch, ed25519_keypair
):
    """Test that verification fails when _signature is missing even when CI='' and strict=False."""
    private_key_hex, public_key_hex = ed25519_keypair
    agents_dir = tmp_path / "AGENTS"
    agents_dir.mkdir()
    manifest_path = agents_dir / "manifest.json"

    monkeypatch.setattr("scripts.prompt_manifest.AGENTS_DIR", agents_dir)
    monkeypatch.setattr("scripts.prompt_manifest.MANIFEST_PATH", manifest_path)
    monkeypatch.setattr("scripts.prompt_manifest.PINNED_PUBLIC_KEY_HEX", public_key_hex)
    monkeypatch.delenv("PROMPT_MANIFEST_PRIVATE_KEY", raising=False)

    agent1 = agents_dir / "agent1.md"
    agent1.write_text("Hello 1", encoding="utf-8", newline="\n")

    generate(private_key=private_key_hex)

    # Remove signature key
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest_data = json.load(f)
    del manifest_data["_signature"]
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=4)

    for ci_val, strict_flag in (
        ("", False),
        ("", True),
        ("true", False),
        ("true", True),
    ):
        monkeypatch.setenv("CI", ci_val)
        with pytest.raises(SystemExit) as excinfo:
            verify(strict=strict_flag)
        assert excinfo.value.code == 1


def test_committed_repository_manifest_and_ci_guardrails(monkeypatch):
    """Verify the committed .github/AGENTS/manifest.json signature and strict CI/hook configuration."""
    repo_root = Path(__file__).resolve().parent.parent
    committed_agents_dir = repo_root / ".github" / "AGENTS"
    committed_manifest = committed_agents_dir / "manifest.json"

    assert committed_manifest.exists(), (
        f"Missing committed manifest: {committed_manifest}"
    )
    manifest_data = json.loads(committed_manifest.read_text(encoding="utf-8"))
    assert "_signature" in manifest_data, (
        "Committed manifest.json must include _signature"
    )
    assert isinstance(manifest_data["_signature"], str)
    assert len(manifest_data["_signature"]) == 128
    assert "merge-conflicts.md" in manifest_data
    assert "pr-gatekeeper.md" in manifest_data
    assert len(PINNED_PUBLIC_KEY_HEX) == 64

    monkeypatch.setattr("scripts.prompt_manifest.AGENTS_DIR", committed_agents_dir)
    monkeypatch.setattr("scripts.prompt_manifest.MANIFEST_PATH", committed_manifest)
    monkeypatch.delenv("PROMPT_MANIFEST_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("CI", "")

    # Must pass strictly with only the committed public key and zero secrets
    verify(strict=True)

    # Verify CI workflow runs strict verification unconditionally without secret/CI="" bypass
    ci_workflow = yaml.safe_load(
        (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    static_steps = ci_workflow["jobs"]["static-checks"]["steps"]
    manifest_step = next(
        (s for s in static_steps if s.get("name") == "Validate Agent Prompts Manifest"),
        None,
    )
    assert manifest_step is not None
    assert "env" not in manifest_step or "PROMPT_MANIFEST_SECRET" not in (
        manifest_step.get("env") or {}
    )
    step_run = manifest_step.get("run", "")
    assert "scripts/prompt_manifest.py verify --strict" in step_run
    assert 'CI=""' not in step_run
    assert "PROMPT_MANIFEST_SECRET" not in step_run

    # Verify pre-commit hook runs strict verification and watches scripts/prompt_manifest.py
    pre_commit_cfg = yaml.safe_load(
        (repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    local_repo = next(r for r in pre_commit_cfg["repos"] if r.get("repo") == "local")
    prompt_hook = next(
        h for h in local_repo["hooks"] if h.get("id") == "validate-agent-prompts"
    )
    assert "scripts/prompt_manifest.py verify --strict" in prompt_hook["entry"]
    files_pattern = re.compile(prompt_hook["files"])
    assert files_pattern.search(".github/AGENTS/manifest.json")
    assert files_pattern.search(".github/AGENTS/pr-gatekeeper.md")
    assert files_pattern.search("scripts/prompt_manifest.py")
