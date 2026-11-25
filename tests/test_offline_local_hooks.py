"""Tests for offline local Git hooks, Git-scoped secret scanning, and redaction helpers."""

from __future__ import annotations

import importlib.metadata as metadata
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import scripts.diagram_schema as diagram_schema
from app.core.text_utils import contains_secrets, sanitize_secret_patterns
from scripts.classify_pr import PRMetadata, classify_pr
from scripts.install_git_hooks import install_git_hooks
from scripts.run_actionlint import run_workflow_lint, validate_workflows_offline
from scripts.run_secret_scan import (
    enumerate_staged_paths,
    enumerate_working_tree_paths,
    run_secret_scan,
)
from tests.conftest import pytest_runtest_logreport
from tests.synthetic_secrets import (
    make_aws_access_key,
    make_bearer_token,
    make_github_pat,
    make_high_entropy_secret,
    make_jwt,
    make_key_value_secret,
    make_mailgun_key,
    make_private_key_pem,
    make_stripe_key,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout.strip()


def _init_temp_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init", "--quiet", "-b", "main")
    _git(repo_dir, "config", "user.name", "Test User")
    _git(repo_dir, "config", "user.email", "test@example.com")
    shutil.copy2(REPO_ROOT / ".gitignore", repo_dir / ".gitignore")
    shutil.copy2(REPO_ROOT / ".gitleaks.toml", repo_dir / ".gitleaks.toml")
    (repo_dir / "app").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "app" / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo_dir, "add", ".gitignore", ".gitleaks.toml", "app/sample.py")
    _git(repo_dir, "commit", "--quiet", "-m", "init")


def test_precommit_config_uses_only_offline_local_hooks():
    """Verify .pre-commit-config.yaml declares zero remote URLs and resolves local hook entry points."""
    precommit_cfg = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    repos = precommit_cfg.get("repos", [])
    assert repos and all(r.get("repo") == "local" for r in repos), (
        f"Expected only offline local repos in .pre-commit-config.yaml, got {[r.get('repo') for r in repos]!r}"
    )
    for repo_entry in repos:
        repo_url = str(repo_entry.get("repo", ""))
        assert not repo_url.startswith(("http://", "https://", "git@", "ssh://")), (
            f"Remote pre-commit hook URL forbidden for offline setup: {repo_url!r}"
        )

    pyproject_cfg = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    dev_deps = pyproject_cfg.get("dependency-groups", {}).get("dev", [])
    assert any(str(dep).startswith("pre-commit-hooks>=5.0.0") for dep in dev_deps), (
        "pyproject.toml [dependency-groups].dev must lock pre-commit-hooks>=5.0.0"
    )

    local_repo = next(r for r in repos if r["repo"] == "local")
    hooks = {h["id"]: h for h in local_repo["hooks"]}

    installed_pc_scripts = {
        ep.name
        for ep in metadata.distribution("pre_commit_hooks").entry_points
        if ep.group == "console_scripts"
    }
    for pc_hook_id in (
        "check-added-large-files",
        "check-case-conflict",
        "check-json",
        "check-toml",
        "check-yaml",
        "check-merge-conflict",
        "debug-statements",
        "no-commit-to-branch",
    ):
        assert pc_hook_id in hooks
        assert hooks[pc_hook_id].get("language") == "system"
        entry_parts = hooks[pc_hook_id]["entry"].split()
        assert entry_parts[:2] == ["uv", "run"] and len(entry_parts) >= 3
        assert entry_parts[2] in installed_pc_scripts, (
            f"Hook {pc_hook_id!r} entry {entry_parts[2]!r} not in pre-commit-hooks console_scripts"
        )

    assert hooks["no-commit-to-branch"].get("args") == ["--branch", "main"]
    assert hooks["no-commit-to-branch"].get("pass_filenames") is False
    assert hooks["no-commit-to-branch"].get("always_run") is True

    for secret_hook_id, expected_mode in (
        ("gitleaks", "--mode staged"),
        ("gitleaks-dir", "--mode working-tree"),
    ):
        hook = hooks[secret_hook_id]
        assert hook.get("language") == "system"
        assert "scripts/run_secret_scan.py" in hook.get("entry", "")
        assert expected_mode in hook.get("entry", "")
        assert hook.get("args") == ["--config", ".gitleaks.toml"]
        assert hook.get("pass_filenames") is False

    actionlint_hook = hooks["actionlint"]
    assert actionlint_hook.get("language") == "system"
    assert "scripts/run_actionlint.py" in actionlint_hook.get("entry", "")
    assert re.search(actionlint_hook["files"], ".github/workflows/ci.yml")
    assert re.search(actionlint_hook["files"], ".github/actionlint.yaml")

    assert "ruff-exception-ceilings" in hooks


def test_contributor_onboarding_diagram_spec_is_deduplicated_and_lazy():
    """Verify CONTRIBUTOR_ONBOARDING_SPEC resolves lazily from the single factory with install_git_hooks.py."""
    assert "CONTRIBUTOR_ONBOARDING_SPEC" not in diagram_schema.__dict__, (
        "CONTRIBUTOR_ONBOARDING_SPEC must not be duplicated eagerly in module globals"
    )
    diagram_schema.reset_diagram_specs_cache()
    spec_via_attr = diagram_schema.CONTRIBUTOR_ONBOARDING_SPEC
    spec_via_dict = diagram_schema.SYSTEM_DIAGRAM_SPECS["contributor_onboarding"]
    assert spec_via_attr is spec_via_dict
    node_labels = {node.id: node.label for node in spec_via_attr.nodes}
    assert (
        node_labels["C"] == "Install Hooks: uv run python scripts/install_git_hooks.py"
    )


def test_gitignore_blocks_dotenv_files_and_allows_dotenv_example(tmp_path: Path):
    """Verify .gitignore ignores .env and .env.* while keeping .env.example trackable."""
    repo_dir = tmp_path / "repo"
    _init_temp_repo(repo_dir)

    (repo_dir / ".env").write_text("SORTIFY_MAX_FOLDERS=5\n", encoding="utf-8")
    (repo_dir / ".env.local").write_text("SORTIFY_MAX_FOLDERS=6\n", encoding="utf-8")
    (repo_dir / ".env.production").write_text(
        "SORTIFY_MAX_FOLDERS=7\n", encoding="utf-8"
    )
    (repo_dir / ".env.example").write_text("SORTIFY_MAX_FOLDERS=12\n", encoding="utf-8")

    _git(repo_dir, "add", ".")
    staged = set(enumerate_staged_paths(repo_dir))
    assert ".env.example" in staged
    assert ".env" not in staged
    assert ".env.local" not in staged
    assert ".env.production" not in staged


def test_conftest_pytest_runtest_logreport_scrubs_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify failed pytest tracebacks scrub synthetic secrets before writing pytest_failures.txt."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    stripe_secret = make_stripe_key("9876543210abcdef98765432")
    gh_secret = make_github_pat("abcdef1234567890abcdef1234567890abcd")
    pem_secret = make_private_key_pem()

    fake_report = SimpleNamespace(
        failed=True,
        when="call",
        nodeid=f"tests/test_demo.py::test_leak[{stripe_secret}]",
        longrepr=(
            f"AssertionError: leaked stripe={stripe_secret} and github={gh_secret}\n"
            f"{pem_secret}\n"
            "line 2 preserved"
        ),
    )

    pytest_runtest_logreport(fake_report)
    failures_file = tmp_path / "pytest_failures.txt"
    assert failures_file.is_file()
    written = failures_file.read_text(encoding="utf-8")

    assert stripe_secret not in written
    assert gh_secret not in written
    assert "BEGIN PRIVATE KEY" not in written
    assert "line 2 preserved" in written
    assert not contains_secrets(written)


def test_synthetic_secrets_helpers_detected_and_self_clean(tmp_path: Path):
    """Verify tests/synthetic_secrets.py builds valid secret probes while scanning clean itself."""
    standalone_probes = [
        make_stripe_key(),
        make_github_pat(),
        make_aws_access_key(),
        make_mailgun_key(),
        make_jwt(),
        make_bearer_token(),
        make_private_key_pem(),
        make_high_entropy_secret(),
    ]
    for probe in standalone_probes:
        assert contains_secrets(probe), f"Expected contains_secrets True for {probe!r}"
        scrubbed = sanitize_secret_patterns(probe, replacement="[REDACTED]")
        assert not contains_secrets(scrubbed), (
            f"Expected scrubbed output clean for {probe!r}, got {scrubbed!r}"
        )

    kv_probe = make_key_value_secret()
    assert contains_secrets(kv_probe)
    kv_scrubbed = sanitize_secret_patterns(kv_probe, replacement="[REDACTED]")
    assert kv_scrubbed == "api_key=[REDACTED]"
    scrubbed_file = tmp_path / "scrubbed.txt"
    scrubbed_file.write_text(kv_scrubbed + "\n", encoding="utf-8")
    assert (
        run_secret_scan(
            repo_root=tmp_path,
            config_path=REPO_ROOT / ".gitleaks.toml",
            filenames=[str(scrubbed_file)],
            force_offline=True,
        )
        == 0
    )

    rc = run_secret_scan(
        repo_root=REPO_ROOT,
        config_path=REPO_ROOT / ".gitleaks.toml",
        filenames=["tests/synthetic_secrets.py"],
        force_offline=True,
    )
    assert rc == 0


@pytest.mark.parametrize("force_offline", [True, False])
def test_unstaged_gitignored_artifacts_do_not_fail_secret_scans(
    tmp_path: Path, force_offline: bool
):
    """Verify unstaged secrets in gitignored logs, keys, .actual diffs, and .env pass both modes."""
    repo_dir = tmp_path / "repo"
    _init_temp_repo(repo_dir)

    secret = make_stripe_key("ABCDEF1234567890abcdef1234")
    ignored_targets = [
        repo_dir / "pytest_failures.txt",
        repo_dir / "smoke_test_error.txt",
        repo_dir / "autosorter.log",
        repo_dir / "sandbox" / "secret.key",
        repo_dir / "tests" / "snapshots" / "tui_svg" / "modal.actual",
        repo_dir / ".env",
    ]
    for target in ignored_targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"LEAK={secret}\n", encoding="utf-8")

    wt_paths = set(enumerate_working_tree_paths(repo_dir))
    for rel in (
        "pytest_failures.txt",
        "smoke_test_error.txt",
        "autosorter.log",
        "sandbox/secret.key",
        "tests/snapshots/tui_svg/modal.actual",
        ".env",
    ):
        assert rel not in wt_paths

    assert (
        run_secret_scan(
            mode="staged",
            repo_root=repo_dir,
            config_path=repo_dir / ".gitleaks.toml",
            force_offline=force_offline,
        )
        == 0
    )
    assert (
        run_secret_scan(
            mode="working-tree",
            repo_root=repo_dir,
            config_path=repo_dir / ".gitleaks.toml",
            force_offline=force_offline,
        )
        == 0
    )


@pytest.mark.parametrize("force_offline", [True, False])
def test_staged_secret_force_staged_dotenv_and_partial_staging_fail_both_modes(
    tmp_path: Path, force_offline: bool
):
    """Verify staged secrets, force-staged .env, and partially unstaged secrets exit 1 in both modes."""
    # Case 1: Staged secret in tracked file
    repo_tracked = tmp_path / "repo_tracked"
    _init_temp_repo(repo_tracked)
    secret = make_stripe_key("ABCDEF1234567890abcdef1234")
    (repo_tracked / "app" / "sample.py").write_text(
        f'API_TOKEN = "{secret}"\n', encoding="utf-8"
    )
    _git(repo_tracked, "add", "app/sample.py")

    assert (
        run_secret_scan(
            mode="staged",
            repo_root=repo_tracked,
            config_path=repo_tracked / ".gitleaks.toml",
            force_offline=force_offline,
        )
        == 1
    )
    assert (
        run_secret_scan(
            mode="working-tree",
            repo_root=repo_tracked,
            config_path=repo_tracked / ".gitleaks.toml",
            force_offline=force_offline,
        )
        == 1
    )

    # Case 2: Partial staging edge case — secret remains staged in index while working-tree file is cleaned
    (repo_tracked / "app" / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert (
        run_secret_scan(
            mode="staged",
            repo_root=repo_tracked,
            config_path=repo_tracked / ".gitleaks.toml",
            force_offline=force_offline,
        )
        == 1
    )
    assert (
        run_secret_scan(
            mode="working-tree",
            repo_root=repo_tracked,
            config_path=repo_tracked / ".gitleaks.toml",
            force_offline=force_offline,
        )
        == 1
    )

    # Case 3: Force-staged .env file (`git add -f .env`)
    repo_env = tmp_path / "repo_env"
    _init_temp_repo(repo_env)
    (repo_env / ".env").write_text(
        f"OPENAI_API_KEY={make_github_pat('ABCDEF1234567890abcdef1234567890ABCD')}\n",
        encoding="utf-8",
    )
    _git(repo_env, "add", "-f", ".env")

    assert (
        run_secret_scan(
            mode="staged",
            repo_root=repo_env,
            config_path=repo_env / ".gitleaks.toml",
            force_offline=force_offline,
        )
        == 1
    )
    assert (
        run_secret_scan(
            mode="working-tree",
            repo_root=repo_env,
            config_path=repo_env / ".gitleaks.toml",
            force_offline=force_offline,
        )
        == 1
    )


@pytest.mark.parametrize("force_offline", [True, False])
def test_sortify_key_document_filenames_allowed_while_credentials_rejected(
    tmp_path: Path, force_offline: bool
):
    """Verify *_key benign document filenames pass whereas sk_live_ and secret.key fail with exit 1."""
    repo_dir = tmp_path / "repo"
    _init_temp_repo(repo_dir)

    benign_code = "\n".join(
        [
            'node_key = "invoice_q4.pdf"',
            'file_key = "quarterly_report_2026.docx"',
            'dest_key = "Finance/invoice_q4.pdf"',
            'item_key = "budget_2026_v2.xlsx"',
            'scoped_key = "workspace::invoice_q4.pdf"',
            'row_key = "study_0042_summary.csv"',
            'fixture = {"node_key": "invoice_q4.pdf", "file_key": "scan_01.png"}',
            "",
        ]
    )
    (repo_dir / "app" / "benign_keys.py").write_text(benign_code, encoding="utf-8")
    (repo_dir / "tests" / "test_benign_keys.py").write_text(
        benign_code, encoding="utf-8"
    )
    _git(repo_dir, "add", "app/benign_keys.py", "tests/test_benign_keys.py")

    assert (
        run_secret_scan(
            mode="staged",
            repo_root=repo_dir,
            config_path=repo_dir / ".gitleaks.toml",
            force_offline=force_offline,
        )
        == 0
    )
    assert (
        run_secret_scan(
            mode="working-tree",
            repo_root=repo_dir,
            config_path=repo_dir / ".gitleaks.toml",
            force_offline=force_offline,
        )
        == 0
    )

    # Disallowed credential extensions on Sortify *_key identifiers must fail with exit code 1
    for ext in ("key", "pem", "env", "pfx", "p12"):
        bad_file = repo_dir / "tests" / "test_bad_ext.py"
        bad_file.write_text(f'node_key = "secret.{ext}"\n', encoding="utf-8")
        _git(repo_dir, "add", "tests/test_bad_ext.py")
        assert (
            run_secret_scan(
                mode="staged",
                repo_root=repo_dir,
                config_path=repo_dir / ".gitleaks.toml",
                force_offline=force_offline,
            )
            == 1
        )
        assert (
            run_secret_scan(
                mode="working-tree",
                repo_root=repo_dir,
                config_path=repo_dir / ".gitleaks.toml",
                force_offline=force_offline,
            )
            == 1
        )

    # Real API key assignment must also fail with exit code 1
    (repo_dir / "tests" / "test_bad_ext.py").write_text(
        f'api_key = "{make_stripe_key("ABCDEF1234567890abcdef1234")}"\n',
        encoding="utf-8",
    )
    _git(repo_dir, "add", "tests/test_bad_ext.py")
    assert (
        run_secret_scan(
            mode="staged",
            repo_root=repo_dir,
            config_path=repo_dir / ".gitleaks.toml",
            force_offline=force_offline,
        )
        == 1
    )
    assert (
        run_secret_scan(
            mode="working-tree",
            repo_root=repo_dir,
            config_path=repo_dir / ".gitleaks.toml",
            force_offline=force_offline,
        )
        == 1
    )


def test_run_actionlint_offline_validates_repo_workflows_and_rejects_invalid_yaml(
    tmp_path: Path,
):
    """Verify offline workflow linting passes current workflows and rejects invalid workflows."""
    assert run_workflow_lint(repo_root=REPO_ROOT, force_offline=True) == 0

    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    bad_wf = wf_dir / "broken.yml"
    bad_wf.write_text(
        "name: Broken\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    needs: [missing-job]\n    steps: []\n",
        encoding="utf-8",
    )
    errs = validate_workflows_offline(tmp_path, [bad_wf])
    assert any("unknown job 'missing-job'" in e for e in errs)
    assert any("non-empty 'steps' list" in e for e in errs)
    assert run_workflow_lint(repo_root=tmp_path, force_offline=True) == 1

    dup_wf = wf_dir / "dup.yml"
    dup_wf.write_text(
        "name: Dup\nname: DupAgain\non: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n",
        encoding="utf-8",
    )
    assert (
        run_workflow_lint(
            repo_root=tmp_path, filenames=[str(dup_wf)], force_offline=True
        )
        == 1
    )

    cycle_wf = wf_dir / "cycle.yml"
    cycle_wf.write_text(
        "name: Cycle\non: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    needs: b\n    steps:\n      - run: echo a\n  b:\n    runs-on: ubuntu-latest\n    needs: a\n    steps:\n      - run: echo b\n",
        encoding="utf-8",
    )
    cycle_errs = validate_workflows_offline(tmp_path, [cycle_wf])
    assert any("cycle detected" in e for e in cycle_errs)


def test_install_git_hooks_handles_standard_repo_and_core_hookspath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify scripts/install_git_hooks.py exits 0 in standard checkouts and core.hooksPath sandboxes."""
    isolated_global = tmp_path / "gitconfig_empty"
    isolated_global.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(isolated_global))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    # 1. Standard Git repository without core.hooksPath installs .git/hooks/pre-commit
    std_repo = tmp_path / "std_repo"
    _init_temp_repo(std_repo)
    shutil.copy2(
        REPO_ROOT / ".pre-commit-config.yaml", std_repo / ".pre-commit-config.yaml"
    )
    assert install_git_hooks(repo_root=std_repo) == 0
    hook_file = std_repo / ".git" / "hooks" / "pre-commit"
    assert hook_file.is_file()
    assert os.access(hook_file, os.X_OK) or os.name == "nt"

    # 2. Sandbox repository where Git sets core.hooksPath exits 0 cleanly without error
    sandbox_repo = tmp_path / "sandbox_repo"
    _init_temp_repo(sandbox_repo)
    _git(sandbox_repo, "config", "core.hooksPath", "/dev/null")
    assert install_git_hooks(repo_root=sandbox_repo) == 0
    assert not (sandbox_repo / ".git" / "hooks" / "pre-commit").exists()


def test_pr_adding_key_fixture_and_redaction_test_stays_low_risk_eligible():
    """Verify PRs adding TUI fixture filenames and redaction tests skip .gitleaks.toml and stay low-risk."""
    meta = PRMetadata(
        pr_number=2048,
        title="feat: add duplicate fixture and notification redaction test",
        author="contributor",
        labels=["automerge"],
        changed_files=[
            "app/ui/catalog.py",
            "tests/test_notifications.py",
        ],
        additions=28,
        deletions=4,
    )
    res = classify_pr(meta)
    assert res.eligible is True
    assert res.risk_level == "low"
    assert "automerge:eligible" in res.labels_to_add
