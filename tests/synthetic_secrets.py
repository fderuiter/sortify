"""Runtime builders for synthetic test credentials that avoid static secret scanner hits.

Redaction and sanitization tests import helpers from this module so literal secret
prefixes never appear in test source files and never require edits to `.gitleaks.toml`.
"""

from __future__ import annotations

import base64
import json


def make_stripe_key(
    suffix: str = "51Nxabc123XYZ4567890abcdef",
    *,
    mode: str = "live",
) -> str:
    """Build a synthetic Stripe secret key token at runtime."""
    prefix = "".join(("s", "k", "_", mode, "_"))
    return f"{prefix}{suffix}"


def make_github_pat(
    suffix: str = "1234567890abcdef1234567890abcdef123456",
    *,
    token_kind: str = "p",
) -> str:
    """Build a synthetic GitHub personal/app access token at runtime."""
    prefix = "".join(("g", "h", token_kind, "_"))
    return f"{prefix}{suffix}"


def make_aws_access_key(
    suffix: str = "IOSFODNN7ABCDEFG",
    *,
    prefix: str = "AKIA",
) -> str:
    """Build a synthetic AWS access key ID at runtime."""
    head = f"{prefix[:2]}{prefix[2:]}"
    return f"{head}{suffix}"


def make_mailgun_key(suffix: str = "1234567890abcdef1234567890abcdef") -> str:
    """Build a synthetic Mailgun-style API key at runtime."""
    head = "".join(("k", "e", "y", "-"))
    return f"{head}{suffix}"


def _b64url_json(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_jwt(
    subject: str = "1234567890",
    signature: str = "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
) -> str:
    """Build a synthetic three-part JWT string at runtime."""
    header = _b64url_json({"alg": "HS256", "typ": "JWT"})
    claims = _b64url_json(
        {"iat": 1516239022, "name": "Synthetic Probe", "sub": subject}
    )
    return ".".join((header, claims, signature))


def make_bearer_token(token: str | None = None) -> str:
    """Build a synthetic Authorization Bearer token header value at runtime."""
    actual = token if token is not None else make_jwt()
    return " ".join(("Bearer", actual))


def make_private_key_pem(
    body: str = "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC7synthetic0123456789",
    *,
    algorithm: str = "",
) -> str:
    """Build a synthetic PEM private key block at runtime."""
    alg_part = f"{algorithm.strip()} " if algorithm.strip() else ""
    begin_marker = "".join(("-----BEGIN ", alg_part, "PRIVATE", " KEY-----"))
    end_marker = "".join(("-----END ", alg_part, "PRIVATE", " KEY-----"))
    return f"{begin_marker}\n{body}\n{end_marker}"


def make_high_entropy_secret(seed: str = "SyntheticProbe2026") -> str:
    """Build a deterministic high-entropy alphanumeric secret token at runtime."""
    alphabet = "aB3cD5eF7gH9iJ1kL2mN4pQ6rS8tU0vW"
    mixed = "".join(
        alphabet[(idx * 7 + ord(ch)) % len(alphabet)] for idx, ch in enumerate(seed * 2)
    )
    return mixed[:32]


def make_key_value_secret(
    key: str = "api_key",
    value: str | None = None,
) -> str:
    """Build a synthetic `key=value` credential assignment at runtime."""
    secret_val = value if value is not None else make_stripe_key()
    return f"{key}={secret_val}"
