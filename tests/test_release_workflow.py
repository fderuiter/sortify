import os

import yaml


def test_release_workflow_attestations_and_sbom():
    """
    Verify that .github/workflows/release.yml correctly configures:
    1. id-token: write and attestations: write permissions.
    2. Anchore Syft CycloneDX SBOM generation step.
    3. Build provenance attestation step (actions/attest-build-provenance).
    4. SBOM attestation step (actions/attest-sbom).
    5. SBOM asset upload in GitHub Release upload step.
    """
    workflow_path = os.path.join(".github", "workflows", "release.yml")
    assert os.path.exists(workflow_path), f"Workflow file missing: {workflow_path}"

    with open(workflow_path, "r", encoding="utf-8") as f:
        workflow = yaml.safe_load(f)

    # 1. Verify permissions
    permissions = workflow.get("permissions", {})
    assert permissions.get("contents") == "write"
    assert permissions.get("id-token") == "write"
    assert permissions.get("attestations") == "write"

    # 2. Verify steps in build-binaries job
    jobs = workflow.get("jobs", {})
    assert "build-binaries" in jobs
    job = jobs["build-binaries"]
    steps = job.get("steps", [])

    step_uses = [step.get("uses", "") for step in steps if "uses" in step]
    step_names = [step.get("name", "") for step in steps if "name" in step]

    # Anchore Syft SBOM step check
    syft_steps = [s for s in steps if "anchore/sbom-action" in s.get("uses", "")]
    assert len(syft_steps) > 0, "Missing anchore/sbom-action step in release.yml"
    syft_step = syft_steps[0]
    syft_with = syft_step.get("with", {})
    assert syft_with.get("format") == "cyclonedx-json"
    assert "${{ matrix.artifact_name }}.sbom.json" in syft_with.get("output-file", "")

    # Build provenance attestation step check
    provenance_steps = [
        s for s in steps if "actions/attest-build-provenance" in s.get("uses", "")
    ]
    assert len(provenance_steps) > 0, (
        "Missing actions/attest-build-provenance step in release.yml"
    )
    prov_with = provenance_steps[0].get("with", {})
    assert "${{ matrix.artifact_name }}" in prov_with.get("subject-path", "")

    # SBOM attestation step check
    attest_sbom_steps = [s for s in steps if "actions/attest-sbom" in s.get("uses", "")]
    assert len(attest_sbom_steps) > 0, "Missing actions/attest-sbom step in release.yml"
    sbom_attest_with = attest_sbom_steps[0].get("with", {})
    assert "${{ matrix.artifact_name }}" in sbom_attest_with.get("subject-path", "")
    assert "${{ matrix.artifact_name }}.sbom.json" in sbom_attest_with.get(
        "sbom-path", ""
    )

    # Upload Release Artifacts step check
    release_steps = [
        s for s in steps if "softprops/action-gh-release" in s.get("uses", "")
    ]
    assert len(release_steps) > 0, (
        "Missing softprops/action-gh-release step in release.yml"
    )
    release_with = release_steps[0].get("with", {})
    files = release_with.get("files", "")
    assert "${{ matrix.artifact_name }}.sbom.json" in files
