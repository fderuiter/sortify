import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.generate_docs import convert_notebook_to_markdown, main


def test_all_notebooks_execution():
    """Verify that all interactive notebooks execute cleanly without errors."""
    notebooks_dir = Path("notebooks")
    nb_files = sorted(notebooks_dir.glob("*.ipynb"))
    assert len(nb_files) >= 3, f"Expected at least 3 notebooks, found {len(nb_files)}"

    for nb_path in nb_files:
        with open(nb_path, "r", encoding="utf-8") as f:
            nb = json.load(f)

        global_env = {"__file__": str(nb_path.resolve()), "__name__": "__main__"}
        code_cells = []
        for cell in nb.get("cells", []):
            if cell.get("cell_type") == "code":
                source = cell.get("source", [])
                cell_code = "".join(source) if isinstance(source, list) else str(source)
                if cell_code.strip():
                    code_cells.append(cell_code)

        full_code = "\n\n".join(code_cells)
        try:
            exec(compile(full_code, str(nb_path), "exec"), global_env)
        finally:
            from app.core.db_conn import clear_connection_cache

            clear_connection_cache(only_current_and_inactive=False)


def test_convert_notebook_to_markdown(tmp_path):
    """Test converting a notebook dict structure to markdown."""
    nb_content = {
        "cells": [
            {
                "cell_type": "markdown",
                "source": ["# Test Title\n", "This is a tutorial."],
            },
            {
                "cell_type": "code",
                "source": ["x = 10\n", "print(x)"],
            },
        ]
    }
    nb_file = tmp_path / "test.ipynb"
    md_file = tmp_path / "test.md"

    with open(nb_file, "w", encoding="utf-8") as f:
        json.dump(nb_content, f)

    convert_notebook_to_markdown(nb_file, md_file)

    assert md_file.exists()
    content = md_file.read_text(encoding="utf-8")
    assert "# Test Title" in content
    assert "This is a tutorial." in content
    assert "```python\nx = 10\nprint(x)\n```" in content


def test_notebook_drift_detection_fails_on_drift(tmp_path):
    """Test that docs pipeline --check detects drift in notebook tutorial files."""
    # Run generate_docs with --check and verify sys.exit(1) when a generated notebook file is unsynced
    with patch("sys.argv", ["generate_docs.py", "--check"]):
        with (
            patch("scripts.generate_docs.generate_api_docs"),
            patch("scripts.generate_docs.generate_ui_docs"),
            patch("scripts.generate_docs.generate_admin_guide"),
            patch("scripts.generate_docs.update_security_md"),
            patch("subprocess.run") as mock_run,
            patch("sys.exit") as mock_exit,
        ):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="Mock CLI help text\n"
            )

            # Alter one of the tutorial markdown files temporarily to simulate drift
            tutorial_file = Path("docs/tutorials/01_ml_analyzer_clustering.md")
            original_content = tutorial_file.read_text(encoding="utf-8")
            try:
                tutorial_file.write_text("DRIFT_OUT_OF_SYNC_CONTENT", encoding="utf-8")

                main()

                mock_exit.assert_called_once_with(1)
            finally:
                tutorial_file.write_text(original_content, encoding="utf-8")
