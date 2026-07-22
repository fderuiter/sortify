#!/usr/bin/env python3
"""Dedicated CLI smoke test script to verify argument compatibility."""

import os
import subprocess
import sys


def run_command(command, expected_args):
    """Run a CLI command with --help and ensure it contains expected arguments."""
    try:
        env = os.environ.copy()
        env["COLUMNS"] = "80"
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        output = result.stdout

        missing = []
        for arg in expected_args:
            if arg not in output:
                missing.append(arg)

        if missing:
            print(f"Error: {command} is missing arguments in help output: {missing}")
            print(f"Output was:\n{output}")
            return False

        print(f"Success: {command} contains all expected arguments.")
        return True

    except subprocess.CalledProcessError as e:
        print(f"Error running {command}: exit code {e.returncode}")
        print(f"Stderr: {e.stderr}")
        return False


def main():
    """Run all CLI smoke tests."""
    success = True

    # sandbox_cli.py arguments documented in admin_guide.md
    sandbox_expected = [
        "reset",
        "extract",
        "analyze",
        "Reset the sandbox dataset",
        "Extract text from a specific sandbox file",
        "Run the analysis pipeline",
    ]
    if not run_command([sys.executable, "sandbox_cli.py", "--help"], sandbox_expected):
        success = False

    # app/main.py demo flag mentioned in contributor.md
    main_expected = ["--demo", "Run interactive CLI demo mode"]
    if not run_command([sys.executable, "app/main.py", "--help"], main_expected):
        success = False

    if not success:
        sys.exit(1)

    print("All CLI smoke tests passed successfully.")
    sys.exit(0)


if __name__ == "__main__":
    main()
