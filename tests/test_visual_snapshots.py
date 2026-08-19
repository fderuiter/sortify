import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest
from PIL import Image, ImageChops, ImageDraw


def _setup_playwright_browsers_path():
    try:
        import playwright

        pw_dir = os.path.dirname(playwright.__file__)
        local_browsers = os.path.abspath(
            os.path.join(pw_dir, "driver", "package", ".local-browsers")
        )
        if os.path.exists(local_browsers):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = local_browsers
            return local_browsers
    except Exception:
        pass

    user_cache = os.path.expanduser("~/.cache/ms-playwright")
    if os.path.exists(user_cache):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = user_cache
        return user_cache

    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_path and env_path != "0":
        abs_env_path = os.path.abspath(env_path)
        if os.path.exists(abs_env_path):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = abs_env_path
            return abs_env_path

    return None


browsers_path = _setup_playwright_browsers_path()

PLAYWRIGHT_AVAILABLE = False
if browsers_path:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        PLAYWRIGHT_AVAILABLE = True
    except Exception:
        PLAYWRIGHT_AVAILABLE = False

SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "snapshots")

VIEWPORTS = [
    ("mobile", 320, 800),
    ("tablet", 768, 800),
    ("desktop", 1280, 800),
]


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_nicegui_server(enable_pseudoloc: bool = False):
    port = find_free_port()

    # Create a temporary script file to run as a real file
    # This prevents NiceGUI's interactive script detection from failing
    # We also mock ui.notify to be a no-op to prevent non-deterministic notification overlays
    launcher_content = f"""import sys
import os
sys.path.insert(0, '/app')

if {enable_pseudoloc}:
    from tests.pseudoloc import apply_pseudolocalization
    apply_pseudolocalization()

from nicegui import ui
ui.notify = lambda *args, **kwargs: None

from app.ui.app import run_app
from app.config import AppSettings

s = AppSettings()
s.AI_CONSENT_GRANTED = None
run_app(s, port={port}, show=False)
"""
    # Create in tests directory to ensure relative paths resolve cleanly if needed
    launcher_file = tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False, dir="/app/tests"
    )
    launcher_file.write(launcher_content)
    launcher_file.close()

    isolated_tmp = tempfile.mkdtemp()

    cmd = [sys.executable, launcher_file.name]

    env = os.environ.copy()
    env["NICEGUI_SHOW_WELCOME"] = "False"
    env["PYTHONPATH"] = "/app"
    env["TMPDIR"] = isolated_tmp
    env["TEMP"] = isolated_tmp
    env["TMP"] = isolated_tmp
    env["AUTOSORTER_SESSION_BASE_DIR"] = os.path.join(isolated_tmp, "autosorter_sessions")
    # Remove PYTEST_CURRENT_TEST so NiceGUI doesn't think it is running within pytest in the subprocess
    if "PYTEST_CURRENT_TEST" in env:
        del env["PYTEST_CURRENT_TEST"]

    log_file = tempfile.NamedTemporaryFile(suffix=".log", delete=False)
    proc = subprocess.Popen(cmd, env=env, stdout=log_file, stderr=subprocess.STDOUT)

    # Wait for the port to open
    start_time = time.time()
    opened = False
    while time.time() - start_time < 30:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.connect(("127.0.0.1", port))
                opened = True
                break
            except Exception:
                time.sleep(0.1)

    if not opened:
        proc.terminate()
        # Read and display logs for troubleshooting
        log_file.close()
        with open(log_file.name, "r") as lf:
            logs = lf.read()
        os.remove(log_file.name)
        if os.path.exists(launcher_file.name):
            os.remove(launcher_file.name)
        pytest.fail(
            f"Failed to start NiceGUI server within 30 seconds. Subprocess logs:\n{logs}"
        )

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            log_file.close()
            if os.path.exists(log_file.name):
                os.remove(log_file.name)
            if os.path.exists(launcher_file.name):
                os.remove(launcher_file.name)
            shutil.rmtree(isolated_tmp, ignore_errors=True)


@pytest.fixture
def nicegui_server():
    yield from _start_nicegui_server(enable_pseudoloc=False)


@pytest.fixture
def nicegui_server_pseudoloc():
    yield from _start_nicegui_server(enable_pseudoloc=True)


def assert_visual_snapshot(snapshot_name, actual_image_path):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    baseline_path = os.path.join(SNAPSHOT_DIR, f"{snapshot_name}_baseline.png")
    actual_path = os.path.join(SNAPSHOT_DIR, f"{snapshot_name}_actual.png")
    diff_path = os.path.join(SNAPSHOT_DIR, f"{snapshot_name}_diff.png")

    # Save actual image to the target path
    shutil.copy(actual_image_path, actual_path)

    update_snapshots = os.environ.get("UPDATE_SNAPSHOTS") == "1"

    # Preserving existing desktop baseline comparisons
    if not os.path.exists(baseline_path):
        if snapshot_name.startswith("desktop_"):
            legacy_name = snapshot_name[len("desktop_"):]
            legacy_baseline = os.path.join(SNAPSHOT_DIR, f"{legacy_name}_baseline.png")
            if os.path.exists(legacy_baseline):
                shutil.copy(legacy_baseline, baseline_path)

    if not os.path.exists(baseline_path) or update_snapshots:
        shutil.copy(actual_image_path, baseline_path)
        # Remove any lingering diff files
        if os.path.exists(diff_path):
            os.remove(diff_path)
        if not update_snapshots:
            pytest.fail(
                f"Baseline snapshot for {snapshot_name} generated for the first time. Run again to verify."
            )
        return

    # Compare actual vs baseline
    img1 = Image.open(baseline_path).convert("RGB")
    img2 = Image.open(actual_path).convert("RGB")

    if img1.size != img2.size:
        # Sizes differ, definitely a visual mismatch! Save diff
        img2.save(diff_path)
        pytest.fail(
            f"Snapshot size mismatch for {snapshot_name}: baseline={img1.size}, actual={img2.size}"
        )

    # Pixel-by-pixel diff with color tolerance for anti-aliasing / font smoothing noise
    diff = ImageChops.difference(img1, img2)
    diff_data = diff.load()
    width, height = diff.size
    different_pixels = 0

    highlight = img2.copy()
    draw = ImageDraw.Draw(highlight)

    # Ignore minor anti-aliasing / font smoothing noise (max channel difference <= 25)
    COLOR_THRESHOLD = 25

    for y in range(height):
        for x in range(width):
            r, g, b = diff_data[x, y]
            if max(r, g, b) > COLOR_THRESHOLD:
                different_pixels += 1
                # Highlight in red
                draw.point((x, y), fill=(255, 0, 0))

    if different_pixels >= 2:
        highlight.save(diff_path)
        pytest.fail(
            f"Visual mismatch detected for {snapshot_name}: {different_pixels} pixels differ. "
            f"Check diff highlights at: {diff_path}"
        )
    else:
        # If they match, delete any leftover diff file
        if os.path.exists(diff_path):
            os.remove(diff_path)


def run_visual_snapshot_pass(
    server_url: str, viewport_name: str, width: int, height: int, pseudoloc: bool = False
):
    suffix = "_pseudoloc" if pseudoloc else ""
    disable_animations_css = (
        "*, *::before, *::after { "
        "animation: none !important; "
        "animation-duration: 0s !important; "
        "animation-delay: 0s !important; "
        "transition: none !important; "
        "transition-duration: 0s !important; "
        "transition-delay: 0s !important; "
        "}"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": height})

        page.goto(server_url)
        try:
            page.add_style_tag(content=disable_animations_css)
        except Exception:
            pass

        # Helper to clear focus, move mouse, and wait for fonts before taking screenshot
        def prepare_for_screenshot():
            try:
                page.mouse.move(0, 0)
                page.evaluate("() => document.activeElement && document.activeElement.blur()")
                page.evaluate("document.fonts.ready")
            except Exception:
                pass

        # 1. Wizard View Snapshot
        page.wait_for_selector('[aria-label="Setup Wizard Title"]', timeout=8000)
        page.wait_for_timeout(1000)  # wait for layout to settle

        prepare_for_screenshot()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            temp_path = f.name
        try:
            page.screenshot(path=temp_path)
            assert_visual_snapshot(f"{viewport_name}_wizard_view{suffix}", temp_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        # 2. Main View Snapshot
        page.locator('[aria-label="Decline Button"]').first.click()
        try:
            page.wait_for_selector(
                '[aria-label="Setup Wizard Title"]', state="hidden", timeout=5000
            )
        except Exception:
            pass
        try:
            page.wait_for_selector(".q-dialog", state="hidden", timeout=5000)
        except Exception:
            pass
        try:
            page.add_style_tag(content=disable_animations_css)
        except Exception:
            pass
        page.wait_for_selector('[aria-label="Settings Button"]', timeout=5000)
        page.wait_for_selector(
            '[aria-label="AI Offline Warning Label"]',
            state="visible",
            timeout=15000,
        )
        page.wait_for_timeout(2000)  # wait for animations and fade transitions to settle

        prepare_for_screenshot()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            temp_path = f.name
        try:
            page.screenshot(path=temp_path)
            assert_visual_snapshot(f"{viewport_name}_main_view{suffix}", temp_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        # 3. Settings View Snapshot
        page.locator('[aria-label="Settings Button"]').click()
        page.wait_for_selector('[aria-label="Settings Dialog Title"]', timeout=5000)
        try:
            page.add_style_tag(content=disable_animations_css)
        except Exception:
            pass
        page.wait_for_timeout(1000)  # wait for layout to settle

        prepare_for_screenshot()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            temp_path = f.name
        try:
            page.screenshot(path=temp_path)
            assert_visual_snapshot(f"{viewport_name}_settings_view{suffix}", temp_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        browser.close()


@pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="playwright is not installed")
@pytest.mark.parametrize("viewport_name,width,height", VIEWPORTS)
def test_visual_snapshots(nicegui_server, viewport_name, width, height):
    run_visual_snapshot_pass(nicegui_server, viewport_name, width, height, pseudoloc=False)


@pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="playwright is not installed")
@pytest.mark.parametrize("viewport_name,width,height", VIEWPORTS)
def test_visual_snapshots_pseudoloc(nicegui_server_pseudoloc, viewport_name, width, height):
    run_visual_snapshot_pass(
        nicegui_server_pseudoloc, viewport_name, width, height, pseudoloc=True
    )

