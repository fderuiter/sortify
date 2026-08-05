"""Script to post a failure log as a comment on pull request #236."""

import json
import os
import sys
import urllib.request


def main():
    if len(sys.argv) < 3:
        print("Usage: python post_comment.py <log_file> <title>")
        sys.exit(1)

    log_file = sys.argv[1]
    title = sys.argv[2]

    if not os.path.exists(log_file):
        print(f"Log file '{log_file}' not found. Skipping comment.")
        return

    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-150:]
        log_content = "".join(lines)
    except Exception as e:
        print(f"Error reading log file: {e}")
        sys.exit(1)

    body = f"### {title}\n```\n{log_content}\n```"
    token = os.environ.get("GITHUB_TOKEN")

    if not token:
        print("GITHUB_TOKEN environment variable not set. Skipping comment.")
        return

    req = urllib.request.Request(
        "https://api.github.com/repos/fderuiter/sortify/issues/236/comments",
        data=json.dumps({"body": body}).encode("utf-8"),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req):
            print("Comment posted successfully!")
    except Exception as e:
        print(f"Error posting comment: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
