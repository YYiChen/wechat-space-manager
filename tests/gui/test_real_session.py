"""Read-only facade tests (no Qt required).

The facade is exercised with an injected fake session so no real WeChat data,
key or upstream package is needed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from wechat_cleaner.domain.contracts import (
    DecodeArtifact,
    DecodeVariant,
    FileIdentity,
    MappingConfidence,
    MediaRecord,
    MediaType,
)
from wechat_cleaner.gui.real_session import (
    CacheStatus,
    FacadeError,
    FakeRealSessionFacade,
    PreviewOutcome,
    RealSessionFacade,
    resolve_preview_variant,
)

ACCOUNT_ROOT = "C:\\synthetic\\xwechat_files\\wxid_synthetic_alpha\\db_storage"


def _record(media_type: MediaType = MediaType.IMAGE, size: int = 1024) -> MediaRecord:
    return MediaRecord(
        account_id="wxid_synthetic_alpha",
        file=FileIdentity(
            relative_path=f"msg/attach/8a8b/2026-02/Img/{uuid4().hex}.dat",
            byte_size=size,
            modified_time_ns=1,
        ),
        media_type=media_type,
        observed_at=datetime(2026, 2, 1, tzinfo=UTC),
        mapping_confidence=MappingConfidence.UNMAPPED,
        mapping_reason="filesystem scan only",
    )


class FakeBackend:
    """Stand-in for RealMediaBackend behind the facade's export path."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[MediaRecord] = []

    def export_original(self, record: MediaRecord):
        self.calls.append(record)
        if self.fail:
            raise _decode_error("DECODE_FAILED", "source media could not be read")
        from wechat_cleaner.real_media import MediaFormat
        from wechat_cleaner.real_media.adapter import ExportedOriginal

        return ExportedOriginal(
            payload=b"jpg-bytes",
            image_format="JPEG",
            width=64,
            height=48,
            is_original=True,
            container=MediaFormat.V2,
            relative_path=record.file.relative_path,
        )


class FakeSession:
    """Stand-in for RealReadOnlySession used by the facade tests."""

    def __init__(
        self,
        *,
        records: tuple[MediaRecord, ...],
        cache_root: str,
        fail_preview=False,
        export_fail=False,
    ):
        self._records = records
        self.cache_root = cache_root
        self.fail_preview = fail_preview
        self.closed = False
        self.cleared_decoded = 0
        self.preview_variants: list[str] = []
        self._backend = FakeBackend(fail=export_fail)

    def database_summary(self) -> dict:
        return {"database_count": 35, "database_bytes": 1_381_945_344,
                "keyed_count": 27, "unkeyed_count": 8, "account_count": 1}

    def records(self, limit: int | None = None, progress=None):
        return self._records if limit is None else self._records[:limit]

    def preview(self, record, variant=DecodeVariant.THUMBNAIL, max_edge_px=256):
        self.preview_variants.append(variant.value)
        if self.fail_preview:
            raise _decode_error("DECODE_FAILED", "source container not supported")
        return DecodeArtifact(
            request_id=uuid4(),
            media_id=record.media_id,
            created_at=datetime.now(UTC),
            cache_session_id=uuid4(),
            variant=variant,
            output_file=FileIdentity(
                relative_path=f"sessions/demo/{record.media_id}.png",
                byte_size=2048,
                modified_time_ns=1,
            ),
            output_format="PNG",
            decoder_id="fake-session",
        )

    def manifest(self):
        class _Manifest:
            decoded_cache_files = ("sessions/demo/a.png",)
            key_cache_relative_paths = ("db-cache/keys.json",)

            def to_anonymous_dict(self):
                return {"session_version": "real-session-1", "preview_count": 1}

        return _Manifest()

    def clear_decoded_cache(self) -> int:
        self.cleared_decoded += 1
        return 1

    def close(self) -> None:
        self.closed = True


def _decode_error(code: str, message: str) -> Exception:
    from wechat_cleaner.domain.contracts import ContractError, ContractErrorCode

    class _Err(Exception):
        def __init__(self) -> None:
            super().__init__(message)
            self.error = ContractError(code=ContractErrorCode(code), message=message)

    return _Err()


@pytest.fixture
def facade(tmp_path: Path):
    cache_root = str(tmp_path / "cache")
    Path(cache_root).mkdir()
    (Path(cache_root) / "sessions" / "demo").mkdir(parents=True)
    (Path(cache_root) / "sessions" / "demo" / "a.png").write_bytes(b"\x89PNG data")
    (Path(cache_root) / "db-cache").mkdir()
    (Path(cache_root) / "db-cache" / "keys.json").write_text("{}", encoding="utf-8")
    records = (_record(), _record(MediaType.THUMBNAIL, 2048), _record(MediaType.VIDEO, 4096))
    f = RealSessionFacade(
        session_factory=lambda **kwargs: FakeSession(records=records, cache_root=cache_root)
    )
    f.connect("C:\\synthetic\\xwechat_files", cache_root, "wxid_synthetic_alpha")
    return f


def test_connect_requires_session_before_reads():
    fresh = RealSessionFacade(session_factory=lambda **_: FakeSession(records=(), cache_root=""))

    with pytest.raises(FacadeError):
        fresh.database_summary()


def test_discover_reports_facade_error_on_missing_root(tmp_path: Path):
    facade = RealSessionFacade()

    with pytest.raises(FacadeError):
        facade.discover(str(tmp_path / "nope"))


def test_discover_uses_real_discovery(tmp_path: Path):
    root = tmp_path / "xwechat_files"
    for name, size in (("wxid_alpha_d729", 4096), ("wxid_beta_1111", 256)):
        target = root / name / "db_storage" / "message"
        target.mkdir(parents=True)
        (target / "message_0.db").write_bytes(b"\x00" * size)
    facade = RealSessionFacade()

    options = facade.discover(str(root))

    assert len(options) == 2
    assert sum(1 for option in options if option.is_largest) == 1


def test_load_records_and_availability(facade):
    records = facade.load_records()

    assert len(records) == 3
    assert facade.record(records[0].media_id) is records[0]


def test_preview_success_reports_artifact_path(facade):
    record = facade.load_records()[1]

    outcome = facade.preview(record)

    assert isinstance(outcome, PreviewOutcome)
    assert outcome.ok is True
    assert outcome.variant == "thumbnail"
    assert outcome.output_path.endswith(".png")


def test_smart_variant_upgrades_small_pictures_only():
    small = _record(MediaType.IMAGE, 512 * 1024)
    big = _record(MediaType.IMAGE, 4 * 1024 * 1024)
    small_thumbnail = _record(MediaType.THUMBNAIL, 512 * 1024)

    # A small picture is shown at original quality ...
    assert resolve_preview_variant(small, "auto") is DecodeVariant.ORIGINAL
    # ... a big one keeps the thumbnail so the wait stays short ...
    assert resolve_preview_variant(big, "auto") is DecodeVariant.THUMBNAIL
    # ... a "缩略图" record *is* the small version, so there is nothing to
    # upgrade, and an unknown choice degrades instead of raising.
    assert resolve_preview_variant(small_thumbnail, "auto") is DecodeVariant.THUMBNAIL
    assert resolve_preview_variant(small, "original") is DecodeVariant.ORIGINAL
    assert resolve_preview_variant(small, "thumbnail") is DecodeVariant.THUMBNAIL
    assert resolve_preview_variant(small, "nonsense") is DecodeVariant.THUMBNAIL


def test_auto_variant_flows_through_the_facade(facade):
    small_image = facade.load_records()[0]

    outcome = facade.preview(small_image, variant="auto")

    assert outcome.ok is True
    assert outcome.variant == "original"


def test_preview_failure_reports_contract_code(tmp_path: Path):
    cache_root = str(tmp_path / "cache")
    Path(cache_root).mkdir()
    records = (_record(),)
    facade = RealSessionFacade(
        session_factory=lambda **_: FakeSession(records=records, cache_root=cache_root,
                                                fail_preview=True)
    )
    facade.connect("C:\\synthetic\\xwechat_files", cache_root)

    outcome = facade.preview(records[0])

    assert outcome.ok is False
    assert outcome.error_code == "DECODE_FAILED"
    assert "not supported" in outcome.error_message


def test_cache_status_counts_files_and_manifests(facade):
    status = facade.cache_status()

    assert isinstance(status, CacheStatus)
    assert status.file_count == 2  # a.png + keys.json
    assert status.decoded_count == 1
    assert status.key_cache_count == 1


def test_clear_decoded_cache_delegates(facade):
    assert facade.clear_decoded_cache() == 1


def test_clear_all_local_cache_removes_only_cache_root(facade, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive", encoding="utf-8")

    removed = facade.clear_all_local_cache()

    assert removed == 2
    assert outside.read_text(encoding="utf-8") == "must survive"
    assert not (Path(facade.cache_root) / "sessions").exists()


def test_manifest_is_anonymous(facade):
    payload = facade.manifest()

    assert json.dumps(payload).find("wxid") == -1


def test_fake_facade_covers_the_same_surface():
    fake = FakeRealSessionFacade(records=(_record(), _record(MediaType.VIDEO, 512)))
    options = fake.discover("C:\\synthetic")
    assert len(options) == 2
    fake.connect("C:\\synthetic", "C:\\synthetic-cache")
    assert len(fake.load_records()) == 2
    assert fake.preview(fake.load_records()[1]).ok is False  # video unsupported
    assert fake.cache_status().decoded_count == 3
    assert fake.clear_decoded_cache() == 3
    assert fake.clear_all_local_cache() == 43
    fake.close()


def test_fake_facade_load_records_accepts_and_reports_progress():
    fake = FakeRealSessionFacade(records=(_record(), _record(MediaType.VIDEO, 512)))
    fake.connect("C:\\synthetic", "C:\\synthetic-cache")

    calls: list[int] = []
    records = fake.load_records(progress=calls.append)

    assert len(records) == 2
    assert calls == [2]
    # No progress requested: no calls, unchanged records.
    assert fake.load_records() == records


def test_export_original_delegates_to_backend(facade):
    records = facade.load_records()

    outcome = facade.export_original(records[0])

    assert outcome.ok is True
    assert outcome.payload == b"jpg-bytes"
    assert outcome.image_format == "JPEG"
    assert (outcome.width, outcome.height) == (64, 48)
    assert outcome.is_original is True
    assert outcome.byte_size == len(b"jpg-bytes")
    assert facade._session._backend.calls == [records[0]]


def test_export_original_reports_backend_failure(facade):
    records = facade.load_records()
    facade._session._backend.fail = True

    outcome = facade.export_original(records[0])

    assert outcome.ok is False
    assert outcome.error_code == "DECODE_FAILED"
    assert "could not be read" in outcome.error_message


def test_fake_facade_export_original_rejects_non_image():
    fake = FakeRealSessionFacade(records=(_record(), _record(MediaType.VIDEO, 512)))
    fake.connect("C:\\synthetic", "C:\\synthetic-cache")

    exported = fake.export_original(fake.load_records()[0])
    refused = fake.export_original(fake.load_records()[1])

    assert exported.ok is True
    assert exported.payload == b"fake-export-bytes"
    assert refused.ok is False
    assert "不是图片" in refused.error_message
