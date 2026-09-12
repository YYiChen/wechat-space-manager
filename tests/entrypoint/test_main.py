from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest

from wechat_cleaner import __main__ as entrypoint


def test_help_does_not_try_to_import_gui(capsys):
    assert entrypoint.main(["--help"]) == 0
    assert "legacy" in capsys.readouterr().out


def test_legacy_mode_delegates_without_gui(monkeypatch):
    seen: list[list[str]] = []

    def fake_legacy(argv):
        seen.append(argv)
        return 7

    monkeypatch.setattr("wechat_cleaner.ui.cli.main", fake_legacy)
    assert entrypoint.main(["legacy", "browse", "--json"]) == 7
    assert seen == [["browse", "--json"]]


def test_legacy_alias_delegates(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(
        "wechat_cleaner.ui.cli.main",
        lambda argv: seen.append(argv) or 0,
    )
    assert entrypoint.main(["--legacy-cli", "cache", "--cache-root", "cache"]) == 0
    assert seen == [["cache", "--cache-root", "cache"]]


def test_missing_gui_is_actionable(monkeypatch, capsys):
    def missing_gui(name: str):
        raise ModuleNotFoundError(name="wechat_cleaner.gui")

    monkeypatch.setattr(entrypoint.importlib, "import_module", missing_gui)
    assert entrypoint.main([]) == 2
    assert "P3-GUI-SHELL/P3-GUI-FLOW" in capsys.readouterr().err


def test_gui_module_receives_arguments(monkeypatch):
    seen: list[list[str]] = []
    fake_module = types.SimpleNamespace(main=lambda argv: seen.append(argv) or 3)
    monkeypatch.setattr(entrypoint.importlib, "import_module", lambda name: fake_module)
    assert entrypoint.main(["--gui", "--offscreen"]) == 3
    assert seen == [["--offscreen"]]


def test_no_argument_launches_production_gui_offscreen(tmp_path):
    pytest.importorskip("PySide6")
    environment = os.environ.copy()
    environment.update(
        {
            "QT_QPA_PLATFORM": "offscreen",
            "LOCALAPPDATA": str(tmp_path / "localappdata"),
        }
    )
    code = (
        "from PySide6.QtCore import QTimer; "
        "from PySide6.QtWidgets import QApplication; "
        "from wechat_cleaner.__main__ import main; "
        "app=QApplication([]); QTimer.singleShot(300, app.quit); "
        "raise SystemExit(main([]))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr[-2_000:]
