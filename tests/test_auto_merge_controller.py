"""Tests for Auto-Merge Controller."""

from __future__ import annotations

from unittest.mock import MagicMock

from scripts.auto_merge_controller import (
    AutoMergeAction,
    AutoMergeResult,
    GitHubClient,
    automerge_label_applied_by_maintainer,
    build_pr_metadata,
    is_pr_eligible_for_automerge,
    process_auto_merge_queue,
)

VERIFIED_COMMIT = {"sha": "c1", "commit": {"verification": {"verified": True}}}


def _stub_pr_contents(client, filenames, labels=None):
    """Make the API-sourced re-classification see the given files and commits."""
    client.list_pull_request_files.return_value = [
        {"filename": name, "additions": 1, "deletions": 0} for name in filenames
    ]
    client.list_pull_request_commits.return_value = [VERIFIED_COMMIT]


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
    _stub_pr_contents(client, ["docs/guide.md"])
    client.get_required_ci_conclusion.return_value = "success"
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
    _stub_pr_contents(client, ["docs/guide.md"])
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
    _stub_pr_contents(client, ["docs/guide.md"])
    client.get_required_ci_conclusion.return_value = None

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
    _stub_pr_contents(client, ["docs/guide.md"])
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


def _eligible_pr(number, sha="sha_head", labels=None):
    return {
        "number": number,
        "title": "docs: tweak",
        "draft": False,
        "state": "open",
        "head": {"sha": sha},
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "stitch"},
        "labels": [{"name": name} for name in (labels or ["automerge:eligible"])],
    }


def test_process_queue_reclassifies_and_blocks_self_approved_pr():
    """A PR that labels itself eligible but touches a sensitive path is not merged."""
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 300, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    client.get_pull_request.return_value = _eligible_pr(300)
    client.get_commit_verification.return_value = {"verified": True}
    _stub_pr_contents(client, ["scripts/classify_pr.py"])
    client.get_required_ci_conclusion.return_value = "success"

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.NONE
    client.merge_pull_request.assert_not_called()
    client.update_branch.assert_not_called()


def test_process_queue_requires_green_required_ci_check_run():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 301, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    client.get_pull_request.return_value = _eligible_pr(301)
    client.get_commit_verification.return_value = {"verified": True}
    _stub_pr_contents(client, ["docs/guide.md"])
    client.get_required_ci_conclusion.return_value = "failure"

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.NONE
    client.merge_pull_request.assert_not_called()


def test_get_required_ci_conclusion_ignores_non_actions_check_runs():
    client = GitHubClient("fake_token", "owner", "repo")
    client._get = MagicMock(
        return_value={
            "check_runs": [
                {
                    "id": 9,
                    "name": "Required CI",
                    "conclusion": "success",
                    "app": {"slug": "some-other-app"},
                },
                {
                    "id": 5,
                    "name": "Required CI",
                    "conclusion": "failure",
                    "app": {"slug": "github-actions"},
                },
            ]
        }
    )
    assert client.get_required_ci_conclusion("sha") == "failure"
    client._get.assert_called_once_with(
        "commits/sha/check-runs",
        params={"check_name": "Required CI", "per_page": 100},
    )


def test_get_required_ci_conclusion_uses_latest_actions_run():
    client = GitHubClient("fake_token", "owner", "repo")
    client._get = MagicMock(
        return_value={
            "check_runs": [
                {
                    "id": 1,
                    "name": "Required CI",
                    "conclusion": "failure",
                    "app": {"slug": "github-actions"},
                },
                {
                    "id": 2,
                    "name": "Required CI",
                    "conclusion": "success",
                    "app": {"slug": "github-actions"},
                },
            ]
        }
    )
    assert client.get_required_ci_conclusion("sha") == "success"


def test_get_required_ci_conclusion_missing_returns_none():
    client = GitHubClient("fake_token", "owner", "repo")
    client._get = MagicMock(return_value={"check_runs": []})
    assert client.get_required_ci_conclusion("sha") is None


def test_build_pr_metadata_uses_api_data():
    client = MagicMock(spec=GitHubClient)
    client.list_pull_request_files.return_value = [
        {"filename": "app/x.py", "additions": 3, "deletions": 2},
        {"filename": "docs/y.md", "additions": 1, "deletions": 0},
    ]
    client.list_pull_request_commits.return_value = [
        VERIFIED_COMMIT,
        {"sha": "bad", "commit": {"verification": {"verified": False}}},
    ]
    client.list_issue_events.return_value = [_label_event("fderuiter")]
    client.get_collaborator_permission.return_value = "admin"
    pr = _eligible_pr(302, labels=["automerge", "dependencies"])
    pr["title"] = "Bump foo from 1.0 to 2.0"
    pr["user"] = {"login": "notdependabot"}

    meta = build_pr_metadata(client, pr)
    assert meta.changed_files == ["app/x.py", "docs/y.md"]
    assert (meta.additions, meta.deletions) == (4, 2)
    assert meta.all_commits_verified is False
    assert meta.unverified_commit_shas == ["bad"]
    assert meta.labels == ["automerge", "dependencies"]
    assert meta.is_dependabot is False
    # Version parsing only applies to the real Dependabot login.
    assert meta.dependency_update_type == "minor"


def test_build_pr_metadata_reads_dependabot_versions():
    client = MagicMock(spec=GitHubClient)
    _stub_pr_contents(client, ["uv.lock"])
    pr = _eligible_pr(303, labels=["dependencies"])
    pr["user"] = {"login": "dependabot[bot]"}

    pr["title"] = "Bump rich from 13.7.1 to 14.0.0"
    assert build_pr_metadata(client, pr).dependency_update_type == "major"
    pr["title"] = "Bump rich from 13.7.1 to 13.9.4"
    assert build_pr_metadata(client, pr).dependency_update_type == "minor"


def _label_event(login, actor_type="User", label="automerge"):
    return {
        "event": "labeled",
        "label": {"name": label},
        "actor": {"login": login, "type": actor_type},
    }


def test_automerge_label_counts_only_from_a_maintainer():
    client = MagicMock(spec=GitHubClient)
    client.list_issue_events.return_value = [_label_event("fderuiter")]
    client.get_collaborator_permission.return_value = "admin"
    assert automerge_label_applied_by_maintainer(client, 1) is True

    client.get_collaborator_permission.return_value = "read"
    assert automerge_label_applied_by_maintainer(client, 1) is False


def test_automerge_label_from_a_bot_is_ignored():
    client = MagicMock(spec=GitHubClient)
    client.get_collaborator_permission.return_value = "write"
    for event in (
        _label_event("google-labs-stitch[bot]", actor_type="Bot"),
        _label_event("github-actions[bot]"),
    ):
        client.list_issue_events.return_value = [event]
        assert automerge_label_applied_by_maintainer(client, 1) is False


def test_automerge_label_uses_the_latest_labelling():
    client = MagicMock(spec=GitHubClient)
    client.list_issue_events.return_value = [
        _label_event("fderuiter"),
        {"event": "unlabeled", "label": {"name": "automerge"}, "actor": {}},
        _label_event("stitch-bot[bot]", actor_type="Bot"),
    ]
    client.get_collaborator_permission.return_value = "admin"
    assert automerge_label_applied_by_maintainer(client, 1) is False


def test_automerge_label_check_fails_closed_on_api_error():
    client = MagicMock(spec=GitHubClient)
    client.list_issue_events.side_effect = RuntimeError("403")
    assert automerge_label_applied_by_maintainer(client, 1) is False


def test_process_queue_ignores_automerge_label_added_by_pr_author():
    """A bot that labels its own production PR 'automerge' is not merged."""
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 304, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    client.get_pull_request.return_value = _eligible_pr(
        304, labels=["automerge:eligible", "automerge"]
    )
    client.get_commit_verification.return_value = {"verified": True}
    _stub_pr_contents(client, ["app/core/analyzer.py"])
    client.list_issue_events.return_value = [
        _label_event("google-labs-stitch[bot]", actor_type="Bot")
    ]
    client.get_required_ci_conclusion.return_value = "success"

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.NONE
    client.merge_pull_request.assert_not_called()


def test_process_queue_merges_production_pr_labelled_by_maintainer():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 305, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    client.get_pull_request.return_value = _eligible_pr(
        305, labels=["automerge:eligible", "automerge"]
    )
    client.get_commit_verification.return_value = {"verified": True}
    _stub_pr_contents(client, ["app/core/analyzer.py"])
    client.list_issue_events.return_value = [_label_event("fderuiter")]
    client.get_collaborator_permission.return_value = "admin"
    client.get_required_ci_conclusion.return_value = "success"
    client.merge_pull_request.return_value = True

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.MERGED
    assert res.pr_id == 305


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


def test_process_queue_merges_automated_security_patch_pr():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {
            "number": 500,
            "labels": [
                {"name": "automerge:eligible"},
                {"name": "security"},
                {"name": "dependencies"},
            ],
            "draft": False,
        }
    ]
    client.get_pull_request.return_value = {
        "number": 500,
        "title": "[Security] Patch certifi from 2026.7.22 to 2026.10.19",
        "draft": False,
        "state": "open",
        "head": {"sha": "sha_sec_123", "ref": "security/patch-certifi-2026-10-19"},
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "github-actions[bot]"},
        "labels": [
            {"name": "security"},
            {"name": "dependencies"},
            {"name": "automerge"},
            {"name": "automerge:eligible"},
        ],
    }
    client.get_commit_verification.return_value = {
        "verified": True,
        "reason": "valid",
    }
    _stub_pr_contents(client, ["uv.lock"])
    client.get_required_ci_conclusion.return_value = "success"
    client.merge_pull_request.return_value = True

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.MERGED
    assert res.pr_id == 500
