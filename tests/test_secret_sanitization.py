import time
from unittest.mock import MagicMock, patch

from app.core.analyzer_strategies import GenerativeNamingStrategy
from app.core.policy_engine import PolicyEngine
from app.core.quarantine_interceptor import scrub_pii_from_text
from app.core.text_utils import (
    contains_secrets,
    sanitize_secret_patterns,
)


def test_contains_secrets_and_sanitization_formats():
    """Verify secret pattern detection and sanitization across API keys, JWTs, Bearer tokens, private keys, SSNs, credit cards, and high-entropy tokens."""
    secrets = [
        "sk_live_51Nxabc123XYZ4567890abcdef",
        "ghp_1234567890abcdef1234567890abcdef123456",
        "AKIAIOSFODNN7EXAMPLE",
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC...\n-----END PRIVATE KEY-----",
        "123-45-6789",
        "4532 1234 5678 9012",
        "a8F93b12C4d5E6f7A8b9C0d1e2f3a4b5",
    ]

    for secret in secrets:
        assert contains_secrets(secret) is True, f"Failed to detect secret in: {secret}"
        sanitized = sanitize_secret_patterns(secret)
        assert not contains_secrets(sanitized), f"Sanitized text still contains secret: {sanitized}"


def test_generative_naming_strategy_secret_scrubbing_and_fallback():
    """Verify GenerativeNamingStrategy._get_cluster_keywords scrubs secret patterns and falls back to TF-IDF when title length < 2."""
    strategy = GenerativeNamingStrategy()
    strategy._model_initialized = True
    strategy.generator = MagicMock()
    strategy.stop_words = {"the", "and"}
    strategy.max_features = 3

    docs = ["Financial report for fiscal year 2026", "Quarterly earnings and tax summaries"]

    # 1. Full secret title -> scrubbed to < 2 chars -> triggers fallback to TF-IDF super()._get_cluster_keywords(docs)
    with patch.object(strategy, "_run_prompt", return_value="sk_live_51Nxabc123XYZ4567890abcdef"):
        folder_name = strategy._get_cluster_keywords(docs)
        # Should fall back to TF-IDF keywords
        assert folder_name != "sk_live_51Nxabc123XYZ4567890abcdef"
        assert not contains_secrets(folder_name)
        assert len(folder_name) >= 2

    # 2. Mixed title containing secret + legitimate words -> secret scrubbed, remaining words preserved
    with patch.object(strategy, "_run_prompt", return_value="Project ghp_1234567890abcdef1234567890abcdef123456 Reports"):
        folder_name = strategy._get_cluster_keywords(docs)
        assert folder_name == "Project Reports"
        assert not contains_secrets(folder_name)

    # 3. AWS Key title -> scrubbed to < 2 chars -> fallback
    with patch.object(strategy, "_run_prompt", return_value="AKIAIOSFODNN7EXAMPLE"):
        folder_name = strategy._get_cluster_keywords(docs)
        assert not contains_secrets(folder_name)
        assert folder_name != "AKIAIOSFODNN7EXAMPLE"


def test_quarantine_interceptor_secret_redaction():
    """Verify scrub_pii_from_text in quarantine_interceptor redacts credentials using [REDACTED_SECRET]."""
    raw_doc = "API Key: sk_live_51Nxabc123XYZ4567890abcdef\nSSN: 123-45-6789"
    scrubbed = scrub_pii_from_text(raw_doc)

    assert "sk_live_" not in scrubbed
    assert "[REDACTED_SECRET]" in scrubbed
    assert "[REDACTED_SSN]" in scrubbed


def test_policy_engine_secret_matching():
    """Verify PolicyEngine reuses shared secret detection logic."""
    doc_with_secret = "Configuration file containing Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    doc_clean = "Standard project documentation without credentials"

    assert PolicyEngine.contains_secrets(doc_with_secret) is True
    assert PolicyEngine.contains_secrets(doc_clean) is False

    policies = [
        {
            "type": "secret",
            "expression": "secret",
            "target_path": "Quarantine/Secrets",
            "priority": 100,
        }
    ]

    rule_match = PolicyEngine.evaluate_policies("config.env", doc_with_secret, None, policies)
    assert rule_match is not None
    assert rule_match["target_path"] == "Quarantine/Secrets"

    rule_no_match = PolicyEngine.evaluate_policies("readme.md", doc_clean, None, policies)
    assert rule_no_match is None


def test_secret_sanitization_performance():
    """Verify pattern matching logic runs in under 5ms per title."""
    candidate_titles = [
        "Project sk_live_51Nxabc123XYZ4567890abcdef Reports",
        "Quarterly Financial Statements 2026",
        "ghp_1234567890abcdef1234567890abcdef123456 Notes",
        "Medical Records Phase 3",
        "AKIAIOSFODNN7EXAMPLE Data",
    ] * 20  # 100 titles

    start_time = time.perf_counter()
    for title in candidate_titles:
        _ = sanitize_secret_patterns(title)
    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    avg_ms = elapsed_ms / len(candidate_titles)
    assert avg_ms < 5.0, f"Average execution time {avg_ms:.3f}ms exceeded 5ms limit"


def test_legitimate_vocabulary_preserved():
    """Verify legitimate folder names and English vocabulary are preserved intact."""
    legitimate_titles = [
        "Quarterly Financial Statements",
        "Project Alpha Documentation 2026",
        "Clinical Trial Phase 3 Data",
        "Human Resources Payroll Summaries",
    ]

    for title in legitimate_titles:
        assert contains_secrets(title) is False
        assert sanitize_secret_patterns(title) == title
