"""Offscreen tests for the first-run disclosure dialogs.

Modal dialogs are driven by arming one-shot timers that click a named button
on whatever ``QMessageBox`` is on screen; the nested event loop inside
``QMessageBox.exec`` delivers them.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from wechat_cleaner.gui import disclaimer
from wechat_cleaner.gui.disclaimer import (
    DISCLAIMER_VERSION,
    ensure_disclaimer_accepted,
    is_disclaimer_accepted,
)
from wechat_cleaner.gui.settings import ENV_OVERRIDE, load_settings


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Point settings.json at a disposable file for one test."""
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "settings.json"))
    monkeypatch.delenv(disclaimer.SKIP_ENV_VAR, raising=False)
    return tmp_path


def _click_first_found(texts: list[str]) -> None:
    """Click the first visible message-box button whose text is in ``texts``."""
    app = QApplication.instance()
    assert app is not None
    for widget in app.topLevelWidgets():
        if not isinstance(widget, QMessageBox) or not widget.isVisible():
            continue
        for button in widget.findChildren(QPushButton):
            if button.text() in texts and button.isEnabled():
                button.click()
                return
    raise AssertionError(f"no visible button among {texts!r}")


def test_accepted_version_skips_dialogs(qtbot, isolated_settings):
    from wechat_cleaner.gui.settings import save_settings

    assert save_settings({"disclaimer_accepted_version": DISCLAIMER_VERSION}) is True
    assert is_disclaimer_accepted() is True

    # No timers armed: any dialog would hang the test past this line.
    assert ensure_disclaimer_accepted() is True


def test_skip_env_bypasses_dialogs(qtbot, isolated_settings, monkeypatch):
    monkeypatch.setenv(disclaimer.SKIP_ENV_VAR, "1")

    assert ensure_disclaimer_accepted() is True


def test_stale_version_reshows_dialogs(qtbot, isolated_settings):
    from wechat_cleaner.gui.settings import save_settings

    assert save_settings({"disclaimer_accepted_version": DISCLAIMER_VERSION - 1}) is True
    assert is_disclaimer_accepted() is False

    QTimer.singleShot(400, lambda: _click_first_found(["继续"]))
    QTimer.singleShot(900, lambda: _click_first_found(["确认，仅本次"]))

    assert ensure_disclaimer_accepted() is True
    # "仅本次" must not persist.
    assert load_settings().get("disclaimer_accepted_version", 0) < DISCLAIMER_VERSION


def test_remember_persists_and_second_call_skips(qtbot, isolated_settings):
    QTimer.singleShot(400, lambda: _click_first_found(["继续"]))
    QTimer.singleShot(900, lambda: _click_first_found(["确认且今后不再弹出"]))

    assert ensure_disclaimer_accepted() is True
    assert load_settings().get("disclaimer_accepted_version") == DISCLAIMER_VERSION
    assert is_disclaimer_accepted() is True
    assert ensure_disclaimer_accepted() is True


def test_quit_on_first_dialog_returns_false(qtbot, isolated_settings):
    QTimer.singleShot(400, lambda: _click_first_found(["退出软件"]))

    assert ensure_disclaimer_accepted() is False


def test_quit_on_risk_dialog_returns_false(qtbot, isolated_settings):
    QTimer.singleShot(400, lambda: _click_first_found(["继续"]))
    QTimer.singleShot(900, lambda: _click_first_found(["退出软件"]))

    assert ensure_disclaimer_accepted() is False
    assert is_disclaimer_accepted() is False


def test_corrupt_settings_reads_as_not_accepted(qtbot, isolated_settings):
    from wechat_cleaner.gui.settings import settings_path

    settings_path().write_text("{not json", encoding="utf-8")

    assert is_disclaimer_accepted() is False
