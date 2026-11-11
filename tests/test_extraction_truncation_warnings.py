"""Unit tests for spreadsheet extraction truncation flags and notification event dispatching."""

import logging

import openpyxl

from app.config import Settings
from app.core.extractor import ExtractionResult, ExtractionStatus, extract_file_text
from app.core.extractor_strategies import CsvExtractor, XlsxExtractor
from app.ui.notifications import NotificationManager


def test_extraction_result_truncation_metadata_defaults():
    """Verify ExtractionResult initializes with safe truncation defaults and supports dict lookups."""
    # Default non-truncated state
    default_result = ExtractionResult(text="sample text", status=ExtractionStatus.SUCCESS)
    assert default_result.truncated is False
    assert default_result.truncation_reason is None
    assert default_result.truncated_bounds is None
    assert default_result["truncated"] is False
    assert default_result["truncation_reason"] is None
    assert default_result["truncated_bounds"] is None
    assert default_result.get("truncated") is False
    assert default_result.get("non_existent", "default_val") == "default_val"
    assert "truncated" in default_result
    assert "truncation_reason" in default_result
    assert "truncated_bounds" in default_result

    # Truncated state
    bounds = {"max_rows": 100, "max_characters": 500, "max_chars": 500}
    truncated_result = ExtractionResult(
        text="partial text",
        status=ExtractionStatus.SUCCESS,
        truncated=True,
        truncation_reason="MAX_ROWS_EXCEEDED",
        truncated_bounds=bounds,
    )
    assert truncated_result.truncated is True
    assert truncated_result.truncation_reason == "MAX_ROWS_EXCEEDED"
    assert truncated_result.truncated_bounds == bounds
    assert truncated_result["truncated"] is True
    assert truncated_result["truncation_reason"] == "MAX_ROWS_EXCEEDED"
    assert truncated_result["truncated_bounds"] == bounds


def test_csv_extractor_records_max_rows_truncation(tmp_path):
    """Verify CsvExtractor sets truncated=True and truncation_reason='MAX_ROWS_EXCEEDED' when row limit is reached."""
    csv_file = tmp_path / "large_dataset.csv"
    lines = ["id,value"] + [f"{i},data_{i}" for i in range(25)]
    csv_file.write_text("\n".join(lines), encoding="utf-8")

    extractor = CsvExtractor()
    settings = Settings(TABULAR_MAX_ROWS=5, TABULAR_MAX_CHARACTERS=100000)
    text = extractor.extract(str(csv_file), settings=settings)

    assert "id value" in text
    assert extractor.truncated is True
    assert extractor.truncation_reason == "MAX_ROWS_EXCEEDED"
    assert extractor.truncated_bounds is not None
    assert extractor.truncated_bounds["max_rows"] == 5


def test_csv_extractor_records_max_characters_truncation(tmp_path):
    """Verify CsvExtractor sets truncated=True and truncation_reason='MAX_CHARACTERS_EXCEEDED' when character limit is reached."""
    csv_file = tmp_path / "long_text.csv"
    lines = ["header_col"] + [f"this_is_a_very_long_string_row_{i}" for i in range(10)]
    csv_file.write_text("\n".join(lines), encoding="utf-8")

    extractor = CsvExtractor()
    settings = Settings(TABULAR_MAX_ROWS=1000, TABULAR_MAX_CHARACTERS=30)
    extractor.extract(str(csv_file), settings=settings)

    assert extractor.truncated is True
    assert extractor.truncation_reason == "MAX_CHARACTERS_EXCEEDED"
    assert extractor.truncated_bounds["max_characters"] == 30


def test_csv_extractor_not_truncated_when_within_bounds(tmp_path):
    """Verify CsvExtractor leaves truncated=False when content is within all limits."""
    csv_file = tmp_path / "small.csv"
    csv_file.write_text("a,b\n1,2\n3,4", encoding="utf-8")

    extractor = CsvExtractor()
    settings = Settings(TABULAR_MAX_ROWS=100, TABULAR_MAX_CHARACTERS=10000)
    extractor.extract(str(csv_file), settings=settings)

    assert extractor.truncated is False
    assert extractor.truncation_reason is None
    assert extractor.truncated_bounds is None


def test_xlsx_extractor_records_max_sheets_truncation(tmp_path):
    """Verify XlsxExtractor sets truncated=True and truncation_reason='MAX_SHEETS_EXCEEDED' when sheet limit is exceeded."""
    xlsx_file = tmp_path / "multi_sheet.xlsx"
    wb = openpyxl.Workbook()
    default_ws = wb.active
    default_ws.title = "Sheet1"
    default_ws.append(["Col1", "Col2"])
    default_ws.append(["Val1", "Val2"])

    for i in range(2, 6):
        ws = wb.create_sheet(title=f"Sheet{i}")
        ws.append([f"S{i}_C1", f"S{i}_C2"])
    wb.save(xlsx_file)
    wb.close()

    extractor = XlsxExtractor()
    settings = Settings(TABULAR_MAX_SHEETS=2, TABULAR_MAX_ROWS=100, TABULAR_MAX_CHARACTERS=100000)
    extractor.extract(str(xlsx_file), settings=settings)

    assert extractor.truncated is True
    assert extractor.truncation_reason == "MAX_SHEETS_EXCEEDED"
    assert extractor.truncated_bounds["max_sheets"] == 2


def test_xlsx_extractor_records_max_characters_truncation(tmp_path):
    """Verify XlsxExtractor sets truncated=True and truncation_reason='MAX_CHARACTERS_EXCEEDED' when character threshold is exceeded."""
    xlsx_file = tmp_path / "single_sheet.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Single"
    ws.append(["ColumnA", "ColumnB"])
    for i in range(10):
        ws.append([f"LongDetailedValue_{i}", f"MoreContent_{i}"])
    wb.save(xlsx_file)
    wb.close()

    extractor = XlsxExtractor()
    settings = Settings(TABULAR_MAX_SHEETS=10, TABULAR_MAX_ROWS=100, TABULAR_MAX_CHARACTERS=35)
    extractor.extract(str(xlsx_file), settings=settings)

    assert extractor.truncated is True
    assert extractor.truncation_reason == "MAX_CHARACTERS_EXCEEDED"
    assert extractor.truncated_bounds["max_characters"] == 35


def test_xlsx_extractor_records_max_rows_truncation(tmp_path):
    """Verify XlsxExtractor sets truncated=True and truncation_reason='MAX_ROWS_EXCEEDED' when rows exceed threshold."""
    xlsx_file = tmp_path / "many_rows.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Rows"
    ws.append(["Header1", "Header2"])
    for i in range(20):
        ws.append([f"Row_{i}", f"Val_{i}"])
    wb.save(xlsx_file)
    wb.close()

    extractor = XlsxExtractor()
    settings = Settings(TABULAR_MAX_SHEETS=10, TABULAR_MAX_ROWS=4, TABULAR_MAX_CHARACTERS=100000)
    extractor.extract(str(xlsx_file), settings=settings)

    assert extractor.truncated is True
    assert extractor.truncation_reason == "MAX_ROWS_EXCEEDED"
    assert extractor.truncated_bounds["max_rows"] == 4


def test_extract_file_text_logs_warning_and_dispatches_notification_for_csv(tmp_path, caplog):
    """Verify extract_file_text logs a structured warning and triggers NotificationManager.notify on truncated CSV."""
    csv_file = tmp_path / "financial_records.csv"
    lines = ["account,balance"] + [f"ACC_{i},{1000 + i}" for i in range(30)]
    csv_file.write_text("\n".join(lines), encoding="utf-8")

    notif_events = []
    notif_mgr = NotificationManager.get_instance()
    notif_mgr.register_handler(notif_events.append)

    try:
        settings = Settings(TABULAR_MAX_ROWS=5, TABULAR_MAX_CHARACTERS=100000)
        with caplog.at_level(logging.WARNING):
            result = extract_file_text(str(csv_file), settings=settings)

        assert result.truncated is True
        assert result.truncation_reason == "MAX_ROWS_EXCEEDED"
        assert result.truncated_bounds["max_rows"] == 5

        # Verify structured warning log
        warning_logs = [record for record in caplog.records if record.levelno >= logging.WARNING]
        assert len(warning_logs) >= 1
        assert any("Tabular extraction truncated" in rec.message and "MAX_ROWS_EXCEEDED" in rec.message for rec in warning_logs)

        # Verify NotificationManager.notify dispatched a warning event
        assert len(notif_events) >= 1
        last_event = notif_events[-1]
        assert last_event["type"] == "warning"
        assert "financial_records.csv" in last_event["message"]
        assert "MAX_ROWS_EXCEEDED" in last_event["message"]
    finally:
        if notif_events.append in notif_mgr._custom_handlers:
            notif_mgr._custom_handlers.remove(notif_events.append)


def test_extract_file_text_logs_warning_and_dispatches_notification_for_xlsx(tmp_path, caplog):
    """Verify extract_file_text logs warning and triggers NotificationManager.notify on multi-sheet XLSX truncation."""
    xlsx_file = tmp_path / "project_sheets.xlsx"
    wb = openpyxl.Workbook()
    default_ws = wb.active
    default_ws.title = "Overview"
    default_ws.append(["Project", "Status"])
    default_ws.append(["Alpha", "Active"])

    for s_idx in range(2, 6):
        ws = wb.create_sheet(title=f"Phase_{s_idx}")
        ws.append([f"Task_{s_idx}", "Pending"])
    wb.save(xlsx_file)
    wb.close()

    notif_events = []
    notif_mgr = NotificationManager.get_instance()
    notif_mgr.register_handler(notif_events.append)

    try:
        settings = Settings(TABULAR_MAX_SHEETS=2, TABULAR_MAX_ROWS=100, TABULAR_MAX_CHARACTERS=100000)
        with caplog.at_level(logging.WARNING):
            result = extract_file_text(str(xlsx_file), settings=settings)

        assert result.truncated is True
        assert result.truncation_reason == "MAX_SHEETS_EXCEEDED"
        assert result.truncated_bounds["max_sheets"] == 2

        # Verify warning log
        warning_logs = [record for record in caplog.records if record.levelno >= logging.WARNING]
        assert any("MAX_SHEETS_EXCEEDED" in rec.message for rec in warning_logs)

        # Verify NotificationManager dispatched event
        assert len(notif_events) >= 1
        event = notif_events[-1]
        assert event["type"] == "warning"
        assert "project_sheets.xlsx" in event["message"]
        assert "MAX_SHEETS_EXCEEDED" in event["message"]
    finally:
        if notif_events.append in notif_mgr._custom_handlers:
            notif_mgr._custom_handlers.remove(notif_events.append)


def test_extract_file_text_non_truncated_no_warning_no_notification(tmp_path, caplog):
    """Verify non-truncated spreadsheets do not emit warnings or dispatch notifications."""
    csv_file = tmp_path / "clean.csv"
    csv_file.write_text("x,y\n1,2\n3,4", encoding="utf-8")

    notif_events = []
    notif_mgr = NotificationManager.get_instance()
    notif_mgr.register_handler(notif_events.append)

    try:
        settings = Settings(TABULAR_MAX_ROWS=1000, TABULAR_MAX_CHARACTERS=10000)
        with caplog.at_level(logging.WARNING):
            result = extract_file_text(str(csv_file), settings=settings)

        assert result.truncated is False
        assert result.truncation_reason is None
        assert result.truncated_bounds is None

        # Verify no truncation warnings logged
        truncation_warnings = [
            rec for rec in caplog.records
            if rec.levelno >= logging.WARNING and "Tabular extraction truncated" in rec.message
        ]
        assert len(truncation_warnings) == 0

        # Verify no notification events dispatched
        assert len(notif_events) == 0
    finally:
        if notif_events.append in notif_mgr._custom_handlers:
            notif_mgr._custom_handlers.remove(notif_events.append)


def test_xlsx_pandas_fallback_truncation(mocker):
    """Verify XlsxExtractor sets truncation state properly in pandas fallback path."""
    mock_excel_file = mocker.patch("pandas.ExcelFile")
    mock_xl_instance = mock_excel_file.return_value
    mock_xl_instance.sheet_names = ["Sheet1", "Sheet2", "Sheet3"]

    mock_pd_read = mocker.patch("pandas.read_excel")
    mock_df = mocker.MagicMock()
    mock_df.to_string.return_value = "Sample tabular row data"
    mock_pd_read.return_value = mock_df

    extractor = XlsxExtractor()
    settings = Settings(TABULAR_MAX_SHEETS=2, TABULAR_MAX_ROWS=100, TABULAR_MAX_CHARACTERS=100000)
    extractor.extract("non_existent_file.xlsx", settings=settings)

    assert extractor.truncated is True
    assert extractor.truncation_reason == "MAX_SHEETS_EXCEEDED"
    assert extractor.truncated_bounds["max_sheets"] == 2
