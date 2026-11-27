"""Tests for CI workflow configuration, stage gates, and dedicated Pages docs workflow."""

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

UPLOAD_PAGES_ARTIFACT_SHA = "fc324d3547104276b827a68afc52ff2a11cc49c9"
DEPLOY_PAGES_SHA = "368f82528645a54fb793d4d04e342629a3f51346"


def _load_ci_workflow():
    workflow_path = Path(".github/workflows/ci.yml")
    assert workflow_path.exists(), f"Workflow file missing: {workflow_path}"
    with open(workflow_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_docs_workflow():
    workflow_path = Path(".github/workflows/docs.yml")
    assert workflow_path.exists(), f"Workflow file missing: {workflow_path}"
    with open(workflow_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_no_changes_path_filter_job():
    """Verify that the redundant changes path filter job is removed from ci.yml."""
    workflow = _load_ci_workflow()
    jobs = workflow.get("jobs", {})
    assert "changes" not in jobs, (
        "Redundant 'changes' path filter job should be removed from ci.yml"
    )


def test_static_checks_validates_docs_without_artifact_upload_or_doc_deploy():
    """Verify static-checks validates docs under read-only permissions and ci.yml removes doc-deploy."""
    raw_ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    workflow = _load_ci_workflow()
    jobs = workflow.get("jobs", {})
    assert "static-checks" in jobs, "Missing 'static-checks' job in ci.yml"
    assert "doc-deploy" not in jobs, (
        "Legacy 'doc-deploy' job must be removed from ci.yml in favor of docs.yml"
    )
    assert "refs/heads/gh-pages" not in raw_ci, (
        "ci.yml must not push commits to refs/heads/gh-pages"
    )

    for job_name, job_cfg in jobs.items():
        perms = job_cfg.get("permissions") or {}
        assert perms.get("contents") != "write", (
            f"ci.yml job {job_name!r} must not request contents: write"
        )

    job = jobs["static-checks"]
    assert "permissions" not in job or job.get("permissions") == {"contents": "read"}

    steps = job.get("steps", [])
    checkout_step = next(
        (s for s in steps if "actions/checkout@" in s.get("uses", "")), None
    )
    assert checkout_step is not None, "Missing actions/checkout step in static-checks"
    assert (checkout_step.get("with") or {}).get("persist-credentials") is False
    assert (checkout_step.get("with") or {}).get("fetch-depth") == 0

    sync_step = next((s for s in steps if "uv sync" in s.get("run", "")), None)
    assert sync_step is not None, "Missing uv sync step in static-checks"
    assert (
        sync_step.get("run", "").strip() == "uv sync --frozen --group dev --group docs"
    )

    assert not any("actions/setup-node@" in s.get("uses", "") for s in steps), (
        "Dead actions/setup-node step must be removed from static-checks"
    )
    assert not any("@mermaid-js/mermaid-cli" in s.get("run", "") for s in steps), (
        "Dead @mermaid-js/mermaid-cli installation must be removed from static-checks"
    )
    assert not any("diagram_toolchain.py" in s.get("run", "") for s in steps), (
        "Duplicate standalone diagram_toolchain.py verify step must be removed from static-checks"
    )

    compile_step = next(
        (s for s in steps if "scripts/generate_docs.py --check" in s.get("run", "")),
        None,
    )
    assert compile_step is not None, (
        "Missing 'scripts/generate_docs.py --check' step in static-checks"
    )
    assert compile_step.get("name") == "Validate Documentation and Integrity"
    assert "if" not in compile_step, (
        "Documentation compilation and validation must run on PRs as well as main"
    )

    assert not any(
        (s.get("with") or {}).get("name") == "docs-site"
        or s.get("name") == "Upload Documentation Site Artifact"
        for s in steps
    ), "Upload Documentation Site Artifact must be removed from ci.yml static-checks"

    assert steps.index(sync_step) < steps.index(compile_step), (
        "uv sync must run before doc validation"
    )


def test_docs_workflow_triggers_build_and_oidc_pages_deployment():
    """Verify docs.yml triggers on main pushes and workflow_dispatch, builds in read-only build-docs, and deploys via OIDC in deploy-docs."""
    docs_path = Path(".github/workflows/docs.yml")
    raw_docs = docs_path.read_text(encoding="utf-8")
    workflow = _load_docs_workflow()

    triggers = workflow.get("on") or workflow.get(True) or {}
    assert "workflow_dispatch" in triggers, (
        "docs.yml must support workflow_dispatch for post-merge auto-merge orchestration"
    )
    push_cfg = triggers.get("push") or {}
    assert push_cfg.get("branches") == ["main"], (
        "docs.yml push trigger must target branches: ['main']"
    )
    push_paths = set(push_cfg.get("paths") or [])
    for required_filter in (
        ".github/workflows/docs.yml",
        "docs/**",
        "mkdocs.yml",
        "*.md",
        "app/**",
        "scripts/**",
        "sandbox_cli.py",
        "network_rules.json",
        "pyproject.toml",
        "uv.lock",
    ):
        assert required_filter in push_paths, (
            f"docs.yml push.paths missing documentation/source input filter {required_filter!r}"
        )

    assert workflow.get("permissions") == {"contents": "read"}
    wf_concurrency = workflow.get("concurrency") or {}
    assert "ci-" not in str(wf_concurrency.get("group", "")), (
        "docs.yml concurrency must stay independent of ci.yml"
    )
    assert wf_concurrency.get("cancel-in-progress") is False

    jobs = workflow.get("jobs") or {}
    assert set(jobs) == {"build-docs", "deploy-docs"}

    # 1. Verify read-only build-docs job
    build_job = jobs["build-docs"]
    assert build_job.get("timeout-minutes") == 15
    assert build_job.get("permissions") == {"contents": "read"}
    assert not _needs_set(build_job)

    build_steps = build_job.get("steps") or []
    checkout_step = next(
        (s for s in build_steps if "actions/checkout@" in s.get("uses", "")), None
    )
    assert checkout_step is not None, "Missing actions/checkout step in build-docs"
    assert (checkout_step.get("with") or {}).get("persist-credentials") is False

    sync_steps = [s for s in build_steps if "uv sync" in s.get("run", "")]
    assert len(sync_steps) == 1, "build-docs must run uv sync exactly once"
    sync_step = sync_steps[0]
    assert "--frozen" in sync_step.get("run", "")
    assert "--group docs" in sync_step.get("run", "")

    build_step = next(
        (
            s
            for s in build_steps
            if "scripts/generate_docs.py --check --local-only --build"
            in s.get("run", "")
        ),
        None,
    )
    assert build_step is not None, (
        "build-docs must run 'scripts/generate_docs.py --check --local-only --build'"
    )

    upload_step = next(
        (
            s
            for s in build_steps
            if s.get("uses")
            == f"actions/upload-pages-artifact@{UPLOAD_PAGES_ARTIFACT_SHA}"
        ),
        None,
    )
    assert upload_step is not None, (
        f"build-docs must upload Pages artifact via actions/upload-pages-artifact@{UPLOAD_PAGES_ARTIFACT_SHA}"
    )
    upload_path = str((upload_step.get("with") or {}).get("path", "")).rstrip("/")
    assert upload_path == "site", (
        f"Expected upload-pages-artifact path 'site/', got {upload_path!r}"
    )
    assert (
        build_steps.index(checkout_step)
        < build_steps.index(sync_step)
        < build_steps.index(build_step)
        < build_steps.index(upload_step)
    )

    # 2. Verify isolated OIDC deploy-docs job
    deploy_job = jobs["deploy-docs"]
    assert _needs_set(deploy_job) == {"build-docs"}
    assert deploy_job.get("if") == "github.ref == 'refs/heads/main'"
    assert deploy_job.get("timeout-minutes") == 15
    assert deploy_job.get("concurrency") == {
        "group": "pages-deploy",
        "cancel-in-progress": False,
    }
    assert deploy_job.get("permissions") == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }

    env_cfg = deploy_job.get("environment")
    if isinstance(env_cfg, dict):
        assert env_cfg.get("name") == "github-pages"
        assert env_cfg.get("url") == "${{ steps.deployment.outputs.page_url }}"
    else:
        assert env_cfg == "github-pages"

    deploy_steps = deploy_job.get("steps") or []
    forbidden_uses = (
        "actions/checkout",
        "astral-sh/setup-uv",
        "actions/setup-python",
        "actions/setup-node",
    )
    for step in deploy_steps:
        uses = step.get("uses", "")
        run_cmd = step.get("run", "")
        assert not any(action in uses for action in forbidden_uses), (
            f"deploy-docs must not check out code or install build toolchains, found {uses!r}"
        )
        assert not run_cmd.strip(), (
            f"deploy-docs must not execute shell build commands, found {run_cmd!r}"
        )

    deploy_step = next(
        (
            s
            for s in deploy_steps
            if s.get("uses") == f"actions/deploy-pages@{DEPLOY_PAGES_SHA}"
        ),
        None,
    )
    assert deploy_step is not None, (
        f"deploy-docs must publish via actions/deploy-pages@{DEPLOY_PAGES_SHA}"
    )
    if isinstance(env_cfg, dict) and "steps.deployment.outputs.page_url" in str(
        env_cfg.get("url", "")
    ):
        assert deploy_step.get("id") == "deployment"

    assert "contents: write" not in raw_docs
    assert "gh-pages" not in raw_docs


def _find_git_bash() -> str:
    if sys.platform == "win32":
        git_exe = shutil.which("git")
        if git_exe:
            git_root = Path(git_exe).resolve().parent.parent
            for candidate in (
                git_root / "bin" / "bash.exe",
                git_root / "usr" / "bin" / "bash.exe",
            ):
                if candidate.is_file():
                    return str(candidate)
    bash_exe = shutil.which("bash")
    assert bash_exe is not None, "bash executable is required to test workflow scripts"
    return bash_exe


def _run_git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout.strip()


FAST_STAGE_JOBS = {"security-and-audit", "type-check", "static-checks"}
GATED_TEST_JOBS = ("build-and-test", "heavy-tests", "verify-offline-bundle")


def _needs_set(job: dict) -> set[str]:
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return {needs}
    return set(needs)


def test_fast_stage_jobs_run_in_parallel_with_least_privilege():
    """Verify first-stage fast checks have no upstream needs and keep least privilege."""
    workflow = _load_ci_workflow()
    assert workflow.get("permissions") == {"contents": "read"}

    jobs = workflow.get("jobs", {})
    for job_name in FAST_STAGE_JOBS:
        assert job_name in jobs, f"Missing fast-check job {job_name!r} in ci.yml"
        assert not _needs_set(jobs[job_name]), (
            f"First-stage job {job_name!r} must run immediately without needs"
        )

    pr_read_jobs = [
        name
        for name, job in jobs.items()
        if (job.get("permissions") or {}).get("pull-requests") == "read"
    ]
    assert pr_read_jobs == ["security-and-audit"], (
        f"Only 'security-and-audit' should request pull-requests: read, got {pr_read_jobs}"
    )


@pytest.mark.parametrize("job_name", GATED_TEST_JOBS)
def test_expensive_test_jobs_gated_behind_fast_checks(job_name: str):
    """Verify expensive test and bundle jobs wait for all three fast-check jobs."""
    workflow = _load_ci_workflow()
    jobs = workflow.get("jobs", {})
    assert job_name in jobs, f"Missing {job_name!r} job in ci.yml"

    job = jobs[job_name]
    assert _needs_set(job) == FAST_STAGE_JOBS, (
        f"{job_name!r} must declare needs on {sorted(FAST_STAGE_JOBS)}"
    )
    assert "if" not in job, (
        f"{job_name!r} must not override default failure/skip gating with an 'if' condition"
    )


def test_required_ci_gate_depends_on_all_upstream_jobs_and_fails_closed():
    """Verify required-ci aggregates all six upstream jobs and fails closed on failure or skip."""
    workflow = _load_ci_workflow()
    jobs = workflow.get("jobs", {})
    assert "required-ci" in jobs, "Missing 'required-ci' job in ci.yml"

    job = jobs["required-ci"]
    assert job.get("name") == "Required CI"
    assert job.get("if") == "${{ always() }}"

    expected_upstream = FAST_STAGE_JOBS | set(GATED_TEST_JOBS)
    assert _needs_set(job) == expected_upstream

    steps = job.get("steps", [])
    gate_step = next(
        (s for s in steps if "actions/github-script@" in s.get("uses", "")), None
    )
    assert gate_step is not None, (
        "Missing actions/github-script gate step in required-ci"
    )

    script = (gate_step.get("with") or {}).get("script", "")
    assert "${{ toJson(needs) }}" in script
    assert "jobData.result !== 'success'" in script
    assert "core.setFailed(" in script


def test_heavy_tests_runs_single_uv_sync_with_ml_extra():
    """Verify heavy-tests runs uv sync once with --extra ml during dependency installation."""
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    optional_deps = pyproject.get("project", {}).get("optional-dependencies", {})
    assert "ml" in optional_deps and optional_deps["ml"], (
        "pyproject.toml must define non-empty [project.optional-dependencies].ml"
    )

    workflow = _load_ci_workflow()
    jobs = workflow.get("jobs", {})
    assert "heavy-tests" in jobs, "Missing 'heavy-tests' job in ci.yml"

    steps = jobs["heavy-tests"].get("steps", [])
    uv_sync_steps = [s for s in steps if "uv sync" in s.get("run", "")]
    assert len(uv_sync_steps) == 1, (
        f"heavy-tests must execute 'uv sync' in exactly one step, found {len(uv_sync_steps)}"
    )

    install_step = uv_sync_steps[0]
    assert install_step.get("name") == "Install dependencies"
    install_run = install_step.get("run", "")
    assert install_run.count("uv sync") == 1
    assert install_run.strip() == "uv sync --frozen --group dev --extra ml"

    test_step = next(
        (s for s in steps if s.get("name") == "Heavy ML and Integration Tests"), None
    )
    assert test_step is not None, "Missing 'Heavy ML and Integration Tests' step"
    assert "uv sync" not in test_step.get("run", "")
    assert 'pytest -m "slow"' in test_step.get("run", "")
    assert "--ignore=tests/test_visual_snapshots.py" in test_step.get("run", "")
    assert steps.index(install_step) < steps.index(test_step)


def test_no_ci_job_executes_duplicate_uv_sync():
    """Verify no job in ci.yml executes uv sync more than once."""
    workflow = _load_ci_workflow()
    for job_name, job in workflow.get("jobs", {}).items():
        sync_count = sum(
            step.get("run", "").count("uv sync") for step in job.get("steps", [])
        )
        assert sync_count <= 1, (
            f"Job {job_name!r} runs 'uv sync' {sync_count} times; expected at most once"
        )


def test_verify_offline_bundle_frozen_and_ml_verification():
    """Verify verify-offline-bundle validates the frozen manifest, checks git diff, and tests ML imports."""
    workflow = _load_ci_workflow()
    jobs = workflow.get("jobs", {})
    assert "verify-offline-bundle" in jobs
    assert "verify-offline-bundle" in _needs_set(jobs["required-ci"])

    steps = jobs["verify-offline-bundle"].get("steps", [])
    run_scripts = "\n".join(s.get("run", "") for s in steps)

    assert "scripts/prepare_offline.py --cpu --frozen" in run_scripts
    assert "git diff --exit-code" in run_scripts
    assert "RUNNER_TEMP" in run_scripts
    assert "scripts/install_offline.py offline-install" in run_scripts
    assert ".venv/bin/smart-autosorter" in run_scripts
    assert "is_relative_to(" in run_scripts
    for mod in ("torch", "easyocr", "onnxruntime", "transformers", "llama_cpp"):
        assert mod in run_scripts, f"Missing '{mod}' import in verify-offline-bundle"
    assert "is_ml_available()" in run_scripts
    assert "torch.cuda.is_available()" in run_scripts


def test_type_check_job_installs_all_groups_emits_annotations_and_enforces_ratchet():
    """Verify type-check preserves job ID/name, installs scoped dev group, emits GitHub annotations, and enforces multi-platform check_ty_warnings.py."""
    from scripts.check_ty_warnings import PLATFORMS

    workflow = _load_ci_workflow()
    jobs = workflow.get("jobs", {})
    assert "type-check" in jobs, "Missing 'type-check' job in ci.yml"

    job = jobs["type-check"]
    assert job.get("name") == "Type Check (ty)"
    steps = job.get("steps", [])

    sync_step = next((s for s in steps if "uv sync" in s.get("run", "")), None)
    assert sync_step is not None, "Missing uv sync step in type-check"
    assert sync_step.get("run", "").strip() == "uv sync --frozen --group dev"

    ratchet_step = next(
        (s for s in steps if "scripts/check_ty_warnings.py" in s.get("run", "")),
        None,
    )
    assert ratchet_step is not None, (
        "Missing 'scripts/check_ty_warnings.py' step in type-check"
    )
    assert "--github-annotations" in ratchet_step.get("run", "")
    assert set(PLATFORMS) == {"linux", "darwin", "win32"}
    assert steps.index(sync_step) < steps.index(ratchet_step)


def test_pre_commit_ty_check_triggers_on_app_scripts_pyproject_and_lockfile():
    """Verify ty-check in .pre-commit-config.yaml runs the warning ratchet and triggers on app, scripts, pyproject.toml, and uv.lock."""
    import re

    config = yaml.safe_load(Path(".pre-commit-config.yaml").read_text(encoding="utf-8"))
    local_repo = next(r for r in config.get("repos", []) if r.get("repo") == "local")
    ty_hook = next(h for h in local_repo.get("hooks", []) if h.get("id") == "ty-check")

    assert "scripts/check_ty_warnings.py" in ty_hook.get("entry", "")
    assert ty_hook.get("pass_filenames") is True
    assert ty_hook.get("require_serial") is True
    pattern = re.compile(ty_hook["files"])
    for expected_match in (
        "sandbox_cli.py",
        "app/main.py",
        "app/core/history.py",
        "scripts/build.py",
        "scripts/check_ty_warnings.py",
        "tests/conftest.py",
        "tests/ty_baseline.json",
        ".agents/skills/run-sortify/driver.py",
        "pyproject.toml",
        "uv.lock",
    ):
        assert pattern.search(expected_match), (
            f"Expected ty-check files regex {ty_hook['files']!r} to match {expected_match!r}"
        )


def _validate_gitleaks_config(gitleaks_cfg: dict) -> None:
    """Fail closed if .gitleaks.toml drops default rule inheritance or adds blanket exclusions."""
    import re

    extend = gitleaks_cfg.get("extend")
    assert isinstance(extend, dict), "Missing [extend] table in .gitleaks.toml"
    assert extend.get("useDefault") is True, (
        ".gitleaks.toml must set [extend] useDefault = true to load built-in rules"
    )
    assert not extend.get("disabledRules"), (
        f".gitleaks.toml must not disable built-in rules globally: {extend.get('disabledRules')!r}"
    )

    global_allowlist = gitleaks_cfg.get("allowlist")
    assert isinstance(global_allowlist, dict), (
        "Missing top-level [allowlist] table in .gitleaks.toml"
    )
    for forbidden_global_key in ("commits", "regexes", "stopwords"):
        assert not global_allowlist.get(forbidden_global_key), (
            f"Top-level [allowlist] must not set global {forbidden_global_key!r}"
        )

    global_path_patterns = [re.compile(p) for p in global_allowlist.get("paths", [])]
    assert global_path_patterns, (
        "Top-level [allowlist] must define cache/artifact paths"
    )

    for ignored_path in (
        ".venv/pyvenv.cfg",
        "venv/bin/activate",
        "tests/__pycache__/test_ci_workflow.cpython-312.pyc",
        ".pytest_cache/v/cache/nodeids",
        ".ruff_cache/0.17.5/cache",
        ".coverage",
        "coverage.json",
        "coverage.xml",
        "htmlcov/index.html",
        "build/lib/app/main.py",
        "dist/smart-autosorter/smart-autosorter",
        "site/index.html",
        "smart_autosorter.egg-info/PKG-INFO",
    ):
        assert any(p.search(ignored_path) for p in global_path_patterns), (
            f"Expected .gitleaks.toml [allowlist].paths to ignore {ignored_path!r}"
        )

    for enforced_path in (
        "app/main.py",
        "app/config.py",
        "app/core/crypto.py",
        "app/binaries/manifest.json",
        "scripts/build.py",
        "scripts/prompt_manifest.py",
        ".github/workflows/ci.yml",
        ".github/workflows/scheduled-audit.yml",
        "tests/test_notifications.py",
        "tests/test_key_audit_ledger.py",
        "tests/test_unified_lifecycle_telemetry.py",
        "tests/test_file_renamer.py",
        "tests/test_tui.py",
        "tests/test_validate_links.py",
    ):
        assert not any(p.search(enforced_path) for p in global_path_patterns), (
            f"Top-level [allowlist].paths must never exclude {enforced_path!r}"
        )

    rules = gitleaks_cfg.get("rules", [])
    assert isinstance(rules, list) and rules, (
        "Expected scoped [[rules]] allowlists in .gitleaks.toml"
    )
    synthetic_probes = (
        "sk_" + "live_ABCDEF1234567890abcdef1234",
        "gh" + "p_ABCDEF1234567890abcdef1234567890ABCD",
        "AK" + "IAIOSFODNN7ABCDEFG",
        "PROMPT_MANIFEST_" + 'PRIVATE_KEY = "' + ("a1b2c3d4e5f60718" * 4) + '"',
        "REMEDIATION_APP_" + 'PRIVATE_KEY = "-----BEGIN ' + 'PRIVATE KEY-----"',
    )
    for rule in rules:
        rule_id = rule.get("id")
        assert isinstance(rule_id, str) and rule_id, "Rule entry missing id"
        assert set(rule.keys()) <= {"id", "allowlists"}, (
            f"Rule {rule_id!r} must inherit default detection attributes without overriding {set(rule.keys()) - {'id', 'allowlists'}!r}"
        )
        allowlists = rule.get("allowlists", [])
        assert isinstance(allowlists, list) and allowlists, (
            f"Rule {rule_id!r} must define non-empty [[rules.allowlists]]"
        )
        for entry in allowlists:
            assert entry.get("condition") == "AND", (
                f"Rule {rule_id!r} allowlist {entry.get('description')!r} must set condition = 'AND'"
            )
            assert not entry.get("stopwords"), (
                f"Rule {rule_id!r} allowlist must not use broad stopwords"
            )
            paths = entry.get("paths", [])
            regexes = entry.get("regexes", [])
            assert paths and regexes, (
                f"Rule {rule_id!r} allowlist {entry.get('description')!r} must scope both paths and regexes"
            )
            compiled_paths = [re.compile(p) for p in paths]
            if any(p.search("app/binaries/manifest.json") for p in compiled_paths):
                commits = entry.get("commits", [])
                assert set(commits) == {
                    "c501b29b8b503622bcf81b4cfe39ff308397ad10",
                    "2174c81a7e883a0ca46bc0220d6bc18c1b5e5166",
                    "78525ca1bd270557938ec5c7e1e67de177fa3b38",
                }, (
                    "app/binaries/manifest.json allowlist must pin exact historical commit SHAs"
                )

            compiled_regexes = [re.compile(r) for r in regexes]
            for probe in synthetic_probes:
                assert not any(r.search(probe) for r in compiled_regexes), (
                    f"Rule {rule_id!r} regexes must not match fresh secret probe {probe!r}"
                )


def test_precommit_hooks_full_repo_readonly_parity():
    """Verify pre-commit hooks match CI full-repo read-only execution and trigger patterns."""
    import re
    import tomllib

    repo_root = Path(__file__).resolve().parent.parent
    precommit_cfg = yaml.safe_load(
        (repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    assert [r["repo"] for r in precommit_cfg["repos"]] == ["local"]
    local_repo = precommit_cfg["repos"][0]
    hooks = {h["id"]: h for h in local_repo["hooks"]}

    assert "gitleaks" in hooks
    assert "scripts/run_secret_scan.py --mode staged" in hooks["gitleaks"]["entry"]
    assert hooks["gitleaks"].get("args") == ["--config", ".gitleaks.toml"]
    assert hooks["gitleaks"].get("pass_filenames") is False

    assert "gitleaks-dir" in hooks
    gitleaks_dir_hook = hooks["gitleaks-dir"]
    assert gitleaks_dir_hook.get("pass_filenames") is False, (
        "gitleaks-dir must set pass_filenames: false"
    )
    assert "scripts/run_secret_scan.py --mode working-tree" in gitleaks_dir_hook.get(
        "entry", ""
    )
    assert gitleaks_dir_hook.get("args") == ["--config", ".gitleaks.toml"]

    gitleaks_cfg = tomllib.loads(
        (repo_root / ".gitleaks.toml").read_text(encoding="utf-8")
    )
    _validate_gitleaks_config(gitleaks_cfg)

    for hook_id in (
        "validate-docs",
        "validate-diagram-artifacts",
        "validate-config-schema",
    ):
        assert hooks[hook_id].get("pass_filenames") is False, (
            f"{hook_id} must set pass_filenames: false"
        )

    for hook_id in (
        "validate-signatures",
        "validate-architecture",
    ):
        assert hooks[hook_id].get("pass_filenames", True) is True, (
            f"{hook_id} must enable pass_filenames"
        )
        assert hooks[hook_id].get("pass_filenames") is True, (
            f"{hook_id} must explicitly set pass_filenames: true"
        )

    assert hooks["deptry"].get("pass_filenames") is False, (
        "deptry must set pass_filenames: false"
    )

    assert "--check" in hooks["validate-config-schema"]["entry"]

    # Trigger regex coverage checks
    assert re.search(hooks["validate-docs"]["files"], "network_rules.json")
    assert re.search(hooks["validate-docs"]["files"], "app/config_schema.json")
    assert re.search(hooks["validate-docs"]["files"], "pyproject.toml")

    for path in (
        "app/config.py",
        "app/config_schema.json",
        "pyproject.toml",
        "uv.lock",
    ):
        assert re.search(hooks["validate-config-schema"]["files"], path)

    for path in (
        "app/main.py",
        "app/config.py",
        "app/cli/ledger_cli.py",
        "app/core/analyzer.py",
        "app/plugins/clinical_compliance/plugin.py",
        "app/ui/protocols.py",
        "sandbox_cli.py",
        "tests/snapshots/api/core/orphan.json",
        "tests/test_visual_snapshots.py",
        "pyproject.toml",
        "uv.lock",
    ):
        assert re.search(hooks["validate-signatures"]["files"], path)

    for ignored_sig_path in (
        "tests/test_analyzer.py",
        "app/ui/tui.py",
    ):
        assert not re.search(hooks["validate-signatures"]["files"], ignored_sig_path)

    for path in (
        "app/main.py",
        "scripts/validate_architecture.py",
        "sandbox_cli.py",
        "pyproject.toml",
        "uv.lock",
    ):
        assert re.search(hooks["deptry"]["files"], path)

    assert not re.search(hooks["deptry"]["files"], "tests/test_analyzer.py")

    for path in (
        "app/config.py",
        "scripts/validate_architecture.py",
        "scripts/diagram_schema.py",
        "docs/assets/diagrams/core_architecture.svg",
        "pyproject.toml",
        "uv.lock",
    ):
        assert re.search(hooks["validate-architecture"]["files"], path)

    workflow = _load_ci_workflow()
    static_steps = workflow["jobs"]["static-checks"]["steps"]
    cfg_step = next(
        s for s in static_steps if s.get("name") == "Validate Configuration Schema"
    )
    assert "scripts/validate_config.py --check" in cfg_step.get("run", "")
    build_steps = workflow["jobs"]["build-and-test"]["steps"]
    assert not any(
        "scripts/validate_config.py" in s.get("run", "") for s in build_steps
    ), "validate_config.py --check must run once in static-checks, not build-and-test"


def test_gitleaks_config_fails_closed_on_missing_extend_or_blanket_exclusions():
    """Verify _validate_gitleaks_config rejects dropped rule inheritance, disabled rules, and blanket exclusions, and docs/governance stay synced."""
    import copy
    import tomllib

    repo_root = Path(__file__).resolve().parent.parent
    base_cfg = tomllib.loads((repo_root / ".gitleaks.toml").read_text(encoding="utf-8"))
    _validate_gitleaks_config(base_cfg)

    # Dropping [extend] or setting useDefault = false must fail closed
    no_extend = copy.deepcopy(base_cfg)
    no_extend.pop("extend", None)
    with pytest.raises(AssertionError, match="extend"):
        _validate_gitleaks_config(no_extend)

    disabled_default = copy.deepcopy(base_cfg)
    disabled_default["extend"]["useDefault"] = False
    with pytest.raises(AssertionError, match="useDefault"):
        _validate_gitleaks_config(disabled_default)

    disabled_rule = copy.deepcopy(base_cfg)
    disabled_rule["extend"]["disabledRules"] = ["generic-api-key"]
    with pytest.raises(AssertionError, match="disable built-in rules"):
        _validate_gitleaks_config(disabled_rule)

    # Blanket directory or test-module exclusions in [allowlist].paths must fail closed
    for blanket_path in (
        "^app/",
        "^scripts/",
        "^tests/",
        "tests/test_notifications.py",
    ):
        blanket_cfg = copy.deepcopy(base_cfg)
        blanket_cfg["allowlist"]["paths"].append(blanket_path)
        with pytest.raises(AssertionError, match="must never exclude"):
            _validate_gitleaks_config(blanket_cfg)

    # Weakening [[rules.allowlists]] condition or adding unanchored prefix regexes must fail closed
    or_condition_cfg = copy.deepcopy(base_cfg)
    or_condition_cfg["rules"][0]["allowlists"][0]["condition"] = "OR"
    with pytest.raises(AssertionError, match="condition = 'AND'"):
        _validate_gitleaks_config(or_condition_cfg)

    broad_regex_cfg = copy.deepcopy(base_cfg)
    broad_regex_cfg["rules"][1]["allowlists"][0]["regexes"].append("sk_live_")
    with pytest.raises(AssertionError, match="must not match fresh secret probe"):
        _validate_gitleaks_config(broad_regex_cfg)

    # Contributor guide, agent guide, and protected-path governance stay synchronized
    agents_md = (repo_root / "AGENTS.md").read_text(encoding="utf-8")
    assert "gitleaks-dir" in agents_md
    assert ".gitleaks.toml" in agents_md

    contributing_md = (repo_root / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "gitleaks-dir" in contributing_md
    assert ".gitleaks.toml" in contributing_md

    codeowners = (repo_root / ".github/CODEOWNERS").read_text(encoding="utf-8")
    assert ".gitleaks.toml @fderuiter" in codeowners

    auto_merge_doc = (repo_root / "docs/auto_merge_policy.md").read_text(
        encoding="utf-8"
    )
    assert "`.gitleaks.toml`" in auto_merge_doc


def test_ci_and_precommit_isolate_live_link_checks_and_retire_validate_imports():
    """Verify CI static-checks and pre-commit run local-only link checks and omit retired validate_imports."""
    workflow = _load_ci_workflow()
    static_steps = workflow["jobs"]["static-checks"]["steps"]
    static_runs = "\n".join(s.get("run", "") for s in static_steps)

    assert "scripts/validate_links.py --local-only" in static_runs
    assert "scripts/generate_docs.py --check --local-only" in static_runs
    assert "validate_imports.py" not in static_runs
    assert "deptry" in static_runs
    assert not Path("scripts/validate_imports.py").exists()

    precommit_cfg = yaml.safe_load(
        Path(".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    local_repo = next(r for r in precommit_cfg["repos"] if r["repo"] == "local")
    hooks = {h["id"]: h for h in local_repo["hooks"]}

    assert "validate-imports" not in hooks
    assert "deptry" in hooks
    validate_docs_entry = hooks["validate-docs"]["entry"]
    assert "--check" in validate_docs_entry
    assert "--no-build" in validate_docs_entry
    assert "--local-only" in validate_docs_entry

    contributing = Path("CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "SKIP=no-commit-to-branch uv run pre-commit run --all-files" in contributing
    assert "scripts/generate_docs.py --check --no-build --local-only" in contributing

    pr_template = Path(".github/pull_request_template.md").read_text(encoding="utf-8")
    assert "scripts/generate_docs.py --check --no-build --local-only" in pr_template


def test_ci_workflow_branch_concurrency_and_main_protection():
    """Verify pull_request and workflow_dispatch share branch concurrency while main runs finish intact."""
    workflow = _load_ci_workflow()
    concurrency = workflow.get("concurrency")
    assert isinstance(concurrency, dict), "Missing top-level concurrency in ci.yml"

    group = str(concurrency.get("group", ""))
    assert group.startswith("ci-${{"), f"Unexpected concurrency group: {group!r}"
    # Feature branches share the branch name across pull_request (github.head_ref) and workflow_dispatch (github.ref_name)
    assert "github.head_ref" in group
    assert "github.ref_name" in group
    # Fork PRs cannot collide with same-named branches (e.g. 'main') in the base repository
    assert "github.event.pull_request.head.repo.full_name != github.repository" in group

    cancel_expr = str(concurrency.get("cancel-in-progress", ""))
    assert cancel_expr == "${{ github.ref != 'refs/heads/main' }}"


def test_security_and_audit_scopes_gitleaks_on_workflow_dispatch(tmp_path: Path):
    """Verify workflow_dispatch scopes gitleaks to branch commits rather than full repo history."""
    import json

    workflow = _load_ci_workflow()
    steps = workflow["jobs"]["security-and-audit"]["steps"]

    scope_step = next(
        (s for s in steps if s.get("id") == "scope-gitleaks"),
        None,
    )
    assert scope_step is not None, "Missing 'scope-gitleaks' step in security-and-audit"
    assert scope_step.get("if") == "github.event_name == 'workflow_dispatch'"
    assert scope_step.get("shell") == "python"

    gitleaks_step = next(
        (s for s in steps if "gitleaks/gitleaks-action@" in s.get("uses", "")),
        None,
    )
    assert gitleaks_step is not None, (
        "Missing gitleaks-action step in security-and-audit"
    )
    assert steps.index(scope_step) < steps.index(gitleaks_step)

    gitleaks_env = gitleaks_step.get("env") or {}
    assert (
        gitleaks_env.get("GITHUB_EVENT_NAME")
        == "${{ github.event_name == 'workflow_dispatch' && 'push' || github.event_name }}"
    )
    assert (
        gitleaks_env.get("GITHUB_EVENT_PATH")
        == "${{ steps.scope-gitleaks.outputs.event_path || github.event_path }}"
    )

    # Execute the scope-gitleaks Python script against a real git repository
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _run_git(repo_dir, "init", "--quiet", "-b", "main")
    _run_git(repo_dir, "config", "user.name", "Test User")
    _run_git(repo_dir, "config", "user.email", "test@example.com")

    main_shas: list[str] = []
    for i in (1, 2):
        (repo_dir / "main.txt").write_text(f"main {i}\n", encoding="utf-8")
        _run_git(repo_dir, "add", "main.txt")
        _run_git(repo_dir, "commit", "--quiet", "-m", f"main {i}")
        main_shas.append(_run_git(repo_dir, "rev-parse", "HEAD"))

    # Branch off main at main_shas[0], add 2 feature commits, then merge main_shas[1] (simulating update_branch)
    _run_git(repo_dir, "checkout", "--quiet", "-b", "feat/branch", main_shas[0])
    feat_shas: list[str] = []
    for i in (1, 2):
        (repo_dir / "feat.txt").write_text(f"feat {i}\n", encoding="utf-8")
        _run_git(repo_dir, "add", "feat.txt")
        _run_git(repo_dir, "commit", "--quiet", "-m", f"feat {i}")
        feat_shas.append(_run_git(repo_dir, "rev-parse", "HEAD"))

    _run_git(repo_dir, "update-ref", "refs/remotes/origin/main", main_shas[1])
    _run_git(repo_dir, "merge", "--no-edit", "--quiet", "refs/remotes/origin/main")
    merged_head_sha = _run_git(repo_dir, "rev-parse", "HEAD")

    def run_scope_script(git_ref: str) -> dict:
        runner_temp = tmp_path / f"temp_{git_ref.replace('/', '_')}"
        runner_temp.mkdir(parents=True, exist_ok=True)
        in_event = runner_temp / "in_event.json"
        in_event.write_text(json.dumps({"inputs": None}), encoding="utf-8")
        gh_out = runner_temp / "gh_output.txt"
        gh_out.write_text("", encoding="utf-8")

        env = os.environ.copy()
        env.update(
            {
                "GITHUB_EVENT_PATH": str(in_event),
                "GITHUB_REF": git_ref,
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_OUTPUT": str(gh_out),
            }
        )
        subprocess.run(
            [sys.executable, "-c", scope_step["run"]],
            cwd=repo_dir,
            env=env,
            check=True,
        )
        out_lines = dict(
            line.split("=", 1)
            for line in gh_out.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        scoped_file = Path(out_lines["event_path"])
        return json.loads(scoped_file.read_text(encoding="utf-8"))

    feat_payload = run_scope_script("refs/heads/feat/branch")
    assert feat_payload["commits"] == [
        {"id": feat_shas[0]},
        {"id": merged_head_sha},
    ]

    _run_git(repo_dir, "checkout", "--quiet", "main")
    main_payload = run_scope_script("refs/heads/main")
    assert main_payload["commits"] == [{"id": main_shas[1]}]


def test_ci_heavy_tests_model_weights_cache_excludes_pyproject_toml():
    """Verify heavy-tests model-weights cache hashes uv.lock and scripts/build.py without pyproject.toml or restore-keys."""
    workflow = _load_ci_workflow()
    steps = workflow["jobs"]["heavy-tests"]["steps"]
    cache_step = next(
        (
            s
            for s in steps
            if "actions/cache@" in s.get("uses", "")
            and str((s.get("with") or {}).get("key", "")).startswith("model-weights-")
        ),
        None,
    )
    assert cache_step is not None, "Missing model-weights cache step in heavy-tests"
    cache_with = cache_step["with"]
    key = cache_with["key"]
    assert "'uv.lock'" in key
    assert "'scripts/build.py'" in key
    assert "pyproject.toml" not in key
    assert "restore-keys" not in cache_with


def test_precommit_check_yaml_does_not_enable_global_unsafe_mode():
    """Verify check-yaml in .pre-commit-config.yaml enforces safe YAML without --unsafe."""
    import re

    precommit_cfg = yaml.safe_load(
        Path(".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    local_repo = next(r for r in precommit_cfg["repos"] if r["repo"] == "local")
    check_yaml = next(h for h in local_repo["hooks"] if h.get("id") == "check-yaml")
    assert "check-yaml" in check_yaml.get("entry", "")
    assert "--unsafe" not in (check_yaml.get("args") or []), (
        "check-yaml must not pass --unsafe globally"
    )
    exclude_re = check_yaml.get("exclude", "")
    assert exclude_re and re.search(exclude_re, "mkdocs.yml")
    assert not re.search(exclude_re, ".github/workflows/ci.yml")

    validate_docs = next(
        h for h in local_repo["hooks"] if h.get("id") == "validate-docs"
    )
    assert re.search(validate_docs["files"], "mkdocs.yml")


def test_ci_jobs_scope_uv_groups_drop_notebooks_and_deduplicate_checks():
    """Verify notebooks group is removed, CI jobs scope uv sync groups, and duplicate PE/config/snapshot steps are pruned."""
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dep_groups = pyproject.get("dependency-groups", {})
    assert "notebooks" not in dep_groups
    assert set(dep_groups) == {"dev", "docs", "build"}

    uv_lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    for pkg in uv_lock.get("package", []):
        dev_deps = pkg.get("dev-dependencies") or {}
        assert "notebooks" not in dev_deps
        assert pkg.get("name") not in {"jupyter", "ipykernel", "notebook", "jupyterlab"}

    workflow = _load_ci_workflow()
    jobs = workflow["jobs"]

    for job_name, job in jobs.items():
        for step in job.get("steps", []):
            run_cmd = step.get("run", "")
            assert "--all-groups" not in run_cmd, (
                f"Job {job_name!r} must use scoped --group flags instead of --all-groups"
            )

    build_steps = jobs["build-and-test"]["steps"]
    build_sync = next(s for s in build_steps if "uv sync" in s.get("run", ""))
    assert build_sync["run"].strip() == "uv sync --frozen --group dev --group build"
    assert not any(
        "verify_pe_dependencies.py" in s.get("run", "") for s in build_steps
    ), "Duplicate verify_pe_dependencies.py step must be removed from build-and-test"

    cfg_occurrences = [
        (job_name, s.get("name"))
        for job_name, job in jobs.items()
        for s in job.get("steps", [])
        if "scripts/validate_config.py" in s.get("run", "")
    ]
    assert cfg_occurrences == [("static-checks", "Validate Configuration Schema")]

    offline_steps = jobs["verify-offline-bundle"]["steps"]
    offline_cache = next(
        s
        for s in offline_steps
        if "actions/cache@" in s.get("uses", "")
        and str((s.get("with") or {}).get("key", "")).startswith("offline-wheels-")
    )
    cache_paths = {
        line.strip()
        for line in offline_cache["with"]["path"].splitlines()
        if line.strip()
    }
    assert "~/.cache/pip" in cache_paths
    assert "~/.cache/uv" in cache_paths
    cache_key = offline_cache["with"]["key"]
    assert "'offline_bundle/requirements.txt'" in cache_key
    assert "pyproject.toml" not in cache_key


def test_static_checks_doc_build_flag_gating(tmp_path: Path):
    """Execute static-checks Validate Documentation and Integrity script across PR and main scenarios."""
    workflow = _load_ci_workflow()
    compile_step = next(
        s
        for s in workflow["jobs"]["static-checks"]["steps"]
        if s.get("name") == "Validate Documentation and Integrity"
    )
    script = compile_step["run"]
    bash_exe = _find_git_bash()

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _run_git(repo_dir, "init", "--quiet", "-b", "main")
    _run_git(repo_dir, "config", "user.name", "Test User")
    _run_git(repo_dir, "config", "user.email", "test@example.com")
    (repo_dir / "README.md").write_text("# Sortify\n", encoding="utf-8")
    (repo_dir / "app").mkdir()
    (repo_dir / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    _run_git(repo_dir, "add", ".")
    _run_git(repo_dir, "commit", "--quiet", "-m", "initial")
    base_sha = _run_git(repo_dir, "rev-parse", "HEAD")

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    args_log = tmp_path / "uv_args.txt"
    uv_stub = stub_bin / "uv"
    uv_stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" > "{args_log.as_posix()}"\n',
        encoding="utf-8",
    )
    uv_stub.chmod(0o755)

    def run_doc_step(event_name: str, git_ref: str, b_sha: str, h_sha: str) -> str:
        args_log.write_text("", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{stub_bin}{os.pathsep}{env.get('PATH', '')}",
                "EVENT_NAME": event_name,
                "GITHUB_REF_FULL": git_ref,
                "BASE_SHA": b_sha,
                "HEAD_SHA": h_sha,
            }
        )
        subprocess.run(
            [bash_exe, "-e", "-c", script],
            cwd=repo_dir,
            env=env,
            check=True,
        )
        return args_log.read_text(encoding="utf-8").strip()

    # 1. Code-only PR passes --no-build
    _run_git(repo_dir, "checkout", "--quiet", "-b", "feat/code", base_sha)
    (repo_dir / "app" / "main.py").write_text("x = 2\n", encoding="utf-8")
    _run_git(repo_dir, "add", "app/main.py")
    _run_git(repo_dir, "commit", "--quiet", "-m", "code change")
    code_sha = _run_git(repo_dir, "rev-parse", "HEAD")
    assert run_doc_step(
        "pull_request", "refs/pull/1/merge", base_sha, code_sha
    ).endswith("--check --local-only --no-build")

    # 2. main push and workflow_dispatch always pass --build
    assert run_doc_step("push", "refs/heads/main", "", "").endswith(
        "--check --local-only --build"
    )
    assert run_doc_step("workflow_dispatch", "refs/heads/feat/code", "", "").endswith(
        "--check --local-only --build"
    )

    # 3. PRs touching docs/, mkdocs.yml, root Markdown, or scripts/generate_docs.py pass --build
    for idx, rel_path in enumerate(
        ("docs/guide.md", "mkdocs.yml", "CONTRIBUTING.md", "scripts/generate_docs.py")
    ):
        _run_git(repo_dir, "checkout", "--quiet", "-b", f"feat/doc-{idx}", base_sha)
        target = repo_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("updated\n", encoding="utf-8")
        _run_git(repo_dir, "add", rel_path)
        _run_git(repo_dir, "commit", "--quiet", "-m", f"touch {rel_path}")
        doc_sha = _run_git(repo_dir, "rev-parse", "HEAD")
        assert run_doc_step(
            "pull_request", f"refs/pull/{idx + 2}/merge", base_sha, doc_sha
        ).endswith("--check --local-only --build"), f"Expected --build for {rel_path}"
