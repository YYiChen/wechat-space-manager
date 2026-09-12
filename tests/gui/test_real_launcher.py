"""Launcher tests: conventional data-root probe and entry-point shape.

The Qt event loop is never started here; only the probe and the window wiring
are exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from wechat_cleaner.gui import launcher


def _make_root(base: Path, account: str = "wxid_synthetic_alpha") -> Path:
    target = base / account / "db_storage" / "message"
    target.mkdir(parents=True)
    (target / "message_0.db").write_bytes(b"\x00" * 16)
    return base


def test_probe_finds_conventional_layout(tmp_path: Path, monkeypatch):
    root = _make_root(tmp_path / "xwechat_files")
    monkeypatch.setattr(launcher, "_DEFAULT_ROOT_CANDIDATES", (root,))

    assert launcher.discover_default_data_root() == str(root)


def test_probe_returns_none_when_no_account_layout(tmp_path: Path, monkeypatch):
    empty = tmp_path / "xwechat_files"
    empty.mkdir()
    (empty / "all_users").mkdir()  # not an account layout
    monkeypatch.setattr(launcher, "_DEFAULT_ROOT_CANDIDATES", (empty,))

    assert launcher.discover_default_data_root() is None


def test_probe_skips_unreadable_candidates(tmp_path: Path, monkeypatch):
    missing = tmp_path / "absent"
    good = _make_root(tmp_path / "good")
    monkeypatch.setattr(launcher, "_DEFAULT_ROOT_CANDIDATES", (missing, good))

    assert launcher.discover_default_data_root() == str(good)


def test_probe_prefers_root_with_largest_accounts(tmp_path: Path, monkeypatch):
    # A stale partial copy (Documents-style) must lose to the real install,
    # even when it appears first in the candidate list.
    stale = _make_root(tmp_path / "stale", account="wxid_synthetic_stale")
    (stale / "wxid_synthetic_stale" / "db_storage" / "message" / "message_0.db").write_bytes(
        b"\x00" * 16
    )
    real = _make_root(tmp_path / "real", account="wxid_synthetic_primary")
    (real / "wxid_synthetic_primary" / "db_storage" / "message" / "message_0.db").write_bytes(
        b"\x00" * 4096
    )
    monkeypatch.setattr(launcher, "_DEFAULT_ROOT_CANDIDATES", (stale, real))

    assert launcher.discover_default_data_root() == str(real)


def test_launch_and_main_are_callable():
    assert callable(launcher.launch)
    assert callable(launcher.main)
