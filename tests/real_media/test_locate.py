"""Read-only location and availability classification."""

from __future__ import annotations

import pytest

from wechat_cleaner.decoder.formats import DecoderFailure
from wechat_cleaner.domain.contracts import ContractErrorCode, MediaType
from wechat_cleaner.real_media import MediaAvailability, availability, locate, resolve_source_path

IMAGE_DIR = "msg/attach/8a8b0c0d/2026-02/Img"


def test_original_file_is_available(account_root, stager, record_from_identity, image_factory):
    identity = stager(f"{IMAGE_DIR}/abc123.dat", image_factory())
    state = availability(account_root, record_from_identity(identity))

    assert state is MediaAvailability.ORIGINAL_AVAILABLE


def test_thumbnail_only_when_original_missing(
    account_root, stager, record_from_identity, image_factory
):
    identity = stager(f"{IMAGE_DIR}/abc123_t.dat", image_factory())
    state = availability(account_root, record_from_identity(identity))

    assert state is MediaAvailability.THUMBNAIL_ONLY


def test_original_available_when_sibling_exists(
    account_root, stager, record_from_identity, image_factory
):
    identity = stager(f"{IMAGE_DIR}/abc123_t.dat", image_factory())
    stager(f"{IMAGE_DIR}/abc123.dat", image_factory(color=(10, 20, 30)))
    state = availability(account_root, record_from_identity(identity))

    assert state is MediaAvailability.ORIGINAL_AVAILABLE


def test_thumbnail_sibling_falls_back_to_wide_variant(account_root, stager, image_factory):
    from wechat_cleaner.real_media.locate import thumbnail_sibling

    stager(f"{IMAGE_DIR}/abc_t_W.dat", image_factory())
    found = thumbnail_sibling(account_root, f"{IMAGE_DIR}/abc.dat")

    assert found is not None and found.name.endswith("_t_W.dat")


def test_thumbnail_sibling_returns_none_without_variants(account_root, stager, image_factory):
    from wechat_cleaner.real_media.locate import thumbnail_sibling

    stager(f"{IMAGE_DIR}/abc.dat", image_factory())

    assert thumbnail_sibling(account_root, f"{IMAGE_DIR}/abc.dat") is None


def test_missing_when_file_absent(account_root, record_from_identity, image_factory):
    from wechat_cleaner.domain.contracts import FileIdentity

    identity = FileIdentity(
        relative_path=f"{IMAGE_DIR}/nothere.dat", byte_size=10, modified_time_ns=1
    )
    state = availability(account_root, record_from_identity(identity))

    assert state is MediaAvailability.MISSING


def test_explicit_thumbnail_media_type_is_thumbnail_only(
    account_root, stager, record_from_identity, image_factory
):
    identity = stager(f"{IMAGE_DIR}/plain.dat", image_factory())
    record = record_from_identity(identity, media_type=MediaType.THUMBNAIL)

    assert availability(account_root, record) is MediaAvailability.THUMBNAIL_ONLY


def test_video_record_availability(account_root, stager, record_from_identity):
    identity = stager("msg/video/2026-02/clip.mp4", b"\x00\x00\x00\x18ftypmp42")
    record = record_from_identity(identity, media_type=MediaType.VIDEO)

    assert availability(account_root, record) is MediaAvailability.ORIGINAL_AVAILABLE


def test_locate_reports_size_and_thumbnail_flag(
    account_root, stager, record_from_identity, image_factory
):
    payload = image_factory()
    identity = stager(f"{IMAGE_DIR}/loc_t.dat", payload)
    located = locate(account_root, record_from_identity(identity))

    assert located.exists is True
    assert located.byte_size == len(payload)
    assert located.is_thumbnail is True


def test_path_escape_is_rejected(account_root):
    with pytest.raises(DecoderFailure) as exc_info:
        resolve_source_path(account_root, "..\\..\\outside\\secret.dat")
    assert exc_info.value.code is ContractErrorCode.INVALID_PATH
