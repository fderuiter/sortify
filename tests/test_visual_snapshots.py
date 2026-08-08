import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest
from PIL import Image, ImageChops, ImageDraw

try:
    from playwright.sync_api import sync_playwright

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "snapshots")


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def nicegui_server():
    port = find_free_port()

    # Create a temporary script file to run as a real file
    # This prevents NiceGUI's interactive script detection from failing
    # We also mock ui.notify to be a no-op to prevent non-deterministic notification overlays
    launcher_content = f"""import sys
import os
sys.path.insert(0, '/app')

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

    cmd = [sys.executable, launcher_file.name]

    env = os.environ.copy()
    env["NICEGUI_SHOW_WELCOME"] = "False"
    env["PYTHONPATH"] = "/app"
    # Remove PYTEST_CURRENT_TEST so NiceGUI doesn't think it is running within pytest in the subprocess
    if "PYTEST_CURRENT_TEST" in env:
        del env["PYTEST_CURRENT_TEST"]

    log_file = tempfile.NamedTemporaryFile(suffix=".log", delete=False)
    proc = subprocess.Popen(cmd, env=env, stdout=log_file, stderr=subprocess.STDOUT)

    # Wait for the port to open
    start_time = time.time()
    opened = False
    while time.time() - start_time < 15:
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
            f"Failed to start NiceGUI server within 15 seconds. Subprocess logs:\n{logs}"
        )

    yield f"http://127.0.0.1:{port}"

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


def assert_visual_snapshot(snapshot_name, actual_image_path):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    baseline_path = os.path.join(SNAPSHOT_DIR, f"{snapshot_name}_baseline.png")
    actual_path = os.path.join(SNAPSHOT_DIR, f"{snapshot_name}_actual.png")
    diff_path = os.path.join(SNAPSHOT_DIR, f"{snapshot_name}_diff.png")

    # Save actual image to the target path
    shutil.copy(actual_image_path, actual_path)

    update_snapshots = os.environ.get("UPDATE_SNAPSHOTS") == "1"

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

    # Pixel-by-pixel diff
    diff = ImageChops.difference(img1, img2)
    diff_data = diff.load()
    width, height = diff.size
    different_pixels = 0

    highlight = img2.copy()
    draw = ImageDraw.Draw(highlight)

    for y in range(height):
        for x in range(width):
            r, g, b = diff_data[x, y]
            if r > 0 or g > 0 or b > 0:
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


@pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="playwright is not installed")
def test_visual_snapshots(nicegui_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Set standard screen size as 1280x800
        page = browser.new_page(viewport={"width": 1280, "height": 800})

        # Navigate to NiceGUI local server
        page.goto(nicegui_server)

        # 1. Wizard View Snapshot
        # Wait for wizard dialog elements to appear on startup
        page.wait_for_selector('[aria-label="Setup Wizard Title"]', timeout=8000)
        page.wait_for_timeout(1500)  # wait for animations to settle

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            temp_path = f.name
        try:
            page.screenshot(path=temp_path)
            assert_visual_snapshot("wizard_view", temp_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        # 2. Main View Snapshot
        # Click decline button in wizard to get to main view
        page.locator('[aria-label="Decline Button"]').click()
        page.wait_for_selector('[aria-label="Application Title"]', timeout=5000)
        page.wait_for_timeout(1500)  # wait for animations to settle

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            temp_path = f.name
        try:
            page.screenshot(path=temp_path)
            assert_visual_snapshot("main_view", temp_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        # 3. Settings View Snapshot
        # Click settings button in header to open settings view
        page.locator('[aria-label="Settings Button"]').click()
        page.wait_for_selector('[aria-label="Settings Dialog Title"]', timeout=5000)
        page.wait_for_timeout(1500)  # wait for animations to settle

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            temp_path = f.name
        try:
            page.screenshot(path=temp_path)
            assert_visual_snapshot("settings_view", temp_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        browser.close()
