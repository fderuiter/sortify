import os
from unittest.mock import MagicMock, patch

from scripts.generate_docs import (
    audit_handwritten_docs,
    get_handwritten_docs,
    main,
    validate_mermaid_diagrams,
)


def test_validate_mermaid_diagrams_clean():
    errors = validate_mermaid_diagrams()
    assert errors == []


def test_validate_mermaid_diagrams_detects_invalid_type(tmp_path):
    bad_doc = tmp_path / "bad_mermaid.md"
    bad_doc.write_text("```mermaid\nunknownDiagramType\nA --> B\n```\n")

    with patch("os.walk", return_value=[(str(tmp_path), [], ["bad_mermaid.md"])]):
        errors = validate_mermaid_diagrams()
        assert len(errors) == 1
        assert "unknown diagram type 'unknownDiagramType'" in errors[0]


def test_validate_mermaid_diagrams_detects_unbalanced_brackets(tmp_path):
    bad_doc = tmp_path / "bad_brackets.md"
    bad_doc.write_text("```mermaid\nflowchart TD\nA[Unclosed bracket --> B\n```\n")

    with patch("os.walk", return_value=[(str(tmp_path), [], ["bad_brackets.md"])]):
        errors = validate_mermaid_diagrams()
        assert len(errors) == 1
        assert "unbalanced brackets" in errors[0]


def test_get_handwritten_docs():
    docs = get_handwritten_docs()
    assert "README.md" in docs
    assert "PRIVACY.md" in docs
    assert any("docs/" in d for d in docs)
    assert all("\\" not in d for d in docs)
    # Ensure generated files are excluded
    assert "docs/api_reference.md" not in docs
    assert "docs/ui.md" not in docs
    assert "docs/admin_guide.md" not in docs
    assert "SECURITY.md" not in docs


def test_audit_handwritten_docs_clean():
    errors = audit_handwritten_docs()
    assert errors == []


def test_audit_handwritten_docs_detects_invalid_path(tmp_path):
    bad_doc = tmp_path / "bad_guide.md"
    bad_doc.write_text(
        "Reference to nonexistent file: `app/nonexistent_module_xyz123.py`\n"
    )

    with patch(
        "scripts.generate_docs.get_handwritten_docs", return_value=[str(bad_doc)]
    ):
        errors = audit_handwritten_docs()
        assert len(errors) == 1
        assert "Invalid codebase path reference" in errors[0]
        assert "app/nonexistent_module_xyz123.py" in errors[0]


def test_audit_handwritten_docs_detects_broken_link(tmp_path):
    bad_doc = tmp_path / "bad_link_doc.md"
    bad_url = "https://" + "example.invalid/404_not_found"
    bad_doc.write_text(f"Broken link: {bad_url}\n")

    with (
        patch(
            "scripts.generate_docs.get_handwritten_docs", return_value=[str(bad_doc)]
        ),
        patch(
            "scripts.validate_links.validate_url",
            return_value=(False, "HTTP Error 404: Not Found", True),
        ),
    ):
        errors = audit_handwritten_docs()
        assert len(errors) == 1
        assert "Broken external link" in errors[0]
        assert bad_url in errors[0]


def test_main_strict_flag():
    """Verify that --strict (default) and --no-strict set mkdocs arguments correctly."""
    with patch("sys.argv", ["generate_docs.py", "--no-strict"]):
        with (
            patch("scripts.generate_docs.compile_diagram_assets") as mock_diag,
            patch("scripts.generate_docs.generate_tutorial_docs") as mock_tut,
            patch("scripts.generate_docs.generate_api_docs") as mock_api,
            patch("scripts.generate_docs.generate_ui_docs") as mock_ui,
            patch("scripts.generate_docs.generate_admin_guide") as mock_admin,
            patch("scripts.generate_docs.update_security_md") as mock_sec,
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            main()

            # Verify generators are called
            mock_diag.assert_called_once()
            mock_tut.assert_called_once()
            mock_api.assert_called_once()
            mock_ui.assert_called_once()
            mock_admin.assert_called_once()
            mock_sec.assert_called_once()

            # Verify build command does NOT have --strict
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            cmd = args[0]
            assert "--strict" not in cmd


def test_main_default_strict():
    """Verify that by default mkdocs is run with --strict."""
    with patch("sys.argv", ["generate_docs.py"]):
        with (
            patch("scripts.generate_docs.compile_diagram_assets") as mock_diag,
            patch("scripts.generate_docs.generate_tutorial_docs") as mock_tut,
            patch("scripts.generate_docs.generate_api_docs") as mock_api,
            patch("scripts.generate_docs.generate_ui_docs") as mock_ui,
            patch("scripts.generate_docs.generate_admin_guide") as mock_admin,
            patch("scripts.generate_docs.update_security_md") as mock_sec,
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            main()

            mock_diag.assert_called_once()
            mock_tut.assert_called_once()
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            cmd = args[0]
            assert "--strict" in cmd


def test_main_detects_unsynced_files_on_check():
    """Verify that --check detects modified files and exits with code 1."""
    original_open = open
    with patch("sys.argv", ["generate_docs.py", "--check"]):
        with (
            patch("scripts.generate_docs.compile_diagram_assets"),
            patch("scripts.generate_docs.generate_tutorial_docs"),
            patch("scripts.generate_docs.generate_api_docs"),
            patch("scripts.generate_docs.generate_ui_docs"),
            patch("scripts.generate_docs.generate_admin_guide"),
            patch("scripts.generate_docs.update_security_md"),
            patch("subprocess.run") as mock_run,
            patch("os.path.exists", return_value=True),
            patch("builtins.open") as mock_open,
            patch("sys.exit") as mock_exit,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            file_contents = {
                os.path.join("notebooks", "01_ml_analyzer_clustering.ipynb"): [
                    "nb1",
                    "nb1",
                ],
                os.path.join("notebooks", "02_multi_format_text_extraction.ipynb"): [
                    "nb2",
                    "nb2",
                ],
                os.path.join("notebooks", "03_virtual_sorting_verification.ipynb"): [
                    "nb3",
                    "nb3",
                ],
                os.path.join("docs", "tutorials", "01_ml_analyzer_clustering.md"): [
                    "tut1",
                    "tut1",
                ],
                os.path.join(
                    "docs", "tutorials", "02_multi_format_text_extraction.md"
                ): ["tut2", "tut2"],
                os.path.join(
                    "docs", "tutorials", "03_virtual_sorting_verification.md"
                ): ["tut3", "tut3"],
                os.path.join("docs", "api_reference.md"): ["content1", "content1"],
                os.path.join("docs", "ui.md"): ["content2", "different_content2"],
                os.path.join("docs", "admin_guide.md"): ["content3", "content3"],
                "SECURITY.md": ["content4", "content4"],
            }

            counters = {k: 0 for k in file_contents}

            class MockFile:
                def __init__(self, filepath):
                    self.filepath = filepath

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_val, exc_tb):
                    pass

                def read(self):
                    idx = counters[self.filepath]
                    counters[self.filepath] = min(
                        idx + 1, len(file_contents[self.filepath]) - 1
                    )
                    return file_contents[self.filepath][idx]

            def mock_open_side_effect(filepath, *args, **kwargs):
                if filepath in file_contents:
                    return MockFile(filepath)
                return original_open(filepath, *args, **kwargs)

            mock_open.side_effect = mock_open_side_effect

            main()

            mock_exit.assert_called_once_with(1)


def test_main_clean_on_check():
    """Verify that --check exits cleanly with 0 if no files were changed."""
    original_open = open
    with patch("sys.argv", ["generate_docs.py", "--check"]):
        with (
            patch("scripts.generate_docs.compile_diagram_assets"),
            patch("scripts.generate_docs.generate_tutorial_docs"),
            patch("scripts.generate_docs.generate_api_docs"),
            patch("scripts.generate_docs.generate_ui_docs"),
            patch("scripts.generate_docs.generate_admin_guide"),
            patch("scripts.generate_docs.update_security_md"),
            patch("subprocess.run") as mock_run,
            patch("os.path.exists", return_value=True),
            patch("builtins.open") as mock_open,
            patch("sys.exit") as mock_exit,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            file_contents = {
                os.path.join("notebooks", "01_ml_analyzer_clustering.ipynb"): [
                    "nb1",
                    "nb1",
                ],
                os.path.join("notebooks", "02_multi_format_text_extraction.ipynb"): [
                    "nb2",
                    "nb2",
                ],
                os.path.join("notebooks", "03_virtual_sorting_verification.ipynb"): [
                    "nb3",
                    "nb3",
                ],
                os.path.join("docs", "tutorials", "01_ml_analyzer_clustering.md"): [
                    "tut1",
                    "tut1",
                ],
                os.path.join(
                    "docs", "tutorials", "02_multi_format_text_extraction.md"
                ): ["tut2", "tut2"],
                os.path.join(
                    "docs", "tutorials", "03_virtual_sorting_verification.md"
                ): ["tut3", "tut3"],
                os.path.join("docs", "api_reference.md"): ["content1", "content1"],
                os.path.join("docs", "ui.md"): ["content2", "content2"],
                os.path.join("docs", "admin_guide.md"): ["content3", "content3"],
                "SECURITY.md": ["content4", "content4"],
            }

            counters = {k: 0 for k in file_contents}

            class MockFile:
                def __init__(self, filepath):
                    self.filepath = filepath

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_val, exc_tb):
                    pass

                def read(self):
                    idx = counters[self.filepath]
                    counters[self.filepath] = min(
                        idx + 1, len(file_contents[self.filepath]) - 1
                    )
                    return file_contents[self.filepath][idx]

            def mock_open_side_effect(filepath, *args, **kwargs):
                if filepath in file_contents:
                    return MockFile(filepath)
                return original_open(filepath, *args, **kwargs)

            mock_open.side_effect = mock_open_side_effect

            main()

            mock_exit.assert_not_called()


def test_component_diagram_spec_serialization():
    from app.ui.diagram_schema import (
        ComponentDiagramSpec,
        DiagramEdge,
        DiagramNode,
        DiagramSubgraph,
    )

    spec = ComponentDiagramSpec(
        id="test_spec",
        title="Test Diagram Spec",
        diagram_type="graph",
        direction="TD",
        subgraphs=[DiagramSubgraph(id="sub1", title="Sub Group 1", nodes=["n1"])],
        nodes=[
            DiagramNode(id="n1", label="Node 1", shape="round"),
            DiagramNode(id="n2", label="Node 2", shape="database"),
        ],
        edges=[
            DiagramEdge(source="n1", target="n2", label="connects"),
        ],
    )

    mmd = spec.to_mermaid()
    assert "graph TD" in mmd
    assert 'subgraph sub1 ["Sub Group 1"]' in mmd
    assert 'n1("Node 1")' in mmd
    assert 'n2[("Node 2")]' in mmd
    assert "n1 -->|connects| n2" in mmd


def test_diagram_toolchain_build_and_verify(tmp_path):
    from scripts.diagram_toolchain import build_diagrams

    with patch("scripts.diagram_toolchain.render_diagram_artifact", return_value=True):
        # Test build mode
        success = build_diagrams(output_dir=tmp_path, force=True, verify_only=False)
        assert success is True
        assert (tmp_path / "architecture_dataflow.mmd").exists()
        assert (tmp_path / ".build_cache.json").exists()

        # Test verify mode
        success_verify = build_diagrams(
            output_dir=tmp_path, force=False, verify_only=True
        )
        assert success_verify is True


def test_diagram_toolchain_cli_main_verify():
    import scripts.diagram_toolchain as dt

    with (
        patch("sys.argv", ["diagram_toolchain.py", "--verify"]),
        patch("scripts.diagram_toolchain.build_diagrams", return_value=True),
        patch("sys.exit") as mock_exit,
    ):
        dt.main()
        mock_exit.assert_called_once_with(0)


def test_find_mmdc_executable():
    from scripts.diagram_toolchain import find_mmdc_executable

    cmd = find_mmdc_executable()
    if cmd and any("npx" in arg for arg in cmd):
        assert "--yes" in cmd
