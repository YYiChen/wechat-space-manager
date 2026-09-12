"""Offscreen acceptance tests for the selection and safety-gated flow."""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("PySide6")

from wechat_cleaner.gui.services import FakeApplicationFacade
from wechat_cleaner.gui.window import MainWindow


def _wait(qtbot, predicate, timeout: int = 5000) -> None:
    qtbot.waitUntil(predicate, timeout=timeout)


def _loaded_window(qtbot) -> MainWindow:
    window = MainWindow(FakeApplicationFacade())
    qtbot.addWidget(window)
    _wait(qtbot, lambda: window._filter_result is not None)
    return window


def test_selection_plan_preflight_dry_run_and_recovery(qtbot) -> None:
    window = _loaded_window(qtbot)
    window.select_button.click()
    _wait(qtbot, lambda: window._selection is not None)
    assert window.plan_button.isEnabled()

    window.plan_button.click()
    _wait(qtbot, lambda: window._plan is not None)
    window.preflight_button.click()
    _wait(qtbot, lambda: window._preflight is not None)
    assert window._preflight.can_execute
    window.dry_run_button.click()
    _wait(qtbot, lambda: window._receipt is not None)
    assert window.confirm_button.isEnabled()
    window.confirm_button.click()
    _wait(qtbot, lambda: window.active_task_name is None)
    assert "未执行任何文件操作" in window.execution_summary.text()
    window.recovery_button.click()
    _wait(qtbot, lambda: "试运行" in window.execution_summary.text())


def test_filter_change_invalidates_confirmation(qtbot) -> None:
    window = _loaded_window(qtbot)
    window.select_button.click()
    _wait(qtbot, lambda: window._selection is not None)
    window.plan_button.click()
    _wait(qtbot, lambda: window._plan is not None)
    window.preflight_button.click()
    _wait(qtbot, lambda: window._preflight is not None)
    window.dry_run_button.click()
    _wait(qtbot, lambda: window._receipt is not None)
    assert window.confirm_button.isEnabled()

    window.add_condition("new condition")
    assert not window.confirm_button.isEnabled()
    assert window._plan is None


def test_gui_has_no_executor_or_file_mutation_imports() -> None:
    import wechat_cleaner.gui.services as services
    import wechat_cleaner.gui.window as window

    combined = inspect.getsource(services) + inspect.getsource(window)
    for forbidden in ("wechat_cleaner.executor", "os.remove", "Path.unlink", "shutil.move"):
        assert forbidden not in combined
