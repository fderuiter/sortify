"""Tests for interactive CLI onboarding workspace selection prompt."""

import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.native_dialog import NativeDialogError
from tests.test_cli_subcommands import create_sample_corpus


def extract_json(output):
    """Extract and parse JSON object from combined terminal output."""
    start = output.find("{")
    end = output.rfind("}")
    if start != -1 and end != -1:
        return json.loads(output[start : end + 1])
    raise ValueError(f"No JSON object found in output: {output}")


def run_interactive_cli(args, inputs=None, env=None, is_tty=True):
    """Run CLI with simulated interactive terminal and simulated user inputs."""
    old_env = os.environ.copy()
    repo_root = str(Path(__file__).parent.parent.resolve())
    os.environ["PYTHONPATH"] = repo_root + os.pathsep + os.environ.get("PYTHONPATH", "")

    if env:
        os.environ.update(env)

    stdout_cap = io.StringIO()
    stderr_cap = io.StringIO()
    test_args = ["main.py"] + args

    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = is_tty
    mock_stdin.encoding = "utf-8"

    code = 0
    input_iter = iter(inputs or [])

    def mock_input(prompt=""):
        # Write prompt to stdout just like real interactive terminal input does
        stdout_cap.write(prompt)
        try:
            return next(input_iter)
        except StopIteration:
            raise EOFError("Simulated EOF") from None

    with (
        patch("sys.argv", test_args),
        patch("sys.stdout", stdout_cap),
        patch("sys.stderr", stderr_cap),
        patch("sys.stdin", mock_stdin),
        patch("builtins.input", side_effect=mock_input),
    ):
        try:
            from app.main import main

            main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        except Exception as e:
            stderr_cap.write(str(e))
            code = 1
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    return code, stdout_cap.getvalue(), stderr_cap.getvalue()


@pytest.mark.xdist_group(name="interactive_onboarding")
def test_interactive_sort_displays_guided_setup_prompt():
    """Verify sortify sort in interactive terminal displays guided setup prompt."""
    code, stdout, stderr = run_interactive_cli(["sort"], inputs=["q"])
    assert code == 1
    assert "Sortify - Workspace Onboarding" in stdout
    assert "No target directory specified" in stdout
    assert "[1] Demo Workspace" in stdout
    assert "[2] Downloads" in stdout
    assert "[3] Documents" in stdout
    assert "[4] Native Folder Picker" in stdout
    assert "[5] Custom Directory" in stdout
    assert "[q] Quit" in stdout
    assert "Select an option [1-5, q]:" in stdout


@pytest.mark.xdist_group(name="interactive_onboarding")
def test_interactive_scan_displays_guided_setup_prompt():
    """Verify sortify scan in interactive terminal displays guided setup prompt."""
    code, stdout, stderr = run_interactive_cli(["scan"], inputs=["q"])
    assert code == 1
    assert "Sortify - Workspace Onboarding" in stdout
    assert "[1] Demo Workspace" in stdout
    assert "[4] Native Folder Picker" in stdout


@pytest.mark.xdist_group(name="interactive_onboarding")
def test_select_demo_preset_from_prompt(tmp_path, monkeypatch):
    """Verify selecting Demo Workspace preset from prompt sets workspace path to sandbox/demo_workspace."""
    monkeypatch.chdir(tmp_path)
    demo_dir = tmp_path / "sandbox" / "demo_workspace"
    create_sample_corpus(demo_dir)

    code, stdout, stderr = run_interactive_cli(["sort", "--json"], inputs=["1"])
    assert code == 0, f"Expected 0, got {code}. Stderr: {stderr}"
    data = extract_json(stdout)
    assert data["status"] == "success"
    assert Path(data["target_directory"]).resolve() == demo_dir.resolve()


@pytest.mark.xdist_group(name="interactive_onboarding")
def test_select_demo_preset_by_name(tmp_path, monkeypatch):
    """Verify entering 'demo' string selects Demo Workspace preset."""
    monkeypatch.chdir(tmp_path)
    demo_dir = tmp_path / "sandbox" / "demo_workspace"
    create_sample_corpus(demo_dir)

    code, stdout, stderr = run_interactive_cli(["sort", "--json"], inputs=["demo"])
    assert code == 0, f"Expected 0, got {code}. Stderr: {stderr}"
    data = extract_json(stdout)
    assert Path(data["target_directory"]).resolve() == demo_dir.resolve()


@pytest.mark.xdist_group(name="interactive_onboarding")
def test_select_native_directory_invoked():
    """Verify selecting native folder browser invokes select_native_directory() and accepts target."""
    with tempfile.TemporaryDirectory() as target_dir:
        create_sample_corpus(target_dir)

        with patch("app.core.native_dialog.select_native_directory") as mock_native:
            mock_native.return_value = target_dir
            code, stdout, stderr = run_interactive_cli(["sort", "--json"], inputs=["4"])
            assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
            mock_native.assert_called_once()
            data = extract_json(stdout)
            assert (
                Path(data["target_directory"]).resolve() == Path(target_dir).resolve()
            )


@pytest.mark.xdist_group(name="interactive_onboarding")
def test_select_custom_path_verified_existence(tmp_path):
    """Verify selecting custom path verifies existence and executes sorting."""
    workspace = tmp_path / "my_custom_workspace"
    create_sample_corpus(workspace)

    code, stdout, stderr = run_interactive_cli(
        ["sort", "--json"], inputs=["5", str(workspace)]
    )
    assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
    data = extract_json(stdout)
    assert Path(data["target_directory"]).resolve() == workspace.resolve()


@pytest.mark.xdist_group(name="interactive_onboarding")
def test_select_custom_path_nonexistent_repairs_or_prompts(tmp_path):
    """Verify entering a non-existent path displays an error and re-prompts until valid."""
    nonexistent = tmp_path / "does_not_exist"
    valid = tmp_path / "valid_workspace"
    create_sample_corpus(valid)

    # First attempt non-existent, then supply valid workspace
    code, stdout, stderr = run_interactive_cli(
        ["sort", "--json"], inputs=["5", str(nonexistent), "5", str(valid)]
    )
    assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
    assert "does not exist or is not a directory" in stderr
    data = extract_json(stdout)
    assert Path(data["target_directory"]).resolve() == valid.resolve()


@pytest.mark.xdist_group(name="interactive_onboarding")
def test_non_interactive_flag_exits_immediately_code_1():
    """Verify running sortify sort --non-interactive without directory parameters exits immediately with code 1."""
    code, stdout, stderr = run_interactive_cli(
        ["sort", "--non-interactive"], is_tty=True
    )
    assert code == 1
    assert "Either target directory or --preset must be specified." in stderr
    assert "Sortify - Workspace Onboarding" not in stdout


@pytest.mark.xdist_group(name="interactive_onboarding")
def test_non_interactive_env_exits_immediately_code_1():
    """Verify NON_INTERACTIVE=1 env var causes immediate exit with status 1."""
    code, stdout, stderr = run_interactive_cli(
        ["sort"], env={"NON_INTERACTIVE": "1"}, is_tty=True
    )
    assert code == 1
    assert "Either target directory or --preset must be specified." in stderr
    assert "Sortify - Workspace Onboarding" not in stdout


@pytest.mark.xdist_group(name="interactive_onboarding")
def test_headless_non_tty_exits_immediately_code_1():
    """Verify headless/piped non-tty execution without directory parameters exits with code 1."""
    code, stdout, stderr = run_interactive_cli(["sort"], is_tty=False)
    assert code == 1
    assert "Either target directory or --preset must be specified." in stderr
    assert "Sortify - Workspace Onboarding" not in stdout


@pytest.mark.xdist_group(name="interactive_onboarding")
def test_quiet_flag_suppresses_banners():
    """Verify --quiet flag suppresses decorative banner and introductory text."""
    code, stdout, stderr = run_interactive_cli(["sort", "-q"], inputs=["q"])
    assert code == 1
    assert "Sortify - Workspace Onboarding" not in stdout
    assert "No target directory specified" not in stdout
    assert "[1] Demo Workspace" in stdout


@pytest.mark.xdist_group(name="interactive_onboarding")
def test_no_color_flag_omits_ansi_sequences():
    """Verify --no-color flag strips ANSI styling escape sequences from prompt output."""
    code, stdout, stderr = run_interactive_cli(["sort", "--no-color"], inputs=["q"])
    assert code == 1
    assert "\x1b[" not in stdout
    assert "\033[" not in stdout
    assert "Sortify - Workspace Onboarding" in stdout


@pytest.mark.xdist_group(name="interactive_onboarding")
def test_native_dialog_cancellation_re_prompts():
    """Verify native dialog returning None displays cancellation notice and allows another choice."""
    with patch("app.core.native_dialog.select_native_directory", return_value=None):
        code, stdout, stderr = run_interactive_cli(["sort"], inputs=["4", "q"])
        assert code == 1
        assert "Directory selection cancelled." in stderr


@pytest.mark.xdist_group(name="interactive_onboarding")
def test_native_dialog_error_handled_gracefully():
    """Verify NativeDialogError displays warning and remains in prompt loop."""
    with patch(
        "app.core.native_dialog.select_native_directory",
        side_effect=NativeDialogError("Zenity not installed"),
    ):
        code, stdout, stderr = run_interactive_cli(["sort"], inputs=["4", "q"])
        assert code == 1
        assert "Native folder dialog unavailable: Zenity not installed." in stderr
