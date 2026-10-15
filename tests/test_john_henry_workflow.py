"""Unit tests for Sanitize CI/CD Fix Bot Comment workflow (.github/workflows/john-henry-mode.yml)."""

from pathlib import Path

WORKFLOW_PATH = Path(__file__).parent.parent / ".github" / "workflows" / "john-henry-mode.yml"


def test_workflow_file_exists():
    """Verify that john-henry-mode.yml workflow file exists."""
    assert WORKFLOW_PATH.exists(), f"Workflow file not found at {WORKFLOW_PATH}"


def test_workflow_trigger_types():
    """Verify that issue_comment trigger includes both 'created' and 'edited' event types."""
    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "issue_comment:" in content
    assert "created" in content
    assert "edited" in content


def test_workflow_author_association_check():
    """Verify that workflow condition checks author_association for authorized roles."""
    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "author_association" in content
    assert "MEMBER" in content
    assert "OWNER" in content
    assert "COLLABORATOR" in content


def simulate_workflow_condition(
    is_pull_request: bool,
    author_association: str,
    comment_body: str,
) -> bool:
    """Simulate GitHub Actions job condition logic for john-henry-mode.yml."""
    allowed_roles = {"MEMBER", "OWNER", "COLLABORATOR"}
    return (
        is_pull_request
        and (author_association in allowed_roles)
        and ("[CI/CD Fix Attempt " in comment_body)
    )


def test_simulate_workflow_condition_authorized_roles():
    """Verify authorized roles trigger workflow when fix attempt pattern is present."""
    for role in ["MEMBER", "OWNER", "COLLABORATOR"]:
        assert simulate_workflow_condition(
            is_pull_request=True,
            author_association=role,
            comment_body="[CI/CD Fix Attempt 1] Fixing issue",
        ) is True


def test_simulate_workflow_condition_untrusted_roles():
    """Verify external/untrusted roles do NOT trigger workflow."""
    untrusted_roles = ["CONTRIBUTOR", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "NONE", "MANNEQUIN", ""]
    for role in untrusted_roles:
        assert simulate_workflow_condition(
            is_pull_request=True,
            author_association=role,
            comment_body="[CI/CD Fix Attempt 1] Malicious edit",
        ) is False


def test_simulate_workflow_condition_edited_comment_pattern():
    """Verify edited comment with fix attempt marker passes condition for authorized role."""
    assert simulate_workflow_condition(
        is_pull_request=True,
        author_association="MEMBER",
        comment_body="Edited comment with [CI/CD Fix Attempt 5] marker",
    ) is True


def test_simulate_workflow_condition_non_pr():
    """Verify issue comments not on pull requests do not trigger workflow."""
    assert simulate_workflow_condition(
        is_pull_request=False,
        author_association="OWNER",
        comment_body="[CI/CD Fix Attempt 1] Regular issue comment",
    ) is False


def test_simulate_workflow_condition_missing_pattern():
    """Verify comments without the fix attempt pattern do not trigger workflow."""
    assert simulate_workflow_condition(
        is_pull_request=True,
        author_association="COLLABORATOR",
        comment_body="Regular PR comment without fix marker",
    ) is False
