#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Portable, zero-setup launcher for Succorfish BLE & Serial Monitor.

Works on macOS (Apple Silicon & Intel), Windows, and Linux with no manual
installation or setup:
  - Uses only the Python standard library.
  - Reuses an existing portable runtime inside ``.portable-env/`` if present.
  - Otherwise creates a local isolated environment inside this folder
    (automatically downloading a self-contained CPython 3.12 with Tcl/Tk
    bundled if the host Python is missing tkinter or is older than 3.10),
    installs the runtime dependencies (bleak, pyserial, pycryptodome), and
    launches ``debug_monitor.py``.

Usage:
    python3 portable_run.py            # prepare (first run only) and launch GUI
    python3 portable_run.py --check    # prepare/verify runtime without launching GUI
    python3 portable_run.py --offline  # launch using existing local runtime only
    python3 portable_run.py --clean    # delete .portable-env/
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
APP_ENTRY = ROOT_DIR / "debug_monitor.py"
PORTABLE_BASE = ROOT_DIR / ".portable-env"

RUNTIME_PACKAGES = (
    "bleak>=0.22.0",
    "pyserial>=3.5",
    "pycryptodome>=3.20.0",
)

# Pinned fallback release from astral-sh/python-build-standalone (includes Tcl/Tk).
PBS_API_LATEST = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
PBS_FALLBACK_TAG = "20250317"
PBS_FALLBACK_PY = "3.12.9"


def _log(msg: str) -> None:
    print(f"[portable] {msg}", flush=True)


def _platform_tag() -> str:
    sys_name = sys.platform
    if sys_name.startswith("win"):
        sys_name = "win32"
    arch = platform.machine().lower()
    if arch in ("amd64", "x86_64"):
        arch = "x86_64"
    elif arch in ("arm64", "aarch64"):
        arch = "arm64"
    return f"{sys_name}-{arch}"


def _pbs_triple() -> str | None:
    arch = platform.machine().lower()
    if sys.platform == "darwin":
        if arch in ("arm64", "aarch64"):
            return "aarch64-apple-darwin"
        if arch in ("x86_64", "amd64"):
            return "x86_64-apple-darwin"
    elif sys.platform.startswith("win"):
        if arch in ("amd64", "x86_64"):
            return "x86_64-pc-windows-msvc"
    elif sys.platform.startswith("linux"):
        if arch in ("x86_64", "amd64"):
            return "x86_64-unknown-linux-gnu"
        if arch in ("aarch64", "arm64"):
            return "aarch64-unknown-linux-gnu"
    return None


def _python_bin(env_dir: Path) -> Path:
    if sys.platform.startswith("win"):
        for rel in ("Scripts/python.exe", "python.exe"):
            cand = env_dir / rel
            if cand.is_file():
                return cand
        return env_dir / "Scripts" / "python.exe"
    for rel in ("bin/python3", "bin/python"):
        cand = env_dir / rel
        if cand.is_file():
            return cand
    return env_dir / "bin" / "python3"


def _check_python(py_exe: Path, require_deps: bool = False) -> bool:
    if not py_exe.is_file():
        return False
    imports = "import sys, tkinter"
    if require_deps:
        imports += ", bleak, serial, Crypto"
    code = f"{imports}; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
    try:
        res = subprocess.run(
            [str(py_exe), "-c", code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
        return res.returncode == 0
    except Exception:
        return False


def _resolve_pbs_url(triple: str) -> str:
    """Resolve the latest python-build-standalone 3.12 install_only archive URL."""
    try:
        req = urllib.request.Request(
            PBS_API_LATEST,
            headers={"User-Agent": "Succorfish-BLE-Monitor-Launcher/1.0"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            if (
                name.startswith("cpython-3.12.")
                and f"-{triple}-install_only.tar.gz" in name
                and not name.endswith(".sha256")
            ):
                url = asset.get("browser_download_url")
                if url:
                    return url
    except Exception:
        pass
    return (
        f"https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{PBS_FALLBACK_TAG}/cpython-{PBS_FALLBACK_PY}+{PBS_FALLBACK_TAG}-{triple}-install_only.tar.gz"
    )


def _download_portable_python(dest_dir: Path) -> Path:
    triple = _pbs_triple()
    if not triple:
        raise RuntimeError(
            f"Unsupported platform/architecture ({sys.platform}/{platform.machine()}) for automatic portable Python download."
        )
    url = _resolve_pbs_url(triple)
    _log(f"Downloading standalone portable Python (with Tcl/Tk) for {triple}...")
    _log(f"  {url}")
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "python-standalone.tar.gz"
        req = urllib.request.Request(url, headers={"User-Agent": "Succorfish-BLE-Monitor-Launcher/1.0"})
        with urllib.request.urlopen(req, timeout=180) as resp, open(archive, "wb") as out:
            shutil.copyfileobj(resp, out)
        _log("Extracting portable Python...")
        with tarfile.open(archive, "r:gz") as tf:
            extract_kwargs = {"filter": "data"} if sys.version_info >= (3, 12) else {}
            tf.extractall(tmp, **extract_kwargs)
        extracted = Path(tmp) / "python"
        if not extracted.is_dir():
            raise RuntimeError("Unexpected archive layout in portable Python download.")
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.move(str(extracted), str(dest_dir))
    py_exe = _python_bin(dest_dir)
    if not py_exe.is_file():
        raise RuntimeError(f"Portable Python executable not found at {py_exe}")
    return py_exe


def _install_deps(py_exe: Path) -> None:
    _log("Installing runtime packages (bleak, pyserial, pycryptodome)...")
    cmd = [
        str(py_exe), "-m", "pip", "install",
        "--disable-pip-version-check", "--no-input",
        *RUNTIME_PACKAGES,
    ]
    res = subprocess.run(cmd)
    if res.returncode != 0:
        raise RuntimeError("Failed to install runtime packages via pip.")


def ensure_portable_python(offline: bool = False) -> Path:
    """Return a ready Python executable with tkinter + all runtime dependencies."""
    tag = _platform_tag()
    env_root = PORTABLE_BASE / tag
    standalone_dir = env_root / "python"
    venv_dir = env_root / "venv"

    # 1. Reuse existing standalone portable Python in .portable-env/
    cand = _python_bin(standalone_dir)
    if _check_python(cand, require_deps=True):
        return cand

    # 2. Reuse existing local venv in .portable-env/
    cand_venv = _python_bin(venv_dir)
    if _check_python(cand_venv, require_deps=True):
        return cand_venv

    # 3. Reuse Windows .build-venv if present
    if sys.platform.startswith("win"):
        win_venv = _python_bin(ROOT_DIR / ".build-venv")
        if _check_python(win_venv, require_deps=True):
            return win_venv

    # 4. If current interpreter already has tkinter + all deps, run directly
    cur_py = Path(sys.executable)
    if _check_python(cur_py, require_deps=True):
        return cur_py

    if offline:
        raise RuntimeError(
            "Offline mode requested, but no ready portable Python environment was found. "
            "Run once with internet access to populate .portable-env/."
        )

    # 5. If standalone python exists but is missing deps, install them
    if _check_python(cand, require_deps=False):
        _install_deps(cand)
        return cand

    # 6. If current Python >= 3.10 has tkinter + venv, create a local portable venv
    if _check_python(cur_py, require_deps=False):
        try:
            _log(f"Creating local portable venv at {venv_dir}...")
            env_root.mkdir(parents=True, exist_ok=True)
            res = subprocess.run([str(cur_py), "-m", "venv", str(venv_dir)])
            if res.returncode == 0:
                vpy = _python_bin(venv_dir)
                if _check_python(vpy, require_deps=False):
                    _install_deps(vpy)
                    return vpy
        except Exception as exc:
            _log(f"Local venv creation failed ({exc}); falling back to standalone portable Python...")

    # 7. Download self-contained standalone portable Python (includes Tcl/Tk)
    py_exe = _download_portable_python(standalone_dir)
    _install_deps(py_exe)
    return py_exe


def _print_env_summary(py_exe: Path) -> None:
    code = (
        "import sys, tkinter, importlib.metadata as m; "
        "print(f'Python {sys.version.split()[0]} | Tk {tkinter.TkVersion} | "
        "bleak {m.version(\"bleak\")} | pyserial {m.version(\"pyserial\")} | "
        "pycryptodome {m.version(\"pycryptodome\")}')"
    )
    res = subprocess.run([str(py_exe), "-c", code], capture_output=True, text=True)
    if res.returncode == 0 and res.stdout.strip():
        _log(f"Ready: {res.stdout.strip()}")
    else:
        _log(f"Ready: {py_exe}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Portable zero-setup launcher for Succorfish BLE & Serial Monitor")
    parser.add_argument("--check", action="store_true", help="Prepare and verify the portable runtime without launching the GUI")
    parser.add_argument("--offline", action="store_true", help="Use only an already-prepared local runtime (no downloads)")
    parser.add_argument("--clean", action="store_true", help="Remove .portable-env/ and exit")
    args = parser.parse_args(argv)

    if args.clean:
        if PORTABLE_BASE.exists():
            shutil.rmtree(PORTABLE_BASE)
            _log(f"Removed {PORTABLE_BASE}")
        else:
            _log("Nothing to clean.")
        return 0

    if not APP_ENTRY.is_file():
        _log(f"ERROR: {APP_ENTRY} not found.")
        return 1

    try:
        py_exe = ensure_portable_python(offline=args.offline)
    except Exception as exc:
        _log(f"ERROR: {exc}")
        return 1

    _print_env_summary(py_exe)
    if args.check:
        return 0

    _log(f"Launching {APP_ENTRY.name}...")
    return subprocess.call([str(py_exe), str(APP_ENTRY)], cwd=str(ROOT_DIR))


if __name__ == "__main__":
    raise SystemExit(main())
