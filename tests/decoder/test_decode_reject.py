"""Failure paths and R2 boundaries for the decoder.

Helpers arrive via pytest fixtures from ``conftest.py`` — no cross-module
imports, so flat-directory module-name collisions (e.g. other agents'
``conftest`` on ``sys.path``) cannot break collection.
"""

from __future__ import annotations

import pytest

from wechat_cleaner.decoder import DecodeError, DecoderLimits, decode
from wechat_cleaner.decoder.cache import ensure_within_cache
from wechat_cleaner.decoder.formats import DecoderFailure
from wechat_cleaner.domain.contracts import ContractErrorCode, MediaType

MULTI_BYTE_KEY = bytes(range(32))
SENTINEL_BLOB = bytes(range(1, 255)) * 4  # deliberately not an image
RELATIVE_STEM = "msg/attach/8a8b0c0d0e0f10111213141516171819/2026-02/Image/alpha_001"


def _expect_code(exc_info, code: ContractErrorCode) -> None:
    assert isinstance(exc_info.value, DecodeError)
    assert exc_info.value.error.code is code


def test_missing_copy_is_rejected(request_factory, stager, cache_root, copy_root, png_factory):
    identity = stager(f"{RELATIVE_STEM}.png", png_factory())
    request = request_factory(identity)

    # The approved copy disappears before decoding.
    (copy_root / identity.relative_path).unlink()

    with pytest.raises(DecodeError) as exc_info:
        decode(request, copy_root=str(copy_root))
    _expect_code(exc_info, ContractErrorCode.FILE_IDENTITY_MISMATCH)


def test_copy_size_mismatch(request_factory, stager, cache_root, copy_root, png_factory):
    identity = stager(f"{RELATIVE_STEM}.png", png_factory())
    request = request_factory(identity)

    # Mutate the staged copy after recording its identity.
    target = copy_root / identity.relative_path
    target.write_bytes(target.read_bytes() + b"\x00")

    with pytest.raises(DecodeError) as exc_info:
        decode(request, copy_root=str(copy_root))
    _expect_code(exc_info, ContractErrorCode.FILE_IDENTITY_MISMATCH)


def test_copy_mtime_mismatch(request_factory, stager, cache_root, copy_root, png_factory):
    import os

    identity = stager(f"{RELATIVE_STEM}.png", png_factory())
    request = request_factory(identity)

    target = copy_root / identity.relative_path
    os.utime(target, ns=(identity.modified_time_ns, identity.modified_time_ns + 1_000_000))

    with pytest.raises(DecodeError) as exc_info:
        decode(request, copy_root=str(copy_root))
    _expect_code(exc_info, ContractErrorCode.FILE_IDENTITY_MISMATCH)


def test_copy_sha256_mismatch(request_factory, stager, cache_root, copy_root, png_factory):
    import hashlib

    identity = stager(f"{RELATIVE_STEM}.png", png_factory())
    wrong_digest = hashlib.sha256(b"different").hexdigest()
    tampered = identity.model_copy(update={"sha256": wrong_digest})
    request = request_factory(tampered)

    with pytest.raises(DecodeError) as exc_info:
        decode(request, copy_root=str(copy_root))
    _expect_code(exc_info, ContractErrorCode.FILE_IDENTITY_MISMATCH)


def test_video_media_is_not_decodable(request_factory, stager, cache_root, copy_root):
    identity = stager("msg/video/2026-03/clip_001.mp4", b"\x00\x00\x00\x18ftypmp42")
    request = request_factory(identity, media_type=MediaType.VIDEO)

    with pytest.raises(DecodeError) as exc_info:
        decode(request, copy_root=str(copy_root))
    _expect_code(exc_info, ContractErrorCode.DECODER_UNAVAILABLE)


def test_sentinel_blob_without_key_is_unavailable(
    request_factory, stager, cache_root, copy_root
):
    identity = stager(f"{RELATIVE_STEM}.dat", SENTINEL_BLOB)
    request = request_factory(identity)

    with pytest.raises(DecodeError) as exc_info:
        decode(request, copy_root=str(copy_root))
    _expect_code(exc_info, ContractErrorCode.DECODER_UNAVAILABLE)


def test_multi_byte_encrypted_without_key_is_unavailable(
    request_factory, stager, cache_root, copy_root, png_factory, xor_factory
):
    identity = stager(f"{RELATIVE_STEM}.dat", xor_factory(png_factory(), MULTI_BYTE_KEY))
    request = request_factory(identity)

    with pytest.raises(DecodeError) as exc_info:
        decode(request, copy_root=str(copy_root))
    _expect_code(exc_info, ContractErrorCode.DECODER_UNAVAILABLE)


def test_wrong_key_fails_cleanly(
    request_factory, stager, cache_root, copy_root, png_factory, xor_factory
):
    identity = stager(f"{RELATIVE_STEM}.dat", xor_factory(png_factory(), MULTI_BYTE_KEY))
    request = request_factory(identity)

    with pytest.raises(DecodeError) as exc_info:
        decode(request, copy_root=str(copy_root), key_provider=lambda: bytes(range(32, 64)))
    _expect_code(exc_info, ContractErrorCode.DECODE_FAILED)


def test_corrupt_plain_image_fails(request_factory, stager, cache_root, copy_root, png_factory):
    good = png_factory()
    truncated = good[: len(good) // 2]
    identity = stager(f"{RELATIVE_STEM}.png", truncated)
    request = request_factory(identity)

    with pytest.raises(DecodeError) as exc_info:
        decode(request, copy_root=str(copy_root))
    _expect_code(exc_info, ContractErrorCode.DECODE_FAILED)


def test_total_cache_limit_enforced(request_factory, stager, cache_root, copy_root, png_factory):
    identity = stager(f"{RELATIVE_STEM}.png", png_factory())
    request = request_factory(identity)
    limits = DecoderLimits(max_total_cache_bytes=0, max_single_output_bytes=1 << 20)

    with pytest.raises(DecodeError) as exc_info:
        decode(request, copy_root=str(copy_root), limits=limits)
    _expect_code(exc_info, ContractErrorCode.CACHE_LIMIT_EXCEEDED)


def test_single_output_limit_enforced(request_factory, stager, cache_root, copy_root, png_factory):
    identity = stager(f"{RELATIVE_STEM}.png", png_factory())
    request = request_factory(identity)
    limits = DecoderLimits(max_total_cache_bytes=1 << 30, max_single_output_bytes=8)

    with pytest.raises(DecodeError) as exc_info:
        decode(request, copy_root=str(copy_root), limits=limits)
    _expect_code(exc_info, ContractErrorCode.CACHE_LIMIT_EXCEEDED)


def test_cache_output_cannot_escape_cache_root(tmp_path):
    inside = tmp_path / "cache"
    inside.mkdir()
    escape = tmp_path / "outside"
    escape.mkdir()

    with pytest.raises(DecoderFailure) as exc_info:
        ensure_within_cache(inside, str(escape / "stolen.png"))
    assert exc_info.value.code is ContractErrorCode.INVALID_PATH

    ensure_within_cache(inside, str(inside / "sessions" / "x.png"))  # must not raise


def test_account_mismatch_is_rejected_by_contract(tmp_path, file_stager, png_factory):
    """Decoder consumes MediaRecord; the contract itself blocks cross-account input."""
    from datetime import UTC, datetime

    import pydantic

    from wechat_cleaner.domain.contracts import (
        ContactKind,
        ContactRef,
        MappingConfidence,
        MediaRecord,
        MediaType,
    )

    staging = tmp_path / "staging"
    staging.mkdir()
    identity = file_stager(staging, "msg/attach/2026-02/Image/x.png", png_factory())
    outsider = ContactRef(
        account_id="wxid_someone_else",
        contact_id="contact_1",
        kind=ContactKind.DIRECT,
    )
    with pytest.raises(pydantic.ValidationError):
        MediaRecord(
            account_id="wxid_synthetic_alpha",
            file=identity,
            media_type=MediaType.IMAGE,
            observed_at=datetime.now(UTC),
            contact=outsider,
            mapping_confidence=MappingConfidence.HIGH,
            mapping_reason="synthetic fixture mapping",
        )


def test_key_is_never_persisted_in_cache(
    request_factory, stager, cache_root, copy_root, png_factory, xor_factory
):
    identity = stager(f"{RELATIVE_STEM}.dat", xor_factory(png_factory(), MULTI_BYTE_KEY))
    decode(
        request_factory(identity),
        copy_root=str(copy_root),
        key_provider=lambda: MULTI_BYTE_KEY,
    )

    from pathlib import Path

    for path in Path(cache_root).rglob("*"):
        if not path.is_file():
            continue
        blob = path.read_bytes()
        assert MULTI_BYTE_KEY not in blob
        assert MULTI_BYTE_KEY.hex().encode() not in blob
