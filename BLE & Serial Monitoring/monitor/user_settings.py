"""@file user_settings.py
@brief Small per-user settings file of the monitor (paths chosen once, such as the Delphis picture models)
@version 1.0
@date 2026-11-22

Stored as JSON in %APPDATA%\\Succorfish\\BLE Monitor\\settings.json (the home folder elsewhere). A missing or damaged
file gives the defaults; a failed save is logged by the caller, never fatal.
"""
import json
import os
import sys


def _default_settings_folder() -> str:
  if sys.platform == "darwin":
    base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
  elif sys.platform.startswith("win"):
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
  else:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
  return os.path.join(base, "Succorfish", "BLE Monitor")


SETTINGS_FOLDER = _default_settings_folder()
SETTINGS_FILE = os.path.join(SETTINGS_FOLDER, "settings.json")


def load() -> dict:
  """All settings ({} when there is no readable file)."""
  try:
    with open(SETTINGS_FILE, encoding="utf-8") as handle:
      data = json.load(handle)
    return data if isinstance(data, dict) else {}
  except (OSError, ValueError):
    return {}


def get(key: str, default=None):
  """One setting."""
  return load().get(key, default)


def put(key: str, value) -> bool:
  """Store one setting; False when the file cannot be written."""
  data = load()
  data[key] = value
  try:
    os.makedirs(SETTINGS_FOLDER, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as handle:
      json.dump(data, handle, indent=2)
    return True
  except OSError:
    return False
