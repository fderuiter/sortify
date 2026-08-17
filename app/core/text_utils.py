"""Text sanitization and cleaning utilities for ingested content."""

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
