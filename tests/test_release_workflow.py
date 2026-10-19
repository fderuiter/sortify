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
    assert "refs/tags/" in publish.get("if", "")

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
