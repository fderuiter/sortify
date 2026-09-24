"""Text sanitization and cleaning utilities for ingested content."""

import math
import re

# 1. Florence-2/VLM Location Tokens: e.g., <loc_150>, <loc_12>
# Matches <loc_integer> where integer has 1 to 4 digits
FLORENCE_LOC_PATTERN = re.compile(r"<loc_\d{1,4}>")

# 2. General Coordinate Arrays:
# Matches single or nested brackets enclosing only numbers, commas, periods, and whitespace.
# e.g., [100, 150, 200, 250] or [[100, 150], [200, 250]]
COORD_ARRAY_PATTERN = re.compile(r"\[\s*(?:\[[\s\d.,]+\]|[\s\d.,])+\s*\]")

# 3. HTML and XML tag pattern: e.g., <div>, </span>, <OD>, <OCR_WITH_REGION_AND_BOX>
# Matches any sequence starting with < and ending with > which doesn't contain > in between.
HTML_XML_TAG_PATTERN = re.compile(
    r"</?[a-zA-Z0-9_\-:]+(?:\s+[a-zA-Z0-9_\-:]+=(?:\"[^\"]*\"|'[^']*'|[^'\">\s]+))*\s*/?>"
)

# 4. Truncated trailing VLM/HTML tags pattern: matches incomplete tags cut off at the end of text
TRUNCATED_TAG_PATTERN = re.compile(r"<[a-zA-Z0-9_\-:/]*$")

# 5. Whitespace patterns
HORIZONTAL_WHITESPACE_PATTERN = re.compile(r"[ \t]+")
VERTICAL_WHITESPACE_PATTERN = re.compile(r"\s*\n\s*")

# 6. Centralized Secret & Credential Patterns
SECRET_KEY_PATTERNS = [
    # Stripe / General sk_ live or test keys
    re.compile(r"\bsk_(?:live|test)_[a-zA-Z0-9]{20,}\b"),
    # GitHub Tokens (ghp_, gho_, ghu_, ghs_, ghr_)
    re.compile(r"\bgh[pousr]_[a-zA-Z0-9]{36}\b"),
    # AWS Access Key ID (AKIA or ASIA followed by 16 alphanumeric characters)
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # AWS Secret Access Key or generic secret key key-value pairs
    re.compile(
        r"\b(?:aws_secret_access_key|api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*[a-zA-Z0-9_\-/+=]{16,}\b",
        re.IGNORECASE,
    ),
    # Mailgun / Generic API key format (e.g. key-3ax6...)
    re.compile(r"\bkey-[a-zA-Z0-9]{32}\b"),
    # Generic sk_ key format
    re.compile(r"\bsk_[a-zA-Z0-9_]{20,}\b"),
]

BEARER_TOKEN_PATTERN = re.compile(
    r"\bBearer\s+[a-zA-Z0-9_\-\.=]{16,}\b", re.IGNORECASE
)
JWT_PATTERN = re.compile(
    r"\beyJ[a-zA-Z0-9_\=-]+\.eyJ[a-zA-Z0-9_\=-]+\.[a-zA-Z0-9_\=-]+\b"
)
PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN\s+(?:[A-Z0-9_-]+\s+)?PRIVATE\s+KEY-----[\s\S]*?-----END\s+(?:[A-Z0-9_-]+\s+)?PRIVATE\s+KEY-----|"
    r"-----BEGIN\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----",
    re.IGNORECASE,
)
SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CREDIT_CARD_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,16}\b")


def calculate_shannon_entropy(text: str) -> float:
    """Calculate Shannon entropy (in bits per symbol) of a string."""
    if not text:
        return 0.0
    length = len(text)
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    entropy = 0.0
    for count in counts.values():
        p = count / length
        entropy -= p * math.log2(p)
    return entropy


def _is_high_entropy_token(token: str) -> bool:
    """Check if a single word/token exhibits high Shannon entropy indicative of secret credentials."""
    clean_token = token.strip(".,;:\"'()[]{}<>!@#$%^&*+=/")
    if len(clean_token) < 20:
        return False
    has_digit = any(c.isdigit() for c in clean_token)
    has_lower = any(c.islower() for c in clean_token)
    has_upper = any(c.isupper() for c in clean_token)

    if (
        (has_digit and (has_lower or has_upper))
        or (has_lower and has_upper)
        or len(clean_token) >= 32
    ):
        entropy = calculate_shannon_entropy(clean_token)
        if entropy >= 3.8 and len(clean_token) >= 24:
            return True
        if entropy >= 4.2 and len(clean_token) >= 20:
            return True
    return False


def contains_secrets(text: str) -> bool:
    """Check if text contains API keys, JWT tokens, Bearer tokens, private keys, SSNs, credit cards, or high-entropy secrets."""
    if not isinstance(text, str) or not text:
        return False

    for pat in SECRET_KEY_PATTERNS:
        if pat.search(text):
            return True
    if BEARER_TOKEN_PATTERN.search(text):
        return True
    if JWT_PATTERN.search(text):
        return True
    if PRIVATE_KEY_PATTERN.search(text):
        return True
    if SSN_PATTERN.search(text):
        return True

    for match in CREDIT_CARD_PATTERN.finditer(text):
        digits = "".join(c for c in match.group(0) if c.isdigit())
        if 13 <= len(digits) <= 16:
            return True

    words = text.split()
    for word in words:
        if _is_high_entropy_token(word):
            return True

    return False


def sanitize_secret_patterns(text: str, replacement: str = "") -> str:
    """Scrub or replace secret tokens and API credentials from string text.

    Scrubs API keys (sk_live_, ghp_, AKIA), Bearer tokens, JWT strings,
    private keys, SSNs, credit card numbers, and high-entropy secret tokens.
    """
    if not isinstance(text, str) or not text:
        return text if text is not None else ""

    result = text

    result = PRIVATE_KEY_PATTERN.sub(replacement, result)
    result = JWT_PATTERN.sub(replacement, result)
    result = BEARER_TOKEN_PATTERN.sub(replacement, result)

    for pat in SECRET_KEY_PATTERNS:
        result = pat.sub(replacement, result)

    result = SSN_PATTERN.sub(replacement, result)

    def _card_replacer(match: re.Match) -> str:
        s = match.group(0)
        digits = "".join(c for c in s if c.isdigit())
        if 13 <= len(digits) <= 16:
            return replacement
        return s

    result = CREDIT_CARD_PATTERN.sub(_card_replacer, result)

    words = result.split()
    new_words = []
    modified = False
    for word in words:
        if _is_high_entropy_token(word):
            if replacement:
                new_words.append(replacement)
            modified = True
        else:
            new_words.append(word)

    if modified:
        result = " ".join(new_words)

    if not replacement:
        result = " ".join(result.split())

    return result


def sanitize_text(text: str) -> str:
    """Sanitize extracted text synchronously during the extraction lifecycle.

    This function strips:
    - Florence-2/VLM location tokens (e.g., <loc_150>)
    - Numeric coordinate arrays in brackets (e.g., [100, 150, 200, 250])
    - General HTML/XML tags (e.g., <div>, <OD>)
    It then normalizes whitespaces and preserves standard status messages.
    """
    if not text:
        return text

    # Standard status strings (e.g., [STATUS:EMPTY], [STATUS:SKIPPED]) are returned as-is
    if text.startswith("[STATUS:") and text.endswith("]"):
        return text

    # Strip Florence-2 location tokens
    text = FLORENCE_LOC_PATTERN.sub("", text)

    # Strip numeric coordinate arrays
    text = COORD_ARRAY_PATTERN.sub("", text)

    # Strip generic HTML / XML tags and Florence-2 task tags
    text = HTML_XML_TAG_PATTERN.sub("", text)

    # Strip truncated trailing VLM/HTML tag at the very end of the text stream
    text_rstrip = text.rstrip()
    if TRUNCATED_TAG_PATTERN.search(text_rstrip):
        text = TRUNCATED_TAG_PATTERN.sub("", text_rstrip)

    # Normalize horizontal whitespaces (spaces, tabs)
    text = HORIZONTAL_WHITESPACE_PATTERN.sub(" ", text)

    # Normalize multiple newlines to a single newline
    text = VERTICAL_WHITESPACE_PATTERN.sub("\n", text)

    return text.strip()

