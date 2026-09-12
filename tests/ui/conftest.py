"""UI test fixtures.

All records are synthetic; helpers arrive through pytest fixtures so that no
test module performs cross-directory imports (flat test directories share the
module namespace).
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wechat_cleaner.domain.contracts import (
    ContactKind,
    ContactRef,
    FileIdentity,
    MappingConfidence,
    MediaRecord,
    MediaType,
)

ACCOUNT_ID = "wxid_synthetic_alpha"


def make_record(
    *,
    media_type: MediaType = MediaType.IMAGE,
    confidence: MappingConfidence = MappingConfidence.HIGH,
    contact_id: str | None = "contact_alpha",
    byte_size: int = 1024,
    observed_at: datetime | None = None,
    relative_path: str | None = None,
    regenerable: bool = False,
    modified_time_ns: int = 1_000,
    message_local_id: int | None = None,
) -> MediaRecord:
    observed = observed_at or datetime(2026, 1, 15, tzinfo=UTC)
    relative = relative_path or f"msg/attach/{Path(relative_path or 'x').stem}/2026-02/Image/a.dat"
    if confidence is MappingConfidence.EXACT and message_local_id is None:
        message_local_id = 1
    return MediaRecord(
        account_id=ACCOUNT_ID,
        file=FileIdentity(
            relative_path=relative,
            byte_size=byte_size,
            modified_time_ns=modified_time_ns,
        ),
        media_type=media_type,
        observed_at=observed,
        contact=(
            ContactRef(
                account_id=ACCOUNT_ID, contact_id=contact_id, kind=ContactKind.DIRECT
            )
            if contact_id
            else None
        ),
        mapping_confidence=confidence,
        mapping_reason="synthetic ui fixture",
        is_regenerable_cache=regenerable,
        message_local_id=message_local_id,
    )


def make_png_bytes(width: int = 32, height: int = 24, color=(90, 140, 200)) -> bytes:
    from PIL import Image  # noqa: PLC0415 - lazy import

    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def stage_copy(copy_root: Path, relative_path: str, payload: bytes) -> FileIdentity:
    target = copy_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    stat = target.stat()
    return FileIdentity(
        relative_path=relative_path, byte_size=stat.st_size, modified_time_ns=stat.st_mtime_ns
    )


@pytest.fixture
def record_factory():
    return make_record


@pytest.fixture
def png_factory():
    return make_png_bytes


@pytest.fixture
def stager(tmp_path: Path):
    root = tmp_path / "staging"
    root.mkdir()

    def _stage(relative_path: str, payload: bytes) -> FileIdentity:
        return stage_copy(root, relative_path, payload)

    _stage.root = root  # type: ignore[attr-defined]
    return _stage


@pytest.fixture
def cache_root(tmp_path: Path) -> str:
    root = tmp_path / "cache"
    root.mkdir()
    return str(root)


@pytest.fixture
def next_hour() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)
