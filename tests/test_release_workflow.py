import os

import yaml


def _load_release_workflow():
    workflow_path = os.path.join(".github", "workflows", "release.yml")
    assert os.path.exists(workflow_path), f"Workflow file missing: {workflow_path}"
    with open(workflow_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _steps_using(steps, action):
    return [s for s in steps if action in s.get("uses", "")]


def test_release_workflow_attestations_and_sbom():
    """
    Verify that .github/workflows/release.yml correctly configures:
    1. A read-only token by default, with write scopes only on the publish job.
    2. Anchore Syft CycloneDX SBOM generation in the build job.
    3. Build provenance attestation step (actions/attest-build-provenance).
    4. SBOM attestation step (actions/attest-sbom).
    5. SBOM asset upload in GitHub Release upload step.
    """
    workflow = _load_release_workflow()
    jobs = workflow.get("jobs", {})

    # 1. Least privilege: the build runs on PRs and executes third-party code.
    assert workflow.get("permissions") == {"contents": "read"}
    assert "build-binaries" in jobs
    assert "publish" in jobs
    assert "permissions" not in jobs["build-binaries"]
    publish = jobs["publish"]
    assert publish.get("permissions") == {
        "contents": "write",
        "id-token": "write",
        "attestations": "write",
    }
    assert publish.get("needs") == "build-binaries"
    assert publish.get("if") == "github.event_name == 'release'"

    build_steps = jobs["build-binaries"].get("steps", [])
    checkouts = _steps_using(build_steps, "actions/checkout")
    assert checkouts and all(
        s.get("with", {}).get("persist-credentials") is False for s in checkouts
    )

    # 2. Anchore Syft SBOM step check
    syft_steps = _steps_using(build_steps, "anchore/sbom-action")
    assert len(syft_steps) > 0, "Missing anchore/sbom-action step in release.yml"
    syft_with = syft_steps[0].get("with", {})
    assert syft_with.get("format") == "cyclonedx-json"
    assert "${{ matrix.artifact_name }}.sbom.json" in syft_with.get("output-file", "")
    assert syft_with.get("upload-release-assets") is False

    publish_steps = publish.get("steps", [])

    # 3. Build provenance attestation step check
    provenance_steps = _steps_using(publish_steps, "actions/attest-build-provenance")
    assert len(provenance_steps) > 0, (
        "Missing actions/attest-build-provenance step in release.yml"
    )
    prov_with = provenance_steps[0].get("with", {})
    assert "${{ matrix.artifact_name }}" in prov_with.get("subject-path", "")

    model_prov_steps = [
        s
        for s in provenance_steps
        if "smart-autosorter-models.zip" in s.get("with", {}).get("subject-path", "")
    ]
    assert len(model_prov_steps) == 1, (
        "Missing actions/attest-build-provenance step for model weights in release.yml"
    )
    assert model_prov_steps[0].get("if") == "matrix.os_name == 'Linux'"

    # 4. SBOM attestation step check
    attest_sbom_steps = _steps_using(publish_steps, "actions/attest-sbom")
    assert len(attest_sbom_steps) > 0, "Missing actions/attest-sbom step in release.yml"
    sbom_attest_with = attest_sbom_steps[0].get("with", {})
    assert "${{ matrix.artifact_name }}" in sbom_attest_with.get("subject-path", "")
    assert "${{ matrix.artifact_name }}.sbom.json" in sbom_attest_with.get(
        "sbom-path", ""
    )

    # 5. Upload Release Artifacts step check
    release_steps = _steps_using(publish_steps, "softprops/action-gh-release")
    assert len(release_steps) > 0, (
        "Missing softprops/action-gh-release step in release.yml"
    )
    files = release_steps[0].get("with", {}).get("files", "")
    assert "${{ matrix.artifact_name }}.sbom.json" in files


def test_publish_matrix_matches_build_matrix():
    """Every built archive is published, and artifact names line up across jobs."""
    jobs = _load_release_workflow()["jobs"]
    built = {
        e["artifact_name"]
        for e in jobs["build-binaries"]["strategy"]["matrix"]["include"]
    }
    published = {
        e["artifact_name"] for e in jobs["publish"]["strategy"]["matrix"]["include"]
    }
    assert built == published

    upload = _steps_using(jobs["build-binaries"]["steps"], "actions/upload-artifact")
    download = _steps_using(jobs["publish"]["steps"], "actions/download-artifact")
    assert upload[0]["with"]["name"] == "release-${{ runner.os }}"
    assert download[0]["with"]["name"] == "release-${{ matrix.os_name }}"
    os_names = {e["os_name"] for e in jobs["publish"]["strategy"]["matrix"]["include"]}
    # runner.os values on GitHub-hosted runners
    assert os_names == {"Windows", "Linux", "macOS"}


def test_release_builds_and_publishes_once():
    """A published release is the only publish trigger, and each asset has one uploader."""
    workflow = _load_release_workflow()
    # PyYAML reads the `on:` key as True.
    triggers = workflow.get("on", workflow.get(True))
    assert "push" not in triggers, "tag pushes would build and publish a second time"
    assert triggers["release"] == {"types": ["published"]}

    uploads = _steps_using(
        workflow["jobs"]["publish"]["steps"], "softprops/action-gh-release"
    )
    models = [s for s in uploads if "smart-autosorter-models.zip" in s["with"]["files"]]
    assert len(models) == 1
    assert models[0].get("if") == "matrix.os_name == 'Linux'"
    assert "smart-autosorter-models.zip.sha256" in models[0]["with"]["files"]


def test_release_workflow_pe_dependency_verification():
    """Verify that .github/workflows/release.yml verifies PE binary dependencies on Windows before archiving."""
    workflow = _load_release_workflow()
    build_steps = workflow["jobs"]["build-binaries"]["steps"]

    verify_steps = [
        s for s in build_steps if s.get("name") == "Verify PE binary dependencies"
    ]
    assert len(verify_steps) == 1, (
        "Missing 'Verify PE binary dependencies' step in build-binaries job in release.yml"
    )

    verify_step = verify_steps[0]
    assert verify_step.get("if") == "runner.os == 'Windows'"
    assert "uv run python scripts/verify_pe_dependencies.py" in verify_step.get(
        "run", ""
    )

    step_names = [s.get("name") for s in build_steps]
    build_idx = step_names.index("Build executable")
    verify_idx = step_names.index("Verify PE binary dependencies")
    archive_win_idx = step_names.index("Package as Archive (Windows)")

    assert build_idx < verify_idx < archive_win_idx, (
        f"Step order invalid: Build ({build_idx}) must precede Verify ({verify_idx}), "
        f"which must precede Windows Archive ({archive_win_idx})"
    )

    # Ensure verification strictly precedes all Windows archive packaging steps
    for idx, step in enumerate(build_steps):
        if (
            "archive" in step.get("name", "").lower()
            and step.get("if") == "runner.os == 'Windows'"
        ):
            assert verify_idx < idx, (
                f"PE verification must run before archive step '{step.get('name')}'"
            )


def test_release_workflow_pull_request_paths_include_manifests():
    """Verify pull_request.paths triggers dry-run builds when dependency manifests or packaging files change."""
    workflow = _load_release_workflow()
    triggers = workflow.get("on", workflow.get(True))
    pr_trigger = triggers.get("pull_request", {})
    assert pr_trigger.get("branches") == ["main"]

    paths = set(pr_trigger.get("paths", []))
    expected_paths = {
        ".github/workflows/release.yml",
        "scripts/build.py",
        "smart-autosorter.spec",
        "scripts/verify_pe_dependencies.py",
        "pyproject.toml",
        "uv.lock",
    }
    assert expected_paths.issubset(paths), (
        f"Missing expected pull_request.paths in release.yml: {sorted(expected_paths - paths)}"
    )


def test_release_workflow_concurrency_cancels_only_pull_requests():
    """Verify top-level concurrency cancels superseded PR builds while letting published releases finish."""
    workflow = _load_release_workflow()
    concurrency = workflow.get("concurrency")
    assert isinstance(concurrency, dict), "Missing top-level concurrency in release.yml"
    assert (
        concurrency.get("group")
        == "release-${{ github.event.pull_request.number || github.event.release.tag_name || github.run_id }}"
    )
    assert (
        concurrency.get("cancel-in-progress")
        == "${{ github.event_name == 'pull_request' }}"
    )


def test_release_workflow_smoke_test_before_archiving():
    """Verify build-binaries runs --smoke-test across all OSes before packaging release archives."""
    workflow = _load_release_workflow()
    build_job = workflow["jobs"]["build-binaries"]
    build_steps = build_job["steps"]

    smoke_steps = [
        s
        for s in build_steps
        if s.get("name") == "Verify compiled executable (Smoke Test)"
    ]
    assert len(smoke_steps) == 1, (
        "Missing 'Verify compiled executable (Smoke Test)' step in build-binaries job in release.yml"
    )

    smoke_step = smoke_steps[0]
    assert "if" not in smoke_step, (
        "Smoke test step must run on Windows, Linux, and macOS without an OS filter"
    )
    assert smoke_step.get("shell") == "python"

    keyring_backend = (smoke_step.get("env") or {}).get("PYTHON_KEYRING_BACKEND") or (
        build_job.get("env") or {}
    ).get("PYTHON_KEYRING_BACKEND")
    assert keyring_backend == "keyring.backends.fail.Keyring"

    run_script = smoke_step.get("run", "")
    assert "smart-autosorter" in run_script
    assert "--smoke-test" in run_script
    assert "smoke_test_error.txt" in run_script
    assert "sys.exit(" in run_script

    step_names = [s.get("name") for s in build_steps]
    build_idx = step_names.index("Build executable")
    pe_verify_idx = step_names.index("Verify PE binary dependencies")
    smoke_idx = step_names.index("Verify compiled executable (Smoke Test)")
    archive_win_idx = step_names.index("Package as Archive (Windows)")
    archive_unix_idx = step_names.index("Package as Archive (Unix)")

    assert build_idx < pe_verify_idx < smoke_idx < archive_win_idx
    assert smoke_idx < archive_unix_idx

    for idx, step in enumerate(build_steps):
        if "archive" in step.get("name", "").lower():
            assert smoke_idx < idx, (
                f"Smoke test must run before archive step '{step.get('name')}'"
            )
