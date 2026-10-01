"""Serialized Auto-Merge Controller for Sortify.

Evaluates eligible PRs sequentially, updates branches behind main, verifies Required CI,
and executes squash merges without resolving conflicts automatically.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


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

    def get_commit_status_checks(self, sha: str) -> Dict[str, str]:
        """Fetch status checks for a given commit SHA."""
        data = self._get(f"commits/{sha}/status")
        statuses = {}
        for item in data.get("statuses", []):
            statuses[item["context"]] = item["state"]
        return statuses

    def get_check_runs(self, sha: str) -> Dict[str, str]:
        """Fetch check runs for a given commit SHA."""
        data = self._get(f"commits/{sha}/check-runs")
        check_runs = {}
        for run in data.get("check_runs", []):
            check_runs[run["name"]] = run["conclusion"] or run["status"]
        return check_runs

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


def process_auto_merge_queue(client: GitHubClient) -> Optional[int]:
    """Process open PRs and attempt to auto-merge the first eligible PR.

    Returns merged PR number or None.
    """
    prs = client.list_open_pull_requests()
    eligible_candidates = [
        pr for pr in prs if is_pr_eligible_for_automerge(pr) and not pr.get("draft")
    ]

    if not eligible_candidates:
        logger.info("No eligible auto-merge PR candidates found.")
        return None

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
            return pr_number  # Let CI trigger and process after re-validation

        # Verify status checks on head SHA
        check_runs = client.get_check_runs(head_sha)
        statuses = client.get_commit_status_checks(head_sha)

        required_ci_passed = (
            check_runs.get("Required CI") == "success"
            or statuses.get("Required CI") == "success"
        )
        classification_passed = (
            check_runs.get("automerge/classification") == "success"
            or statuses.get("automerge/classification") == "success"
        )

        if not (required_ci_passed and classification_passed):
            logger.info(
                f"PR #{pr_number} checks not green on head SHA (Required CI: {required_ci_passed}, automerge/classification: {classification_passed})."
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
            return pr_number
        else:
            logger.error(f"Failed to execute squash merge for PR #{pr_number}.")

    return None


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

    merged_pr = process_auto_merge_queue(client)
    if merged_pr:
        print(f"Auto-merged PR #{merged_pr}")
    else:
        print("No PR was auto-merged in this cycle.")


if __name__ == "__main__":
    main()
