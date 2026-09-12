"""Cache control: inspect usage, purge expired sessions, report limits.

All mutation is delegated to the decoder's own cache API (``purge_expired``);
this layer adds visibility only and never touches files outside the decode
cache root.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from wechat_cleaner.decoder.cache import purge_expired as _purge_expired
from wechat_cleaner.decoder.cache import usage_bytes

SESSIONS_DIRNAME = "sessions"


@dataclass(frozen=True, slots=True)
class CacheStatus:
    """Aggregate cache facts for display; relative structure only."""

    total_bytes: int
    file_count: int
    session_count: int


def cache_status(cache_root: str | Path) -> CacheStatus:
    sessions_root = Path(cache_root) / SESSIONS_DIRNAME
    total_bytes = usage_bytes(cache_root)
    file_count = 0
    session_count = 0
    if sessions_root.exists():
        for directory in sessions_root.iterdir():
            if not directory.is_dir():
                continue
            session_count += 1
            file_count += sum(1 for item in directory.iterdir() if item.is_file())
    return CacheStatus(
        total_bytes=total_bytes, file_count=file_count, session_count=session_count
    )


def purge_expired(cache_root: str | Path, now: datetime | None = None) -> int:
    """Remove expired sessions via the decoder cache API."""
    return _purge_expired(cache_root, now or datetime.now(UTC))


def over_limit(cache_root: str | Path, max_bytes: int) -> bool:
    return usage_bytes(cache_root) > max_bytes


def reclaimable_after_purge(cache_root: str | Path, now: datetime | None = None) -> int:
    """Bytes currently held by sessions that would be purged (dry run)."""
    sessions_root = Path(cache_root) / SESSIONS_DIRNAME
    if not sessions_root.exists():
        return 0
    moment = now or datetime.now(UTC)
    total = 0
    for directory in sessions_root.iterdir():
        if not directory.is_dir():
            continue
        expired = False
        for sidecar in directory.glob("*.artifact.json"):
            payload = _read_expires_at(sidecar)
            if payload is not None and payload <= moment:
                expired = True
                break
        if expired:
            total += sum(item.stat().st_size for item in directory.iterdir() if item.is_file())
    return total


def _read_expires_at(sidecar: Path) -> datetime | None:
    import json

    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8")).get("expires_at")
        if not raw:
            return None
        parsed = datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo else parsed.astimezone()
    except (OSError, json.JSONDecodeError, ValueError):
        return None
