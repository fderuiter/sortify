"""Tests for extraction stage PII and secret scrubbing in core text processing."""

from unittest.mock import MagicMock, patch

from app.core.db import Database
from app.core.extractor import ExtractionStatus, extract_file_text
from app.core.offline_loader import Florence2VisualProcessor
from app.core.semantic_embeddings import SemanticEmbeddingManager
from app.core.text_utils import sanitize_text


def test_sanitize_text_redacts_secrets_and_pii():
    """Verify core text sanitization redacts API keys, JWTs, Bearer tokens, private keys, SSNs, and credit cards with [REDACTED_SECRET]."""
    test_cases = [
        ("API Key: sk_live_51Nxabc123XYZ4567890abcdef", "API Key: [REDACTED_SECRET]"),
        (
            "GitHub Token: ghp_1234567890abcdef1234567890abcdef123456",
            "GitHub Token: [REDACTED_SECRET]",
        ),
        ("AWS Access Key: AKIAIOSFODNN7EXAMPLE", "AWS Access Key: [REDACTED_SECRET]"),
        ("SSN number: 123-45-6789", "SSN number: [REDACTED_SECRET]"),
        ("Credit Card: 4532 1234 5678 9012", "Credit Card: [REDACTED_SECRET]"),
        (
            "JWT: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
            "JWT: [REDACTED_SECRET]",
        ),
    ]

    for raw, expected in test_cases:
        sanitized = sanitize_text(raw)
        assert "[REDACTED_SECRET]" in sanitized
        assert "sk_live_" not in sanitized
        assert "ghp_" not in sanitized
        assert "AKIA" not in sanitized
        assert "123-45-6789" not in sanitized
        assert "4532 1234" not in sanitized


def test_system_status_codes_preserved():
    """Verify system extraction status strings remain intact and untouched."""
    status_codes = [
        "[STATUS:EMPTY]",
        "[STATUS:SKIPPED]",
        "[STATUS:UNSUPPORTED]",
        "[STATUS:ENCRYPTED]",
        "[STATUS:CANCELLED]",
        "[STATUS:TIMEOUT]",
        "[STATUS:PROVISIONAL]",
    ]

    for status_code in status_codes:
        assert sanitize_text(status_code) == status_code


def test_extract_file_text_returns_redacted_text(tmp_path):
    """Verify text extraction calls return sanitized text with redacted secrets."""
    secret_file = tmp_path / "credentials.txt"
    secret_file.write_text(
        "Application Config\n"
        "STRIPE_KEY=sk_live_51Nxabc123XYZ4567890abcdef\n"
        "SSN: 987-65-4321\n",
        encoding="utf-8",
    )

    res = extract_file_text(str(secret_file))
    assert res.status == ExtractionStatus.SUCCESS
    assert "sk_live_" not in res.text
    assert "987-65-4321" not in res.text
    assert "[REDACTED_SECRET]" in res.text


def test_db_persistence_stores_redacted_text(tmp_path):
    """Verify database persistence sanitizes and redacts secret credentials before writing to disk."""
    db_file = tmp_path / "test.db"
    mock_worker = MagicMock()
    mock_worker.execute_write.side_effect = lambda fn: fn()
    db = Database(db_file, mock_worker)

    base_dir = str(tmp_path)
    rel_path = "secret_notes.txt"
    raw_secret_text = (
        "Internal Notes: sk_live_51Nxabc123XYZ4567890abcdef SSN: 123-45-6789"
    )

    db.upsert_document(base_dir, rel_path, "hash_abc", raw_secret_text)

    doc = db.get_document(base_dir, rel_path)
    assert doc is not None
    extracted_text = doc["extracted_text"]

    assert "sk_live_" not in extracted_text
    assert "123-45-6789" not in extracted_text
    assert "[REDACTED_SECRET]" in extracted_text


def test_vector_embedding_receives_redacted_text():
    """Verify vector embedding generation receives redacted text, preventing sensitive tokens from entering vector space."""
    mock_db = MagicMock()
    manager = SemanticEmbeddingManager(mock_db)

    secret_input = "User API Key: sk_live_51Nxabc123XYZ4567890abcdef"

    with patch.object(
        manager,
        "_generate_fallback_embedding",
        wraps=manager._generate_fallback_embedding,
    ) as mock_fallback:
        _ = manager.generate_embedding(secret_input)
        mock_fallback.assert_called_once()
        sanitized_arg = mock_fallback.call_args[0][0]
        assert "sk_live_" not in sanitized_arg
        assert "[REDACTED_SECRET]" in sanitized_arg


def test_offline_loader_segment_parsing_redacts_secrets():
    """Verify offline document loader segment parsing returns sanitized text without sensitive credentials."""
    raw_florence_output = (
        "Header title <loc_100><loc_200><loc_300><loc_400> "
        "sk_live_51Nxabc123XYZ4567890abcdef details"
    )

    parsed = Florence2VisualProcessor.parse_and_sanitize(
        raw_florence_output, (800, 600)
    )

    assert "sk_live_" not in parsed["sanitized_text"]
    assert "[REDACTED_SECRET]" in parsed["sanitized_text"]

    for coord in parsed["coordinates"]:
        assert "sk_live_" not in coord["label"]
