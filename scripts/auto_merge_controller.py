"""Serialized Auto-Merge Controller for Sortify.

Evaluates eligible PRs sequentially, updates branches behind main, verifies Required CI,
and executes squash merges without resolving conflicts automatically.

Trust model: nothing the PR under evaluation can set is trusted on its own. Labels
and the ``automerge/classification`` status only pre-filter candidates; before merging,
the controller re-runs the risk classifier from its own (default-branch) checkout on
metadata fetched from the API, and it accepts ``Required CI`` only as a check run
created by GitHub Actions, never as a plain commit status. Run it as a module
(``python -m scripts.auto_merge_controller``) so the classifier import resolves.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import requests

from scripts.classify_pr import (
    BOT_AUTHORS,
    ClassificationResult,
    PRMetadata,
    classify_pr,
    dependency_update_type_from_title,
)

logger = logging.getLogger(__name__)

REQUIRED_CI_CHECK = "Required CI"
GITHUB_ACTIONS_APP_SLUG = "github-actions"
DEPENDABOT_LOGIN = "dependabot[bot]"
AUTOMERGE_OPT_IN_LABEL = "automerge"
MAINTAINER_PERMISSIONS = {"admin", "maintain", "write"}


class AutoMergeAction(str, Enum):
    """Actions performed by the auto-merge queue processor."""

    MERGED = "MERGED"
    UPDATED = "UPDATED"
    NONE = "NONE"


@dataclass
class AutoMergeResult:
    """Result object returned by process_auto_merge_queue."""

    action: AutoMergeAction
    pr_id: Optional[int] = None

    @property
    def pr_number(self) -> Optional[int]:
        return self.pr_id

    @property
    def pull_request_id(self) -> Optional[int]:
        return self.pr_id


class GitHubClient:
    """Client for GitHub REST API calls with error handling."""

    def __init__(self, token: str, owner: str, repo: str):
        self.token = token
        self.owner = owner
        self.repo = repo
        self.base_url = f"https://api.github.com/repos/{owner}/{repo}"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
        }

    def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        resp = requests.get(url, headers=self.headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, endpoint: str, json_data: Dict[str, Any]) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        resp = requests.post(url, headers=self.headers, json=json_data, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def _put(self, endpoint: str, json_data: Dict[str, Any]) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        resp = requests.put(url, headers=self.headers, json=json_data, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def list_open_pull_requests(self) -> List[Dict[str, Any]]:
        """Fetch list of open pull requests."""
        return self._get("pulls", params={"state": "open", "per_page": 50})

    def get_pull_request(self, pr_number: int) -> Dict[str, Any]:
        """Get detailed information for a specific pull request."""
        return self._get(f"pulls/{pr_number}")

    def update_branch(self, pr_number: int, expected_head_sha: str) -> bool:
        """Update PR branch with latest main via GitHub update-branch API.

        Returns True if successful, False if merge conflict occurs.
        """
        try:
            self._put(
                f"pulls/{pr_number}/update-branch",
                {"expected_head_sha": expected_head_sha},
            )
            return True
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 422:
                logger.warning(
                    f"Merge conflict detected when updating PR #{pr_number}: {e}"
                )
                return False
            raise

    def _get_paginated(
        self, endpoint: str, params: Optional[Dict[str, Any]] = None
    ) -> List[Any]:
        items: List[Any] = []
        page = 1
        while True:
            batch = self._get(
                endpoint, params={**(params or {}), "per_page": 100, "page": page}
            )
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    def list_pull_request_files(self, pr_number: int) -> List[Dict[str, Any]]:
        """List the files changed by a pull request."""
        return self._get_paginated(f"pulls/{pr_number}/files")

    def list_pull_request_commits(self, pr_number: int) -> List[Dict[str, Any]]:
        """List the commits of a pull request."""
        return self._get_paginated(f"pulls/{pr_number}/commits")

    def get_required_ci_conclusion(self, sha: str) -> Optional[str]:
        """Return the conclusion of the latest GitHub Actions ``Required CI`` check run.

        Commit statuses and check runs from other apps are ignored, because anything
        holding a ``statuses: write`` token (including a workflow added by the PR
        itself) can post a status with that name.
        """
        data = self._get(
            f"commits/{sha}/check-runs",
            params={"check_name": REQUIRED_CI_CHECK, "per_page": 100},
        )
        runs = [
            run
            for run in data.get("check_runs", [])
            if run.get("name") == REQUIRED_CI_CHECK
            and (run.get("app") or {}).get("slug") == GITHUB_ACTIONS_APP_SLUG
        ]
        if not runs:
            return None
        latest = max(runs, key=lambda run: run.get("id", 0))
        return latest.get("conclusion") or latest.get("status")

    def list_issue_events(self, pr_number: int) -> List[Dict[str, Any]]:
        """List the issue events (labelled, unlabelled, ...) of a PR."""
        return self._get_paginated(f"issues/{pr_number}/events")

    def get_collaborator_permission(self, login: str) -> str:
        """Return a user's permission on the repository, or "none"."""
        data = self._get(f"collaborators/{login}/permission")
        return str(data.get("permission") or "none")

    def get_commit_verification(self, sha: str) -> Dict[str, Any]:
        """Fetch signature verification metadata for a given commit SHA."""
        data = self._get(f"commits/{sha}")
        if isinstance(data, dict):
            commit_obj = data.get("commit", {})
            if isinstance(commit_obj, dict):
                verification = commit_obj.get("verification", {})
                if isinstance(verification, dict):
                    return verification
        return {}

    def merge_pull_request(
        self, pr_number: int, head_sha: str, commit_title: str, commit_message: str
    ) -> bool:
        """Execute squash merge for a PR."""
        try:
            self._put(
                f"pulls/{pr_number}/merge",
                {
                    "sha": head_sha,
                    "merge_method": "squash",
                    "commit_title": commit_title,
                    "commit_message": commit_message,
                },
            )
            return True
        except requests.HTTPError as e:
            logger.error(f"Failed to merge PR #{pr_number}: {e}")
            return False

    def add_labels(self, pr_number: int, labels: List[str]) -> None:
        """Add labels to a PR."""
        self._post(f"issues/{pr_number}/labels", {"labels": labels})


def is_pr_eligible_for_automerge(pr: Dict[str, Any]) -> bool:
    """Check if PR has automerge eligibility labels."""
    labels = [lbl["name"] for lbl in pr.get("labels", [])]
    if (
        "disable-automerge" in labels
        or "automerge:blocked" in labels
        or "automerge:conflict" in labels
    ):
        return False
    return "automerge:eligible" in labels


def automerge_label_applied_by_maintainer(client: GitHubClient, pr_number: int) -> bool:
    """Whether the latest ``automerge`` label on a PR was added by a maintainer.

    The PR's author, or a bot with label rights, can add the label too, so the
    opt-in only counts when a person with write access or above applied it
    (ADR 0009). Any API failure counts as untrusted.
    """
    try:
        events = client.list_issue_events(pr_number)
        labelled = [
            e
            for e in events
            if e.get("event") == "labeled"
            and (e.get("label") or {}).get("name") == AUTOMERGE_OPT_IN_LABEL
        ]
        if not labelled:
            return False
        actor = labelled[-1].get("actor") or {}
        login = actor.get("login", "")
        if not login or actor.get("type") == "Bot" or login.endswith("[bot]"):
            return False
        return client.get_collaborator_permission(login) in MAINTAINER_PERMISSIONS
    except Exception as exc:
        logger.warning(
            f"Could not verify who labelled PR #{pr_number} '{AUTOMERGE_OPT_IN_LABEL}': {exc}"
        )
        return False


def build_pr_metadata(client: GitHubClient, full_pr: Dict[str, Any]) -> PRMetadata:
    """Collect classifier input for a PR from the GitHub API rather than from labels."""
    pr_number = full_pr["number"]
    files = client.list_pull_request_files(pr_number)
    commits = client.list_pull_request_commits(pr_number)
    unverified = [
        c["sha"]
        for c in commits
        if not ((c.get("commit") or {}).get("verification") or {}).get("verified")
    ]
    labels = [lbl["name"] for lbl in full_pr.get("labels", [])]
    title = full_pr.get("title", "")
    author = (full_pr.get("user") or {}).get("login", "")
    is_dependabot = author == DEPENDABOT_LOGIN
    is_security_patch = (
        ("security" in labels and "dependencies" in labels)
        or full_pr.get("head", {}).get("ref", "").startswith("security/")
    ) and (author in BOT_AUTHORS or author.endswith("[bot]"))
    dependency_update_type = (
        dependency_update_type_from_title(title)
        if (is_dependabot or is_security_patch)
        else "minor"
    )
    if (
        AUTOMERGE_OPT_IN_LABEL in labels
        and not (is_dependabot or is_security_patch)
        and not automerge_label_applied_by_maintainer(client, pr_number)
    ):
        labels = [name for name in labels if name != AUTOMERGE_OPT_IN_LABEL]

    return PRMetadata(
        pr_number=pr_number,
        title=title,
        author=author,
        is_draft=bool(full_pr.get("draft")),
        labels=labels,
        changed_files=[f["filename"] for f in files],
        additions=sum(f.get("additions", 0) for f in files),
        deletions=sum(f.get("deletions", 0) for f in files),
        is_dependabot=is_dependabot,
        is_security_patch=is_security_patch,
        dependency_update_type=dependency_update_type,
        all_commits_verified=bool(commits) and not unverified,
        unverified_commit_shas=unverified,
    )


def reclassify_pr(
    client: GitHubClient, full_pr: Dict[str, Any]
) -> ClassificationResult:
    """Re-run the risk classifier with trusted code on API-sourced metadata."""
    return classify_pr(build_pr_metadata(client, full_pr))


def process_auto_merge_queue(client: GitHubClient) -> AutoMergeResult:
    """Process open PRs and attempt to auto-merge or update the first eligible PR.

    Returns an AutoMergeResult object indicating the action taken and PR ID.
    """
    prs = client.list_open_pull_requests()
    eligible_candidates = [
        pr for pr in prs if is_pr_eligible_for_automerge(pr) and not pr.get("draft")
    ]

    if not eligible_candidates:
        logger.info("No eligible auto-merge PR candidates found.")
        return AutoMergeResult(action=AutoMergeAction.NONE, pr_id=None)

    # Sort candidates by PR number (FIFO)
    eligible_candidates.sort(key=lambda x: x["number"])

    for pr in eligible_candidates:
        pr_number = pr["number"]
        full_pr = client.get_pull_request(pr_number)

        if full_pr.get("draft") or full_pr.get("state") != "open":
            continue

        head_sha = full_pr["head"]["sha"]
        mergeable_state = full_pr.get("mergeable_state")

        logger.info(
            f"Evaluating candidate PR #{pr_number} (head: {head_sha[:8]}, mergeable_state: {mergeable_state})"
        )

        # Handle merge conflicts
        if full_pr.get("mergeable") is False or mergeable_state == "dirty":
            logger.warning(
                f"PR #{pr_number} has merge conflicts. Applying 'automerge:conflict'."
            )
            client.add_labels(pr_number, ["automerge:conflict", "automerge:blocked"])
            continue

        # Verify commit signature on head SHA
        verification = client.get_commit_verification(head_sha)
        if not verification.get("verified", False):
            reason = verification.get("reason", "unverified")
            logger.warning(
                f"Head commit {head_sha[:8]} signature verification failed ({reason}) for PR #{pr_number}. Blocking auto-merge."
            )
            client.add_labels(pr_number, ["automerge:blocked", "risk:high"])
            continue

        # Labels and the classification status can be set by the PR's own
        # workflows, so recompute eligibility from trusted code before acting.
        classification = reclassify_pr(client, full_pr)
        if not classification.eligible:
            logger.warning(
                f"PR #{pr_number} is labelled eligible but trusted re-classification "
                f"blocks it: {'; '.join(classification.blocking_reasons)}"
            )
            continue

        # Check if branch is behind main
        if mergeable_state == "behind":
            logger.info(f"PR #{pr_number} is behind main. Requesting branch update...")
            success = client.update_branch(pr_number, head_sha)
            if not success:
                logger.warning(
                    f"Branch update failed for PR #{pr_number} due to conflict."
                )
                client.add_labels(
                    pr_number, ["automerge:conflict", "automerge:blocked"]
                )
                continue
            logger.info(
                f"Branch updated for PR #{pr_number}. Waiting for required CI on updated SHA..."
            )
            return AutoMergeResult(action=AutoMergeAction.UPDATED, pr_id=pr_number)

        # Verify Required CI on head SHA (GitHub Actions check run only)
        required_ci = client.get_required_ci_conclusion(head_sha)
        if required_ci != "success":
            logger.info(
                f"PR #{pr_number} Required CI is not green on head SHA ({required_ci})."
            )
            continue

        # Execute Squash Merge
        commit_title = f"{full_pr['title']} (#{pr_number})"
        commit_message = f"Auto-merged low-risk PR #{pr_number}\n\nAuthor: {full_pr['user']['login']}"

        merged = client.merge_pull_request(
            pr_number, head_sha, commit_title, commit_message
        )
        if merged:
            logger.info(f"Successfully auto-merged PR #{pr_number}!")
            return AutoMergeResult(action=AutoMergeAction.MERGED, pr_id=pr_number)
        else:
            logger.error(f"Failed to execute squash merge for PR #{pr_number}.")

    return AutoMergeResult(action=AutoMergeAction.NONE, pr_id=None)


def main() -> None:
    """CLI entry point for auto-merge controller."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo_full = os.environ.get("GITHUB_REPOSITORY", "fderuiter/sortify")

    if not token:
        logger.error("GITHUB_TOKEN or GH_TOKEN is required.")
        sys.exit(1)

    parts = repo_full.split("/")
    if len(parts) != 2:
        logger.error(f"Invalid GITHUB_REPOSITORY format: {repo_full}")
        sys.exit(1)

    owner, repo = parts[0], parts[1]
    client = GitHubClient(token, owner, repo)

    result = process_auto_merge_queue(client)
    if result.action == AutoMergeAction.MERGED:
        print(f"Auto-merged PR #{result.pr_id}")
    elif result.action == AutoMergeAction.UPDATED:
        print(f"Updated branch for PR #{result.pr_id}")
    else:
        print("No PR was auto-merged in this cycle.")


if __name__ == "__main__":
    main()
