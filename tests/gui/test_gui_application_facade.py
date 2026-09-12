"""Synthetic offscreen flow through the real application facade adapter."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from wechat_cleaner.gui.services import (
    ApplicationFacadeAdapter,
    DesktopFacadeConfig,
    FakeApplicationFacade,
    create_application_facade_from_args,
)
from wechat_cleaner.gui.window import MainWindow


def _fixture_builder():
    path = Path(__file__).resolve().parents[1] / "fixtures_synthetic" / "build_phase3_fixture.py"
    spec = importlib.util.spec_from_file_location("gui_phase3_fixture_builder", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load Phase 3 fixture builder")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _wait(qtbot, predicate, timeout: int = 5000) -> None:
    qtbot.waitUntil(predicate, timeout=timeout)


def test_production_facade_is_default_and_demo_is_explicit() -> None:
    production = create_application_facade_from_args([])
    demo = create_application_facade_from_args(["--demo"])
    try:
        assert isinstance(production, ApplicationFacadeAdapter)
        assert isinstance(demo, FakeApplicationFacade)
    finally:
        production.close()


def test_real_application_facade_adapter_drives_safe_flow(qtbot, tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    _fixture_builder().build_phase3_fixture(fixture, seed=20260912)
    facade = ApplicationFacadeAdapter(
        DesktopFacadeConfig(
            index_path=tmp_path / "index.sqlite",
            source_root=fixture / "accounts",
            database_copy=fixture / "databases" / "synthetic-v1.sqlite",
            account_id="wxid_synthetic_alpha_p3",
        )
    )
    window = MainWindow(facade)
    qtbot.addWidget(window)
    _wait(qtbot, lambda: window._filter_result is not None)
    assert window._filter_result.summary.total_count > 0

    window.select_button.click()
    _wait(qtbot, lambda: window._selection is not None)
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
    assert "未配置受控恢复目录" in window.execution_summary.text()
    assert "未执行任何文件操作" in window.status_label.text()
