"""Cache control: status accounting, expiry purge and limit reporting."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wechat_cleaner.ui import cache_status, over_limit, purge_expired, reclaimable_after_purge


def _make_session(cache_root: str, session_id: str, files: dict[str, bytes]) -> Path:
    directory = Path(cache_root) / "sessions" / session_id
    directory.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        (directory / name).write_bytes(payload)
    return directory


def _sidecar(directory: Path, expires_at: datetime | None) -> None:
    payload = {"expires_at": expires_at.isoformat() if expires_at else None}
    target = directory / "x_original.png.artifact.json"
    target.write_text(json.dumps(payload), encoding="utf-8")


def test_empty_cache_reports_zeroes(cache_root):
    status = cache_status(cache_root)
    assert status.total_bytes == 0
    assert status.file_count == 0
    assert status.session_count == 0


def test_status_counts_sessions_and_files(cache_root):
    _make_session(cache_root, "s1", {"a.png": b"x" * 100, "a.png.artifact.json": b"{}"})
    _make_session(cache_root, "s2", {"b.png": b"y" * 50})
    status = cache_status(cache_root)
    assert status.total_bytes == 152  # 100 + 2 (sidecar "{}") + 50
    assert status.file_count == 3
    assert status.session_count == 2


def test_purge_expired_removes_only_expired(cache_root):
    now = datetime.now(UTC)
    stale = _make_session(cache_root, "stale", {"a.png": b"x" * 40})
    _sidecar(stale, now - timedelta(hours=1))
    fresh = _make_session(cache_root, "fresh", {"b.png": b"y" * 60})
    _sidecar(fresh, now + timedelta(hours=1))

    removed = purge_expired(cache_root, now)
    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()
    status = cache_status(cache_root)
    expected = 60 + (fresh / "x_original.png.artifact.json").stat().st_size
    assert status.total_bytes == expected
    assert status.session_count == 1


def test_reclaimable_after_purge_dry_run(cache_root):
    now = datetime.now(UTC)
    stale = _make_session(cache_root, "stale", {"a.png": b"x" * 40})
    _sidecar(stale, now - timedelta(minutes=5))
    _make_session(cache_root, "fresh", {"b.png": b"y" * 60})

    # A purge removes whole sessions; the dry run accounts the stale session
    # directory in full (payload + sidecar) and leaves fresh untouched.
    expected = 40 + (stale / "x_original.png.artifact.json").stat().st_size
    assert reclaimable_after_purge(cache_root, now) == expected
    assert stale.exists()


def test_over_limit_boundary(cache_root):
    _make_session(cache_root, "s1", {"a.png": b"x" * 100})
    assert over_limit(cache_root, 99) is True
    assert over_limit(cache_root, 100) is False
