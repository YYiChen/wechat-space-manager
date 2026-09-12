"""Tests for remembering and auto-detecting the WeChat data root.

Nothing here touches a real WeChat install: the probes run against synthetic
``db_storage`` trees under ``tmp_path``, and the settings file is redirected to
a temporary path through the documented environment override.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wechat_cleaner.gui import data_root, settings


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch) -> Path:
    """Point the settings store at a throwaway file for every test."""
    target = tmp_path / "config" / "settings.json"
    monkeypatch.setenv(settings.ENV_OVERRIDE, str(target))
    return target


def _make_root(base: Path, account: str = "wxid_synthetic_alpha", size: int = 16) -> Path:
    """Create a directory that looks like a WeChat data root."""
    target = base / account / "db_storage" / "message"
    target.mkdir(parents=True)
    (target / "message_0.db").write_bytes(b"\x00" * size)
    return base


# ----------------------------------------------------------------------
# settings storage
# ----------------------------------------------------------------------
def test_settings_round_trip(_isolated_settings: Path):
    assert settings.load_settings() == {}
    stored = r"C:\somewhere\xwechat_files"

    assert settings.remember_session(stored, "wxid_alpha") is True

    assert _isolated_settings.is_file()
    assert settings.load_settings() == {"data_root": stored, "account_id": "wxid_alpha"}
    assert settings.remembered_account() == "wxid_alpha"
    # That path does not exist on the test machine, so the *usable* accessor
    # rejects it even though the raw value is stored.
    assert settings.remembered_data_root() is None


def test_remembered_root_is_offered_while_it_still_exists(tmp_path: Path):
    real = _make_root(tmp_path / "xwechat_files")

    settings.remember_session(str(real), "wxid_alpha")

    assert settings.remembered_data_root() == str(real)


def test_remembered_root_is_ignored_once_it_disappears(tmp_path: Path):
    gone = tmp_path / "unplugged" / "xwechat_files"
    settings.remember_session(str(gone), "wxid_alpha")

    assert settings.remembered_data_root() is None
    # The account survives: only the unusable path is rejected.
    assert settings.remembered_account() == "wxid_alpha"


def test_a_corrupt_settings_file_reads_as_empty(_isolated_settings: Path):
    _isolated_settings.parent.mkdir(parents=True, exist_ok=True)
    _isolated_settings.write_text("{not json at all", encoding="utf-8")

    assert settings.load_settings() == {}
    assert settings.remembered_data_root() is None


def test_an_empty_root_is_never_remembered():
    assert settings.remember_session("   ") is False


# ----------------------------------------------------------------------
# probe ranking
# ----------------------------------------------------------------------
def test_best_root_ranks_by_database_bytes(tmp_path: Path):
    stale = _make_root(tmp_path / "documents" / "xwechat_files", size=16)
    real = _make_root(tmp_path / "install" / "xwechat_files", size=8192)

    # The stale copy is probed first and must still lose.
    assert data_root.best_root((stale, real)) == str(real)
    assert data_root.best_root((real, stale)) == str(real)


def test_best_root_skips_missing_and_non_account_dirs(tmp_path: Path):
    empty = tmp_path / "xwechat_files"
    empty.mkdir()
    (empty / "all_users").mkdir()  # not an account layout
    good = _make_root(tmp_path / "good" / "xwechat_files")

    assert data_root.best_root((tmp_path / "absent", empty)) is None
    assert data_root.best_root((tmp_path / "absent", empty, good)) == str(good)


# ----------------------------------------------------------------------
# the shallow drive sweep
# ----------------------------------------------------------------------
def test_sweep_finds_an_install_below_a_drive_root(tmp_path: Path):
    # Layout of the real complaint: <drive>/<unusual dir>/xwechat_files
    custom = _make_root(tmp_path / "!D" / "xwechat_files", size=4096)
    _make_root(tmp_path / "not_a_root", size=16)  # a directory that is not one

    candidates = data_root.shallow_drive_candidates((tmp_path,))

    assert custom in candidates
    assert tmp_path / "xwechat_files" in candidates
    assert data_root.best_root(candidates) == str(custom)


def test_sweep_survives_an_unreadable_drive(tmp_path: Path):
    missing = tmp_path / "Z-does-not-exist"

    assert data_root.shallow_drive_candidates((missing,)) == ()


# ----------------------------------------------------------------------
# what find_data_root prefers
# ----------------------------------------------------------------------
def test_find_data_root_prefers_an_explicit_override(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(data_root, "remembered_data_root", lambda: str(tmp_path / "remembered"))

    assert data_root.find_data_root(str(tmp_path / "explicit")) == str(tmp_path / "explicit")


def test_find_data_root_prefers_what_worked_last_time(tmp_path: Path, monkeypatch):
    remembered = tmp_path / "remembered" / "xwechat_files"
    monkeypatch.setattr(data_root, "remembered_data_root", lambda: str(remembered))
    probed: list = []

    def fake_best(candidates):
        probed.append(tuple(candidates))
        return "probed"

    monkeypatch.setattr(data_root, "best_root", fake_best)

    assert data_root.find_data_root() == str(remembered)
    assert probed == [], "a remembered root must skip the disk sweep entirely"


def test_find_data_root_probes_when_nothing_is_remembered(tmp_path: Path, monkeypatch):
    good = _make_root(tmp_path / "swept" / "xwechat_files", size=2048)
    monkeypatch.setattr(data_root, "remembered_data_root", lambda: None)
    monkeypatch.setattr(data_root, "CONVENTIONAL_ROOTS", ())
    monkeypatch.setattr(data_root, "shallow_drive_candidates", lambda: (good,))

    assert data_root.find_data_root() == str(good)
