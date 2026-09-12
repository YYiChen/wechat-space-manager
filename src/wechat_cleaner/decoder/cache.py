"""Bounded, inspectable decode cache.

Cache layout under ``cache_root``::

    cache_root/
        sessions/<cache_session_id>/<media_id>_<variant>.<ext>
        sessions/<cache_session_id>/<media_id>_<variant>.artifact.json

Rules enforced here:

* every output path is verified to stay inside ``cache_root``;
* total cache usage can be measured so the pipeline can enforce limits;
* artifacts carry ``expires_at`` and expired sessions are purged on demand;
* writes are atomic (temporary file + ``os.replace``).

The cache never stores key material, WeChat account paths, or source file
paths: only contract payloads built from relative, privacy-safe fields.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from wechat_cleaner.domain.contracts import ContractErrorCode, DecodeVariant

from .formats import DecoderFailure

SESSIONS_DIRNAME = "sessions"


def session_dir(cache_root: str | Path, cache_session_id: uuid.UUID) -> Path:
    return Path(cache_root) / SESSIONS_DIRNAME / str(cache_session_id)


def output_stem(media_id: uuid.UUID, variant: DecodeVariant) -> str:
    return f"{media_id}_{variant.value}"


def ensure_within_cache(cache_root: str | Path, candidate: str | Path) -> Path:
    """Resolve *candidate* and refuse anything escaping ``cache_root``."""
    root = Path(cache_root).resolve()
    resolved = Path(candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise DecoderFailure(
            ContractErrorCode.INVALID_PATH,
            "cache output path escapes the approved cache root",
        )
    return resolved


def usage_bytes(cache_root: str | Path) -> int:
    sessions_root = Path(cache_root) / SESSIONS_DIRNAME
    total = 0
    if not sessions_root.exists():
        return 0
    for dirpath, _dirnames, filenames in os.walk(sessions_root):
        for filename in filenames:
            try:
                total += (Path(dirpath) / filename).stat().st_size
            except OSError:  # pragma: no cover - racing deletions
                continue
    return total


def purge_expired(cache_root: str | Path, now: datetime) -> int:
    """Delete sessions whose artifacts expired strictly before *now*."""
    sessions_root = Path(cache_root) / SESSIONS_DIRNAME
    if not sessions_root.exists():
        return 0
    removed = 0
    for directory in sorted(sessions_root.iterdir()):
        if not directory.is_dir():
            continue
        expired = False
        for sidecar in directory.glob("*.artifact.json"):
            try:
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            expires_at = payload.get("expires_at")
            if not expires_at:
                continue
            try:
                expiry = datetime.fromisoformat(expires_at)
            except ValueError:
                continue
            if expiry.tzinfo is None:
                expiry = expiry.astimezone()
            if expiry <= now:
                expired = True
                break
        if expired:
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
    return removed


def atomic_write(path: Path, data: bytes) -> None:
    """Write *data* to *path* atomically within the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():  # pragma: no branch - only true on failure
            temporary.unlink(missing_ok=True)
