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
        report["ok"] = True
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
