from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.generate_docs import (
    _collect_bindings,
    _describe_bounds,
    _describe_default,
    _describe_type,
    _format_key,
    audit_adr_status,
    audit_auto_merge_policy,
    audit_handwritten_docs,
    audit_model_hosts,
    audit_privacy_policy,
    get_handwritten_docs,
    main,
    update_security_md,
    validate_mermaid_diagrams,
    validate_mkdocs_config,
    validate_mkdocs_site,
    validate_security_policy,
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


def test_validate_security_policy_clean():
    """Verify security policy documentation validation passes on repo's SECURITY.md."""
    errors = validate_security_policy()
    assert errors == []


def test_validate_security_policy_detects_invalid_fallback_policy(tmp_path):
    """Verify security policy validation fails when SECURITY.md contains invalid fallback policy text."""
    bad_sec = tmp_path / "SECURITY.md"
    bad_sec.write_text(
        "# Security Policy\n\n"
        "## Telemetry and Network Access\n\n"
        "- **Model security advisories.** Before a downloaded or side-loaded model is loaded, "
        "`app/core/security_advisory.py` queries `https://security.huggingface.co/api/advisories` with "
        "the model's repository id and the SHA-256 hash of the model file. This is on by default. "
        "Point `SECURITY_ADVISORY_ENDPOINT` at a mirror to change where it goes, and "
        "`SECURITY_ADVISORY_FALLBACK_POLICY` (`strict` or `permissive`) decides what happens when the endpoint "
        "is unreachable: the default, `permissive`, loads the model anyway, and `strict` refuses it.\n\n"
        "## Network Dependencies\n"
    )
    errors = validate_security_policy(doc_path=str(bad_sec))
    assert len(errors) == 1
    assert "permissive" in errors[0]
    assert "strict" in errors[0]


def test_validate_security_policy_safely_handles_unexpected_formatting(tmp_path):
    """Verify security policy validator safely handles missing target headings or unexpected formatting without crashing."""
    bad_sec = tmp_path / "SECURITY.md"
    bad_sec.write_text("# Security Policy\n\nMinimal doc without telemetry section.\n")
    errors = validate_security_policy(doc_path=str(bad_sec))
    assert errors == []


def test_update_security_md_fails_on_policy_drift(tmp_path, monkeypatch):
    """Verify update_security_md raises ValueError when SECURITY.md contains invalid fallback policy prose."""
    import json

    (tmp_path / "app").mkdir()
    (tmp_path / "network_rules.json").write_text(
        json.dumps({"targets": ["app"], "rules": []})
    )
    bad_sec = tmp_path / "SECURITY.md"
    bad_sec.write_text(
        "# Security Policy\n\n"
        "## Telemetry and Network Access\n\n"
        "- **Model security advisories.** Point `SECURITY_ADVISORY_ENDPOINT` at a mirror to change where it goes, "
        "and `SECURITY_ADVISORY_FALLBACK_POLICY` (`strict` or `permissive`) decides what happens when the endpoint "
        "is unreachable: the default, `permissive`, loads the model anyway, and `strict` refuses it.\n\n"
        "## Network Dependencies\n\nstale\n"
    )
    monkeypatch.chdir(tmp_path)

    with patch("scripts.validate_links.validate_url", return_value=(True, "ok", False)):
        with pytest.raises(ValueError, match="(?i)fallback policy"):
            update_security_md()


def test_audit_privacy_policy_clean():
    """Verify PRIVACY.md passes fail-closed SQLCipher and host directory audits."""
    assert audit_privacy_policy() == []


def test_audit_privacy_policy_detects_sqlite_fallback_claim(tmp_path):
    """Verify PRIVACY.md audit fails when a plain SQLite fallback is claimed."""
    baseline = Path("PRIVACY.md").read_text(encoding="utf-8")
    drifted = baseline + (
        "\nIf SQLCipher is missing, the app falls back to plain SQLite pages.\n"
    )
    bad_privacy = tmp_path / "PRIVACY.md"
    bad_privacy.write_text(drifted, encoding="utf-8")

    errors = audit_privacy_policy(doc_path=str(bad_privacy))
    assert any("plain SQLite fallback" in e for e in errors)

    with patch(
        "scripts.generate_docs.get_handwritten_docs", return_value=[str(bad_privacy)]
    ):
        hw_errors = audit_handwritten_docs()
        assert any("plain SQLite fallback" in e for e in hw_errors)


def test_audit_privacy_policy_detects_missing_fail_closed_or_runtime_drift(
    tmp_path, monkeypatch
):
    """Verify PRIVACY.md audit catches missing fail-closed prose or db_conn flag drift."""
    import app.core.db_conn

    baseline = Path("PRIVACY.md").read_text(encoding="utf-8")
    no_error_claim = baseline.replace(
        "fails closed and raises an error", "continues running"
    )
    bad_privacy = tmp_path / "PRIVACY.md"
    bad_privacy.write_text(no_error_claim, encoding="utf-8")

    errors = audit_privacy_policy(doc_path=str(bad_privacy))
    assert any("fails closed and raises an error" in e for e in errors)

    monkeypatch.setattr(app.core.db_conn, "_disable_pytest_win_fallback", False)
    runtime_drift_errors = audit_privacy_policy(content=baseline, doc_path="PRIVACY.md")
    assert any("_disable_pytest_win_fallback" in e for e in runtime_drift_errors)


@pytest.mark.parametrize(
    "missing_token",
    [
        "~/.autosorter/",
        "AUTOSORTER_APP_DIR",
        "~/.sortify/keys",
        r"%APPDATA%\Sortify\keys",
        "~/.smart-autosorter/",
        "autosorter_sessions_<user>",
    ],
)
def test_audit_privacy_policy_detects_missing_host_directory(missing_token):
    """Verify PRIVACY.md audit reports an error when any host storage path is omitted."""
    baseline = Path("PRIVACY.md").read_text(encoding="utf-8")
    drifted = baseline.replace(missing_token, "REDACTED_PATH")
    errors = audit_privacy_policy(content=drifted, doc_path="PRIVACY.md")
    assert any(missing_token in e for e in errors)


def test_audit_adr_status_clean():
    """Verify all accepted ADRs pass citation and Implementation section audits."""
    assert audit_adr_status() == []


def test_audit_adr_0001_detects_stale_socket_sandbox_citation(tmp_path):
    """Verify ADR 0001 fails audit if it cites shared_registry.py or omits network_guard.py."""
    adr_path = Path("docs/adr/0001-local-first-no-cloud-processing.md")
    baseline = adr_path.read_text(encoding="utf-8")
    drifted = baseline.replace(
        "app/core/network_guard.py", "app/core/shared_registry.py"
    )
    bad_adr_dir = tmp_path / "adr"
    bad_adr_dir.mkdir()
    bad_adr = bad_adr_dir / adr_path.name
    bad_adr.write_text(drifted, encoding="utf-8")

    errors = audit_adr_status(doc_path=str(bad_adr))
    assert any("app/core/network_guard.py" in e for e in errors)
    assert any("app/core/shared_registry.py" in e for e in errors)

    with patch(
        "scripts.generate_docs.get_handwritten_docs", return_value=[str(bad_adr)]
    ):
        hw_errors = audit_handwritten_docs()
        assert any("app/core/network_guard.py" in e for e in hw_errors)


@pytest.mark.parametrize(
    "adr_filename",
    [
        "0006-one-move-ledger-replayed-for-undo.md",
        "0007-one-sort-pipeline-behind-a-composition-root.md",
        "0008-one-app-store-with-numbered-migrations.md",
    ],
)
def test_audit_adr_status_detects_missing_or_incomplete_implementation_section(
    tmp_path, adr_filename
):
    """Verify ADRs 0006, 0007, and 0008 fail audit when Implementation section is missing or omits open items."""
    adr_path = Path("docs/adr") / adr_filename
    baseline = adr_path.read_text(encoding="utf-8")
    without_impl = (
        baseline.split("## Implementation")[0]
        + "## Considered options"
        + baseline.split("## Considered options")[1]
    )

    bad_adr_dir = tmp_path / "adr"
    bad_adr_dir.mkdir()
    bad_adr = bad_adr_dir / adr_filename
    bad_adr.write_text(without_impl, encoding="utf-8")

    errors = audit_adr_status(doc_path=str(bad_adr))
    assert any("## Implementation" in e for e in errors)

    incomplete_impl = (
        baseline.split("## Implementation")[0]
        + "## Implementation\n\nActive: shipped.\n\n## Considered options"
        + baseline.split("## Considered options")[1]
    )
    incomplete_errors = audit_adr_status(
        content=incomplete_impl, doc_path=str(adr_path)
    )
    assert incomplete_errors

    with patch(
        "scripts.generate_docs.get_handwritten_docs", return_value=[str(bad_adr)]
    ):
        hw_errors = audit_handwritten_docs()
        assert any("## Implementation" in e for e in hw_errors)


def test_auto_merge_policy_bot_authors_consistency():
    """Verify that every entry in BOT_AUTHORS exists in docs/auto_merge_policy.md under '1. Risk Classification & Eligibility'."""
    from scripts.classify_pr import BOT_AUTHORS
    from scripts.generate_docs import _extract_markdown_section

    policy_file = Path("docs/auto_merge_policy.md")
    assert policy_file.exists(), f"{policy_file} does not exist"

    content = policy_file.read_text(encoding="utf-8")
    section_content = _extract_markdown_section(
        content, "1. Risk Classification & Eligibility"
    )
    assert section_content is not None, (
        "Missing '## 1. Risk Classification & Eligibility' section in docs/auto_merge_policy.md"
    )

    for bot in BOT_AUTHORS:
        assert bot in section_content, (
            f"Bot author '{bot}' from BOT_AUTHORS is missing from '1. Risk Classification & Eligibility' in docs/auto_merge_policy.md"
        )


def test_audit_auto_merge_policy_uses_section_heading_not_line_slice():
    """Verify audit_auto_merge_policy extracts '1. Risk Classification & Eligibility' dynamically regardless of line offsets."""
    baseline = Path("docs/auto_merge_policy.md").read_text(encoding="utf-8")
    shifted = ("<!-- preamble line -->\n" * 60) + baseline
    assert audit_auto_merge_policy(content=shifted) == []

    # Removing a bot from section 1 fails even if the bot appears in section 2
    section1_missing_stitch = baseline.replace("`stitch`, ", "", 1) + "\n`stitch`\n"
    errors = audit_auto_merge_policy(content=section1_missing_stitch)
    assert any("stitch" in e for e in errors)


def test_audit_auto_merge_policy_detects_missing_bot(tmp_path):
    bad_policy = tmp_path / "auto_merge_policy.md"
    bad_policy.write_text("# Auto-Merge Policy\nOnly dependabot[bot] is supported.\n")
    errors = audit_auto_merge_policy(doc_path=str(bad_policy))
    assert errors
    assert any("stitch" in e for e in errors)
    assert any("jules" in e for e in errors)
    assert any("github-actions[bot]" in e for e in errors)


def test_audit_model_hosts_clean():
    """Verify that audit_model_hosts succeeds on the repository documentation."""
    errors = audit_model_hosts()
    assert errors == []


def test_architecture_doc_model_hosts_and_egress_rules():
    """Verify that docs/architecture.md contains all ALLOWED_MODEL_HOSTS and egress rules."""
    from app.core.downloader import ALLOWED_MODEL_HOSTS

    arch_file = Path("docs/architecture.md")
    assert arch_file.exists(), f"{arch_file} does not exist"

    content = arch_file.read_text(encoding="utf-8")
    assert "### Model Downloader Lifecycle" in content
    section = content.split("### Model Downloader Lifecycle")[1].split("## ")[0]

    for host in ALLOWED_MODEL_HOSTS:
        assert host in section, (
            f"Allowed model host '{host}' is missing from {arch_file}"
        )

    assert "https" in section.lower(), (
        f"HTTPS scheme requirement missing from {arch_file}"
    )
    assert "ip address literal" in section.lower() or "ip literal" in section.lower(), (
        f"IP literal blocking missing from {arch_file}"
    )
    assert "ssrf" in section.lower(), f"SSRF prevention missing from {arch_file}"


def test_security_policy_model_hosts_and_ssrf_rules():
    """Verify that SECURITY.md contains all ALLOWED_MODEL_HOSTS and SSRF protection boundaries."""
    from app.core.downloader import ALLOWED_MODEL_HOSTS

    sec_file = Path("SECURITY.md")
    assert sec_file.exists(), f"{sec_file} does not exist"

    content = sec_file.read_text(encoding="utf-8")
    assert "## Telemetry and Network Access" in content
    section = content.split("## Telemetry and Network Access")[1].split("## ")[0]

    for host in ALLOWED_MODEL_HOSTS:
        assert host in section, (
            f"Allowed model host '{host}' is missing from {sec_file}"
        )

    assert "https" in section.lower(), (
        f"HTTPS scheme requirement missing from {sec_file}"
    )
    assert "ssrf" in section.lower(), (
        f"SSRF protection boundary missing from {sec_file}"
    )
    assert "ip literal" in section.lower(), (
        f"IP literal blocking missing from {sec_file}"
    )


def test_audit_model_hosts_detects_missing_architecture_host(tmp_path):
    """Verify audit_model_hosts detects when an allowed host is missing from docs/architecture.md."""
    bad_arch = tmp_path / "architecture.md"
    bad_arch.write_text(
        "### Model Downloader Lifecycle\n\n"
        "Model downloads only support https and huggingface.co with IP literal blocks and SSRF protection.\n"
    )
    errors = audit_model_hosts(
        architecture_path=str(bad_arch),
        security_path=None,
    )
    assert errors
    assert any("hf.co" in e for e in errors)


def test_audit_model_hosts_detects_missing_security_host(tmp_path):
    """Verify audit_model_hosts detects when an allowed host is missing from SECURITY.md."""
    bad_sec = tmp_path / "SECURITY.md"
    bad_sec.write_text(
        "# Security Policy\n\n"
        "## Telemetry and Network Access\n\n"
        "- **Model downloads** Approved hosts are hf.co over HTTPS with SSRF and IP literal blocks.\n"
    )
    errors = audit_model_hosts(
        architecture_path=None,
        security_path=str(bad_sec),
    )
    assert errors
    assert any("huggingface.co" in e for e in errors)


def test_audit_model_hosts_detects_missing_ssrf_rules(tmp_path):
    """Verify audit_model_hosts detects when SSRF or IP literal rules are omitted."""
    bad_arch = tmp_path / "architecture.md"
    bad_arch.write_text(
        "### Model Downloader Lifecycle\n\n"
        "Allowed hosts are huggingface.co and hf.co over https without any restrictions.\n"
    )
    bad_sec = tmp_path / "SECURITY.md"
    bad_sec.write_text(
        "## Telemetry and Network Access\n\n"
        "Approved hosts are huggingface.co and hf.co over HTTPS.\n"
    )
    errors = audit_model_hosts(
        architecture_path=str(bad_arch),
        security_path=str(bad_sec),
    )
    assert errors
    assert any("literal" in e.lower() for e in errors)
    assert any("ssrf" in e.lower() for e in errors)


def test_audit_model_hosts_detects_new_allowed_host(monkeypatch):
    """Verify audit_model_hosts reports an error if a new host is added to ALLOWED_MODEL_HOSTS without docs."""
    import app.core.downloader

    new_hosts = frozenset(
        app.core.downloader.ALLOWED_MODEL_HOSTS | {"models.example.org"}
    )
    monkeypatch.setattr(app.core.downloader, "ALLOWED_MODEL_HOSTS", new_hosts)

    errors = audit_model_hosts()
    assert errors
    assert any("models.example.org" in e for e in errors)


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
    """Verify that --strict (default) and --no-strict set mkdocs arguments correctly when --build is passed."""
    with patch("sys.argv", ["generate_docs.py", "--build", "--no-strict"]):
        with (
            patch("scripts.generate_docs.compile_diagram_assets") as mock_diag,
            patch("scripts.generate_docs.generate_tutorial_docs") as mock_tut,
            patch("scripts.generate_docs.generate_api_docs") as mock_api,
            patch("scripts.generate_docs.generate_ui_docs") as mock_ui,
            patch("scripts.generate_docs.generate_admin_guide") as mock_admin,
            patch("scripts.generate_docs.update_security_md") as mock_sec,
            patch("scripts.generate_docs.validate_mermaid_diagrams", return_value=[]),
            patch(
                "scripts.generate_docs.validate_mkdocs_site", return_value=[]
            ) as mock_val_site,
            patch("subprocess.run") as mock_run,
            patch.dict("sys.modules", {"mkdocs": MagicMock()}),
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
            mock_val_site.assert_called_once_with()


def test_main_default_strict():
    """Verify that with --build, mkdocs is run with --strict by default."""
    with patch("sys.argv", ["generate_docs.py", "--build"]):
        with (
            patch("scripts.generate_docs.compile_diagram_assets") as mock_diag,
            patch("scripts.generate_docs.generate_tutorial_docs") as mock_tut,
            patch("scripts.generate_docs.generate_api_docs") as mock_api,
            patch("scripts.generate_docs.generate_ui_docs") as mock_ui,
            patch("scripts.generate_docs.generate_admin_guide") as mock_admin,
            patch("scripts.generate_docs.update_security_md") as mock_sec,
            patch("scripts.generate_docs.validate_mermaid_diagrams", return_value=[]),
            patch(
                "scripts.generate_docs.validate_mkdocs_site", return_value=[]
            ) as mock_val_site,
            patch("subprocess.run") as mock_run,
            patch.dict("sys.modules", {"mkdocs": MagicMock()}),
        ):
            mock_run.return_value = MagicMock(returncode=0)

            main()

            mock_diag.assert_called_once()
            mock_tut.assert_called_once()
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            cmd = args[0]
            assert "--strict" in cmd
            mock_val_site.assert_called_once_with()


def test_main_exits_nonzero_when_mkdocs_site_validation_fails(capsys):
    """Verify main() exits with code 1 when --build runs and validate_mkdocs_site reports invalid artifacts."""
    with patch("sys.argv", ["generate_docs.py", "--build"]):
        with (
            patch("scripts.generate_docs.compile_diagram_assets"),
            patch("scripts.generate_docs.generate_tutorial_docs"),
            patch("scripts.generate_docs.generate_api_docs"),
            patch("scripts.generate_docs.generate_ui_docs"),
            patch("scripts.generate_docs.generate_admin_guide"),
            patch("scripts.generate_docs.generate_keyboard_docs"),
            patch("scripts.generate_docs.update_security_md"),
            patch("scripts.generate_docs.sync_config_schema"),
            patch("scripts.generate_docs.validate_mermaid_diagrams", return_value=[]),
            patch(
                "scripts.generate_docs.validate_mkdocs_site",
                return_value=["Missing MkDocs root page: site/index.html"],
            ),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch.dict("sys.modules", {"mkdocs": MagicMock()}),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1
            captured = capsys.readouterr()
            assert "MkDocs site validation failed" in captured.err
            assert "Missing MkDocs root page: site/index.html" in captured.err


def test_validate_mkdocs_site_valid_and_invalid_directories(tmp_path):
    """Verify validate_mkdocs_site catches missing/empty index.html and missing/empty sitemap.xml."""
    # 1. Nonexistent directory
    missing_dir = tmp_path / "nonexistent_site"
    errors = validate_mkdocs_site(str(missing_dir))
    assert any("Missing MkDocs root page" in e for e in errors)
    assert any("Missing MkDocs sitemap" in e for e in errors)

    # 2. Empty index.html and sitemap.xml with zero <loc> entries
    empty_site = tmp_path / "empty_site"
    empty_site.mkdir()
    (empty_site / "index.html").write_text("   \n", encoding="utf-8")
    (empty_site / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>\n',
        encoding="utf-8",
    )
    errors = validate_mkdocs_site(str(empty_site))
    assert any("Empty MkDocs root page" in e for e in errors)
    assert any("no <loc> entries" in e for e in errors)

    # 3. Malformed sitemap.xml
    malformed_site = tmp_path / "malformed_site"
    malformed_site.mkdir()
    (malformed_site / "index.html").write_text(
        "<!doctype html><html><body>Home</body></html>", encoding="utf-8"
    )
    (malformed_site / "sitemap.xml").write_text("<urlset><unclosed>", encoding="utf-8")
    errors = validate_mkdocs_site(str(malformed_site))
    assert any("Invalid XML in MkDocs sitemap" in e for e in errors)

    # 4. Valid site directory
    valid_site = tmp_path / "valid_site"
    valid_site.mkdir()
    (valid_site / "index.html").write_text(
        "<!doctype html><html><body><h1>Smart AutoSorter AI Pro</h1></body></html>",
        encoding="utf-8",
    )
    (valid_site / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        "  <url><loc>https://fderuiter.github.io/sortify/</loc></url>\n"
        "</urlset>\n",
        encoding="utf-8",
    )
    assert validate_mkdocs_site(str(valid_site)) == []


def test_mkdocs_strict_build_generates_index_logo_links_and_sitemap(tmp_path):
    """Integration test running mkdocs build --strict -d to verify index.html, logo links, and sitemap.xml."""
    import re
    import subprocess
    import sys

    from defusedxml import ElementTree as ET

    pytest.importorskip("mkdocs")

    site_dir = tmp_path / "site"
    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "-d", str(site_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"mkdocs build --strict failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    assert validate_mkdocs_site(str(site_dir)) == []

    # Verify root landing page exists and embeds core architecture diagram
    root_index = site_dir / "index.html"
    assert root_index.is_file()
    index_html = root_index.read_text(encoding="utf-8")
    assert "Smart AutoSorter AI Pro" in index_html
    assert "assets/diagrams/core_architecture.svg" in index_html

    # Verify logo links across root and nested pages resolve to site/index.html
    logo_href_re = re.compile(
        r'<a[^>]*class="[^"]*\bmd-logo\b[^"]*"[^>]*href="([^"]+)"'
        r'|<a[^>]*href="([^"]+)"[^>]*class="[^"]*\bmd-logo\b[^"]*"'
    )
    pages_to_check = [
        root_index,
        site_dir / "user_guide" / "index.html",
        site_dir / "tutorials" / "01_ml_analyzer_clustering" / "index.html",
    ]
    for page_path in pages_to_check:
        assert page_path.is_file(), f"Expected built page {page_path}"
        html = page_path.read_text(encoding="utf-8")
        match = logo_href_re.search(html)
        assert match is not None, f"Missing md-logo anchor in {page_path}"
        href = match.group(1) or match.group(2)
        resolved = (page_path.parent / href).resolve()
        target_index = resolved / "index.html" if resolved.is_dir() else resolved
        assert target_index == root_index.resolve(), (
            f"Logo link '{href}' in {page_path} resolved to {target_index}, expected {root_index.resolve()}"
        )

    # Verify sitemap.xml has canonical <loc> URLs for root and existing pages
    sitemap_root = ET.fromstring((site_dir / "sitemap.xml").read_text(encoding="utf-8"))
    locs = {
        (elem.text or "").strip()
        for elem in sitemap_root.iter()
        if (elem.tag == "loc" or elem.tag.endswith("}loc"))
        and (elem.text or "").strip()
    }
    assert "https://fderuiter.github.io/sortify/" in locs
    assert "https://fderuiter.github.io/sortify/user_guide/" in locs
    assert all(url.startswith("https://fderuiter.github.io/sortify/") for url in locs)


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


def test_main_missing_mkdocs_logs_warning_and_skips_build(capsys):
    """Verify that when --build is requested and mkdocs is missing, main() logs a warning and skips build cleanly."""
    with patch("sys.argv", ["generate_docs.py", "--build"]):
        with (
            patch("scripts.generate_docs.compile_diagram_assets"),
            patch("scripts.generate_docs.generate_tutorial_docs"),
            patch("scripts.generate_docs.generate_api_docs"),
            patch("scripts.generate_docs.generate_ui_docs"),
            patch("scripts.generate_docs.generate_admin_guide"),
            patch("scripts.generate_docs.update_security_md"),
            patch("scripts.generate_docs.validate_mermaid_diagrams", return_value=[]),
            patch("subprocess.run") as mock_run,
            patch.dict("sys.modules", {"mkdocs": None}),
        ):
            main()

            mock_run.assert_not_called()
            captured = capsys.readouterr()
            assert (
                "Warning: 'mkdocs' is not installed in the current environment."
                in captured.err
            )
            assert "Skipping MkDocs HTML site generation." in captured.err


def test_main_detects_unsynced_files_on_check():
    """Verify that --check detects modified files and exits with code 1."""
    original_open = open
    with patch("sys.argv", ["generate_docs.py", "--check", "--no-build"]):
        with (
            patch("scripts.generate_docs.compile_diagram_assets"),
            patch("scripts.generate_docs.generate_tutorial_docs"),
            patch("scripts.generate_docs.generate_api_docs"),
            patch("scripts.generate_docs.generate_ui_docs"),
            patch("scripts.generate_docs.generate_admin_guide"),
            patch("scripts.generate_docs.generate_keyboard_docs"),
            patch("scripts.generate_docs.update_security_md"),
            patch("scripts.generate_docs.sync_config_schema"),
            patch("scripts.generate_docs.validate_mermaid_diagrams", return_value=[]),
            patch("scripts.generate_docs.audit_handwritten_docs", return_value=[]),
            patch("subprocess.run") as mock_run,
            patch("scripts.generate_docs.os.path.exists", return_value=True),
            patch("builtins.open") as mock_open,
            patch("sys.exit") as mock_exit,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            file_contents = {
                Path("docs/api_reference.md").as_posix(): ["content1", "content1"],
                Path("docs/ui.md").as_posix(): ["content2", "different_content2"],
                Path("docs/admin_guide.md").as_posix(): ["content3", "content3"],
                "SECURITY.md": ["content4", "content4"],
                Path("app/config_schema.json").as_posix(): ["schema1", "schema1"],
            }

            counters = dict.fromkeys(file_contents, 0)

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
    with patch("sys.argv", ["generate_docs.py", "--check", "--no-build"]):
        with (
            patch("scripts.generate_docs.compile_diagram_assets"),
            patch("scripts.generate_docs.generate_tutorial_docs"),
            patch("scripts.generate_docs.generate_api_docs"),
            patch("scripts.generate_docs.generate_ui_docs"),
            patch("scripts.generate_docs.generate_admin_guide"),
            patch("scripts.generate_docs.generate_keyboard_docs"),
            patch("scripts.generate_docs.update_security_md"),
            patch("scripts.generate_docs.sync_config_schema"),
            patch("scripts.generate_docs.validate_mermaid_diagrams", return_value=[]),
            patch("scripts.generate_docs.audit_handwritten_docs", return_value=[]),
            patch("subprocess.run") as mock_run,
            patch("scripts.generate_docs.os.path.exists", return_value=True),
            patch("builtins.open") as mock_open,
            patch("sys.exit") as mock_exit,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            file_contents = {
                Path("docs/api_reference.md").as_posix(): ["content1", "content1"],
                Path("docs/ui.md").as_posix(): ["content2", "content2"],
                Path("docs/admin_guide.md").as_posix(): ["content3", "content3"],
                "SECURITY.md": ["content4", "content4"],
                Path("app/config_schema.json").as_posix(): ["schema1", "schema1"],
            }

            counters = dict.fromkeys(file_contents, 0)

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


def test_main_detects_unsynced_config_schema_on_check():
    """Verify that --check detects modified config_schema.json and exits with code 1."""
    original_open = open
    with patch("sys.argv", ["generate_docs.py", "--check", "--no-build"]):
        with (
            patch("scripts.generate_docs.compile_diagram_assets"),
            patch("scripts.generate_docs.generate_tutorial_docs"),
            patch("scripts.generate_docs.generate_api_docs"),
            patch("scripts.generate_docs.generate_ui_docs"),
            patch("scripts.generate_docs.generate_admin_guide"),
            patch("scripts.generate_docs.generate_keyboard_docs"),
            patch("scripts.generate_docs.update_security_md"),
            patch("scripts.generate_docs.sync_config_schema"),
            patch("scripts.generate_docs.validate_mermaid_diagrams", return_value=[]),
            patch("scripts.generate_docs.audit_handwritten_docs", return_value=[]),
            patch("subprocess.run") as mock_run,
            patch("scripts.generate_docs.os.path.exists", return_value=True),
            patch("builtins.open") as mock_open,
            patch("sys.exit") as mock_exit,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            file_contents = {
                Path("docs/api_reference.md").as_posix(): ["content1", "content1"],
                Path("docs/ui.md").as_posix(): ["content2", "content2"],
                Path("docs/admin_guide.md").as_posix(): ["content3", "content3"],
                "SECURITY.md": ["content4", "content4"],
                Path("app/config_schema.json").as_posix(): ["schema_old", "schema_new"],
            }

            counters = dict.fromkeys(file_contents, 0)

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


def test_main_sync_config_schema_task_registered_and_invoked():
    """Verify that sync_config_schema task is registered and invoked in main()."""
    with patch("sys.argv", ["generate_docs.py", "--no-build"]):
        with (
            patch("scripts.generate_docs.compile_diagram_assets"),
            patch("scripts.generate_docs.generate_tutorial_docs"),
            patch("scripts.generate_docs.generate_api_docs"),
            patch("scripts.generate_docs.generate_ui_docs"),
            patch("scripts.generate_docs.generate_admin_guide"),
            patch("scripts.generate_docs.generate_keyboard_docs"),
            patch("scripts.generate_docs.update_security_md"),
            patch("scripts.generate_docs.sync_config_schema") as mock_sync_schema,
            patch("scripts.generate_docs.validate_mermaid_diagrams", return_value=[]),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            main()
            mock_sync_schema.assert_called_once()


def test_sync_config_schema_task_execution(tmp_path):
    """Verify sync_config_schema normalizes LOG_FILE and writes schema to disk."""
    import json

    from scripts.validate_config import generate_config_schema, sync_config_schema

    schema = generate_config_schema()
    assert isinstance(schema, dict)
    assert "properties" in schema
    assert "LOG_FILE" in schema["properties"]
    assert schema["properties"]["LOG_FILE"]["default"] == "autosorter.log"

    target_file = tmp_path / "config_schema.json"
    updated = sync_config_schema(schema_path=target_file)
    assert updated is True
    assert target_file.exists()

    with open(target_file, "r", encoding="utf-8") as f:
        written_schema = json.load(f)
    assert written_schema == schema

    # Second run with unchanged content should return False
    updated_again = sync_config_schema(schema_path=target_file)
    assert updated_again is False


def test_component_diagram_spec_serialization():
    from scripts.diagram_schema import (
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

    from scripts.diagram_schema import DiagramNode

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
    from scripts.diagram_schema import ComponentDiagramSpec, DiagramNode

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
    B["Node B"]
    click B "docs/ui.md#appuiheading" "Tooltip B"
    C["Node C"]
    click C "docs/ui.md#appuimixinstree_view" "Tooltip C"
    D["Node D"]
    click D "https://example.com#section" "Tooltip D"
    E["Node E"]
    click E "#local-anchor" "Tooltip E"
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

    # Verify missing anchor in valid markdown target file triggers error
    mmd_missing_anchor = """graph TD
    A["Node A"]
    click A "docs/ui.md#missing_anchor_xyz" "Tooltip Missing Anchor"
    """
    errs_missing_anchor = parse_and_validate_click_directives(mmd_missing_anchor)
    assert len(errs_missing_anchor) == 1
    assert (
        "Line 3: Target anchor 'missing_anchor_xyz' does not exist in 'docs/ui.md'"
        in errs_missing_anchor[0]
    )

    # Verify empty anchor fragment triggers error
    mmd_empty_anchor = """graph TD
    A["Node A"]
    click A "docs/ui.md#" "Tooltip Empty Anchor"
    """
    errs_empty_anchor = parse_and_validate_click_directives(mmd_empty_anchor)
    assert len(errs_empty_anchor) == 1
    assert "Empty anchor fragment" in errs_empty_anchor[0]

    # Verify missing target file triggers error
    mmd_missing_file = """graph TD
    A["Node A"]
    click A "docs/nonexistent_doc.md#some_anchor" "Tooltip Missing File"
    """
    errs_missing_file = parse_and_validate_click_directives(mmd_missing_file)
    assert len(errs_missing_file) == 1
    assert (
        "Click directive target file 'docs/nonexistent_doc.md' does not exist"
        in errs_missing_file[0]
    )

    fallback = generate_fallback_svg("Test Title", mmd_valid)
    assert '<a href="https://example.com" target="_blank"' in fallback
    assert "🔗 Node A -&gt; Tooltip A" in fallback


def _fake_render(cmd, mmd_path, out_path, fmt="svg"):
    out_path.write_text("<svg>rendered</svg>", encoding="utf-8")
    return True


def test_diagram_toolchain_build_and_verify(tmp_path):
    from scripts.diagram_toolchain import build_diagrams

    with (
        patch("scripts.diagram_toolchain.find_mmdc_executable", return_value=["mmdc"]),
        patch("scripts.diagram_toolchain.is_browser_available", return_value=True),
        patch(
            "scripts.diagram_toolchain.render_diagram_artifact",
            side_effect=_fake_render,
        ),
    ):
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


def test_diagram_toolchain_verify_flags_svgs_not_rendered_from_current_spec(
    tmp_path,
):
    from scripts.diagram_toolchain import (
        build_diagrams,
        collect_all_specs,
        svg_source_hash,
    )

    spec_id, spec = next(iter(collect_all_specs().items()))
    with (
        patch(
            "scripts.diagram_toolchain.collect_all_specs", return_value={spec_id: spec}
        ),
        patch("scripts.diagram_toolchain.find_mmdc_executable", return_value=["mmdc"]),
        patch("scripts.diagram_toolchain.is_browser_available", return_value=True),
        patch(
            "scripts.diagram_toolchain.render_diagram_artifact",
            side_effect=_fake_render,
        ),
    ):
        assert build_diagrams(output_dir=tmp_path, force=True) is True
        svg_file = tmp_path / f"{spec_id}.svg"
        assert svg_source_hash(svg_file) is not None
        assert build_diagrams(output_dir=tmp_path, verify_only=True) is True

        # The spec changed but the SVG was not re-rendered.
        svg_file.write_text(
            svg_file.read_text(encoding="utf-8").replace(
                svg_source_hash(svg_file), "0" * 64
            ),
            encoding="utf-8",
        )
        assert build_diagrams(output_dir=tmp_path, verify_only=True) is False

        # A build re-renders it even though the build cache says it is current.
        assert build_diagrams(output_dir=tmp_path) is True
        assert build_diagrams(output_dir=tmp_path, verify_only=True) is True


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
        # Verify never writes assets, so with nothing rendered yet it fails
        verify_ok = build_diagrams(output_dir=tmp_path, force=True, verify_only=True)
        assert verify_ok is False
        assert list(tmp_path.glob("*.svg")) == []

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
    from scripts.diagram_schema import (
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
    from scripts.diagram_schema import (
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
    from scripts.diagram_schema import (
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

    from scripts.diagram_schema import ExternalEntityNode, ProcessNode

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
    from scripts.diagram_schema import (
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


def test_network_rules_vulnerability_database_egress_rules():
    """Verify network_rules.json contains valid egress rules for vulnerability database endpoints."""
    import json
    import re
    from pathlib import Path

    rules_path = Path("network_rules.json")
    assert rules_path.exists(), "network_rules.json must exist"

    raw_json = rules_path.read_text(encoding="utf-8")
    config = json.loads(raw_json)

    assert "rules" in config, "network_rules.json must contain 'rules' array"
    rules = config["rules"]

    # Verify JSON escaping uses '\\.' in source file so dot is regex-escaped
    assert "https://pypi\\\\.org" in raw_json
    assert "https://api\\\\.osv\\\\.dev" in raw_json
    assert "https://api\\\\.github\\\\.com/advisories" in raw_json

    # Locate required vulnerability database endpoints
    pypi_rule = next((r for r in rules if "pypi" in r.get("pattern", "")), None)
    osv_rule = next((r for r in rules if "osv" in r.get("pattern", "")), None)
    gh_rule = next((r for r in rules if "github" in r.get("pattern", "")), None)

    assert pypi_rule is not None, "Missing PyPI vulnerability database egress rule"
    assert osv_rule is not None, "Missing OSV database egress rule"
    assert gh_rule is not None, "Missing GitHub Security Advisory egress rule"

    for rule in (pypi_rule, osv_rule, gh_rule):
        assert rule.get("pattern"), "Rule must define pattern"
        assert rule.get("match_type") == "regex", "Rule match_type must be regex"
        assert rule.get("description"), "Rule must contain non-empty description"
        # Ensure pattern compiles as valid regex
        compiled = re.compile(rule["pattern"])
        assert compiled is not None

    # Verify literal dot matching and wildcard prevention
    assert re.search(pypi_rule["pattern"], "https://pypi.org")
    assert not re.search(pypi_rule["pattern"], "https://pypixorg")

    assert re.search(osv_rule["pattern"], "https://api.osv.dev")
    assert not re.search(osv_rule["pattern"], "https://apiXosvXdev")

    assert re.search(gh_rule["pattern"], "https://api.github.com/advisories")
    assert not re.search(gh_rule["pattern"], "https://apiXgithubXcom/advisories")


def test_update_security_md_scans_only_network_rules_targets(tmp_path, monkeypatch):
    import json

    from scripts.generate_docs import update_security_md

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "client.py").write_text(
        'URL = "https://shipped.example.org/api"\n'
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_client.py").write_text(
        'PROXY = "http://user:secret@fixture.example.net:8080"\n'
    )
    (tmp_path / "network_rules.json").write_text(
        json.dumps({"targets": ["app"], "rules": []})
    )
    (tmp_path / "SECURITY.md").write_text(
        "# Security Policy\n\n## Network Dependencies\n\nstale\n"
    )
    monkeypatch.chdir(tmp_path)

    with patch("scripts.validate_links.validate_url", return_value=(True, "ok", False)):
        update_security_md()

    security_md = (tmp_path / "SECURITY.md").read_text()
    assert "shipped.example.org" in security_md
    assert "fixture.example.net" not in security_md
    assert "stale" not in security_md


def test_compile_diagram_assets_fails_when_a_diagram_fails():
    """A diagram that doesn't render must fail the docs build, not just warn."""
    from scripts.generate_docs import compile_diagram_assets

    with patch("scripts.diagram_toolchain.build_diagrams", return_value=False):
        with pytest.raises(RuntimeError, match="failed schema validation or rendering"):
            compile_diagram_assets()

    with patch("scripts.diagram_toolchain.build_diagrams", return_value=True):
        compile_diagram_assets()


def test_format_key_matches_how_users_type_keys():
    assert _format_key("ctrl+s") == "Ctrl+S"
    assert _format_key("question_mark") == "?"
    assert _format_key("escape") == "Esc"
    assert _format_key("f1") == "F1"
    assert _format_key("x") == "x"
    assert _format_key("G") == "Shift+G"
    assert _format_key("shift+g") == "Shift+G"


def test_collect_bindings_groups_keys_by_description(tmp_path):
    src = tmp_path / "tui.py"
    src.write_text(
        "class HelpModal:\n"
        "    BINDINGS = [\n"
        '        Binding("escape", "close", "Close dialog"),\n'
        '        Binding("f1", "close", "Close dialog", show=False),\n'
        "    ]\n"
        "class AutoSorterTUI:\n"
        '    BINDINGS = [Binding("ctrl+s", "scan_directory", "Scan")]\n'
    )
    found = _collect_bindings(str(src))
    assert found["HelpModal"] == ("Help", [(["Esc", "F1"], "Close dialog")])
    assert found["AutoSorterTUI"][1] == [(["Ctrl+S"], "Scan")]


def test_keyboard_shortcuts_page_is_in_sync():
    """The committed page must match the TUI bindings (run `uv run python scripts/generate_docs.py`)."""
    from scripts.generate_docs import generate_keyboard_docs

    page = Path("docs/keyboard_shortcuts.md")
    before = page.read_text(encoding="utf-8")
    try:
        generate_keyboard_docs()
        assert page.read_text(encoding="utf-8") == before
    finally:
        page.write_text(before, encoding="utf-8", newline="\n")


def test_config_reference_describes_types_defaults_and_bounds():
    from app.config import Settings

    fields = Settings.model_fields
    assert _describe_type(fields["IGNORED_EXTENSIONS"].annotation) == "list[str]"
    assert _describe_type(fields["MAX_FOLDERS"].annotation) == "int"
    assert _describe_bounds(fields["MAX_FOLDERS"]) == "> 0, <= 50"
    assert (
        _describe_bounds(fields["CONFLICT_POLICY"]) == "`skip`, `rename`, `overwrite`"
    )
    # default_factory fields used to render as "PydanticUndefined".
    assert _describe_default(fields["KEYWORD_RULES"]) == "`{}`"
    assert "built-in entries" in _describe_default(fields["STOP_WORDS"])


def test_user_guide_rule_migration_and_export_section():
    """Verify that docs/user_guide.md contains rule migration, importer, conflict resolution, and export documentation."""
    guide_path = Path("docs/user_guide.md")
    assert guide_path.exists(), "docs/user_guide.md must exist"

    content = guide_path.read_text(encoding="utf-8")

    # Section heading and anchor
    assert "## Rule Migration and Export" in content
    assert '<a id="rule-migration-and-export"></a>' in content

    # Template gallery navigation from settings
    assert "[Ctrl+O]" in content
    assert "Starter Rule Template Gallery" in content
    assert "#btn-open-templates" in content
    assert "#btn-import-rules" in content
    assert "#btn-export-rules" in content

    # Importer strategies and supported competitor formats
    assert "Hazel" in content
    assert ".hazelrules" in content
    assert "HazelRuleImporter" in content
    assert "DropIt" in content
    assert "DropItRuleImporter" in content
    assert "TagSpaces" in content
    assert "taggroup.json" in content
    assert "TagSpacesRuleImporter" in content

    # Conflict resolution
    assert "keep_existing" in content
    assert "overwrite" in content
    assert "skip_conflicting" in content
    assert "#dt-rule-conflicts" in content

    # Export formats and workflow
    assert "Sortify JSON" in content
    assert "sortify_json" in content
    assert "hazel" in content
    assert "dropit_ini" in content
    assert "#btn-do-export" in content

    # Edge cases: fallback behaviors and TagSpaces non-export asymmetry
    assert "unmapped" in content.lower() or "unrecognized" in content.lower()
    assert "syntax error" in content.lower()
    assert (
        "exporting back to native tagspaces" in content.lower()
        or "not export back to native tagspaces" in content.lower()
    )


def test_user_guide_rule_placement_cross_reference():
    """Verify that How Files Are Placed in docs/user_guide.md references imported rules and links to rule migration."""
    guide_path = Path("docs/user_guide.md")
    assert guide_path.exists(), "docs/user_guide.md must exist"

    content = guide_path.read_text(encoding="utf-8")
    assert "## How Files Are Placed" in content

    section = content.split("## How Files Are Placed")[1].split("## ")[0]
    step3_line = next(
        (line for line in section.splitlines() if line.startswith("3.")), None
    )
    assert step3_line is not None, "Missing numbered step 3 in How Files Are Placed"
    assert "imported" in step3_line.lower()
    assert "policies" in step3_line.lower()
    assert "[Rule Migration and Export](#rule-migration-and-export)" in step3_line


def test_user_guide_active_rule_manager_section():
    """Verify that docs/user_guide.md documents Active Rule Manager modal, shortcuts, and launch workflows."""
    guide_path = Path("docs/user_guide.md")
    assert guide_path.exists(), "docs/user_guide.md must exist"

    content = guide_path.read_text(encoding="utf-8")

    # Verify review step mentions F2 for rename and Ctrl+R for active rule manager
    review_section = content.split("## Sorting a Folder")[1].split("\n## ")[0]
    step3_line = next(
        (line for line in review_section.splitlines() if line.startswith("3.")), None
    )
    assert step3_line is not None, "Missing review step 3 in Sorting a Folder"
    assert "[F2]" in step3_line
    assert "[Ctrl+R]" in step3_line
    assert "active rule manager" in step3_line.lower()

    # Active Rule Manager subsection & anchor
    assert "### Active Rule Manager" in content
    assert '<a id="active-rule-manager"></a>' in content

    # Launch methods
    assert "[Ctrl+R]" in content
    assert "#btn-open-rules" in content

    # Shortcuts and controls
    assert "#rules-table" in content
    assert "#btn-toggle-rule" in content
    assert "#btn-add-rule" in content
    assert "#btn-edit-rule" in content
    assert "#btn-delete-rule" in content
    assert "#btn-close-rules" in content
    assert "[Space]" in content
    assert "[Enter]" in content
    assert "[Delete]" in content
    assert "[Esc]" in content

    # Focus prerequisite / edge cases
    assert "text input" in content.lower()


def test_user_guide_command_palette_section():
    """Verify that docs/user_guide.md contains the command palette section, anchor, shortcuts, navigation, and workflows."""
    guide_path = Path("docs/user_guide.md")
    assert guide_path.exists(), "docs/user_guide.md must exist"

    content = guide_path.read_text(encoding="utf-8")

    # Section heading and anchor
    assert "## Command Palette" in content
    assert '<a id="command-palette"></a>' in content

    # Palette shortcut and controls
    assert "[Ctrl+K]" in content
    assert "[Up]" in content
    assert "[Down]" in content
    assert "[Enter]" in content
    assert "[Esc]" in content
    assert "#palette-input" in content
    assert "#palette-list" in content

    # Search filtering documentation
    assert "search filtering" in content.lower() or "filter" in content.lower()

    # Workflows without dedicated shortcuts
    assert "Model Manager" in content
    assert "CRO Forensic Ingestion" in content
    assert "Export Active Rules" in content
    assert "Import External Rules" in content
    assert "Export Simulation Report" in content
    assert "Browse Starter Rule Templates" in content

    # Cross reference in sorting instructions
    sorting_section = content.split("## Sorting a Folder")[1].split("\n## ")[0]
    assert "[Ctrl+K]" in sorting_section
    assert "[Command Palette](#command-palette)" in sorting_section
    assert "without memorizing keybindings" in sorting_section


def test_user_guide_rename_shortcut_and_tui_bindings():
    """Verify that docs/user_guide.md cites [F2] for renaming, does not associate [Ctrl+R] with renaming, and AutoSorterTUI bindings prevent key drift."""
    guide_path = Path("docs/user_guide.md")
    assert guide_path.exists(), "docs/user_guide.md must exist"

    content = guide_path.read_text(encoding="utf-8")
    review_section = content.split("## Sorting a Folder")[1].split("\n## ")[0]
    step3_line = next(
        (line for line in review_section.splitlines() if line.startswith("3.")), None
    )
    assert step3_line is not None, "Missing review step 3 in Sorting a Folder"
    assert "[F2]" in step3_line
    assert "rename" in step3_line.lower()

    # Assert docs/user_guide.md cites [F2] for folder renaming and does not associate [Ctrl+R] with renaming
    assert "[Ctrl+R] renames" not in content
    assert "[Ctrl+R] rename" not in content

    # Assert that AutoSorterTUI.BINDINGS retains ctrl+r for open_active_rules and f2 for rename_node
    from app.ui.tui import AutoSorterTUI

    bindings_by_key = {
        getattr(b, "key", None): getattr(b, "action", None)
        for b in AutoSorterTUI.BINDINGS
    }
    assert bindings_by_key.get("ctrl+r") == "open_active_rules"
    assert bindings_by_key.get("f2") == "rename_node"


def test_admin_guide_field_descriptions_policies_and_env_vars():
    """Verify docs/admin_guide.md documents all Settings field descriptions, 6 policy types, 5 actions, 8 post-unarchive values, and runtime env vars."""
    from app.config import Settings
    from app.core.policy_engine import PolicyEngine

    admin_content = Path("docs/admin_guide.md").read_text(encoding="utf-8")

    for name, field_info in Settings.model_fields.items():
        assert f"### `{name}`" in admin_content
        assert field_info.description in admin_content

    for policy_type in (
        "keyword",
        "pattern",
        "override",
        "action",
        "compliance",
        "unarchive",
    ):
        assert f"- **`{policy_type}`**:" in admin_content

    assert (
        "lowercase filename (`os.path.basename(file_path).lower()`) only"
        in admin_content
    )
    assert "lowercase filename or full file path (`file_path.lower()`)" in admin_content

    for action_name in PolicyEngine.SUPPORTED_ACTIONS:
        assert f"- **`{action_name}`**:" in admin_content

    for post_val in (
        "delete",
        "remove",
        "cleanup",
        "move",
        "archive",
        "relocate",
        "retain",
        "keep",
    ):
        assert f"'{post_val}'" in admin_content

    for env_var in (
        "SORTIFY_AI_CONSENT",
        "AUTOSORTER_APP_DIR",
        "NON_INTERACTIVE",
        "NO_COLOR",
        "FORCE_TUI",
        "IGNORE_TERMINAL_SIZE",
        "LITE_BUILD",
        "EASYOCR_MODULE_PATH",
        "SMART_AUTOSORTER_MODELS_ZIP",
        "MODELS_ZIP_PATH",
        "MODEL_PATH",
        "PROMPT_DUMP_FILE",
    ):
        assert f"`{env_var}`" in admin_content


def test_admin_guide_nested_subcommands_json_contracts_and_no_gui():
    """Verify docs/admin_guide.md renders nested ledger/quarantine subcommands, JSON contracts, and omits removed GUI flags."""
    admin_content = Path("docs/admin_guide.md").read_text(encoding="utf-8")

    for sub in ("list", "inspect", "process", "release", "dismiss"):
        assert f"###### `quarantine {sub}`" in admin_content

    for sub in ("status", "reconcile", "purge", "export"):
        assert f"###### `ledger {sub}`" in admin_content

    assert (
        "#### Structured JSON Output, Error Payloads, and Exit Codes" in admin_content
    )
    assert "emit_cli_error" in admin_content
    assert "SortifyArgumentParser.error()" in admin_content
    assert "'partial_success'" in admin_content
    assert "--gui" not in admin_content
    assert "FORCE_GUI" not in admin_content

    user_guide_content = Path("docs/user_guide.md").read_text(encoding="utf-8")
    assert '--set STOP_WORDS "word1,word2,word3"' in user_guide_content
    assert '--set STOP_WORDS \'["word1", "word2", "word3"]\'' in user_guide_content
    assert "--gui" not in user_guide_content


def test_api_reference_includes_dunder_all_init_packages():
    """Verify docs/api_reference.md includes package __init__.py modules defining __all__ such as app.cli."""
    api_content = Path("docs/api_reference.md").read_text(encoding="utf-8")
    assert "## `app.cli`" in api_content
    assert "::: app.cli" in api_content


def test_diagram_toolchain_verify_only_never_mutates_mmd_or_creates_dir(tmp_path):
    """Verify verify_only=True does not create missing output_dir or overwrite drifted .mmd files."""
    from scripts.diagram_toolchain import build_diagrams

    missing_dir = tmp_path / "nonexistent_diagrams"
    assert build_diagrams(output_dir=missing_dir, verify_only=True) is False
    assert not missing_dir.exists()

    out_dir = tmp_path / "existing_diagrams"
    out_dir.mkdir()
    drifted_mmd = out_dir / "core_architecture.mmd"
    drifted_mmd.write_text("flowchart TD\n  STALE --> NODE\n", encoding="utf-8")
    assert build_diagrams(output_dir=out_dir, verify_only=True) is False
    assert drifted_mmd.read_text(encoding="utf-8") == "flowchart TD\n  STALE --> NODE\n"


def test_update_security_md_worker_exception_raises_critical(tmp_path, monkeypatch):
    """Verify worker exceptions during update_security_md fail critically rather than warning."""
    import json

    from scripts.generate_docs import update_security_md

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "net.py").write_text(
        'ENDPOINT = "https://unreachable.invalid/v1"\n', encoding="utf-8"
    )
    (tmp_path / "network_rules.json").write_text(
        json.dumps({"targets": ["app"], "rules": []}), encoding="utf-8"
    )
    sec_md = tmp_path / "SECURITY.md"
    initial_sec = "# Security Policy\n\n## Network Dependencies\n\nstale\n"
    sec_md.write_text(initial_sec, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with patch(
        "scripts.validate_links.validate_url",
        side_effect=RuntimeError("worker boom"),
    ):
        with pytest.raises(Exception, match="failed connectivity validation"):
            update_security_md(sec_file=str(sec_md))

    assert sec_md.read_text(encoding="utf-8") == initial_sec


def test_update_security_md_excludes_xml_namespaces_and_ui_placeholders(
    tmp_path, monkeypatch
):
    """Verify XML xmlns URIs and UI placeholder URLs are omitted from SECURITY.md."""
    import json

    (tmp_path / "app" / "ui" / "modals").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "app" / "ui" / "modals" / "models.py").write_text(
        'Input(placeholder="http://proxy.example.com:8080", id="inp-proxy")\n'
        'Input(placeholder="Optional HTTP/HTTPS proxy URL (e.g. http://corp-proxy.example.net:8080)", id="input-wizard-proxy")\n'
        'LIVE_URL = "https://shipped.example.org/models"\n',
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "diagram_toolchain.py").write_text(
        'SVG = \'<svg xmlns="http://www.w3.org/2000/svg"><div xmlns="http://www.w3.org/1999/xhtml"></div></svg>\'\n',
        encoding="utf-8",
    )
    (tmp_path / "network_rules.json").write_text(
        json.dumps({"targets": ["app", "scripts"], "rules": []}), encoding="utf-8"
    )
    sec_md = tmp_path / "SECURITY.md"
    sec_md.write_text(
        "# Security Policy\n\n## Network Dependencies\n\nplaceholder\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    update_security_md(sec_file=str(sec_md), local_only=True)
    rendered = sec_md.read_text(encoding="utf-8")
    assert "shipped.example.org" in rendered
    assert "proxy.example.com" not in rendered
    assert "corp-proxy.example.net" not in rendered
    assert "www.w3.org" not in rendered


def test_committed_security_md_omits_placeholder_and_xml_namespaces():
    """Verify committed SECURITY.md omits proxy.example.com:8080 and www.w3.org."""
    sec_text = Path("SECURITY.md").read_text(encoding="utf-8")
    assert "proxy.example.com" not in sec_text
    assert "www.w3.org" not in sec_text


def test_generate_docs_local_only_performs_zero_network_calls():
    """Verify generate_docs.py --check --no-build --local-only succeeds offline without network sockets."""
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["HTTPS_PROXY"] = "http://127.0.0.1:9"
    env["HTTP_PROXY"] = "http://127.0.0.1:9"
    env.pop("COLUMNS", None)
    env.pop("AUTOSORTER_APP_DIR", None)
    result = subprocess.run(
        [
            sys.executable,
            "scripts/generate_docs.py",
            "--check",
            "--no-build",
            "--local-only",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_audit_handwritten_docs_memoizes_adr_inspections():
    """Verify _inspect_adr_runtime_state and _count_frontend_store_callers run once across ADR checks."""
    import scripts.generate_docs as gen_docs

    gen_docs._inspect_adr_runtime_state.cache_clear()
    gen_docs._count_frontend_store_callers.cache_clear()

    errors = audit_handwritten_docs(local_only=True)
    assert errors == []

    runtime_info = gen_docs._inspect_adr_runtime_state.cache_info()
    store_info = gen_docs._count_frontend_store_callers.cache_info()
    assert runtime_info.currsize == 1
    assert runtime_info.misses == 1
    assert store_info.currsize == 1
    assert store_info.misses == 1

    # Subsequent ADR status check reuses the memoized state (cache hit)
    assert audit_adr_status() == []
    assert gen_docs._inspect_adr_runtime_state.cache_info().hits >= 1


def test_main_defaults_to_offline_and_skips_mkdocs_build():
    """Verify bare generate_docs.py defaults local_only=True and skips mkdocs build, while --live-links opts into live checks."""
    with patch("sys.argv", ["generate_docs.py"]):
        with (
            patch("scripts.generate_docs.compile_diagram_assets"),
            patch("scripts.generate_docs.generate_tutorial_docs"),
            patch("scripts.generate_docs.generate_api_docs"),
            patch("scripts.generate_docs.generate_ui_docs"),
            patch("scripts.generate_docs.generate_admin_guide"),
            patch("scripts.generate_docs.generate_keyboard_docs"),
            patch("scripts.generate_docs.update_security_md") as mock_sec,
            patch("scripts.generate_docs.sync_config_schema"),
            patch("scripts.generate_docs.validate_mermaid_diagrams", return_value=[]),
            patch("subprocess.run") as mock_run,
            patch.dict("sys.modules", {"mkdocs": MagicMock()}),
        ):
            main()
            mock_sec.assert_called_once_with(local_only=True)
            mock_run.assert_not_called()

    with patch("sys.argv", ["generate_docs.py", "--live-links"]):
        with (
            patch("scripts.generate_docs.compile_diagram_assets"),
            patch("scripts.generate_docs.generate_tutorial_docs"),
            patch("scripts.generate_docs.generate_api_docs"),
            patch("scripts.generate_docs.generate_ui_docs"),
            patch("scripts.generate_docs.generate_admin_guide"),
            patch("scripts.generate_docs.generate_keyboard_docs"),
            patch("scripts.generate_docs.update_security_md") as mock_sec_live,
            patch("scripts.generate_docs.sync_config_schema"),
            patch("scripts.generate_docs.validate_mermaid_diagrams", return_value=[]),
            patch("subprocess.run") as mock_run_live,
        ):
            main()
            mock_sec_live.assert_called_once_with(local_only=False)
            mock_run_live.assert_not_called()


def test_generate_admin_guide_isolates_columns_app_dir_and_sortify_env(
    tmp_path, monkeypatch
):
    """Verify generate_admin_guide() pins COLUMNS=80, clears AUTOSORTER_APP_DIR and SORTIFY_*, and restores env."""
    import os

    import scripts.generate_docs as gen_docs

    committed_admin = Path("docs/admin_guide.md").read_text(encoding="utf-8")
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    uncreated_app_dir = tmp_path / "leaked_app_dir"

    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(uncreated_app_dir))
    monkeypatch.setenv("SORTIFY_MAX_FOLDERS", "999")
    monkeypatch.setattr(gen_docs, "_CHECK_STAGE_DIR", stage_dir)

    gen_docs.generate_admin_guide()

    rendered = (stage_dir / "docs" / "admin_guide.md").read_text(encoding="utf-8")
    assert "`'~/.autosorter/autosorter.log'`" in rendered
    assert str(uncreated_app_dir) not in rendered
    assert rendered == committed_admin
    assert not uncreated_app_dir.exists()

    # Caller environment must be restored untouched
    assert os.environ.get("COLUMNS") == "120"
    assert os.environ.get("AUTOSORTER_APP_DIR") == str(uncreated_app_dir)
    assert os.environ.get("SORTIFY_MAX_FOLDERS") == "999"

    # When COLUMNS was originally unset, it must not remain in os.environ
    monkeypatch.delenv("COLUMNS", raising=False)
    gen_docs.generate_admin_guide()
    assert "COLUMNS" not in os.environ


def test_generate_docs_offline_check_and_sync_under_host_env(tmp_path):
    """Verify bare generate_docs.py --check and write-mode sync succeed offline under COLUMNS=120, AUTOSORTER_APP_DIR, and SORTIFY_MAX_FOLDERS=999."""
    import os
    import subprocess
    import sys

    import scripts.generate_docs as gen_docs

    uncreated_app_dir = tmp_path / "host_env_app_dir"
    env = dict(os.environ)
    env["COLUMNS"] = "120"
    env["AUTOSORTER_APP_DIR"] = str(uncreated_app_dir)
    env["SORTIFY_MAX_FOLDERS"] = "999"
    env["HTTPS_PROXY"] = "http://127.0.0.1:9"
    env["HTTP_PROXY"] = "http://127.0.0.1:9"
    env.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")

    result = subprocess.run(
        [sys.executable, "scripts/generate_docs.py", "--check"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert not uncreated_app_dir.exists()
    assert not Path("site").exists()

    # Also verify bare write-mode sync (redirected to tmp_path) makes zero live network calls
    stage_dir = tmp_path / "sync_out"
    stage_dir.mkdir()

    def _redirect_output(rel_path: str) -> str:
        target = stage_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    sec_copy = stage_dir / "SECURITY.md"
    sec_copy.write_text(
        Path("SECURITY.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    orig_update_sec = update_security_md

    with (
        patch.dict(
            os.environ,
            {
                "COLUMNS": "120",
                "AUTOSORTER_APP_DIR": str(uncreated_app_dir),
                "SORTIFY_MAX_FOLDERS": "999",
            },
        ),
        patch("sys.argv", ["generate_docs.py"]),
        patch.object(gen_docs, "_resolve_output_path", side_effect=_redirect_output),
        patch.object(
            gen_docs,
            "update_security_md",
            side_effect=lambda local_only=True: orig_update_sec(
                sec_file=str(sec_copy), local_only=local_only
            ),
        ) as spy_sec,
        patch.object(gen_docs, "compile_diagram_assets"),
        patch.object(
            gen_docs,
            "sync_config_schema",
            side_effect=lambda: __import__(
                "scripts.validate_config", fromlist=["sync_config_schema"]
            ).sync_config_schema(schema_path=stage_dir / "app" / "config_schema.json"),
        ),
        patch("subprocess.run") as mock_mkdocs,
    ):
        main()
        spy_sec.assert_called_once_with(local_only=True)
        mock_mkdocs.assert_not_called()

    assert not uncreated_app_dir.exists()
    assert (stage_dir / "docs" / "admin_guide.md").read_text(encoding="utf-8") == Path(
        "docs/admin_guide.md"
    ).read_text(encoding="utf-8")


def test_validate_mkdocs_config_accepts_allowlisted_superfences_tag_and_rejects_unsafe_tags(
    tmp_path,
):
    assert validate_mkdocs_config("mkdocs.yml") == []

    valid_cfg = tmp_path / "valid_mkdocs.yml"
    valid_cfg.write_text(
        "site_name: Sortify\n"
        "markdown_extensions:\n"
        "  - pymdownx.superfences:\n"
        "      custom_fences:\n"
        "        - name: mermaid\n"
        "          class: mermaid\n"
        "          format: !!python/name:pymdownx.superfences.fence_code_format\n",
        encoding="utf-8",
    )
    assert validate_mkdocs_config(valid_cfg) == []

    for bad_snippet, expected_substr in (
        (
            "site_name: !!python/object/apply:os.system ['echo pwned']\n",
            "python/object/apply:os.system",
        ),
        (
            "site_name: Sortify\n"
            "markdown_extensions:\n"
            "  - pymdownx.superfences:\n"
            "      format: !!python/name:os.system\n",
            "python/name:os.system",
        ),
        (
            "site_name: !ENV [SITE_NAME, Sortify]\n",
            "!ENV",
        ),
        (
            "site_name: !!python/name:pymdownx.superfences.fence_code_format unexpected_arg\n",
            "empty scalar",
        ),
    ):
        bad_cfg = tmp_path / "bad_mkdocs.yml"
        bad_cfg.write_text(bad_snippet, encoding="utf-8")
        errors = validate_mkdocs_config(bad_cfg)
        assert errors, f"Expected errors for {bad_snippet!r}"
        assert any(expected_substr in err for err in errors), errors


def test_generate_docs_check_no_build_and_build_reject_unsafe_mkdocs_yaml_before_build(
    tmp_path, capsys
):
    import subprocess
    import sys

    valid_cfg = tmp_path / "mkdocs_ok.yml"
    valid_cfg.write_text(
        Path("mkdocs.yml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    ok_proc = subprocess.run(
        [
            sys.executable,
            "scripts/generate_docs.py",
            "--check",
            "--no-build",
            "--local-only",
            "--mkdocs-config",
            str(valid_cfg),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok_proc.returncode == 0, f"stderr:\n{ok_proc.stderr}"

    for bad_tag in (
        "!!python/object/apply:os.system ['id']",
        "!!python/name:builtins.eval",
    ):
        bad_cfg = tmp_path / "mkdocs_bad.yml"
        bad_cfg.write_text(
            f"site_name: Sortify\nextra_payload: {bad_tag}\n",
            encoding="utf-8",
        )
        bad_proc = subprocess.run(
            [
                sys.executable,
                "scripts/generate_docs.py",
                "--check",
                "--no-build",
                "--local-only",
                "--mkdocs-config",
                str(bad_cfg),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert bad_proc.returncode != 0
        assert "Disallowed YAML tag" in bad_proc.stderr

        # Also verify --build halts before invoking mkdocs build subprocess
        with (
            patch(
                "sys.argv",
                [
                    "generate_docs.py",
                    "--check",
                    "--build",
                    "--mkdocs-config",
                    str(bad_cfg),
                ],
            ),
            patch("subprocess.run") as mock_mkdocs_build,
            patch.dict("sys.modules", {"mkdocs": MagicMock()}),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1
            mock_mkdocs_build.assert_not_called()
            captured = capsys.readouterr()
            assert "Disallowed YAML tag" in captured.err
