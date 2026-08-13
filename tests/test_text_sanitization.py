from app.core.extractor import extract_file_text
from app.core.text_utils import sanitize_text


def test_sanitize_text_html_and_xml():
    """Verify that standard XML/HTML tags are stripped and semantic text is preserved."""
    raw_html = (
        "<div>Hello <b>World</b>! Welcome to <span>text sanitization</span>.</div>"
    )
    assert sanitize_text(raw_html) == "Hello World! Welcome to text sanitization."

    raw_xml = (
        "<note>\n<to>User</to>\n<from>System</from>\n<body>Action items</body>\n</note>"
    )
    assert sanitize_text(raw_xml) == "User\nSystem\nAction items"


def test_sanitize_text_florence_location_tokens():
    """Verify that Florence-2 location tokens like <loc_123> or <loc_1234> are removed."""
    raw = "The object is at <loc_120> <loc_85> <loc_300> <loc_420> on the desk."
    assert sanitize_text(raw) == "The object is at on the desk."

    raw_mixed = "<loc_1>First<loc_22>Second<loc_333>Third<loc_4444>Fourth"
    assert sanitize_text(raw_mixed) == "FirstSecondThirdFourth"


def test_sanitize_text_florence_task_tags():
    """Verify that task-specific Florence-2 tags are completely stripped."""
    raw = "<OD> Bounding boxes of objects: <loc_100> <loc_200>"
    assert sanitize_text(raw) == "Bounding boxes of objects:"

    raw_all = (
        "<CAPTION_TO_PHRASE_GROUNDING> A cat on a <OCR_WITH_REGION_AND_BOX> chair."
    )
    assert sanitize_text(raw_all) == "A cat on a chair."


def test_sanitize_text_coordinate_arrays():
    """Verify that numeric coordinate arrays (including nested or float lists) are stripped."""
    raw_simple = "Receipt totals [100, 200, 300, 400] were analyzed."
    assert sanitize_text(raw_simple) == "Receipt totals were analyzed."

    raw_nested = "Coordinates [[10.5, 20.3], [30.1, 40.2]] correspond to the box."
    assert sanitize_text(raw_nested) == "Coordinates correspond to the box."

    raw_with_brackets = "Some list of indices [1, 2, 3, 4, 5]"
    assert sanitize_text(raw_with_brackets) == "Some list of indices"


def test_sanitize_text_whitespace_normalization():
    """Verify that horizontal whitespaces are collapsed, and vertical whitespaces normalized."""
    raw = "Hello   World!   \t  This is   a   test."
    assert sanitize_text(raw) == "Hello World! This is a test."

    raw_newlines = "Paragraph 1\n\n\nParagraph 2\n\n   \nParagraph 3"
    assert sanitize_text(raw_newlines) == "Paragraph 1\nParagraph 2\nParagraph 3"


def test_sanitize_text_status_strings():
    """Verify that standard status messages starting with [STATUS: and ending with ] are untouched."""
    statuses = [
        "[STATUS:EMPTY]",
        "[STATUS:SKIPPED]",
        "[STATUS:UNSUPPORTED]",
        "[STATUS:FAILED]",
        "[STATUS:ENCRYPTED]",
    ]
    for status in statuses:
        assert sanitize_text(status) == status


def test_extractor_level_sanitization_integration(tmp_path):
    """Verify that extract_file_text sanitizes raw text at ingestion synchronously."""
    test_file = tmp_path / "mixed_markup.txt"
    # Write text containing HTML tags, Florence location tokens, and coordinate arrays
    content = (
        "<div>Invoice Details</div>\n"
        "Region: <loc_150> <loc_200>\n"
        "Bbox: [150, 200, 350, 400]\n"
        "Task: <OD>\n"
        "Status: Active"
    )
    test_file.write_text(content, encoding="utf-8")

    result = extract_file_text(str(test_file))

    # The result should have all markup elements stripped, and whitespaces normalized
    expected = "Invoice Details\nRegion:\nBbox:\nTask:\nStatus: Active"
    assert result == expected
