from pathlib import Path
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
            patch("scripts.generate_docs.validate_mermaid_diagrams", return_value=[]),
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
            patch("scripts.generate_docs.validate_mermaid_diagrams", return_value=[]),
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


def test_main_no_build_flag():
    """Verify that --no-build skips running subprocess.run for mkdocs build."""
    with patch("sys.argv", ["generate_docs.py", "--no-build"]):
        with (
            patch("scripts.generate_docs.compile_diagram_assets") as mock_diag,
            patch("scripts.generate_docs.generate_tutorial_docs") as mock_tut,
            patch("scripts.generate_docs.generate_api_docs") as mock_api,
            patch("scripts.generate_docs.generate_ui_docs") as mock_ui,
            patch("scripts.generate_docs.generate_admin_guide") as mock_admin,
            patch("scripts.generate_docs.update_security_md") as mock_sec,
            patch("scripts.generate_docs.validate_mermaid_diagrams", return_value=[]),
            patch("subprocess.run") as mock_run,
        ):
            main()

            mock_diag.assert_called_once()
            mock_tut.assert_called_once()
            mock_api.assert_called_once()
            mock_ui.assert_called_once()
            mock_admin.assert_called_once()
            mock_sec.assert_called_once()
            mock_run.assert_not_called()


def test_main_skip_build_flag():
    """Verify that --skip-build skips running subprocess.run for mkdocs build."""
    with patch("sys.argv", ["generate_docs.py", "--skip-build"]):
        with (
            patch("scripts.generate_docs.compile_diagram_assets") as mock_diag,
            patch("scripts.generate_docs.generate_tutorial_docs") as mock_tut,
            patch("scripts.generate_docs.generate_api_docs") as mock_api,
            patch("scripts.generate_docs.generate_ui_docs") as mock_ui,
            patch("scripts.generate_docs.generate_admin_guide") as mock_admin,
            patch("scripts.generate_docs.update_security_md") as mock_sec,
            patch("scripts.generate_docs.validate_mermaid_diagrams", return_value=[]),
            patch("subprocess.run") as mock_run,
        ):
            main()

            mock_diag.assert_called_once()
            mock_tut.assert_called_once()
            mock_api.assert_called_once()
            mock_ui.assert_called_once()
            mock_admin.assert_called_once()
            mock_sec.assert_called_once()
            mock_run.assert_not_called()


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
            patch("scripts.generate_docs.validate_mermaid_diagrams", return_value=[]),
            patch("scripts.generate_docs.audit_handwritten_docs", return_value=[]),
            patch("subprocess.run") as mock_run,
            patch("scripts.generate_docs.os.path.exists", return_value=True),
            patch("builtins.open") as mock_open,
            patch("sys.exit") as mock_exit,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            file_contents = {
                Path("notebooks/01_ml_analyzer_clustering.ipynb").as_posix(): [
                    "nb1",
                    "nb1",
                ],
                Path("notebooks/02_multi_format_text_extraction.ipynb").as_posix(): [
                    "nb2",
                    "nb2",
                ],
                Path("notebooks/03_virtual_sorting_verification.ipynb").as_posix(): [
                    "nb3",
                    "nb3",
                ],
                Path("docs/tutorials/01_ml_analyzer_clustering.md").as_posix(): [
                    "tut1",
                    "tut1",
                ],
                Path("docs/tutorials/02_multi_format_text_extraction.md").as_posix(): [
                    "tut2",
                    "tut2",
                ],
                Path("docs/tutorials/03_virtual_sorting_verification.md").as_posix(): [
                    "tut3",
                    "tut3",
                ],
                Path("docs/api_reference.md").as_posix(): ["content1", "content1"],
                Path("docs/ui.md").as_posix(): ["content2", "different_content2"],
                Path("docs/admin_guide.md").as_posix(): ["content3", "content3"],
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
                key = (
                    Path(filepath).as_posix()
                    if isinstance(filepath, (str, Path))
                    else str(filepath)
                )
                if key in file_contents:
                    return MockFile(key)
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
            patch("scripts.generate_docs.validate_mermaid_diagrams", return_value=[]),
            patch("scripts.generate_docs.audit_handwritten_docs", return_value=[]),
            patch("subprocess.run") as mock_run,
            patch("scripts.generate_docs.os.path.exists", return_value=True),
            patch("builtins.open") as mock_open,
            patch("sys.exit") as mock_exit,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            file_contents = {
                Path("notebooks/01_ml_analyzer_clustering.ipynb").as_posix(): [
                    "nb1",
                    "nb1",
                ],
                Path("notebooks/02_multi_format_text_extraction.ipynb").as_posix(): [
                    "nb2",
                    "nb2",
                ],
                Path("notebooks/03_virtual_sorting_verification.ipynb").as_posix(): [
                    "nb3",
                    "nb3",
                ],
                Path("docs/tutorials/01_ml_analyzer_clustering.md").as_posix(): [
                    "tut1",
                    "tut1",
                ],
                Path("docs/tutorials/02_multi_format_text_extraction.md").as_posix(): [
                    "tut2",
                    "tut2",
                ],
                Path("docs/tutorials/03_virtual_sorting_verification.md").as_posix(): [
                    "tut3",
                    "tut3",
                ],
                Path("docs/api_reference.md").as_posix(): ["content1", "content1"],
                Path("docs/ui.md").as_posix(): ["content2", "content2"],
                Path("docs/admin_guide.md").as_posix(): ["content3", "content3"],
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
                key = (
                    Path(filepath).as_posix()
                    if isinstance(filepath, (str, Path))
                    else str(filepath)
                )
                if key in file_contents:
                    return MockFile(key)
                return original_open(filepath, *args, **kwargs)

            mock_open.side_effect = mock_open_side_effect

            main()

            mock_exit.assert_not_called()


def test_component_diagram_spec_serialization():
    from app.core.diagram_schema import (
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


def test_diagram_node_click_attributes_and_url_validation():
    import pytest
    from pydantic import ValidationError

    from app.core.diagram_schema import DiagramNode

    # 1. Valid click attributes
    node = DiagramNode(
        id="step1",
        label="Step One",
        url="https://example.com/docs",
        tooltip="Custom Tooltip",
        target="_blank",
        step_number=1,
        step_description="First step description",
    )
    assert node.url == "https://example.com/docs"
    assert node.tooltip == "Custom Tooltip"
    assert node.target == "_blank"
    assert node.step_number == 1
    assert node.step_description == "First step description"

    # Relative links and valid schemes
    valid_urls = [
        "docs/user_guide.md#first-run-steps--setup-wizard",
        "#setup-wizard",
        "http://localhost:8000",
        "mailto:user@example.com",
        "file:///tmp/doc.txt",
    ]
    for url in valid_urls:
        n = DiagramNode(id="test", label="Test", url=url)
        assert n.url == url

    # Unsafe and invalid schemes
    unsafe_urls = [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "ftp://example.com/file",
    ]
    for url in unsafe_urls:
        with pytest.raises(ValidationError):
            DiagramNode(id="test", label="Test", url=url)


def test_component_diagram_spec_click_serialization():
    from app.core.diagram_schema import ComponentDiagramSpec, DiagramNode

    spec = ComponentDiagramSpec(
        id="click_spec",
        title="Click Spec",
        diagram_type="graph",
        direction="TD",
        nodes=[
            DiagramNode(
                id="a",
                label="Node A",
                url="https://example.com/a",
                tooltip="Tooltip A",
                target="_blank",
            ),
            DiagramNode(
                id="b",
                label="Node B",
                url="docs/guide.md",
                step_number=2,
                step_description="Second Step",
            ),
            DiagramNode(
                id="c",
                label="Node C",
                tooltip="Tooltip C Only",
            ),
            DiagramNode(
                id="d",
                label="Node D Plain",
            ),
        ],
    )

    mmd = spec.to_mermaid()
    assert 'click a "https://example.com/a" "Tooltip A" _blank' in mmd
    assert 'click b "docs/guide.md" "Step 2: Second Step"' in mmd
    assert 'click c tooltip "Tooltip C Only"' in mmd
    assert "click d" not in mmd


def test_diagram_toolchain_click_directives_and_fallback_svg():
    from scripts.diagram_toolchain import (
        generate_fallback_svg,
        parse_and_validate_click_directives,
    )

    mmd_valid = """graph TD
    A["Node A"]
    click A "https://example.com" "Tooltip A" _blank
    """
    errs_valid = parse_and_validate_click_directives(mmd_valid)
    assert len(errs_valid) == 0

    mmd_invalid = """graph TD
    A["Node A"]
    click A "javascript:alert(1)" "Tooltip A"
    """
    errs_invalid = parse_and_validate_click_directives(mmd_invalid)
    assert len(errs_invalid) > 0
    assert "Unsafe or invalid URL" in errs_invalid[0]

    fallback = generate_fallback_svg("Test Title", mmd_valid)
    assert '<a href="https://example.com" target="_blank"' in fallback
    assert "🔗 Node A -&gt; Tooltip A" in fallback


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
        assert "--no-install" in cmd or "--yes" in cmd


def test_generate_fallback_svg():
    from scripts.diagram_toolchain import generate_fallback_svg

    title = "Test & Special <Title>"
    mmd = "graph TD\n  A[Node & A] --> B<Node B>\n"
    svg = generate_fallback_svg(title, mmd)

    assert "<svg" in svg
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg
    assert "Test &amp; Special &lt;Title&gt;" in svg
    assert "A[Node &amp; A] --&gt; B&lt;Node B&gt;" in svg


def test_is_browser_available_when_missing():
    from scripts.diagram_toolchain import is_browser_available, reset_browser_cache

    reset_browser_cache()
    with patch("scripts.diagram_toolchain.find_mmdc_executable", return_value=None):
        assert is_browser_available(force_check=True) is False

    reset_browser_cache()
    with patch("subprocess.run", side_effect=Exception("Browser launch error")):
        assert is_browser_available(mmdc_cmd=["mmdc"], force_check=True) is False


def test_is_browser_available_when_present():
    from scripts.diagram_toolchain import is_browser_available, reset_browser_cache

    reset_browser_cache()

    def fake_run(cmd, *args, **kwargs):
        if "-o" in cmd:
            out_idx = cmd.index("-o") + 1
            out_path = Path(cmd[out_idx])
            out_path.write_text("<svg>probe</svg>", encoding="utf-8")
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        assert is_browser_available(mmdc_cmd=["mmdc"], force_check=True) is True


def test_diagram_toolchain_browserless_verify_and_build(tmp_path):
    from scripts.diagram_toolchain import build_diagrams, reset_browser_cache

    reset_browser_cache()
    with patch("scripts.diagram_toolchain.is_browser_available", return_value=False):
        # Test verify mode in browserless environment
        verify_ok = build_diagrams(output_dir=tmp_path, force=True, verify_only=True)
        assert verify_ok is True

        # Test build mode in browserless environment
        build_ok = build_diagrams(output_dir=tmp_path, force=True, verify_only=False)
        assert build_ok is True

        # Verify fallback SVGs were generated
        svg_files = list(tmp_path.glob("*.svg"))
        assert len(svg_files) > 0
        first_svg = svg_files[0].read_text(encoding="utf-8")
        assert "<svg" in first_svg
        assert "Fallback View" in first_svg


def test_diagram_toolchain_cli_verify_failure_on_bad_schema(tmp_path):
    import scripts.diagram_toolchain as dt

    class BadSpec:
        id = "bad_spec"
        title = "Bad Spec"

        def to_mermaid(self):
            raise ValueError("Invalid Mermaid schema")

    dt.reset_browser_cache()
    with (
        patch(
            "sys.argv",
            ["diagram_toolchain.py", "verify", "--output-dir", str(tmp_path)],
        ),
        patch(
            "scripts.diagram_toolchain.collect_all_specs",
            return_value={"bad_spec": BadSpec()},
        ),
        patch("sys.exit") as mock_exit,
    ):
        dt.main()
        mock_exit.assert_called_once_with(1)


def test_sequence_diagram_spec_serialization():
    from app.core.diagram_schema import (
        SequenceActivation,
        SequenceAlt,
        SequenceAltBranch,
        SequenceDiagramSpec,
        SequenceLoop,
        SequenceMessage,
        SequenceNote,
        SequenceOpt,
        SequenceParticipant,
    )

    spec = SequenceDiagramSpec(
        id="test_seq",
        title="Test Sequence",
        autonumber=True,
        participants=[
            SequenceParticipant(id="A", label="User", is_actor=True),
            SequenceParticipant(id="B", label="System"),
        ],
        items=[
            SequenceMessage(source="A", target="B", text="Request"),
            SequenceActivation(target="B", action="activate"),
            SequenceLoop(
                label="Process",
                items=[
                    SequenceMessage(source="B", target="B", text="Self check"),
                ],
            ),
            SequenceOpt(
                label="Cache hit",
                items=[
                    SequenceMessage(source="B", target="A", text="Return cached"),
                ],
            ),
            SequenceAlt(
                branches=[
                    SequenceAltBranch(
                        label="Success",
                        items=[
                            SequenceMessage(
                                source="B", target="A", text="OK", arrow_type="-->>"
                            ),
                        ],
                    ),
                    SequenceAltBranch(
                        label="Error",
                        items=[
                            SequenceMessage(
                                source="B", target="A", text="Fail", arrow_type="-->>"
                            ),
                        ],
                    ),
                ]
            ),
            SequenceNote(position="over", targets=["A", "B"], text="Done"),
            SequenceActivation(target="B", action="deactivate"),
        ],
    )

    mmd = spec.to_mermaid()
    assert "sequenceDiagram" in mmd
    assert "autonumber" in mmd
    assert "actor A as User" in mmd
    assert "participant B as System" in mmd
    assert "A->>B: Request" in mmd
    assert "activate B" in mmd
    assert "loop Process" in mmd
    assert "opt Cache hit" in mmd
    assert "alt Success" in mmd
    assert "else Error" in mmd
    assert "note over A, B: Done" in mmd
    assert "deactivate B" in mmd


def test_state_diagram_spec_serialization():
    from app.core.diagram_schema import (
        StateComposite,
        StateDiagramSpec,
        StateNode,
        StateNote,
        StateTransition,
    )

    spec = StateDiagramSpec(
        id="test_state",
        title="Test State",
        diagram_type="stateDiagram-v2",
        direction="LR",
        states=[
            StateNode(id="Idle", label="Active Service"),
            StateNode(id="Choice1", is_choice=True),
        ],
        transitions=[
            StateTransition(source="[*]", target="Idle", label="Start"),
            StateTransition(source="Idle", target="Choice1"),
        ],
        composite_states=[
            StateComposite(
                id="Processing",
                label="In Progress",
                states=[StateNode(id="Sub1", label="Step 1")],
                transitions=[StateTransition(source="[*]", target="Sub1")],
            )
        ],
        notes=[
            StateNote(position="left of", target="Idle", text="Watchdog note"),
        ],
    )

    mmd = spec.to_mermaid()
    assert "stateDiagram-v2" in mmd
    assert "direction LR" in mmd
    assert "state Choice1 <<choice>>" in mmd
    assert "Idle: Active Service" in mmd
    assert "[*] --> Idle: Start" in mmd
    assert "state Processing [In Progress] {" in mmd
    assert "Sub1: Step 1" in mmd
    assert "note left of Idle: Watchdog note" in mmd


def test_data_flow_diagram_spec_serialization():
    from app.core.diagram_schema import (
        ARCHITECTURE_DATAFLOW_SPEC,
        DataFlowDiagramSpec,
        DataStoreNode,
        DataStreamEdge,
        ExternalEntityNode,
        ProcessNode,
    )

    spec = DataFlowDiagramSpec(
        id="dfd_test",
        title="Test DFD Spec",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            ExternalEntityNode(
                id="ext1",
                label="External Client",
                url="https://example.com/api",
                tooltip="Client App",
            ),
            ProcessNode(
                id="proc1",
                label="Inbound Ingestion Engine",
                tooltip="Process Step 1",
            ),
            DataStoreNode(
                id="ds1",
                label="Document Store DB",
                tooltip="Persisted Database",
            ),
        ],
        edges=[
            DataStreamEdge(
                source="ext1",
                target="proc1",
                label="Ingest Payload",
                contract="RawPayloadSchema",
            ),
            DataStreamEdge(
                source="proc1",
                target="ds1",
                contract="PersistedDocumentModel",
            ),
        ],
    )

    mmd = spec.to_mermaid()
    assert "flowchart TD" in mmd
    assert 'ext1["External Client"]' in mmd
    assert 'proc1(["Inbound Ingestion Engine"])' in mmd
    assert 'ds1[("Document Store DB")]' in mmd
    assert "ext1 -->|Ingest Payload: RawPayloadSchema| proc1" in mmd
    assert "proc1 -->|PersistedDocumentModel| ds1" in mmd
    assert 'click ext1 "https://example.com/api" "Client App"' in mmd

    # Also verify system architecture spec
    assert isinstance(ARCHITECTURE_DATAFLOW_SPEC, DataFlowDiagramSpec)
    arch_mmd = ARCHITECTURE_DATAFLOW_SPEC.to_mermaid()
    assert 'A["Directory Selection"]' in arch_mmd
    assert 'B(["File Extraction & Generator"])' in arch_mmd
    assert 'G[("Generate Sorting Plan")]' in arch_mmd
    assert "A -->|DirectoryPath| B" in arch_mmd


def test_dfd_node_url_validation():
    import pytest
    from pydantic import ValidationError

    from app.core.diagram_schema import ExternalEntityNode, ProcessNode

    # Valid URLs
    proc = ProcessNode(id="p1", label="Process", url="https://example.com/doc")
    assert proc.url == "https://example.com/doc"

    ext = ExternalEntityNode(id="e1", label="External", url="docs/user_guide.md#step1")
    assert ext.url == "docs/user_guide.md#step1"

    # Invalid javascript scheme on ProcessNode
    with pytest.raises(ValidationError):
        ProcessNode(id="p2", label="Process", url="javascript:alert('xss')")

    # Disallowed URI scheme on ExternalEntityNode
    with pytest.raises(ValidationError):
        ExternalEntityNode(id="e2", label="External", url="ftp://unsupported.schema")


def test_collect_all_specs_contains_sequence_and_state():
    from app.core.diagram_schema import (
        DataFlowDiagramSpec,
        SequenceDiagramSpec,
        StateDiagramSpec,
    )
    from scripts.diagram_toolchain import collect_all_specs

    specs = collect_all_specs()
    has_seq = any(isinstance(s, SequenceDiagramSpec) for s in specs.values())
    has_state = any(isinstance(s, StateDiagramSpec) for s in specs.values())
    has_dfd = any(isinstance(s, DataFlowDiagramSpec) for s in specs.values())
    assert has_seq is True
    assert has_state is True
    assert has_dfd is True


def test_check_no_raw_mermaid_in_docs_detects_blocks(tmp_path):
    from scripts.diagram_toolchain import check_no_raw_mermaid_in_docs

    # Clean directory
    clean_doc = tmp_path / "clean.md"
    clean_doc.write_text("# Clean doc\n![Image](assets/diagrams/spec.svg)\n")
    assert check_no_raw_mermaid_in_docs(docs_dir=tmp_path) is True

    # Bad directory with raw mermaid
    bad_doc = tmp_path / "bad.md"
    bad_doc.write_text("# Bad doc\n```mermaid\ngraph TD\nA-->B\n```\n")
    assert check_no_raw_mermaid_in_docs(docs_dir=tmp_path) is False


def test_validate_mermaid_syntax_pure_python():
    from scripts.diagram_toolchain import validate_mermaid_syntax

    # Valid diagram
    valid_mmd = "graph TD\n  A[Start] --> B(Process)\n  subgraph Sub\n    B --> C{Decision}\n  end\n"
    assert validate_mermaid_syntax(valid_mmd) == []

    # Unknown diagram header type
    invalid_header = "invalidGraph TD\n  A --> B\n"
    errs = validate_mermaid_syntax(invalid_header)
    assert len(errs) > 0
    assert "Line 1" in errs[0]
    assert "unknown diagram type 'invalidGraph'" in errs[0]

    # Unbalanced brackets
    invalid_brackets = "flowchart TD\n  Line 2: A[Unclosed Bracket\n  Line 3: B --> C\n"
    errs = validate_mermaid_syntax(invalid_brackets)
    assert len(errs) > 0
    assert "Line 2" in errs[0]
    assert "unbalanced brackets" in errs[0]

    # Hanging relationship arrow
    hanging_arrow = "graph TD\n  A --> B\n  C -->\n"
    errs = validate_mermaid_syntax(hanging_arrow)
    assert len(errs) > 0
    assert "Line 3" in errs[0]
    assert "Hanging relationship arrow" in errs[0]

    # Unclosed subgraph
    unclosed_subgraph = "graph TD\n  subgraph Group1\n  A --> B\n"
    errs = validate_mermaid_syntax(unclosed_subgraph)
    assert len(errs) > 0
    assert "Unclosed structural block" in errs[0]


def test_diagram_toolchain_cli_verify_failure_on_bad_syntax(tmp_path):
    import scripts.diagram_toolchain as dt

    class BadSyntaxSpec:
        id = "bad_syntax_spec"
        title = "Bad Syntax Spec"

        def to_mermaid(self):
            return "graph TD\n  A[Node A --> B\n"

    dt.reset_browser_cache()
    with (
        patch(
            "sys.argv",
            ["diagram_toolchain.py", "verify", "--output-dir", str(tmp_path)],
        ),
        patch(
            "scripts.diagram_toolchain.collect_all_specs",
            return_value={"bad_syntax_spec": BadSyntaxSpec()},
        ),
        patch("sys.exit") as mock_exit,
    ):
        dt.main()
        mock_exit.assert_called_once_with(1)
