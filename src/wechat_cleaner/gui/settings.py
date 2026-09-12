"""Local settings that survive a restart (WeChat data root, last account).

The window would otherwise ask for the WeChat data root on *every* launch,
which is tedious whenever the install does not sit in a conventional place
(``C:\\!D\\xwechat_files`` is a real example).  Remembering the root that
actually connected removes that step.

The file holds two strings the user typed themselves.  It never touches the
WeChat directory, and it is written atomically so a crash mid-write cannot
leave a half-written file behind.  A failure to persist is never fatal - the
session simply continues without the shortcut.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = [
    "load_settings",
    "remember_session",
    "remember_thumbnail_size",
    "remembered_account",
    "remembered_data_root",
    "remembered_thumbnail_size",
    "save_settings",
    "settings_path",
]

_APP_DIRNAME = "WeChatSpaceManager"
_SETTINGS_FILENAME = "settings.json"

# Tests (and portable installs) point this somewhere disposable.
ENV_OVERRIDE = "WCSM_SETTINGS_PATH"


def settings_path() -> Path:
    """Where the settings live, honouring the ``WCSM_SETTINGS_PATH`` override."""
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / _APP_DIRNAME / _SETTINGS_FILENAME


def load_settings() -> dict[str, Any]:
    """Read the settings dict; a missing or corrupt file reads as empty."""
    try:
        raw = settings_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(values: dict[str, Any]) -> bool:
    """Write the settings atomically.  Returns whether they were stored."""
    path = settings_path()
    temp = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(
            json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def remember_session(data_root: str, account_id: str | None = None) -> bool:
    """Remember a data root that connected, so the next launch pre-fills it."""
    root = (data_root or "").strip()
    if not root:
        return False
    values = load_settings()
    values["data_root"] = root
    if account_id:
        values["account_id"] = account_id
    return save_settings(values)


def remembered_data_root() -> str | None:
    """The remembered root, but only while it still looks usable."""
    root = str(load_settings().get("data_root") or "").strip()
    if not root:
        return None
    try:
        if not Path(root).is_dir():
            return None
    except OSError:
        return None
    return root


def remembered_account() -> str | None:
    """The account id connected to last, when one was recorded."""
    account = str(load_settings().get("account_id") or "").strip()
    return account or None


def remember_thumbnail_size(edge: int) -> bool:
    """Remember the album tile size chosen with Ctrl+wheel."""
    try:
        value = int(edge)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    values = load_settings()
    values["thumbnail_size"] = value
    return save_settings(values)


def remembered_thumbnail_size() -> int | None:
    """The remembered tile size, or ``None`` when nothing usable was stored."""
    raw = load_settings().get("thumbnail_size")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None
