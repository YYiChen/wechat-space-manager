"""Decoder test fixtures.

Everything here is synthetic: images are generated with Pillow, encryption is a
deterministic repeating-key XOR, and no real WeChat payload, path, or key is
involved (see ``AGENTS.md``私密数据 rules).
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wechat_cleaner.domain.contracts import (
    DecodeRequest,
    DecodeVariant,
    FileIdentity,
    MappingConfidence,
    MediaRecord,
    MediaType,
)

ACCOUNT_ID = "wxid_synthetic_alpha"
IMAGE_SIZE = (48, 32)
IMAGE_COLOR = (200, 30, 40)
SINGLE_BYTE_KEY = b"\x5a"
MULTI_BYTE_KEY = bytes(range(32))
SENTINEL_BLOB = bytes(range(1, 255)) * 4  # deliberately not an image


def make_png_bytes(
    width: int = IMAGE_SIZE[0], height: int = IMAGE_SIZE[1], color=IMAGE_COLOR
) -> bytes:
    from PIL import Image  # noqa: PLC0415 - lazy import

    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def xor_obfuscate(data: bytes, key: bytes) -> bytes:
    return bytes(value ^ key[index % len(key)] for index, value in enumerate(data))


def stage_copy(copy_root: Path, relative_path: str, payload: bytes) -> FileIdentity:
    target = copy_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    stat = target.stat()
    return FileIdentity(
        relative_path=relative_path,
        byte_size=stat.st_size,
        modified_time_ns=stat.st_mtime_ns,
    )


def make_media_record(
    identity: FileIdentity,
    media_type: MediaType = MediaType.IMAGE,
    account_id: str = ACCOUNT_ID,
) -> MediaRecord:
    return MediaRecord(
        account_id=account_id,
        file=identity,
        media_type=media_type,
        observed_at=datetime.now(UTC),
        mapping_confidence=MappingConfidence.HIGH,
        mapping_reason="synthetic fixture mapping",
    )


def make_decode_request(
    media: MediaRecord,
    variant: DecodeVariant = DecodeVariant.ORIGINAL,
    cache_root: str = "",
    retain_until: datetime | None = None,
) -> DecodeRequest:
    return DecodeRequest(
        media=media,
        variant=variant,
        cache_root=cache_root,
        max_edge_px=24 if variant is DecodeVariant.THUMBNAIL else None,
        retain_until=retain_until,
    )


@pytest.fixture
def copy_root(tmp_path: Path) -> Path:
    root = tmp_path / "staging"
    root.mkdir()
    return root


@pytest.fixture
def cache_root(tmp_path: Path) -> Path:
    root = tmp_path / "cache"
    root.mkdir()
    return str(root)


@pytest.fixture
def png_factory():
    return make_png_bytes


@pytest.fixture
def xor_factory():
    return xor_obfuscate


@pytest.fixture
def stager(copy_root: Path):
    def _stage(relative_path: str, payload: bytes) -> FileIdentity:
        return stage_copy(copy_root, relative_path, payload)

    return _stage


@pytest.fixture
def file_stager():
    """Stage a copy under an arbitrary root the caller owns."""

    def _stage(root: Path, relative_path: str, payload: bytes) -> FileIdentity:
        return stage_copy(root, relative_path, payload)

    return _stage


@pytest.fixture
def request_factory(cache_root: str):
    def _request(
        identity: FileIdentity,
        media_type: MediaType = MediaType.IMAGE,
        variant: DecodeVariant = DecodeVariant.ORIGINAL,
        account_id: str = ACCOUNT_ID,
        retain_until: datetime | None = None,
    ) -> DecodeRequest:
        media = make_media_record(identity, media_type=media_type, account_id=account_id)
        return make_decode_request(
            media, variant=variant, cache_root=cache_root, retain_until=retain_until
        )

    return _request
