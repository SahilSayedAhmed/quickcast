"""
updater.py — QuickCast Auto Updater
=====================================
Checks GitHub releases for a newer version of QuickCast.
If found, shows a popup and downloads + replaces the exe automatically.

HOW IT WORKS:
  1. Reads current version from version.txt bundled inside the exe
  2. Checks GitHub API for latest release version
  3. If newer version found → shows popup asking user to update
  4. If yes → downloads new exe → replaces current exe → restarts app
  5. Runs in background thread — never blocks the UI

GITHUB SETUP:
  - Releases must be tagged as v1.0.0, v1.1.0 etc.
  - Release must have an asset named exactly: QuickCast.exe
"""

import os
import sys
import threading
import logging
import tempfile
import shutil
import subprocess

log = logging.getLogger("quickcast.updater")

# ── CONFIG — update this with your GitHub details ─────────────────────────────
GITHUB_USER    = "SahilSayedAhmed"   # ← change this
GITHUB_REPO    = "quickcast"              # ← change this if different
CURRENT_VERSION_FILE = "version.txt"
# ─────────────────────────────────────────────────────────────────────────────


def get_current_version() -> str:
    """Read version from version.txt bundled in the exe or next to the script."""
    # Check _MEIPASS (bundled inside exe)
    if hasattr(sys, "_MEIPASS"):
        path = os.path.join(sys._MEIPASS, CURRENT_VERSION_FILE)
        if os.path.exists(path):
            return open(path).read().strip()

    # Check next to exe
    path = os.path.join(os.path.dirname(sys.executable), CURRENT_VERSION_FILE)
    if os.path.exists(path):
        return open(path).read().strip()

    # Check next to script
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), CURRENT_VERSION_FILE)
        if os.path.exists(path):
            return open(path).read().strip()
    except Exception:
        pass

    return "0.0.0"


def version_tuple(v: str):
    """Convert version string to tuple for comparison. e.g. '1.2.3' → (1, 2, 3)"""
    try:
        return tuple(int(x) for x in v.strip("v").split("."))
    except Exception:
        return (0, 0, 0)


def check_for_update() -> dict | None:
    """
    Check GitHub API for latest release.
    Returns dict with version + download URL if newer version exists.
    Returns None if up to date or check fails.
    """
    try:
        import urllib.request
        import json

        api_url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(api_url, headers={"User-Agent": "QuickCast-Updater"})

        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        latest_version = data.get("tag_name", "0.0.0").strip("v")
        current_version = get_current_version()

        log.info(f"Current: v{current_version} | Latest: v{latest_version}")

        if version_tuple(latest_version) > version_tuple(current_version):
            # Find the QuickCast.exe asset in the release
            for asset in data.get("assets", []):
                if asset["name"] == "QuickCast.exe":
                    return {
                        "version": latest_version,
                        "download_url": asset["browser_download_url"],
                        "release_notes": data.get("body", ""),
                    }

    except Exception as e:
        log.warning(f"Update check failed: {e}")

    return None


def download_and_install(download_url: str, progress_callback=None) -> bool:
    """
    Download new exe and replace current exe.
    Uses a temp file to avoid corrupting the running exe.
    Returns True on success.
    """
    try:
        import urllib.request

        # Download to temp file
        temp_dir = tempfile.mkdtemp()
        temp_exe = os.path.join(temp_dir, "QuickCast_new.exe")

        log.info(f"Downloading update from: {download_url}")

        def reporthook(count, block_size, total_size):
            if progress_callback and total_size > 0:
                percent = int(count * block_size * 100 / total_size)
                progress_callback(min(percent, 100))

        urllib.request.urlretrieve(download_url, temp_exe, reporthook)

        # Get current exe path
        current_exe = os.path.abspath(sys.executable)
        backup_exe  = current_exe + ".backup"

        # Create a batch script that:
        # 1. Waits for current exe to close
        # 2. Replaces it with new exe
        # 3. Restarts the app
        batch_script = f"""
@echo off
timeout /t 2 /nobreak >nul
move /y "{current_exe}" "{backup_exe}"
move /y "{temp_exe}" "{current_exe}"
del "{backup_exe}" 2>nul
start "" "{current_exe}"
"""
        batch_path = os.path.join(temp_dir, "update.bat")
        with open(batch_path, "w") as f:
            f.write(batch_script)

        # Run batch script and exit current app
        subprocess.Popen(
            ["cmd", "/c", batch_path],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        log.info("Update downloaded — restarting app")
        return True

    except Exception as e:
        log.error(f"Update install failed: {e}")
        return False


class AutoUpdater:
    """
    Runs update check in background thread.
    Shows Qt popup if update available.
    Never blocks the UI.
    """

    def __init__(self, window):
        self.window = window

    def start(self):
        """Start background update check."""
        if GITHUB_USER == "YOUR_GITHUB_USERNAME":
            log.info("Auto-updater: GitHub username not configured — skipping")
            return
        t = threading.Thread(target=self._check_worker, daemon=True)
        t.start()

    def _check_worker(self):
        """Background thread: check for update and notify UI."""
        import time
        time.sleep(5)  # Wait 5s after launch before checking

        update = check_for_update()
        if update:
            log.info(f"Update available: v{update['version']}")
            # Notify UI via signal (thread-safe)
            self.window.update_available_signal.emit(
                update["version"],
                update["download_url"],
            )