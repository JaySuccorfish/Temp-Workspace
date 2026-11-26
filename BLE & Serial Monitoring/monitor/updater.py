"""@file updater.py
@brief Built-in software updater (GitHub Releases + local .exe self-update) for Windows & macOS.

Pure standard library (urllib, json, tempfile, subprocess) -- zero extra dependencies.
Supports:
  1. Checking GitHub Releases (latest tag vs BRAND['version']).
  2. Downloading and launching the latest Windows Setup installer (.exe) or portable .exe,
     or macOS .app.zip.
  3. Offline update from a local .exe file (either a Setup installer or a portable .exe
     that replaces the running executable cleanly after exit).
"""
from __future__ import annotations

from .constants import BRAND
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import webbrowser

GITHUB_REPO = "shrinathpol/Temp-Workspace"
RELEASES_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
_USER_AGENT = f"Succorfish-BLE-Monitor/{BRAND['version']}"


def parse_version(ver: str) -> tuple[int, ...]:
    """'v2.4.0' or '2.4.0.0' -> (2, 4, 0) for reliable numeric comparison."""
    nums = [int(n) for n in re.findall(r"\d+", str(ver or ""))]
    while len(nums) > 3 and nums[-1] == 0:
        nums.pop()
    return tuple(nums) if nums else (0,)


def is_newer_version(remote_ver: str, current_ver: str | None = None) -> bool:
    cur = current_ver if current_ver is not None else BRAND["version"]
    return parse_version(remote_ver) > parse_version(cur)


def check_latest_release(timeout: float = 8.0) -> dict | None:
    """Query GitHub Releases API for the latest release metadata.

    Returns dict with keys: tag, version, name, notes, html_url, asset_name, asset_url, is_newer
    or None if unreachable / no release published yet.
    """
    req = urllib.request.Request(
        RELEASES_API_LATEST,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    tag = (data.get("tag_name") or "").strip()
    if not tag:
        return None
    assets = data.get("assets") or []
    chosen = _pick_platform_asset(assets)
    return {
        "tag": tag,
        "version": tag.lstrip("vV"),
        "name": data.get("name") or tag,
        "notes": (data.get("body") or "").strip(),
        "html_url": data.get("html_url") or RELEASES_PAGE_URL,
        "asset_name": chosen.get("name") if chosen else None,
        "asset_url": chosen.get("browser_download_url") if chosen else None,
        "is_newer": is_newer_version(tag),
    }


def _pick_platform_asset(assets: list[dict]) -> dict | None:
    """Pick the best downloadable asset for the current OS."""
    if sys.platform.startswith("win"):
        # Prefer standard Setup installer (.exe), fall back to portable Windows .exe
        for a in assets:
            name = (a.get("name") or "").lower()
            if "setup" in name and name.endswith(".exe"):
                return a
        for a in assets:
            name = (a.get("name") or "").lower()
            if name.endswith(".exe"):
                return a
    elif sys.platform == "darwin":
        for a in assets:
            name = (a.get("name") or "").lower()
            if ("macos" in name or "darwin" in name or ".app" in name) and name.endswith(".zip"):
                return a
    return None


def download_asset(url: str, filename: str, progress_cb=None, timeout: float = 120.0) -> str:
    """Download a release asset into a temp folder and return its local file path."""
    dest_dir = tempfile.mkdtemp(prefix="sf_monitor_update_")
    dest_path = os.path.join(dest_dir, filename)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress_cb is not None:
                progress_cb(done, total)
    return dest_path


def apply_windows_exe_update(new_exe_path: str) -> str:
    """Apply an update from a Windows Setup installer or standalone portable .exe.

    Returns a short status message; caller should close the app immediately after.
    """
    if not sys.platform.startswith("win"):
        raise RuntimeError("Windows .exe update is only supported on Windows.")
    if not os.path.isfile(new_exe_path):
        raise FileNotFoundError(f"Update file not found: {new_exe_path}")

    base = os.path.basename(new_exe_path).lower()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # Case 1: Standard Setup installer (Inno Setup) -- launch installer and exit cleanly
    if "setup" in base or "install" in base:
        subprocess.Popen([new_exe_path, "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"])
        return "Launching installer to upgrade..."

    # Case 2: Running from a frozen standalone .exe -> replace in-place after this PID exits
    if getattr(sys, "frozen", False):
        current_exe = os.path.abspath(sys.executable)
        pid = os.getpid()
        bat_path = os.path.join(tempfile.gettempdir(), f"sf_self_update_{pid}.bat")
        # Wait for current process to exit, copy new exe over current exe, relaunch, then self-delete
        bat_script = (
            "@echo off\r\n"
            ":wait\r\n"
            f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
            "if not errorlevel 1 (\r\n"
            "  timeout /t 1 /nobreak >nul\r\n"
            "  goto wait\r\n"
            ")\r\n"
            "timeout /t 1 /nobreak >nul\r\n"
            f'copy /Y "{new_exe_path}" "{current_exe}" >nul\r\n'
            f'start "" "{current_exe}"\r\n'
            'del "%~f0" >nul 2>&1\r\n'
        )
        with open(bat_path, "w", encoding="ascii", errors="replace") as fh:
            fh.write(bat_script)
        subprocess.Popen(["cmd.exe", "/c", bat_path], creationflags=flags)
        return "Replacing executable and restarting..."

    # Case 3: Running from Python source -- simply launch the chosen .exe
    subprocess.Popen([new_exe_path])
    return "Launching new executable..."


def open_macos_update(zip_path: str | None = None, url: str | None = None) -> None:
    """Reveal downloaded macOS .app.zip in Finder or open the GitHub Releases page."""
    if zip_path and os.path.isfile(zip_path) and sys.platform == "darwin":
        subprocess.Popen(["open", "-R", zip_path])
    else:
        webbrowser.open(url or RELEASES_PAGE_URL)
