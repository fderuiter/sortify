"""Unit and integration tests for side-by-side path comparison pane in TUI preview UI."""

import asyncio
import os
import tempfile
from typing import Any

import pytest
from rich.table import Table
from textual.widgets import Static, Tree

from app.config import AppSettings
from app.ui.tui import (
    AutoSorterTUI,
    _diff_highlight_paths,
    format_folder_path_comparison,
    format_side_by_side_path_comparison,
)

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory structure for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file1 = os.path.join(tmpdir, "doc1.pdf")
        with open(file1, "w", encoding="utf-8") as f:
            f.write("test content")
        yield tmpdir


def test_diff_highlight_paths_identical():
    """Verify identical paths render without diff highlighting styles."""
    src, tgt = _diff_highlight_paths("/path/to/file.pdf", "/path/to/file.pdf")
    assert src.plain == "/path/to/file.pdf"
    assert tgt.plain == "/path/to/file.pdf"
    assert len(src.spans) == 0 or src.style == "dim white"


def test_diff_highlight_paths_differences():
    """Verify contrasting styles and dual-coded text markers are applied to changed path segments."""
    src, tgt = _diff_highlight_paths("docs/invoice.pdf", "Finance/2026_invoice.pdf")
    assert "[-]" in src.plain
    assert "[+]" in tgt.plain

    # Check that red and strikethrough style exists in src spans and green and underline style exists in tgt spans
    src_styles = [str(span.style) for span in src.spans]
    tgt_styles = [str(span.style) for span in tgt.spans]

    assert any(
        "red" in s and ("strike" in s or "strikethrough" in s) for s in src_styles
    )
    assert any("green" in s and "underline" in s for s in tgt_styles)


def test_format_side_by_side_path_comparison_table():
    """Verify format_side_by_side_path_comparison generates a 2-column Rich Table."""
    table = format_side_by_side_path_comparison(
        source_path="/workspace/docs/report.pdf",
        target_path="/workspace/Finance/report.pdf",
        base_dir="/workspace",
    )
    assert isinstance(table, Table)
    assert len(table.columns) == 2
    assert table.columns[0].header == "Source Path (Current)"
    assert table.columns[1].header == "Target Path (Proposed)"


def test_format_side_by_side_path_comparison_windows_drive_casing():
    """Verify case-insensitive base_dir stripping for Windows drive letters."""
    table = format_side_by_side_path_comparison(
        source_path="C:\\Workspace\\docs\\report.pdf",
        target_path="c:\\workspace\\Finance\\report.pdf",
        base_dir="C:\\workspace",
    )
    assert isinstance(table, Table)
    assert len(table.rows) == 2  # Relative path row + full path row


def test_format_folder_path_comparison_table():
    """Verify format_folder_path_comparison renders multiple file items."""
    files = [
        {
            "filepath": "/workspace/doc1.pdf",
            "key": "doc1.pdf",
            "folder": "Invoices",
            "target_filename": "doc1_invoice.pdf",
        },
        {
            "filepath": "/workspace/doc2.pdf",
            "key": "doc2.pdf",
            "folder": "Invoices",
            "target_filename": "doc2_invoice.pdf",
        },
    ]
    table = format_folder_path_comparison(files, base_dir="/workspace")
    assert isinstance(table, Table)
    assert len(table.columns) == 2


def test_tui_path_comparison_pane_mounted_and_updated(temp_workspace):
    """Verify AutoSorterTUI mounts #path-comparison widget and updates dynamically."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            # Check presence of path comparison widget and header
            path_comp = app.query_one("#path-comparison")
            assert path_comp is not None

            # Populate plan
            file1 = os.path.join(temp_workspace, "report_2026.pdf")
            app.plan = {
                "Finance": {
                    "report_2026.pdf": {
                        "__type__": "file",
                        "filepath": file1,
                        "target_filename": "2026_Finance_report.pdf",
                        "folder": "Finance",
                    }
                }
            }
            app.rebuild_tree()
            await pilot.pause()

            # Select file node
            tree = app.query_one("#plan-tree")
            file_node = tree.root.children[0].children[0]
            app.active_tree_node = file_node
            app._update_inspector(file_node)

            # Verify path comparison panel contains updated Rich Table
            assert isinstance(path_comp.content, Table)

            # Reassign file node to new folder 'Tax'
            app.plan["Tax"] = {
                "report_2026.pdf": app.plan["Finance"]["report_2026.pdf"]
            }
            del app.plan["Finance"]
            app.plan["Tax"]["report_2026.pdf"]["folder"] = "Tax"
            app.rebuild_tree()
            await pilot.pause()

            # Verify widget updated
            assert isinstance(path_comp.content, Table)

    asyncio.run(_test())


def _render_table_plain(table: Any) -> str:
    """Render a Rich Table to plain unstyled text for deterministic assertions."""
    from rich.console import Console

    console = Console(width=500, force_terminal=False, color_system=None)
    with console.capture() as capture:
        console.print(table)
    return capture.get()


def test_scanned_plan_without_filepath_leaf_folder_and_root_views(tmp_path):
    """Verify scanned plans omitting filepath render real source/target diffs across root, folder, and leaf views."""

    async def _test():
        ws = tmp_path / "workspace"
        inbox = ws / "inbox"
        inbox.mkdir(parents=True)
        src_invoice = inbox / "q1_invoice.pdf"
        src_invoice.write_text("Invoice Q1 total due", encoding="utf-8")
        src_contract = ws / "vendor_agreement.txt"
        src_contract.write_text("Master service agreement terms", encoding="utf-8")

        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(ws))

        async with app.run_test(size=(120, 35)) as pilot:
            # 1. Verify folder inspector directly on raw unrendered plan dict (forwarding folder)
            raw_unrendered_folder = {
                "q1_invoice.pdf": {
                    "__type__": "file",
                    "source_root": str(ws),
                    "relative_source": "../inbox/q1_invoice.pdf",
                    "routed_by": "clustering",
                }
            }
            path_comp = app.query_one("#path-comparison", Static)
            folder_meta = app._update_inspector(
                {
                    "is_file": False,
                    "key": "Finance",
                    "folder": "Finance",
                    "info": raw_unrendered_folder,
                },
                announce=False,
            )
            assert "Folder Category:[/bold accent] Finance" in folder_meta
            assert "Relative Path:[/bold] Finance" in folder_meta
            assert isinstance(path_comp.content, Table)
            direct_folder_rendered = _render_table_plain(path_comp.content)
            assert "(None)" not in direct_folder_rendered
            assert "[-]box" in direct_folder_rendered
            assert "q1_invoice.pdf" in direct_folder_rendered
            assert "[+]" in direct_folder_rendered and "[-]" in direct_folder_rendered

            # 2. Full scanned plan omitting filepath (parent-relative IncrementalAnalyzer + RecursiveKMeansStrategy)
            invoice_dict = {
                "__type__": "file",
                "source_root": str(ws),
                "source_path": "inbox/q1_invoice.pdf",
                "relative_source": "../../inbox/q1_invoice.pdf",
                "routed_by": "clustering",
            }
            contract_dict = {
                "__type__": "file",
                "relative_source": "vendor_agreement.txt",
                "source_path": "vendor_agreement.txt",
                "routed_by": "clustering",
            }
            assert "filepath" not in invoice_dict
            assert "filepath" not in contract_dict

            app.plan = {
                "Finance": {
                    "2026": {
                        "q1_invoice.pdf": invoice_dict,
                    }
                },
                "Legal": {
                    "vendor_agreement.txt": contract_dict,
                },
            }
            app.rebuild_tree(run_validation=False)
            await pilot.pause()

            # Object identity preserved on in-place enrichment
            collected = app._collect_file_nodes_from_dict(app.plan)
            assert any(item is invoice_dict for item in collected)
            assert any(item is contract_dict for item in collected)
            assert os.path.normpath(invoice_dict["filepath"]) == os.path.normpath(
                str(src_invoice)
            )
            assert invoice_dict["folder"] == "Finance/2026"
            assert os.path.normpath(contract_dict["filepath"]) == os.path.normpath(
                str(src_contract)
            )
            assert contract_dict["folder"] == "Legal"

            # Root view (#path-comparison)
            root_meta = app._update_inspector(None, announce=False)
            assert "Root folder of sorting plan." in root_meta
            assert isinstance(path_comp.content, Table)
            root_rendered = _render_table_plain(path_comp.content)
            assert "(None)" not in root_rendered
            assert "2026/q1_invoice.pdf" in root_rendered
            assert "[+]Legal/vendor_agreement.txt" in root_rendered
            assert "[-]" in root_rendered

            # Folder view (#path-comparison & #meta-details)
            tree = app.query_one("#plan-tree", Tree)
            finance_node = next(
                n for n in tree.root.children if "Finance" in str(n.label)
            )
            sub_node = finance_node.children[0]
            sub_meta = app._update_inspector(sub_node, announce=False)
            assert "Relative Path:[/bold] Finance/2026" in sub_meta
            sub_rendered = _render_table_plain(path_comp.content)
            assert "(None)" not in sub_rendered
            assert "[-]box" in sub_rendered
            assert "2026/q1_invoice.pdf" in sub_rendered

            # Leaf view (#path-comparison & #meta-details)
            leaf_node = sub_node.children[0]
            leaf_meta = app._update_inspector(leaf_node, announce=False)
            expected_src_posix = src_invoice.as_posix()
            assert f"Path:[/bold] {expected_src_posix}" in leaf_meta
            assert leaf_node.data is not None
            assert "Finance/2026/q1_invoice.pdf" not in (
                leaf_node.data.get("filepath") or ""
            )
            leaf_rendered = _render_table_plain(path_comp.content)
            assert "[+]" in leaf_rendered and "[-]" in leaf_rendered
            clean_leaf = leaf_rendered.replace("[+]", "").replace("[-]", "")
            assert "inbox/q1_invoice.pdf" in clean_leaf
            assert "Finance/2026/q1_invoice.pdf" in clean_leaf

            # Search filter matches real source path segment ("inbox"), not fabricated destination path
            matching_ids = app._build_matching_node_ids(app.plan, "inbox")
            assert id(invoice_dict) in matching_ids
            assert id(contract_dict) not in matching_ids

    asyncio.run(_test())


def test_resolve_file_source_path_multi_root_reassignment_and_override_edge_cases(
    tmp_path,
):
    """Verify multi-root :: keys, reassigned stale ../ relative_source caching, and unset base_dir / destination_override."""

    async def _test():
        root_a = tmp_path / "root_a"
        root_b = tmp_path / "root_b"
        (root_a).mkdir()
        (root_b / "sub").mkdir(parents=True)

        report_a = root_a / "report.pdf"
        report_a.write_text("Report A", encoding="utf-8")
        audit_b = root_b / "sub" / "audit.pdf"
        audit_b.write_text("Audit B", encoding="utf-8")

        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(root_a))

        async with app.run_test(size=(120, 35)) as pilot:
            multi_node_name = f"{root_b.as_posix()}::sub/audit.pdf"
            report_info = {
                "__type__": "file",
                "source_root": str(root_a),
                "relative_source": "../report.pdf",
                "routed_by": "clustering",
            }
            multi_info = {
                "__type__": "file",
                "routed_by": "clustering",
            }
            app.plan = {
                "Finance": {
                    "report.pdf": report_info,
                },
                "Compliance": {
                    multi_node_name: multi_info,
                },
            }
            app.rebuild_tree(run_validation=False)
            await pilot.pause()

            # Multi-root :: key joins source_root before falling back to base_dir
            assert multi_info["filepath"] == audit_b.as_posix()
            assert report_info["filepath"] == report_a.as_posix()

            # Reassign report_info to nested folder while relative_source still has stale '../report.pdf'
            app.plan["Archive"] = {"2026": {"Tax": {"report.pdf": report_info}}}
            del app.plan["Finance"]
            app.rebuild_tree(run_validation=False)
            await pilot.pause()

            assert app.plan["Archive"]["2026"]["Tax"]["report.pdf"] is report_info
            assert report_info["filepath"] == report_a.as_posix()
            assert report_info["folder"] == "Archive/2026/Tax"

            tree = app.query_one("#plan-tree", Tree)
            archive_node = next(
                n for n in tree.root.children if "Archive" in str(n.label)
            )
            reassigned_leaf = archive_node.children[0].children[0].children[0]
            assert reassigned_leaf.data is not None
            assert reassigned_leaf.data["filepath"] == report_a.as_posix()
            assert os.path.normpath(
                reassigned_leaf.data["target_path"]
            ) == os.path.normpath(
                os.path.join(str(root_a), "Archive/2026/Tax", "report.pdf")
            )

        # Unset base_dir + destination_override fallback
        override_dir = tmp_path / "external_vault"
        app_no_base = AutoSorterTUI(settings=settings, base_dir="")
        async with app_no_base.run_test(size=(120, 35)) as pilot:
            override_info = {
                "__type__": "file",
                "destination_override": str(override_dir),
            }
            app_no_base.plan = {
                "Finance": {
                    "standalone.pdf": override_info,
                }
            }
            app_no_base.rebuild_tree(run_validation=False)
            await pilot.pause()

            tree2 = app_no_base.query_one("#plan-tree", Tree)
            leaf2 = tree2.root.children[0].children[0]
            assert leaf2.data is not None
            assert leaf2.data["filepath"] == "standalone.pdf"
            expected_override_target = os.path.normpath(
                os.path.join(str(override_dir), "standalone.pdf")
            )
            assert (
                os.path.normpath(leaf2.data["target_path"]) == expected_override_target
            )
            meta2 = app_no_base._update_inspector(leaf2, announce=False)
            assert "Path:[/bold] standalone.pdf" in meta2
            rendered2 = _render_table_plain(
                app_no_base.query_one("#path-comparison", Static).content
            )
            assert "external_vault/standalone.pdf" in rendered2

    asyncio.run(_test())


def test_resolve_file_source_path_direct_branch_variants(tmp_path):
    """Verify _resolve_file_source_path across :: prefixes, virtual/no-base paths, absolute paths, and empty keys."""
    root_dir = tmp_path / "root_src"
    root_dir.mkdir()
    disk_file = root_dir / "doc_on_disk.pdf"
    disk_file.write_text("content", encoding="utf-8")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(root_dir))

    # 1. source_root:: in relative_source and source_path
    r_prefix = f"{root_dir.as_posix()}::doc_on_disk.pdf"
    assert (
        app._resolve_file_source_path({"relative_source": r_prefix}, "k.pdf", "Finance")
        == disk_file.as_posix()
    )
    assert (
        app._resolve_file_source_path({"source_path": r_prefix}, "k.pdf", "Finance")
        == disk_file.as_posix()
    )

    # 2. Absolute relative_source, source_path, and file_key
    abs_target = "/virtual/abs/doc.pdf"
    assert (
        app._resolve_file_source_path(
            {"relative_source": abs_target}, "k.pdf", "Finance"
        )
        == abs_target
    )
    assert (
        app._resolve_file_source_path({"source_path": abs_target}, "k.pdf", "Finance")
        == abs_target
    )
    assert app._resolve_file_source_path({}, abs_target, "Finance") == abs_target

    # 3. Virtual (non-existent) parent-relative vs root-relative paths, with and without current_folder
    assert (
        app._resolve_file_source_path(
            {"source_root": "/virtual/ws", "relative_source": "../sub/v.pdf"},
            "v.pdf",
            "Finance",
        )
        == "/virtual/ws/sub/v.pdf"
    )
    assert (
        app._resolve_file_source_path(
            {
                "source_root": "/virtual/ws",
                "relative_source": "../sub/v.pdf",
                "source_path": "sub/v.pdf",
            },
            "v.pdf",
            "",
        )
        == "/virtual/ws/sub/v.pdf"
    )
    assert (
        app._resolve_file_source_path(
            {"relative_source": "../doc_on_disk.pdf"},
            "doc_on_disk.pdf",
            "",
        )
        == disk_file.as_posix()
    )

    # 4. Unset base_dir variants (parent-relative, root-relative, source_path, empty key)
    app_empty = AutoSorterTUI(settings=settings, base_dir="")
    assert (
        app_empty._resolve_file_source_path(
            {"relative_source": "../sub/a.pdf"}, "a.pdf", "Finance/2026"
        )
        == "Finance/sub/a.pdf"
    )
    assert (
        app_empty._resolve_file_source_path(
            {"relative_source": "inbox/a.pdf"}, "a.pdf", "Finance"
        )
        == "inbox/a.pdf"
    )
    assert (
        app_empty._resolve_file_source_path(
            {"source_path": "inbox/b.pdf"}, "b.pdf", "Finance"
        )
        == "inbox/b.pdf"
    )
    assert app_empty._resolve_file_source_path({}, "", "Finance") == ""
