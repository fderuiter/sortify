"""Interactive CLI demo module for Smart AutoSorter."""

import json
import os
import sys
import tempfile
from pathlib import Path

from app.core.sample_corpus import generate_sample_corpus


def run_demo(settings):
    """Run an interactive CLI demo in an isolated temporary workspace."""
    print("Starting Smart AutoSorter AI Pro - Interactive CLI Demo...", file=sys.stderr)

    plan = None
    with tempfile.TemporaryDirectory(prefix="sortify-demo-run-") as temp_dir:
        temp_root = Path(temp_dir)
        corpus_dir = temp_root / "corpus"
        state_dir = temp_root / "state"
        keys_dir = temp_root / "keys"
        app_dir = temp_root / "app"
        corpus_dir.mkdir(parents=True, exist_ok=True)

        prev_app_dir = os.environ.get("AUTOSORTER_APP_DIR")
        prev_keys_dir = os.environ.get("SORTIFY_KEYS_DIR")
        os.environ["AUTOSORTER_APP_DIR"] = str(app_dir)
        os.environ["SORTIFY_KEYS_DIR"] = str(keys_dir)

        from app.core.db_conn import clear_connection_cache
        from app.core.pipeline import SortPipeline
        from app.core.session import AppSession
        from app.core.store import open_store

        try:
            print(f"[*] Generating sample corpus in {corpus_dir}...", file=sys.stderr)
            files_to_sort = generate_sample_corpus(str(corpus_dir))
            print(f"[*] Generated {len(files_to_sort)} files.", file=sys.stderr)

            def progress_callback(info=None):
                print("    -> Progress: File extraction complete.", file=sys.stderr)

            print(
                "[*] Processing files and generating sorting plan...", file=sys.stderr
            )
            store = open_store(
                state_dir,
                legacy_session_root=state_dir / "no-legacy-sessions",
            )
            session = AppSession(settings, base_dir=str(corpus_dir), store=store)
            try:
                plan = SortPipeline(session).plan(
                    files_to_sort, progress_callback=progress_callback
                )
            finally:
                session.close()
        finally:
            clear_connection_cache(only_current_and_inactive=False)
            if prev_app_dir is None:
                os.environ.pop("AUTOSORTER_APP_DIR", None)
            else:
                os.environ["AUTOSORTER_APP_DIR"] = prev_app_dir
            if prev_keys_dir is None:
                os.environ.pop("SORTIFY_KEYS_DIR", None)
            else:
                os.environ["SORTIFY_KEYS_DIR"] = prev_keys_dir

    print("\n--- Generated Sorting Plan ---", file=sys.stderr)
    print(json.dumps(plan, indent=2))
    print("------------------------------\n", file=sys.stderr)

    if plan and isinstance(plan, dict):
        print(
            "[+] Success: Demo completed. Sorting plan successfully generated.",
            file=sys.stderr,
        )
        sys.exit(0)
    print("[-] Failure: Failed to generate sorting plan.", file=sys.stderr)
    sys.exit(1)
