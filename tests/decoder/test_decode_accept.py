"""Acceptance paths: decode synthetic copies end to end.

Helpers arrive via pytest fixtures from ``conftest.py`` (directory-scoped, no
cross-module imports: flat test directories share the module namespace).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

from PIL import Image

from wechat_cleaner.decoder import decode
from wechat_cleaner.decoder.cache import purge_expired, usage_bytes
from wechat_cleaner.domain.contracts import DecodeVariant

ACCOUNT_ID = "wxid_synthetic_alpha"
IMAGE_COLOR = (200, 30, 40)
SINGLE_BYTE_KEY = b"\x5a"
MULTI_BYTE_KEY = bytes(range(32))
RELATIVE_STEM = "msg/attach/8a8b0c0d0e0f10111213141516171819/2026-02/Image/alpha_001"


def test_plain_png_roundtrip(request_factory, stager, cache_root, copy_root, png_factory):
    identity = stager(f"{RELATIVE_STEM}.png", png_factory())
    artifact = decode(request_factory(identity), copy_root=str(copy_root))

    assert artifact.output_format == "PNG"
    assert artifact.decoder_id == "plain-image"
    # FileIdentity normalizes separators to Windows backslashes (contract behaviour).
    assert PureWindowsPath(artifact.output_file.relative_path).parts[0] == "sessions"

    output_path = Path(cache_root) / artifact.output_file.relative_path
    assert output_path.is_file()
    with Image.open(output_path) as rendered:
        rendered.load()
        assert rendered.size == (48, 32)
        assert rendered.getpixel((0, 0)) == IMAGE_COLOR

    sidecar = output_path.with_suffix(".png.artifact.json")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["media_id"] == str(artifact.media_id)
    assert payload["output_file"]["relative_path"] == artifact.output_file.relative_path


def test_xor_single_byte_auto_derivation(
    request_factory, stager, cache_root, copy_root, png_factory, xor_factory
):
    identity = stager(f"{RELATIVE_STEM}.dat", xor_factory(png_factory(), SINGLE_BYTE_KEY))
    artifact = decode(request_factory(identity), copy_root=str(copy_root))

    assert artifact.decoder_id == "xor-png"
    assert artifact.output_file.relative_path.endswith(".png")


def test_xor_multi_byte_with_key_provider(
    request_factory, stager, cache_root, copy_root, png_factory, xor_factory
):
    identity = stager(f"{RELATIVE_STEM}.dat", xor_factory(png_factory(), MULTI_BYTE_KEY))
    artifact = decode(
        request_factory(identity),
        copy_root=str(copy_root),
        key_provider=lambda: MULTI_BYTE_KEY,
    )

    assert artifact.decoder_id == "xor-png"


def test_thumbnail_is_downscaled(request_factory, stager, cache_root, copy_root, png_factory):
    identity = stager(f"{RELATIVE_STEM}.png", png_factory(width=96, height=64))
    artifact = decode(
        request_factory(identity, variant=DecodeVariant.THUMBNAIL),
        copy_root=str(copy_root),
    )

    output_path = Path(cache_root) / artifact.output_file.relative_path
    with Image.open(output_path) as thumb:
        thumb.load()
        assert max(thumb.size) <= 24
        assert thumb.size == (24, 16)  # 96x64 scaled to fit 24px edge, aspect kept


def test_shared_cache_session_groups_outputs(
    request_factory, stager, cache_root, copy_root, png_factory
):
    session_id = uuid.uuid4()
    first = stager(f"{RELATIVE_STEM}-a.png", png_factory(color=(10, 20, 30)))
    second = stager(f"{RELATIVE_STEM}-b.png", png_factory(color=(30, 20, 10)))
    artifact_a = decode(
        request_factory(first), copy_root=str(copy_root), cache_session_id=session_id
    )
    artifact_b = decode(
        request_factory(second), copy_root=str(copy_root), cache_session_id=session_id
    )

    assert artifact_a.cache_session_id == session_id == artifact_b.cache_session_id
    assert PureWindowsPath(artifact_a.output_file.relative_path).parts[1] == str(session_id)
    assert PureWindowsPath(artifact_b.output_file.relative_path).parts[1] == str(session_id)


def test_expired_session_is_purged(request_factory, stager, cache_root, copy_root, png_factory):
    identity = stager(f"{RELATIVE_STEM}.png", png_factory())
    stale_until = datetime.now(UTC) - timedelta(hours=1)
    decode(request_factory(identity, retain_until=stale_until), copy_root=str(copy_root))

    assert usage_bytes(cache_root) > 0
    assert purge_expired(cache_root, datetime.now(UTC)) == 1
    assert usage_bytes(cache_root) == 0


def test_artifacts_never_embed_account_or_absolute_paths(
    request_factory, stager, cache_root, copy_root, png_factory
):
    identity = stager(f"{RELATIVE_STEM}.png", png_factory())
    artifact = decode(request_factory(identity), copy_root=str(copy_root))

    output_path = Path(cache_root) / artifact.output_file.relative_path
    for candidate in (output_path, output_path.with_suffix(".png.artifact.json")):
        blob = candidate.read_bytes()
        assert str(copy_root.resolve()).encode() not in blob
        assert str(cache_root).encode() not in blob
        assert ACCOUNT_ID.encode() not in blob
