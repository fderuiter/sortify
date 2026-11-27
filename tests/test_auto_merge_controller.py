"""Tests for Auto-Merge Controller."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call

import pytest
import requests
import yaml

from scripts.auto_merge_controller import (
    AutoMergeAction,
    AutoMergeResult,
    GitHubClient,
    automerge_label_applied_by_maintainer,
    build_pr_metadata,
    is_base_repo_branch,
    is_pr_eligible_for_automerge,
    main,
    parse_args,
    process_auto_merge_queue,
)

VERIFIED_COMMIT = {"sha": "c1", "commit": {"verification": {"verified": True}}}


def _stub_pr_contents(client, filenames, labels=None):
    """Make the API-sourced re-classification see the given files and commits."""
    client.list_pull_request_files.return_value = [
        {"filename": name, "additions": 1, "deletions": 0} for name in filenames
    ]
    client.list_pull_request_commits.return_value = [VERIFIED_COMMIT]
    client.get_collaborator_permission.return_value = "write"


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
        "head": {
            "sha": "sha12345",
            "ref": "docs/fix-typo",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "base": {
            "ref": "main",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
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
    assert client.dispatch_workflow.call_args_list == [
        call("ci.yml", "main"),
        call("docs.yml", "main"),
    ]


def test_process_queue_updates_behind_branch():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 201, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    behind_pr = {
        "number": 201,
        "title": "fix: minor bug",
        "draft": False,
        "state": "open",
        "head": {
            "sha": "sha67890",
            "ref": "fix/minor-bug",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "base": {
            "ref": "main",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "mergeable": True,
        "mergeable_state": "behind",
        "user": {"login": "fderuiter"},
    }
    updated_pr = {
        **behind_pr,
        "head": {
            "sha": "sha_updated_201",
            "ref": "fix/minor-bug",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "mergeable_state": "blocked",
    }
    client.get_pull_request.side_effect = [behind_pr, updated_pr]
    client.get_commit_verification.return_value = {
        "verified": True,
        "reason": "valid",
    }
    _stub_pr_contents(client, ["docs/guide.md"])
    client.update_branch.return_value = True

    res = process_auto_merge_queue(client, sleep_fn=lambda _: None)
    assert res.action == AutoMergeAction.UPDATED
    assert res.pr_id == 201
    assert res.pr_number == 201
    assert res.pull_request_id == 201
    client.update_branch.assert_called_once_with(201, "sha67890")
    client.dispatch_workflow.assert_called_once_with("ci.yml", "fix/minor-bug")
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
        "head": {
            "sha": "sha99999",
            "ref": "docs/blocked-state",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "base": {
            "ref": "main",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "mergeable": True,
        "mergeable_state": "blocked",  # blocked due to requirements, not base staleness
        "user": {"login": "fderuiter"},
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
        "head": {
            "sha": "sha11111",
            "ref": "fix/conflict-branch",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "base": {
            "ref": "main",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "mergeable": True,
        "mergeable_state": "behind",
        "user": {"login": "fderuiter"},
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
    client.dispatch_workflow.assert_not_called()
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
    pr = _eligible_pr(301)
    pr["user"] = {"login": "fderuiter"}
    pr["head"] = {
        "sha": "sha_head",
        "ref": "docs/tweak",
        "repo": {"id": 1, "full_name": "fderuiter/sortify"},
    }
    pr["base"] = {
        "ref": "main",
        "repo": {"id": 1, "full_name": "fderuiter/sortify"},
    }
    client.get_pull_request.return_value = pr
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


def test_main_cli_help(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "--repo" in captured.out
    assert "--token" in captured.out
    assert "Serialized Auto-Merge Controller for Sortify." in captured.out


def test_main_cli_help_short(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["-h"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "--repo" in captured.out
    assert "--token" in captured.out


def test_main_cli_explicit_flags(monkeypatch, caplog):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    created_clients = []

    def mock_init(self, token, owner, repo):
        created_clients.append((token, owner, repo))

    monkeypatch.setattr(GitHubClient, "__init__", mock_init)
    monkeypatch.setattr(
        "scripts.auto_merge_controller.process_auto_merge_queue",
        lambda client: AutoMergeResult(action=AutoMergeAction.NONE, pr_id=None),
    )

    main(["--token", "cli_token_val", "--repo", "custom_owner/custom_repo"])
    assert len(created_clients) == 1
    assert created_clients[0] == ("cli_token_val", "custom_owner", "custom_repo")


def test_parse_args_explicit_flags():
    args = parse_args(["--token", "test_tok", "--repo", "my_org/my_repo"])
    assert args.token == "test_tok"
    assert args.repo == "my_org/my_repo"


def test_parse_args_env_fallbacks(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    args = parse_args([])
    assert args.token is None
    assert args.repo == "fderuiter/sortify"

    monkeypatch.setenv("GITHUB_TOKEN", "env_tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "env_org/env_repo")
    args = parse_args([])
    assert args.token == "env_tok"
    assert args.repo == "env_org/env_repo"

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "gh_tok")
    args = parse_args([])
    assert args.token == "gh_tok"


def test_parse_args_cli_token_overrides_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env_token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "env_owner/env_repo")

    args = parse_args(["--token", "cli_token", "--repo", "cli_owner/cli_repo"])
    assert args.token == "cli_token"
    assert args.repo == "cli_owner/cli_repo"


def test_main_missing_token_error(monkeypatch, caplog):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 1
    assert "GITHUB_TOKEN or GH_TOKEN is required." in caplog.text


def test_main_invalid_repo_format_error(monkeypatch, caplog):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        main(["--token", "valid_tok", "--repo", "invalid_repo_format"])
    assert exc_info.value.code == 1
    assert "Invalid GITHUB_REPOSITORY format: invalid_repo_format" in caplog.text


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
    client.list_issue_events.return_value = [_label_event("fderuiter")]
    client.get_collaborator_permission.return_value = "admin"
    client.get_required_ci_conclusion.return_value = "success"
    client.merge_pull_request.return_value = True

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.MERGED
    assert res.pr_id == 500


def test_process_queue_blocks_automated_security_patch_labelled_by_bot():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {
            "number": 501,
            "labels": [
                {"name": "automerge:eligible"},
                {"name": "security"},
                {"name": "dependencies"},
            ],
            "draft": False,
        }
    ]
    client.get_pull_request.return_value = {
        "number": 501,
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
    client.list_issue_events.return_value = [
        _label_event("google-labs-stitch[bot]", actor_type="Bot")
    ]
    client.get_required_ci_conclusion.return_value = "success"

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.NONE
    client.merge_pull_request.assert_not_called()


def test_process_queue_blocks_security_patch_without_automerge():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {
            "number": 502,
            "labels": [
                {"name": "automerge:eligible"},
                {"name": "security"},
                {"name": "dependencies"},
            ],
            "draft": False,
        }
    ]
    client.get_pull_request.return_value = {
        "number": 502,
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
            {"name": "automerge:eligible"},
        ],
    }
    client.get_commit_verification.return_value = {
        "verified": True,
        "reason": "valid",
    }
    _stub_pr_contents(client, ["uv.lock"])
    client.get_required_ci_conclusion.return_value = "success"

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.NONE
    client.merge_pull_request.assert_not_called()


def test_dispatch_workflow_posts_to_github_actions_endpoint():
    client = GitHubClient("fake_token", "owner", "repo")
    client._post = MagicMock(return_value={})

    client.dispatch_workflow("ci.yml", "main")
    client._post.assert_called_once_with(
        "actions/workflows/ci.yml/dispatches",
        {"ref": "main"},
    )

    client._post.reset_mock()
    client.dispatch_workflow("ci.yml", "feat/branch", inputs={"dry_run": "true"})
    client._post.assert_called_once_with(
        "actions/workflows/ci.yml/dispatches",
        {"ref": "feat/branch", "inputs": {"dry_run": "true"}},
    )


def test_is_base_repo_branch_checks_repository_identity():
    same_repo_pr = {
        "head": {
            "ref": "docs/typo",
            "repo": {"id": 10, "full_name": "fderuiter/sortify"},
        },
        "base": {
            "ref": "main",
            "repo": {"id": 10, "full_name": "fderuiter/sortify"},
        },
    }
    fork_pr = {
        "head": {
            "ref": "docs/typo",
            "repo": {"id": 99, "full_name": "contributor/sortify", "fork": True},
        },
        "base": {
            "ref": "main",
            "repo": {"id": 10, "full_name": "fderuiter/sortify"},
        },
    }
    deleted_fork_pr = {
        "head": {"ref": "docs/typo", "repo": None},
        "base": {
            "ref": "main",
            "repo": {"id": 10, "full_name": "fderuiter/sortify"},
        },
    }
    assert is_base_repo_branch(same_repo_pr) is True
    assert is_base_repo_branch(fork_pr) is False
    assert is_base_repo_branch(deleted_fork_pr) is False
    assert is_base_repo_branch({"head": {"sha": "abc"}}) is False


def test_process_queue_skips_behind_fork_branch_and_merges_next_eligible_pr():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 600, "labels": [{"name": "automerge:eligible"}], "draft": False},
        {"number": 601, "labels": [{"name": "automerge:eligible"}], "draft": False},
    ]
    fork_behind_pr = {
        "number": 600,
        "title": "docs: fork typo fix",
        "draft": False,
        "state": "open",
        "head": {
            "sha": "sha_fork_600",
            "ref": "patch-1",
            "repo": {"id": 99, "full_name": "contributor/sortify", "fork": True},
        },
        "base": {
            "ref": "main",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "mergeable": True,
        "mergeable_state": "behind",
        "user": {"login": "contributor"},
        "labels": [{"name": "automerge:eligible"}, {"name": "automerge"}],
    }
    in_repo_clean_pr = {
        "number": 601,
        "title": "docs: in-repo update",
        "draft": False,
        "state": "open",
        "head": {
            "sha": "sha_clean_601",
            "ref": "docs/in-repo",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "base": {
            "ref": "main",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "fderuiter"},
    }
    client.get_pull_request.side_effect = [fork_behind_pr, in_repo_clean_pr]
    client.get_commit_verification.return_value = {
        "verified": True,
        "reason": "valid",
    }
    _stub_pr_contents(client, ["docs/guide.md"])
    client.list_issue_events.return_value = [_label_event("fderuiter")]
    client.get_required_ci_conclusion.return_value = "success"
    client.merge_pull_request.return_value = True

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.MERGED
    assert res.pr_id == 601
    client.update_branch.assert_not_called()
    client.add_labels.assert_not_called()
    assert client.dispatch_workflow.call_args_list == [
        call("ci.yml", "main"),
        call("docs.yml", "main"),
    ]


def test_process_queue_polls_until_new_head_sha_before_dispatching_ci():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 602, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    behind_pr = {
        "number": 602,
        "title": "docs: stale in-repo branch",
        "draft": False,
        "state": "open",
        "head": {
            "sha": "sha_old_602",
            "ref": "docs/stale-branch",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "base": {
            "ref": "main",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "mergeable": True,
        "mergeable_state": "behind",
        "user": {"login": "fderuiter"},
    }
    updated_pr = {
        **behind_pr,
        "head": {
            "sha": "sha_new_602",
            "ref": "docs/stale-branch",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "mergeable_state": "blocked",
    }
    # Initial fetch, transient poll error, old SHA, then updated SHA
    client.get_pull_request.side_effect = [
        behind_pr,
        requests.ConnectionError("transient"),
        behind_pr,
        updated_pr,
    ]
    client.get_commit_verification.return_value = {"verified": True}
    _stub_pr_contents(client, ["docs/guide.md"])
    client.update_branch.return_value = True

    sleeps: list[float] = []
    res = process_auto_merge_queue(client, sleep_fn=sleeps.append)
    assert res.action == AutoMergeAction.UPDATED
    assert res.pr_id == 602
    assert len(sleeps) == 2
    client.dispatch_workflow.assert_called_once_with("ci.yml", "docs/stale-branch")


def test_process_queue_skips_ci_dispatch_when_head_sha_does_not_advance(caplog):
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 603, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    behind_pr = {
        "number": 603,
        "title": "docs: stalled branch update",
        "draft": False,
        "state": "open",
        "head": {
            "sha": "sha_stalled_603",
            "ref": "docs/stalled",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "base": {
            "ref": "main",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "mergeable": True,
        "mergeable_state": "behind",
        "user": {"login": "fderuiter"},
    }
    client.get_pull_request.return_value = behind_pr
    client.get_commit_verification.return_value = {"verified": True}
    _stub_pr_contents(client, ["docs/guide.md"])
    client.update_branch.return_value = True

    res = process_auto_merge_queue(client, sleep_fn=lambda _: None)
    assert res.action == AutoMergeAction.UPDATED
    assert res.pr_id == 603
    client.dispatch_workflow.assert_not_called()
    assert "did not advance" in caplog.text


def test_process_queue_logs_transient_post_merge_dispatch_error_without_failing(caplog):
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 604, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    client.get_pull_request.return_value = {
        "number": 604,
        "title": "docs: merge despite transient dispatch error",
        "draft": False,
        "state": "open",
        "head": {
            "sha": "sha_merge_604",
            "ref": "docs/merge-604",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "base": {
            "ref": "main",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "fderuiter"},
    }
    client.get_commit_verification.return_value = {"verified": True}
    _stub_pr_contents(client, ["docs/guide.md"])
    client.get_required_ci_conclusion.return_value = "success"
    client.merge_pull_request.return_value = True
    client.dispatch_workflow.side_effect = requests.HTTPError("503 Service Unavailable")

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.MERGED
    assert res.pr_id == 604
    assert client.dispatch_workflow.call_args_list == [
        call("ci.yml", "main"),
        call("docs.yml", "main"),
    ]
    assert "failed to dispatch 'ci.yml' on 'main'" in caplog.text
    assert "failed to dispatch 'docs.yml' on 'main'" in caplog.text


def test_process_queue_dispatches_docs_workflow_even_when_ci_dispatch_fails(caplog):
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 605, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    client.get_pull_request.return_value = {
        "number": 605,
        "title": "docs: publish pages when ci dispatch errors",
        "draft": False,
        "state": "open",
        "head": {
            "sha": "sha_merge_605",
            "ref": "docs/merge-605",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "base": {
            "ref": "main",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "fderuiter"},
    }
    client.get_commit_verification.return_value = {"verified": True}
    _stub_pr_contents(client, ["docs/guide.md"])
    client.get_required_ci_conclusion.return_value = "success"
    client.merge_pull_request.return_value = True
    client.dispatch_workflow.side_effect = [
        requests.HTTPError("502 Bad Gateway"),
        None,
    ]

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.MERGED
    assert res.pr_id == 605
    assert client.dispatch_workflow.call_args_list == [
        call("ci.yml", "main"),
        call("docs.yml", "main"),
    ]
    assert "failed to dispatch 'ci.yml' on 'main'" in caplog.text
    assert "failed to dispatch 'docs.yml' on 'main'" not in caplog.text


def test_auto_merge_controller_workflow_uses_job_level_concurrency_and_permissions():
    workflow_path = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "auto-merge-controller.yml"
    )
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    assert "concurrency" not in workflow, (
        "auto-merge-controller.yml must not declare workflow-level concurrency"
    )
    assert workflow.get("permissions") == {"contents": "read"}

    job = workflow["jobs"]["auto-merge"]
    job_keys = list(job.keys())
    assert "if" in job_keys and "concurrency" in job_keys
    assert job_keys.index("concurrency") > job_keys.index("if")
    assert job["concurrency"] == {
        "group": "auto-merge-controller",
        "cancel-in-progress": False,
    }
    assert job["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
        "issues": "write",
        "actions": "write",
        "checks": "read",
        "statuses": "read",
    }


@pytest.mark.parametrize("pending_state", [None, "queued", "in_progress", "waiting"])
def test_process_queue_halts_on_pending_head_required_ci_without_updating_later_prs(
    pending_state,
):
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 701, "labels": [{"name": "automerge:eligible"}], "draft": False},
        {"number": 702, "labels": [{"name": "automerge:eligible"}], "draft": False},
        {"number": 703, "labels": [{"name": "automerge:eligible"}], "draft": False},
    ]
    head_pr = {
        "number": 701,
        "title": "docs: head pr running ci",
        "draft": False,
        "state": "open",
        "head": {
            "sha": "sha_701",
            "ref": "docs/pr-701",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "base": {"ref": "main", "repo": {"id": 1, "full_name": "fderuiter/sortify"}},
        "mergeable": True,
        "mergeable_state": "blocked",
        "user": {"login": "fderuiter"},
        "labels": [{"name": "automerge:eligible"}],
    }
    client.get_pull_request.return_value = head_pr
    client.get_commit_verification.return_value = {"verified": True}
    _stub_pr_contents(client, ["docs/guide.md"])
    client.get_required_ci_conclusion.return_value = pending_state

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.NONE
    assert res.pr_id is None
    client.get_pull_request.assert_called_once_with(701)
    client.update_branch.assert_not_called()
    client.merge_pull_request.assert_not_called()


@pytest.mark.parametrize("failed_state", ["failure", "cancelled", "timed_out"])
def test_process_queue_advances_past_failed_head_required_ci_to_next_candidate(
    failed_state,
):
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 704, "labels": [{"name": "automerge:eligible"}], "draft": False},
        {"number": 705, "labels": [{"name": "automerge:eligible"}], "draft": False},
    ]
    pr_704 = {
        "number": 704,
        "title": "docs: broken links",
        "draft": False,
        "state": "open",
        "head": {
            "sha": "sha_704",
            "ref": "docs/pr-704",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "base": {"ref": "main", "repo": {"id": 1, "full_name": "fderuiter/sortify"}},
        "mergeable": True,
        "mergeable_state": "blocked",
        "user": {"login": "fderuiter"},
        "labels": [{"name": "automerge:eligible"}],
    }
    pr_705 = {
        "number": 705,
        "title": "docs: green fix",
        "draft": False,
        "state": "open",
        "head": {
            "sha": "sha_705",
            "ref": "docs/pr-705",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "base": {"ref": "main", "repo": {"id": 1, "full_name": "fderuiter/sortify"}},
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "fderuiter"},
        "labels": [{"name": "automerge:eligible"}],
    }
    client.get_pull_request.side_effect = [pr_704, pr_705]
    client.get_commit_verification.return_value = {"verified": True}
    _stub_pr_contents(client, ["docs/guide.md"])
    client.get_required_ci_conclusion.side_effect = [failed_state, "success"]
    client.merge_pull_request.return_value = True

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.MERGED
    assert res.pr_id == 705
    client.merge_pull_request.assert_called_once()
    assert client.merge_pull_request.call_args[0][0] == 705


def test_process_queue_halts_when_head_mergeable_is_unknown():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 706, "labels": [{"name": "automerge:eligible"}], "draft": False},
        {"number": 707, "labels": [{"name": "automerge:eligible"}], "draft": False},
    ]
    pr_706 = {
        "number": 706,
        "title": "docs: computing mergeability",
        "draft": False,
        "state": "open",
        "head": {
            "sha": "sha_706",
            "ref": "docs/pr-706",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "base": {"ref": "main", "repo": {"id": 1, "full_name": "fderuiter/sortify"}},
        "mergeable": None,
        "mergeable_state": "unknown",
        "user": {"login": "fderuiter"},
        "labels": [{"name": "automerge:eligible"}],
    }
    client.get_pull_request.return_value = pr_706
    client.get_commit_verification.return_value = {"verified": True}
    _stub_pr_contents(client, ["docs/guide.md"])

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.NONE
    assert res.pr_id is None
    client.get_pull_request.assert_called_once_with(706)
    client.update_branch.assert_not_called()


def test_fifo_four_pr_queue_updates_each_branch_once():
    """Simulate a 4-PR FIFO queue and verify 3 branch updates total (1 per trailing PR)."""
    client = MagicMock(spec=GitHubClient)
    prs_state: dict[int, dict[str, Any]] = {
        num: {
            "number": num,
            "title": f"docs: change {num}",
            "draft": False,
            "state": "open",
            "head": {
                "sha": f"sha_{num}_v1",
                "ref": f"docs/pr-{num}",
                "repo": {"id": 1, "full_name": "fderuiter/sortify"},
            },
            "base": {
                "ref": "main",
                "repo": {"id": 1, "full_name": "fderuiter/sortify"},
            },
            "mergeable": True,
            "mergeable_state": "clean",
            "user": {"login": "fderuiter"},
            "labels": [{"name": "automerge:eligible"}],
        }
        for num in (710, 711, 712, 713)
    }
    ci_state = {
        "sha_710_v1": "success",
        "sha_711_v1": "success",
        "sha_712_v1": "success",
        "sha_713_v1": "success",
    }
    updates_by_pr: list[int] = []
    merges_by_pr: list[int] = []

    def list_open():
        return [
            {
                "number": p["number"],
                "labels": list(p["labels"]),
                "draft": p["draft"],
            }
            for p in prs_state.values()
            if p["state"] == "open"
        ]

    def get_pr(num: int):
        return dict(prs_state[num])

    def update_branch(num: int, expected_sha: str) -> bool:
        assert prs_state[num]["head"]["sha"] == expected_sha
        updates_by_pr.append(num)
        new_sha = f"sha_{num}_updated_{len(updates_by_pr)}"
        prs_state[num]["head"] = {
            **prs_state[num]["head"],
            "sha": new_sha,
        }
        prs_state[num]["mergeable_state"] = "blocked"
        ci_state[new_sha] = "in_progress"
        return True

    def merge_pr(num: int, head_sha: str, title: str, msg: str) -> bool:
        assert prs_state[num]["head"]["sha"] == head_sha
        merges_by_pr.append(num)
        prs_state[num]["state"] = "closed"
        for other in prs_state.values():
            if other["state"] == "open":
                other["mergeable_state"] = "behind"
        return True

    client.list_open_pull_requests.side_effect = list_open
    client.get_pull_request.side_effect = get_pr
    client.get_commit_verification.return_value = {"verified": True}
    _stub_pr_contents(client, ["docs/guide.md"])
    client.get_required_ci_conclusion.side_effect = lambda sha: ci_state.get(sha)
    client.update_branch.side_effect = update_branch
    client.merge_pull_request.side_effect = merge_pr

    # Cycle 1: PR #710 merges directly; #711-#713 become behind main.
    res = process_auto_merge_queue(client, sleep_fn=lambda _: None)
    assert (res.action, res.pr_id) == (AutoMergeAction.MERGED, 710)

    for expected_pr in (711, 712, 713):
        # Next poll updates only the leading behind PR.
        res_upd = process_auto_merge_queue(client, sleep_fn=lambda _: None)
        assert (res_upd.action, res_upd.pr_id) == (
            AutoMergeAction.UPDATED,
            expected_pr,
        )

        # While the updated head PR's CI is in_progress, subsequent polls halt and do NOT update trailing PRs!
        res_wait = process_auto_merge_queue(client, sleep_fn=lambda _: None)
        assert (res_wait.action, res_wait.pr_id) == (AutoMergeAction.NONE, None)

        # Once CI finishes green, the head PR merges cleanly.
        current_sha = prs_state[expected_pr]["head"]["sha"]
        ci_state[current_sha] = "success"
        prs_state[expected_pr]["mergeable_state"] = "clean"
        res_merge = process_auto_merge_queue(client, sleep_fn=lambda _: None)
        assert (res_merge.action, res_merge.pr_id) == (
            AutoMergeAction.MERGED,
            expected_pr,
        )

    assert merges_by_pr == [710, 711, 712, 713]
    assert updates_by_pr == [711, 712, 713]


def test_reclassify_pr_rejection_blocks_pr_and_skips_subsequent_polls():
    client = MagicMock(spec=GitHubClient)
    labels_on_pr = ["automerge:eligible"]

    def list_open():
        return [
            {
                "number": 720,
                "labels": [{"name": name} for name in labels_on_pr],
                "draft": False,
            }
        ]

    def remove_lbl(pr_num: int, name: str) -> None:
        assert pr_num == 720
        if name in labels_on_pr:
            labels_on_pr.remove(name)

    def add_lbls(pr_num: int, names: list[str]) -> None:
        assert pr_num == 720
        for name in names:
            if name not in labels_on_pr:
                labels_on_pr.append(name)

    client.list_open_pull_requests.side_effect = list_open
    client.remove_label.side_effect = remove_lbl
    client.add_labels.side_effect = add_lbls
    client.get_pull_request.return_value = _eligible_pr(720)
    client.get_commit_verification.return_value = {"verified": True}
    _stub_pr_contents(client, ["scripts/classify_pr.py"])

    # Poll 1: reclassify_pr rejects PR #720, removes automerge:eligible, and adds automerge:blocked.
    res1 = process_auto_merge_queue(client)
    assert res1.action == AutoMergeAction.NONE
    assert "automerge:eligible" not in labels_on_pr
    assert "automerge:blocked" in labels_on_pr
    assert "risk:sensitive" in labels_on_pr
    client.remove_label.assert_called_once_with(720, "automerge:eligible")
    assert client.get_pull_request.call_count == 1

    # Poll 2: PR #720 now carries automerge:blocked and lacks automerge:eligible, so it is skipped immediately.
    res2 = process_auto_merge_queue(client)
    assert res2.action == AutoMergeAction.NONE
    assert client.get_pull_request.call_count == 1


def test_github_client_remove_label_and_get_file_content(monkeypatch):
    client = GitHubClient("tok", "fderuiter", "sortify")
    deleted_urls: list[str] = []

    def _make_response(status_code: int = 200, text: str = "") -> requests.Response:
        resp = requests.Response()
        resp.status_code = status_code
        resp._content = text.encode("utf-8")
        resp.encoding = "utf-8"
        return resp

    def fake_delete(url, headers=None, timeout=None):
        deleted_urls.append(url)
        return _make_response(status_code=404)

    monkeypatch.setattr(requests, "delete", fake_delete)
    client.remove_label(42, "automerge:eligible")
    assert deleted_urls == [
        "https://api.github.com/repos/fderuiter/sortify/issues/42/labels/automerge%3Aeligible"
    ]

    captured_url: list[str] = []
    captured_headers: dict[str, str] = {}
    captured_params: dict[str, str] = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured_url.append(url)
        if headers:
            captured_headers.update(headers)
        if params:
            captured_params.update(params)
        if params and params.get("ref") == "missing_sha":
            return _make_response(status_code=404)
        return _make_response(status_code=200, text="version = 1\n")

    monkeypatch.setattr(requests, "get", fake_get)
    content = client.get_file_content("uv.lock", "abc123")
    assert content == "version = 1\n"
    assert captured_url == [
        "https://api.github.com/repos/fderuiter/sortify/contents/uv.lock"
    ]
    assert captured_params == {"ref": "abc123"}
    assert captured_headers["Accept"] == "application/vnd.github.raw+json"
    assert client.get_file_content("uv.lock", "missing_sha") is None


def test_build_pr_metadata_evaluates_grouped_dependabot_lockfile_diffs():
    client = MagicMock(spec=GitHubClient)
    _stub_pr_contents(
        client, ["pyproject.toml", "uv.lock", "offline_bundle/requirements.txt"]
    )
    pr = {
        "number": 730,
        "title": "Bump the pip group across 1 directory with 2 updates",
        "draft": False,
        "state": "open",
        "base": {"sha": "base_sha_730", "ref": "main"},
        "head": {"sha": "head_sha_730", "ref": "dependabot/pip/group"},
        "user": {"login": "dependabot[bot]"},
        "labels": [{"name": "dependencies"}],
    }
    base_lock = (
        "version = 1\n"
        '[[package]]\nname = "requests"\nversion = "2.31.0"\n'
        '[[package]]\nname = "certifi"\nversion = "2026.7.22"\n'
    )
    minor_head_lock = (
        "version = 1\n"
        '[[package]]\nname = "requests"\nversion = "2.32.3"\n'
        '[[package]]\nname = "certifi"\nversion = "2026.10.19"\n'
    )
    major_head_lock = (
        "version = 1\n"
        '[[package]]\nname = "requests"\nversion = "3.0.0"\n'
        '[[package]]\nname = "certifi"\nversion = "2026.10.19"\n'
    )
    base_reqs = (
        "requests==2.31.0 --hash=sha256:aaa\ncertifi==2026.7.22 --hash=sha256:bbb\n"
    )
    minor_head_reqs = (
        "requests==2.32.3 --hash=sha256:ccc\ncertifi==2026.10.19 --hash=sha256:ddd\n"
    )

    def fetch_minor(path: str, ref: str):
        if path == "uv.lock":
            return base_lock if ref == "base_sha_730" else minor_head_lock
        return base_reqs if ref == "base_sha_730" else minor_head_reqs

    client.get_file_content.side_effect = fetch_minor
    meta_minor = build_pr_metadata(client, pr)
    assert meta_minor.dependency_update_type == "minor"
    assert classify_pr_from_controller(meta_minor).eligible is True

    def fetch_major(path: str, ref: str):
        if path == "uv.lock":
            return base_lock if ref == "base_sha_730" else major_head_lock
        return base_reqs if ref == "base_sha_730" else minor_head_reqs

    client.get_file_content.side_effect = fetch_major
    meta_major = build_pr_metadata(client, pr)
    assert meta_major.dependency_update_type == "major"
    assert classify_pr_from_controller(meta_major).eligible is False

    # Network/API error fetching manifest fails closed to "major"
    client.get_file_content.side_effect = RuntimeError("API down")
    meta_err = build_pr_metadata(client, pr)
    assert meta_err.dependency_update_type == "major"
    assert classify_pr_from_controller(meta_err).eligible is False


def classify_pr_from_controller(meta):
    from scripts.classify_pr import classify_pr

    return classify_pr(meta)


@pytest.mark.parametrize(
    ("user", "head_repo", "permission", "additions", "deletions"),
    [
        (
            {"login": "google-labs-stitch[bot]", "type": "Bot"},
            {"id": 1, "full_name": "fderuiter/sortify"},
            "write",
            5,
            2,
        ),
        (
            {"login": "stitch", "type": "User"},
            {"id": 1, "full_name": "fderuiter/sortify"},
            "write",
            5,
            2,
        ),
        (
            {"login": "contributor", "type": "User"},
            {"id": 99, "full_name": "contributor/sortify", "fork": True},
            "write",
            5,
            2,
        ),
        (
            {"login": "reader", "type": "User"},
            {"id": 1, "full_name": "fderuiter/sortify"},
            "read",
            5,
            2,
        ),
        (
            {"login": "fderuiter", "type": "User"},
            {"id": 1, "full_name": "fderuiter/sortify"},
            "admin",
            350,
            200,
        ),
    ],
)
def test_process_queue_blocks_unlabelled_bot_fork_non_collaborator_and_oversized_docs_prs(
    user, head_repo, permission, additions, deletions
):
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 810, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    client.get_pull_request.return_value = {
        "number": 810,
        "title": "docs: update guide",
        "draft": False,
        "state": "open",
        "head": {
            "sha": "sha_810",
            "ref": "docs/update",
            "repo": head_repo,
        },
        "base": {
            "ref": "main",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "mergeable": True,
        "mergeable_state": "clean",
        "user": user,
        "labels": [{"name": "automerge:eligible"}],
    }
    client.get_commit_verification.return_value = {"verified": True}
    client.list_pull_request_files.return_value = [
        {
            "filename": "docs/user_guide.md",
            "additions": additions,
            "deletions": deletions,
        },
        {"filename": "README.md", "additions": 1, "deletions": 0},
    ]
    client.list_pull_request_commits.return_value = [VERIFIED_COMMIT]
    client.get_collaborator_permission.return_value = permission
    client.get_required_ci_conclusion.return_value = "success"

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.NONE
    assert res.pr_id is None
    client.remove_label.assert_called_once_with(810, "automerge:eligible")
    added = client.add_labels.call_args.args[1]
    assert "automerge:blocked" in added
    client.merge_pull_request.assert_not_called()


def test_process_queue_fails_closed_when_collaborator_permission_lookup_errors():
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 811, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    client.get_pull_request.return_value = {
        "number": 811,
        "title": "docs: fix readme typo",
        "draft": False,
        "state": "open",
        "head": {
            "sha": "sha_811",
            "ref": "docs/readme-typo",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "base": {
            "ref": "main",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "fderuiter", "type": "User"},
        "labels": [{"name": "automerge:eligible"}],
    }
    client.get_commit_verification.return_value = {"verified": True}
    _stub_pr_contents(client, ["README.md"])
    client.get_collaborator_permission.side_effect = RuntimeError("API 500")
    client.get_required_ci_conclusion.return_value = "success"

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.NONE
    client.remove_label.assert_called_once_with(811, "automerge:eligible")
    client.merge_pull_request.assert_not_called()


def test_process_queue_merges_bot_and_fork_docs_pr_when_maintainer_applies_automerge():
    for pr_num, user, head_repo in (
        (
            812,
            {"login": "google-labs-stitch[bot]", "type": "Bot"},
            {"id": 1, "full_name": "fderuiter/sortify"},
        ),
        (
            813,
            {"login": "contributor", "type": "User"},
            {"id": 99, "full_name": "contributor/sortify", "fork": True},
        ),
    ):
        client = MagicMock(spec=GitHubClient)
        client.list_open_pull_requests.return_value = [
            {
                "number": pr_num,
                "labels": [{"name": "automerge:eligible"}, {"name": "automerge"}],
                "draft": False,
            }
        ]
        client.get_pull_request.return_value = {
            "number": pr_num,
            "title": "docs: reviewed contribution",
            "draft": False,
            "state": "open",
            "head": {
                "sha": f"sha_{pr_num}",
                "ref": "docs/reviewed",
                "repo": head_repo,
            },
            "base": {
                "ref": "main",
                "repo": {"id": 1, "full_name": "fderuiter/sortify"},
            },
            "mergeable": True,
            "mergeable_state": "clean",
            "user": user,
            "labels": [{"name": "automerge:eligible"}, {"name": "automerge"}],
        }
        client.get_commit_verification.return_value = {"verified": True}
        _stub_pr_contents(client, ["docs/user_guide.md"])
        client.list_issue_events.return_value = [_label_event("fderuiter")]
        client.get_collaborator_permission.return_value = "admin"
        client.get_required_ci_conclusion.return_value = "success"
        client.merge_pull_request.return_value = True

        res = process_auto_merge_queue(client)
        assert res.action == AutoMergeAction.MERGED
        assert res.pr_id == pr_num


def test_automerge_label_from_bot_authors_without_bot_suffix_is_rejected_even_on_dependabot():
    client = MagicMock(spec=GitHubClient)
    client.get_collaborator_permission.return_value = "admin"
    for bot_login in ("stitch", "jules", "dependabot", "dependabot[bot]"):
        client.list_issue_events.return_value = [
            _label_event(bot_login, actor_type="User")
        ]
        assert automerge_label_applied_by_maintainer(client, 1180) is False

    # Even on a dependabot[bot] PR, a bot-applied 'automerge' label is stripped
    _stub_pr_contents(client, ["pyproject.toml"])
    pr = {
        "number": 1181,
        "title": "Bump the python-major group with 1 update",
        "draft": False,
        "state": "open",
        "base": {
            "sha": "base_1181",
            "ref": "main",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "head": {
            "sha": "head_1181",
            "ref": "dependabot/pip/major",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "user": {"login": "dependabot[bot]", "type": "Bot"},
        "labels": [{"name": "dependencies"}, {"name": "automerge"}],
    }
    client.list_issue_events.return_value = [_label_event("stitch", actor_type="User")]
    meta = build_pr_metadata(client, pr)
    assert "automerge" not in meta.labels


def test_process_queue_merges_grouped_minor_patch_dependabot_and_blocks_breaking_grouped():
    grouped_minor_commit = """build(deps): bump the python-minor-patch group with 3 updates

Updates `ty` from 0.0.104 to 0.0.105
Updates `mpmath` from 1.3.0 to 1.4.1
Updates `narwhals` from 2.28.0 to 2.29.0

---
updated-dependencies:
- dependency-name: ty
  dependency-version: 0.0.105
  dependency-type: direct:development
  update-type: version-update:semver-patch
  dependency-group: python-minor-patch
- dependency-name: mpmath
  dependency-version: 1.4.1
  dependency-type: indirect
  update-type: version-update:semver-minor
  dependency-group: python-minor-patch
- dependency-name: narwhals
  dependency-version: 2.29.0
  dependency-type: indirect
  update-type: version-update:semver-minor
  dependency-group: python-minor-patch
...
"""
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {"number": 1152, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    client.get_pull_request.return_value = {
        "number": 1152,
        "title": "build(deps): bump the python-minor-patch group with 3 updates",
        "body": "Updates `ty` from 0.0.104 to 0.0.105\nUpdates `mpmath` from 1.3.0 to 1.4.1\nUpdates `narwhals` from 2.28.0 to 2.29.0\n",
        "draft": False,
        "state": "open",
        "base": {
            "sha": "base_1152",
            "ref": "main",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "head": {
            "sha": "head_1152",
            "ref": "dependabot/pip/python-minor-patch",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "dependabot[bot]", "type": "Bot"},
        "labels": [{"name": "dependencies"}, {"name": "automerge:eligible"}],
    }
    client.get_commit_verification.return_value = {"verified": True}
    client.list_pull_request_files.return_value = [
        {"filename": "pyproject.toml", "additions": 3, "deletions": 3}
    ]
    client.list_pull_request_commits.return_value = [
        {
            "sha": "dep_commit_1",
            "parents": [{"sha": "p0"}],
            "commit": {
                "message": grouped_minor_commit,
                "verification": {"verified": True},
            },
        },
        {
            "sha": "merge_main_commit",
            "parents": [{"sha": "dep_commit_1"}, {"sha": "main_sha"}],
            "commit": {
                "message": "Merge branch 'main' into dependabot/pip/python-minor-patch",
                "verification": {"verified": True},
            },
        },
    ]
    client.get_required_ci_conclusion.return_value = "success"
    client.merge_pull_request.return_value = True

    res = process_auto_merge_queue(client)
    assert res.action == AutoMergeAction.MERGED
    assert res.pr_id == 1152

    # Now test a grouped Dependabot PR with a 0.x minor bump that carries a stale automerge:eligible label:
    # controller must remove stale automerge:eligible and apply automerge:blocked + risk:high
    zero_x_breaking_commit = grouped_minor_commit.replace(
        "Updates `ty` from 0.0.104 to 0.0.105",
        "Updates `ty` from 0.0.104 to 0.1.0",
    ).replace(
        "dependency-version: 0.0.105\n  dependency-type: direct:development\n  update-type: version-update:semver-patch",
        "dependency-version: 0.1.0\n  dependency-type: direct:development\n  update-type: version-update:semver-minor",
    )
    client_blocked = MagicMock(spec=GitHubClient)
    client_blocked.list_open_pull_requests.return_value = [
        {"number": 1153, "labels": [{"name": "automerge:eligible"}], "draft": False}
    ]
    client_blocked.get_pull_request.return_value = {
        **client.get_pull_request.return_value,
        "number": 1153,
        "body": "Updates `ty` from 0.0.104 to 0.1.0\n",
    }
    client_blocked.get_commit_verification.return_value = {"verified": True}
    client_blocked.list_pull_request_files.return_value = [
        {"filename": "pyproject.toml", "additions": 3, "deletions": 3}
    ]
    client_blocked.list_pull_request_commits.return_value = [
        {
            "sha": "dep_commit_breaking",
            "parents": [{"sha": "p0"}],
            "commit": {
                "message": zero_x_breaking_commit,
                "verification": {"verified": True},
            },
        }
    ]

    res_blocked = process_auto_merge_queue(client_blocked)
    assert res_blocked.action == AutoMergeAction.NONE
    client_blocked.remove_label.assert_called_once_with(1153, "automerge:eligible")
    client_blocked.add_labels.assert_called_once_with(
        1153, ["risk:high", "automerge:blocked"]
    )
    client_blocked.merge_pull_request.assert_not_called()


def test_auto_merge_controller_workflow_gates_before_checkout_and_skips_dev_sync():
    """Verify auto-merge-controller.yml checks open PRs before checkout and runs with --no-project --with requests."""
    workflow_path = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "auto-merge-controller.yml"
    )
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["auto-merge"]["steps"]

    check_step = steps[0]
    assert check_step.get("id") == "check-queue"
    assert "actions/github-script@" in check_step.get("uses", "")
    script = (check_step.get("with") or {}).get("script", "")
    assert "pulls.list" in script
    assert "automerge:eligible" in script
    assert "has_eligible" in script

    checkout_step = next(s for s in steps if "actions/checkout@" in s.get("uses", ""))
    assert steps.index(check_step) < steps.index(checkout_step)

    for step in steps[1:]:
        assert step.get("if") == "steps.check-queue.outputs.has_eligible == 'true'"
        assert "uv sync" not in step.get("run", "")

    run_step = next(s for s in steps if s.get("name") == "Run Auto-Merge Controller")
    assert (
        run_step.get("run", "").strip()
        == "uv run --no-project --with requests python -m scripts.auto_merge_controller"
    )
