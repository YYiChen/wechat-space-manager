"""Frozen-executable entry point for the read-only Windows Beta.

Why a dedicated bootstrap instead of ``python -m wechat_cleaner``:

* inside a PyInstaller bundle ``sys.argv`` handling and the module-search path
  differ from a source checkout, so we pin the import path here;
* the Beta must **only** ever open the read-only real-session window.  Any
  cleanup/plan/execute path is deliberately unreachable from this file.  The
  executor and cleanup packages are excluded at build time, so even a coding
  mistake in the GUI cannot reach them here.

Kept intentionally tiny: it imports the GUI launcher and hands over control.

Self-check mode
---------------
Setting ``WCSM_SELF_TEST=1`` makes the frozen binary open the window offscreen,
collect the button labels it exposes and exit instead of entering the event
loop.  ``scripts/verify_package.py`` uses this to prove, on the *frozen
artifact*, that no delete/cleanup entry point is present.  The flag is inert
unless the environment variable is set.
"""

from __future__ import annotations

import os
import sys


def _clipped_buttons(window) -> list[str]:
    """Labels of visible buttons the user cannot reach.

    A control is fine when it sits inside the visible central widget, or when
    the axis it overflows on belongs to a scrollable column.  Anything else is
    the "buttons vanished below the fold" bug (measured cause: the window's own
    minimum height exceeded the screen).
    """
    from PySide6.QtWidgets import QAbstractButton, QScrollArea

    central = window.centralWidget()
    clipped: list[str] = []
    for button in window.findChildren(QAbstractButton):
        if not button.isVisible():
            continue
        top_left = button.mapTo(central, button.rect().topLeft())
        right = top_left.x() + button.width()
        bottom = top_left.y() + button.height()
        overflow_v = top_left.y() < -1 or bottom > central.height() + 1
        overflow_h = top_left.x() < -1 or right > central.width() + 1
        if not (overflow_v or overflow_h):
            continue
        node = button.parentWidget()
        area = None
        while node is not None:
            if isinstance(node, QScrollArea):
                area = node
                break
            node = node.parentWidget()
        if area is None:
            clipped.append(button.text())
            continue
        if overflow_v and area.verticalScrollBar().maximum() <= 0:
            clipped.append(button.text())
            continue
        if overflow_h and area.horizontalScrollBar().maximum() <= 0:
            clipped.append(button.text())
    return clipped


def _self_test() -> int:
    """Open the read-only window offscreen and report its interactive surface."""
    import json

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QAbstractButton, QApplication

    from wechat_cleaner.gui import RealSessionFacade
    from wechat_cleaner.gui.real_window import RealReadOnlyWindow

    app = QApplication(["self-test"])
    window = RealReadOnlyWindow(RealSessionFacade())
    report: dict[str, object] = {"ok": False}

    def inspect() -> None:
        report["title"] = window.windowTitle()
        report["visible"] = window.isVisible()
        report["buttons"] = [b.text() for b in window.findChildren(QAbstractButton)]
        # Layout contract (F-20260914): the window must be shrinkable, and no
        # control may sit outside the visible area without a scrollable column
        # to reach it.  Both were broken when the preview column's own minimum
        # (716px) pushed the window minimum past the screen height.
        minimum = window.minimumSizeHint()
        report["minimum_size"] = [minimum.width(), minimum.height()]
        window.resize(1024, 640)
        app.processEvents()
        report["resized_to"] = [window.width(), window.height()]
        report["clipped_buttons"] = _clipped_buttons(window)
        report["ok"] = (
            minimum.height() <= 700
            and report["resized_to"][1] == 640
            and not report["clipped_buttons"]
        )
        app.quit()

    window.show()
    QTimer.singleShot(1200, inspect)
    app.exec()

    # Emit ASCII-safe JSON: the parent process may be reading through a legacy
    # Windows code page, and a decode error there would fail the whole gate.
    payload = json.dumps(report, ensure_ascii=True)
    sys.stdout.write(payload + "\n")
    sys.stdout.flush()
    return 0 if report["ok"] else 1


def main() -> int:
    if os.environ.get("WCSM_SELF_TEST") == "1":
        return _self_test()

    # The spec adds ``src/`` to pathex, but a frozen bundle resolves packages
    # from the archive; this import works in both source and frozen modes.
    from wechat_cleaner.gui.launcher import launch

    return launch(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
