"""Launcher for the real read-only desktop window.

Entry point for the Phase 4 Beta: one command opens a window that discovers the
local WeChat data root, lets the user pick an account and previews media without
any capability to modify the WeChat source directory.

Wiring this into ``python -m wechat_cleaner`` (or the packaged executable) is an
orchestrator-owned change to ``__main__.py``; this module provides the callable
entry point and the data-root probe.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..real_db import discover_accounts  # noqa: F401 - re-exported for callers
from . import data_root as _data_root
from .real_session import FakeRealSessionFacade, RealSessionFacade
from .settings import remember_session, remember_thumbnail_size

__all__ = [
    "discover_any_data_root",
    "discover_default_data_root",
    "find_data_root",
    "launch",
    "main",
    "parse_launch_args",
]

# Conventional locations only.  Custom roots are found by the drive sweep, or
# typed by the user as a one-off (and then remembered).
_DEFAULT_ROOT_CANDIDATES: tuple[Path, ...] = _data_root.CONVENTIONAL_ROOTS

find_data_root = _data_root.find_data_root


def discover_any_data_root() -> str | None:
    """Conventional locations *and* the shallow fixed-drive sweep, ranked.

    This is the probe that finds an install outside ``Documents`` (for example
    ``C:\\!D\\xwechat_files``) while still preferring the real account over a
    stale partial copy.
    """
    return _data_root.best_root(
        (*_DEFAULT_ROOT_CANDIDATES, *_data_root.shallow_drive_candidates())
    )


def discover_default_data_root() -> str | None:
    """Return the conventional data root holding the largest account set.

    Several candidate roots can legitimately exist (an old partial copy under
    Documents next to the real install on another drive); picking the *first*
    hit once pre-filled a 77 MB stale copy and misled the user.  Each candidate
    is discovered for real and ranked by total database bytes, so the genuine
    primary install wins.
    """
    return _data_root.best_root(_DEFAULT_ROOT_CANDIDATES)


def parse_launch_args(argv: list[str] | None) -> tuple[bool, str | None]:
    """Return ``(demo_mode, data_root_override)``.

    ``--demo`` selects the in-memory fake facade (documented in the README) so
    the window can be shown without any real WeChat data.  ``--data-root <path>``
    pre-fills the discovery field for a non-conventional install location.
    Unknown flags are ignored rather than fatal: this is a desktop app, and a
    typo should not prevent the window from opening.
    """
    args = list(argv or [])
    demo = False
    data_root: str | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--demo":
            demo = True
        elif token == "--data-root" and index + 1 < len(args):
            data_root = args[index + 1]
            index += 1
        elif token.startswith("--data-root="):
            data_root = token.split("=", 1)[1]
        index += 1
    return demo, data_root


def launch(argv: list[str] | None = None) -> int:
    """Create the Qt application and show the read-only window."""
    from PySide6.QtWidgets import QApplication

    from .real_window import RealReadOnlyWindow
    from .settings import remembered_account, remembered_thumbnail_size

    demo, data_root_override = parse_launch_args(argv)

    app = QApplication.instance() or QApplication(argv or [])
    from .disclaimer import ensure_disclaimer_accepted

    if not ensure_disclaimer_accepted():
        return 0
    from .real_window import _app_icon_file

    icon_file = _app_icon_file()
    if icon_file is not None:
        from PySide6.QtGui import QIcon

        app.setWindowIcon(QIcon(str(icon_file)))
    facade = FakeRealSessionFacade() if demo else RealSessionFacade()
    window = RealReadOnlyWindow(facade)
    # Persist whatever the user gets working, so the next launch opens the way
    # they left it: same data root, same album tile size.
    window.session_connected.connect(remember_session)
    window.thumbnail_size_changed.connect(remember_thumbnail_size)

    saved_size = remembered_thumbnail_size()
    if saved_size:
        window.set_thumb_size(saved_size)

    detected = find_data_root(data_root_override)
    if detected:
        window.set_data_root(detected)
        window.detect_accounts()
        account = remembered_account()
        if account and window.prefer_account(account):
            window.statusBar().showMessage("已自动检测到数据目录与上次的账号，点击「连接」继续")
        else:
            window.statusBar().showMessage("已自动检测到数据目录，请选择账号后点击「连接」")

    window.show()
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    """Script entry point (``wechat-cleaner-real`` style)."""
    return launch(list(argv) if argv is not None else sys.argv[:1])


if __name__ == "__main__":  # pragma: no cover - manual launch
    raise SystemExit(main())
