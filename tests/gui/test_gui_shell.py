"""Offscreen acceptance tests for the PySide6 desktop shell."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from wechat_cleaner.gui.services import FacadeError, FakeApplicationFacade
from wechat_cleaner.gui.window import MainWindow


def _wait(qtbot, predicate, timeout: int = 5000) -> None:
    qtbot.waitUntil(predicate, timeout=timeout)


def test_navigation_and_condition_editor(qtbot) -> None:
    window = MainWindow(FakeApplicationFacade())
    qtbot.addWidget(window)
    _wait(qtbot, lambda: window._filter_result is not None)

    window.navigate("设置/缓存")
    assert window.current_page_name == "设置/缓存"
    window.navigate("精确筛选")
    window.add_condition("联系人 = synthetic_contact_00")
    assert window.conditions_list.count() == 1
    window.remove_condition(0)
    assert window.conditions_list.count() == 0


def test_pagination_and_sort_controls(qtbot) -> None:
    window = MainWindow(FakeApplicationFacade())
    qtbot.addWidget(window)
    _wait(qtbot, lambda: window._filter_result is not None)
    assert window.results_table.rowCount() == 5
    assert window.next_page_button.isEnabled()
    window.next_page()
    _wait(qtbot, lambda: window._page_index == 1 and window._filter_result is not None)
    assert window.results_table.rowCount() == 5
    window.previous_page()
    _wait(qtbot, lambda: window._page_index == 0)
    window.sort_combo.setCurrentIndex(1)
    window.apply_filter_button.click()
    _wait(qtbot, lambda: window._filter_result is not None)
    assert window._filter_spec.sort_by.value == "message_time"


def test_details_and_preview_stay_behind_the_facade(qtbot) -> None:
    window = MainWindow(FakeApplicationFacade())
    qtbot.addWidget(window)
    _wait(qtbot, lambda: window._filter_result is not None)
    window.results_table.selectRow(0)
    assert "详情" in window.record_details.text()
    window.preview_button.click()
    _wait(qtbot, lambda: "预览" in window.preview_summary.text())


def test_cancelled_task_does_not_publish_result(qtbot) -> None:
    class SlowFacade(FakeApplicationFacade):
        def load_records(self):
            time.sleep(0.4)
            return super().load_records()

    window = MainWindow(SlowFacade())
    qtbot.addWidget(window)
    window.cancel_active_task()
    _wait(qtbot, lambda: window.active_task_name is None, timeout=3000)
    assert "取消" in window.status_label.text()


def test_error_recovers_on_retry(qtbot) -> None:
    class FlakyFacade(FakeApplicationFacade):
        failed = False

        def query(self, spec, *, cursor=None):
            if not self.failed:
                self.failed = True
                raise FacadeError("合成查询失败")
            return super().query(spec, cursor=cursor)

    window = MainWindow(FlakyFacade())
    qtbot.addWidget(window)
    _wait(qtbot, lambda: window.active_task_name is None)
    window.facade.failed = False
    window.apply_filter_button.click()
    _wait(qtbot, lambda: "失败" in window.status_label.text())
    window.apply_filter_button.click()
    _wait(qtbot, lambda: window._filter_result is not None)
    assert window.results_table.rowCount() > 0
