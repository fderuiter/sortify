"""Headless driver for the Sortify Textual TUI (agent tooling, not product code).

Launches ``AutoSorterTUI`` under Textual's Pilot (no TTY needed), in an
isolated throwaway app dir + HOME, against a generated sample corpus (or
``--dir``). Commands come from argv (``-c "press ctrl+s"``), a file
(``--script``) or stdin (one per line; interactive REPL when run in tmux).

Commands:
  press KEY [KEY...]   send key presses (textual key names: ctrl+s, enter, down)
  type TEXT            type literal text into the focused widget
  click SELECTOR       click a widget by CSS selector (e.g. #btn-execute)
  wait SECONDS         sleep, letting the app run
  idle [TIMEOUT]       wait until all workers finish (default 120s)
  ss NAME              save screenshot -> <out>/NAME.svg and NAME.png
  tree                 print the plan tree as indented text
  screen               print active screen class, focused widget, status bar
  plan                 print app.plan as JSON
  files                list files under the target dir
  analyze              extract + fit + plan in the app's session, then redraw the
                       tree (what Ctrl+S skips; see SKILL.md gotchas)
  eval PYTHON          eval/exec Python with `app`, `pilot`, `target` in scope
  quit                 exit
Run from the repo root:  uv run python .claude/skills/run-sortify/driver.py
"""

import argparse
import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

FIN = "Quarterly revenue budget invoice tax ledger profit expense accounting balance sheet fiscal report"
TECH = "Python kubernetes docker server deployment API database backend microservice cluster code"


def make_corpus(target: Path) -> None:
    """Write 4 finance + 4 tech text files that cluster into two folders."""
    target.mkdir(parents=True, exist_ok=True)
    for i in range(1, 5):
        (target / f"fin{i}.txt").write_text(f"{FIN} {i}\n")
        (target / f"tech{i}.txt").write_text(f"{TECH} {i}\n")


def svg_to_png(svg: Path, png: Path, width: int, height: int) -> bool:
    """Render a Textual SVG screenshot to PNG with headless Chromium."""
    chrome = CHROME if os.path.exists(CHROME) else shutil.which("chromium")
    if not chrome:
        return False
    # Textual SVGs are ~ 12.2px per column / 24.4px per row, plus chrome.
    w, h = int(width * 12.3) + 40, int(height * 24.5) + 90
    r = subprocess.run(
        [
            chrome,
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-background-networking",
            "--hide-scrollbars",
            f"--screenshot={png}",
            f"--window-size={w},{h}",
            svg.as_uri(),
        ],
        capture_output=True,
        timeout=60,
    )
    return r.returncode == 0 and png.exists()


def analyze(app, target: Path) -> None:
    """Extract, fit and plan in the app's session (the step Ctrl+S skips)."""
    from app.core.extractor import build_corpus_generator
    from app.core.scanner import get_files_from_sources
    from app.core.session import AppSession

    if app.app_session is None:
        app.app_session = AppSession(
            app.settings, str(target), source_dirs=[str(target)]
        )
    s = app.app_session
    files = get_files_from_sources([str(target)])
    for chunk in build_corpus_generator(
        base_dir=str(target),
        items_to_sort=files,
        progress_callback=lambda info=None: None,
        max_workers=2,
        db=s.db,
        settings=app.settings,
    ):
        s.partial_fit(chunk)
    app.plan = s.generate_sorting_plan()


def tree_lines(node, depth=0):
    """Yield the tree under ``node`` as indented label lines."""
    label = str(node.label)
    yield "  " * depth + label
    for child in node.children:
        yield from tree_lines(child, depth + 1)


async def run(args) -> int:
    """Launch the TUI under Pilot and execute driver commands."""
    from app.config import AppSettings
    from app.ui.tui import AutoSorterTUI

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    target = Path(args.dir).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    app = AutoSorterTUI(settings=settings, base_dir=str(target), skip_wizard=True)

    # Clustering spawns a subprocess; multiprocessing's resource tracker must be
    # started before Textual swaps sys.stderr, or spawn dies with
    # "ValueError: bad value(s) in fds_to_keep" and the plan comes back {}.
    from multiprocessing import resource_tracker

    resource_tracker.ensure_running()

    async with app.run_test(size=(args.width, args.height)) as pilot:
        await pilot.pause()

        async def do(line: str) -> bool:
            line = line.strip()
            if not line or line.startswith("#"):
                return True
            cmd, _, rest = line.partition(" ")
            if cmd == "quit":
                return False
            if cmd == "press":
                await pilot.press(*shlex.split(rest))
                await pilot.pause()
            elif cmd == "type":
                await pilot.press(*list(rest))
                await pilot.pause()
            elif cmd == "click":
                await pilot.click(rest)
                await pilot.pause()
            elif cmd == "wait":
                await pilot.pause(float(rest or 1))
            elif cmd == "idle":
                await asyncio.wait_for(
                    app.workers.wait_for_complete(), float(rest or 120)
                )
                await pilot.pause()
                await pilot.pause()
            elif cmd == "ss":
                name = rest or "screen"
                svg = out / f"{name}.svg"
                svg.write_text(app.export_screenshot())
                png = out / f"{name}.png"
                ok = svg_to_png(svg, png, args.width, args.height)
                print(
                    f"saved {svg}"
                    + (f" and {png}" if ok else " (png conversion failed)")
                )
            elif cmd == "tree":
                print("\n".join(tree_lines(app.query_one("#plan-tree").root)))
            elif cmd == "screen":
                focused = app.focused
                status = (
                    app.query_one("#status-bar") if app.query("#status-bar") else None
                )
                print(
                    f"screen={type(app.screen).__name__} "
                    f"focused={type(focused).__name__ if focused else None}#{getattr(focused, 'id', None)}"
                )
                if status is not None:
                    print(f"status={status.render()}")
            elif cmd == "plan":
                print(json.dumps(app.plan, indent=1, default=str))
            elif cmd == "analyze":
                await asyncio.to_thread(analyze, app, target)
                app.rebuild_tree()
                await pilot.pause()
                print(f"plan folders: {sorted(app.plan)}")
            elif cmd == "files":
                for p in sorted(target.rglob("*")):
                    print(p.relative_to(target), "/" if p.is_dir() else "")
            elif cmd == "eval":
                scope = {"app": app, "pilot": pilot, "target": target}
                try:
                    res = eval(rest, scope)
                    if asyncio.iscoroutine(res):
                        res = await res
                    print(repr(res))
                except SyntaxError:
                    exec(rest, scope)
            else:
                print(f"unknown command: {cmd}")
            return True

        lines = list(args.c or [])
        if args.script:
            lines += Path(args.script).read_text().splitlines()
        if lines:
            for line in lines:
                print(f"> {line}", flush=True)
                if not await do(line):
                    break
        else:
            while True:
                print("sortify> ", end="", flush=True)
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line:
                    break
                try:
                    if not await do(line):
                        break
                except Exception as e:  # keep the REPL alive
                    print(f"error: {type(e).__name__}: {e}")
                print(flush=True)
    return 0


def main() -> int:
    """Parse args, isolate app state, build the corpus, and run."""
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("-c", action="append", help="command to run (repeatable)")
    p.add_argument("--script", help="file of commands, one per line")
    p.add_argument("--dir", help="folder to sort (default: fresh 8-file sample corpus)")
    p.add_argument(
        "--out", default=str(REPO / ".sortify-driver"), help="screenshot dir"
    )
    p.add_argument("--width", type=int, default=120)
    p.add_argument("--height", type=int, default=36)
    p.add_argument(
        "--keep-state",
        action="store_true",
        help="reuse AUTOSORTER_APP_DIR/HOME from the environment",
    )
    args = p.parse_args()

    root = Path(tempfile.mkdtemp(prefix="sortify-driver-"))
    if not args.keep_state:
        # Must be set before importing app.*: config resolves paths at import.
        os.environ["AUTOSORTER_APP_DIR"] = str(root / "appdir")
        os.environ["HOME"] = str(root / "home")
        (root / "home").mkdir()
    if not args.dir:
        args.dir = str(root / "inbox")
        make_corpus(Path(args.dir))
    print(f"target={args.dir} state={root}", flush=True)
    sys.path.insert(0, str(REPO))
    os.chdir(REPO)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
