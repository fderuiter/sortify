"""Tests for Auto-Merge Controller."""

from __future__ import annotations

from unittest.mock import MagicMock

from scripts.auto_merge_controller import (
    AutoMergeAction,
    AutoMergeResult,
    GitHubClient,
    is_pr_eligible_for_automerge,
    process_auto_merge_queue,
)


def test_is_pr_eligible_for_automerge():
    pr_eligible = {"labels": [{"name": "automerge:eligible"}]}
    assert is_pr_eligible_for_automerge(pr_eligible) is True

    # Raw automerge label alone is NOT eligible for the controller
    pr_automerge = {"labels": [{"name": "automerge"}]}
    assert is_pr_eligible_for_automerge(pr_automerge) is False

    pr_blocked = {
        "labels": [{"name": "automerge:eligible"}, {"name": "automerge:blocked"}]
    }
    assert is_pr_eligible_for_automerge(pr_blocked) is False

    pr_disabled = {
        "labels": [{"name": "automerge:eligible"}, {"name": "disable-automerge"}]
    }
    assert is_pr_eligible_for_automerge(pr_disabled) is False

    pr_conflict = {
        "labels": [{"name": "automerge:eligible"}, {"name": "automerge:conflict"}]
    }
    assert is_pr_eligible_for_automerge(pr_conflict) is False


def test_process_queue_no_eligible_prs():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 1, "labels": [], "draft": False},
        {"number": 2, "labels": [{"name": "automerge:blocked"}], "draft": False},
    ]

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.NONE
    assert res.pr_id is None
    assert res.pr_number is None
    assert res.pull_request_id is None


def test_get_commit_verification():
    client = GitHubClient("fake_token", "owner", "repo")
    client._get = MagicMock(
        return_value={
            "commit": {
                "verification": {
                    "verified": True,
                    "reason": "valid",
                    "signature": "gpg_sig",
                }
            }
        }
    )
    res = client.get_commit_verification("sha123")
    assert res == {"verified": True, "reason": "valid", "signature": "gpg_sig"}
    client._get.assert_called_once_with("commits/sha123")


def test_process_queue_merges_eligible_pr():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 200, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    client.get_pull_request.return_value = {
        "number": 200,
        "title": "docs: fix typo",
        "draft": False,
        "state": "open",
        "head": {"sha": "sha12345"},
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "fderuiter"},
    }
    client.get_commit_verification.return_value = {
        "verified": True,
        "reason": "valid",
    }
    client.get_check_runs.return_value = {
        "Required CI": "success",
        "automerge/classification": "success",
    }
    client.get_commit_status_checks.return_value = {}
    client.merge_pull_request.return_value = True

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.MERGED
    assert res.pr_id == 200
    assert res.pr_number == 200
    assert res.pull_request_id == 200
    client.merge_pull_request.assert_called_once_with(
        200,
        "sha12345",
        "docs: fix typo (#200)",
        "Auto-merged low-risk PR #200\n\nAuthor: fderuiter",
    )


def test_process_queue_updates_behind_branch():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 201, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    client.get_pull_request.return_value = {
        "number": 201,
        "title": "fix: minor bug",
        "draft": False,
        "state": "open",
        "head": {"sha": "sha67890"},
        "mergeable": True,
        "mergeable_state": "behind",
        "user": {"login": "stitch"},
    }
    client.get_commit_verification.return_value = {
        "verified": True,
        "reason": "valid",
    }
    client.update_branch.return_value = True

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.UPDATED
    assert res.pr_id == 201
    assert res.pr_number == 201
    assert res.pull_request_id == 201
    client.update_branch.assert_called_once_with(201, "sha67890")
    client.merge_pull_request.assert_not_called()


def test_process_queue_does_not_update_blocked_mergeable_state():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 203, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    client.get_pull_request.return_value = {
        "number": 203,
        "title": "fix: minor bug",
        "draft": False,
        "state": "open",
        "head": {"sha": "sha99999"},
        "mergeable": True,
        "mergeable_state": "blocked",  # blocked due to requirements, not base staleness
        "user": {"login": "stitch"},
    }
    client.get_commit_verification.return_value = {
        "verified": True,
        "reason": "valid",
    }
    client.get_check_runs.return_value = {}
    client.get_commit_status_checks.return_value = {}

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.NONE
    assert res.pr_id is None
    client.update_branch.assert_not_called()
    client.merge_pull_request.assert_not_called()


def test_process_queue_handles_branch_update_conflict():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 202, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    client.get_pull_request.return_value = {
        "number": 202,
        "title": "fix: minor bug",
        "draft": False,
        "state": "open",
        "head": {"sha": "sha11111"},
        "mergeable": True,
        "mergeable_state": "behind",
        "user": {"login": "jules"},
    }
    client.get_commit_verification.return_value = {
        "verified": True,
        "reason": "valid",
    }
    client.update_branch.return_value = False  # Conflict on update

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.NONE
    assert res.pr_id is None
    client.add_labels.assert_called_once_with(
        202, ["automerge:conflict", "automerge:blocked"]
    )
    client.merge_pull_request.assert_not_called()


def test_process_queue_blocks_unverified_head_commit():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 204, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    client.get_pull_request.return_value = {
        "number": 204,
        "title": "feat: untrusted change",
        "draft": False,
        "state": "open",
        "head": {"sha": "sha_unverified"},
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "attacker"},
    }
    client.get_commit_verification.return_value = {
        "verified": False,
        "reason": "unsigned",
    }

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.NONE
    assert res.pr_id is None
    client.add_labels.assert_called_once_with(204, ["automerge:blocked", "risk:high"])
    client.merge_pull_request.assert_not_called()


def test_process_queue_bot_commit_verification():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 205, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    client.get_pull_request.return_value = {
        "number": 205,
        "title": "bump ruff",
        "draft": False,
        "state": "open",
        "head": {"sha": "sha_bot_commit"},
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "dependabot[bot]"},
    }
    # Spoofed bot commit with verification.verified == False
    client.get_commit_verification.return_value = {
        "verified": False,
        "reason": "unknown_key",
    }

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.NONE
    assert res.pr_id is None
    client.add_labels.assert_called_once_with(205, ["automerge:blocked", "risk:high"])
    client.merge_pull_request.assert_not_called()


def test_main_cli_output_merge(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "fderuiter/sortify")
    monkeypatch.setattr(
        "scripts.auto_merge_controller.process_auto_merge_queue",
        lambda client: AutoMergeResult(action=AutoMergeAction.MERGED, pr_id=123),
    )
    from scripts.auto_merge_controller import main

    main()
    captured = capsys.readouterr()
    assert "Auto-merged PR #123" in captured.out


def test_main_cli_output_update(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "fderuiter/sortify")
    monkeypatch.setattr(
        "scripts.auto_merge_controller.process_auto_merge_queue",
        lambda client: AutoMergeResult(action=AutoMergeAction.UPDATED, pr_id=456),
    )
    from scripts.auto_merge_controller import main

    main()
    captured = capsys.readouterr()
    assert "Updated branch for PR #456" in captured.out


def test_main_cli_output_none(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "fderuiter/sortify")
    monkeypatch.setattr(
        "scripts.auto_merge_controller.process_auto_merge_queue",
        lambda client: AutoMergeResult(action=AutoMergeAction.NONE, pr_id=None),
    )
    from scripts.auto_merge_controller import main

    main()
    captured = capsys.readouterr()
    assert "No PR was auto-merged in this cycle." in captured.out
