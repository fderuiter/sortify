import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.cli.generate_docs import main

pytestmark = [pytest.mark.slow, pytest.mark.integration]


def test_all_tutorials_execution():
    """Verify that all markdown tutorial code snippets execute cleanly without errors."""
    tutorials_dir = Path("docs/tutorials")
    md_files = sorted(tutorials_dir.glob("*.md"))
    assert len(md_files) >= 3, f"Expected at least 3 tutorials, found {len(md_files)}"

    for md_path in md_files:
        content = md_path.read_text(encoding="utf-8")
        code_blocks = re.findall(r"```python\s*([\s\S]*?)```", content)
        assert code_blocks, f"No Python code blocks found in {md_path}"

        global_env = {"__file__": str(md_path.resolve()), "__name__": "__main__"}
        full_code = "\n\n".join(code_blocks)

        try:
            exec(compile(full_code, str(md_path), "exec"), global_env)
        finally:
            from app.core.db_conn import clear_connection_cache

            clear_connection_cache(only_current_and_inactive=False)


def test_docs_drift_detection_fails_on_drift(tmp_path):
    """Test that docs pipeline --check detects drift in generated doc files."""
    with patch("sys.argv", ["generate_docs.py", "--check"]):
        with (
            patch("app.cli.generate_docs.generate_tutorial_docs"),
            patch("app.cli.generate_docs.generate_ui_docs"),
            patch("app.cli.generate_docs.generate_admin_guide"),
            patch("app.cli.generate_docs.update_security_md"),
            patch("subprocess.run") as mock_run,
            patch("sys.exit") as mock_exit,
        ):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="Mock CLI help text\n"
            )

            api_file = Path("docs/api_reference.md")
            original_content = (
                api_file.read_text(encoding="utf-8") if api_file.exists() else ""
            )
            try:
                api_file.write_text("DRIFT_OUT_OF_SYNC_CONTENT", encoding="utf-8")

                main()

                mock_exit.assert_called_once_with(1)
            finally:
                if original_content:
                    api_file.write_text(original_content, encoding="utf-8")
                elif api_file.exists():
                    api_file.unlink()
