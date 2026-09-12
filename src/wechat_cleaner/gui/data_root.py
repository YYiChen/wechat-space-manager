"""Finding the WeChat data root without asking the user to type it.

Sources, in order of trust:

1. what the user last connected to successfully (``settings.json``)
2. the conventional ``Documents``-style locations
3. a shallow sweep of the fixed drives, which is the only thing that finds an
   install like ``C:\\!D\\xwechat_files``

A candidate only counts when it really holds an account layout (a directory
with ``db_storage``), and when several qualify the one with the most database
bytes wins: a stale partial copy under ``Documents`` is common and must never
shadow the real install.
"""

from __future__ import annotations

import ctypes
import os
import string
from collections.abc import Iterable
from pathlib import Path

from ..real_db import discover_accounts
from .settings import remembered_data_root

__all__ = [
    "CONVENTIONAL_ROOTS",
    "best_root",
    "find_data_root",
    "fixed_drive_letters",
    "shallow_drive_candidates",
]

CONVENTIONAL_ROOTS: tuple[Path, ...] = (
    Path.home() / "Documents" / "xwechat_files",
    Path.home() / "Documents" / "WeChat Files",
    Path("C:/xwechat_files"),
)

# Directory names WeChat uses for its file store (4.x, then 3.x).
_ROOT_DIRNAMES = ("xwechat_files", "WeChat Files")

# GetDriveTypeW return values we care about.
_DRIVE_FIXED = 3


def best_root(candidates: Iterable[Path | str]) -> str | None:
    """Rank candidates by total database bytes; unusable ones are skipped.

    Every candidate is discovered for real, because existence alone says
    nothing: a 77 MB stale copy and a 1.3 GB live account are both "a
    directory with db_storage", and only the byte count tells them apart.
    """
    best: str | None = None
    best_bytes = -1
    for candidate in candidates:
        try:
            if not Path(candidate).is_dir():
                continue
            accounts = discover_accounts(str(candidate))
        except Exception:  # noqa: BLE001 - a broken candidate must not abort the probe
            continue
        if not accounts:
            continue
        total = sum(account.database_bytes for account in accounts)
        if total > best_bytes:
            best_bytes = total
            best = str(candidate)
    return best


def fixed_drive_letters() -> tuple[str, ...]:
    """Letters of locally attached fixed disks (never remote or optical).

    An empty optical drive or an unreachable network share can block for
    seconds inside ``is_dir()``, so the drive *type* is queried through the
    Win32 API and the filesystem is never touched for drives we skip.  Other
    platforms simply get no sweep.
    """
    if os.name != "nt":
        return ()
    try:
        kernel32 = ctypes.windll.kernel32
        mask = kernel32.GetLogicalDrives()
        drive_type = kernel32.GetDriveTypeW
    except (AttributeError, OSError):
        return ()
    letters: list[str] = []
    for index, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << index):
            continue
        try:
            if drive_type(f"{letter}:\\") == _DRIVE_FIXED:
                letters.append(letter)
        except OSError:
            continue
    return tuple(letters)


def shallow_drive_candidates(drives: Iterable[Path] | None = None) -> tuple[Path, ...]:
    """``<drive>:\\xwechat_files`` and ``<drive>:\\<dir>\\xwechat_files``.

    WeChat lets the file directory live anywhere; a real install at
    ``C:\\!D\\xwechat_files`` is invisible to a conventional probe, which is
    exactly the case that made the user retype the path on every launch.  The
    sweep stays one directory deep, so it costs a couple of dozen stats per
    drive instead of walking a whole disk.

    ``drives`` overrides the detected drive list, which keeps the sweep
    testable without depending on the test machine's disk layout.
    """
    if drives is None:
        roots = tuple(Path(f"{letter}:/") for letter in fixed_drive_letters())
    else:
        roots = tuple(drives)
    found: list[Path] = []
    for drive in roots:
        try:
            entries = sorted(drive.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    found.extend(entry / name for name in _ROOT_DIRNAMES)
            except OSError:
                continue
        found.extend(drive / name for name in _ROOT_DIRNAMES)
    return tuple(found)


def find_data_root(override: str | None = None) -> str | None:
    """Best root for this launch: explicit, then remembered, then probed.

    The conventional locations and the drive sweep are ranked *together*: the
    sweep finds installs outside ``Documents``, and ranking stops a stale copy
    from winning merely by being probed first.
    """
    if override:
        return override
    remembered = remembered_data_root()
    if remembered:
        return remembered
    return best_root((*CONVENTIONAL_ROOTS, *shallow_drive_candidates()))
