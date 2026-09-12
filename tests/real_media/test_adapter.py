"""Backend orchestration: availability gates, cache writes and privacy invariants."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from wechat_cleaner.decoder.pipeline import DecodeError, DecoderLimits
from wechat_cleaner.domain.contracts import DecodeVariant, MediaType
from wechat_cleaner.real_media import RealImageKeys, RealMediaBackend

IMAGE_DIR = "msg/attach/8a8b0c0d/2026-02/Img"


def _backend(account_root, cache_root, aes_key, downloader, **kwargs) -> RealMediaBackend:
    return RealMediaBackend(
        account_root=str(account_root),
        cache_root=str(cache_root),
        keys=RealImageKeys(aes_key=aes_key, xor_key=0x88),
        downloader=downloader,
        **kwargs,
    )


def test_decode_image_writes_artifact(
    account_root, cache_root, aes_key, fake_downloader,
    image_factory, dat_factory, stager, record_from_identity,
):
    payload = image_factory(width=80, height=60)
    identity = stager(f"{IMAGE_DIR}/orig.dat", dat_factory(payload, aes_key))
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)

    artifact = backend.decode_image(record_from_identity(identity))

    assert artifact.decoder_id == "real-media-1"
    assert artifact.output_format == "PNG"
    output = Path(cache_root) / artifact.output_file.relative_path
    assert output.is_file()
    assert artifact.output_file.byte_size == output.stat().st_size

    from PIL import Image

    with Image.open(output) as rendered:
        rendered.load()
        assert rendered.size == (80, 60)


def test_original_request_falls_back_to_thumbnail(
    account_root, cache_root, aes_key, fake_downloader,
    image_factory, dat_factory, stager, record_from_identity,
):
    """Only a _t.dat exists, so an ORIGINAL request reports thumbnail truthfully."""
    identity = stager(f"{IMAGE_DIR}/onlythumb_t.dat", dat_factory(image_factory(), aes_key))
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)

    artifact = backend.decode_image(
        record_from_identity(identity), variant=DecodeVariant.ORIGINAL
    )

    assert artifact.variant is DecodeVariant.THUMBNAIL


def test_missing_source_is_rejected(
    account_root, cache_root, aes_key, fake_downloader, record_from_identity
):
    from wechat_cleaner.domain.contracts import FileIdentity

    identity = FileIdentity(
        relative_path=f"{IMAGE_DIR}/gone.dat", byte_size=5, modified_time_ns=1
    )
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)

    with pytest.raises(DecodeError) as exc_info:
        backend.decode_image(record_from_identity(identity))
    assert exc_info.value.error.code.value == "DECODE_FAILED"


def test_non_picture_media_is_not_decodable(
    account_root, cache_root, aes_key, fake_downloader, stager, record_from_identity
):
    """文件/语音没有图片可解；损坏的视频则报 DECODE_FAILED（ffmpeg读过但解不出）。"""
    identity = stager("msg/file/2026-02/doc.pdf", b"%PDF-1.4 not an image")
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)

    with pytest.raises(DecodeError) as exc_info:
        backend.decode_image(record_from_identity(identity, media_type=MediaType.FILE))
    assert exc_info.value.error.code.value == "DECODER_UNAVAILABLE"

    broken = stager("msg/video/2026-02/clip.mp4", b"\x00\x00\x00\x18ftypmp42")
    with pytest.raises(DecodeError) as exc_info:
        backend.decode_image(record_from_identity(broken, media_type=MediaType.VIDEO))
    assert exc_info.value.error.code.value == "DECODE_FAILED"


def test_cache_limit_is_enforced(
    account_root, cache_root, aes_key, fake_downloader,
    image_factory, dat_factory, stager, record_from_identity,
):
    identity = stager(f"{IMAGE_DIR}/big.dat", dat_factory(image_factory(), aes_key))
    backend = _backend(
        account_root,
        cache_root,
        aes_key,
        fake_downloader,
        limits=DecoderLimits(max_total_cache_bytes=0, max_single_output_bytes=1 << 20),
    )

    with pytest.raises(DecodeError) as exc_info:
        backend.decode_image(record_from_identity(identity))
    assert exc_info.value.error.code.value == "CACHE_LIMIT_EXCEEDED"


def test_keys_never_reach_disk(
    account_root, cache_root, aes_key, fake_downloader,
    image_factory, dat_factory, stager, record_from_identity,
):
    identity = stager(f"{IMAGE_DIR}/privacy.dat", dat_factory(image_factory(), aes_key))
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)
    artifact = backend.decode_image(record_from_identity(identity))

    blob = json.dumps(artifact.model_dump(mode="json"))
    assert aes_key not in blob
    output_bytes = (Path(cache_root) / artifact.output_file.relative_path).read_bytes()
    assert aes_key.encode() not in output_bytes
    # The source account root must not leak into the artifact payload either.
    assert str(account_root) not in blob


def test_keys_match_validates_against_source(
    account_root, cache_root, aes_key, fake_downloader,
    image_factory, dat_factory, stager,
):
    identity = stager(f"{IMAGE_DIR}/match.dat", dat_factory(image_factory(), aes_key))
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)

    assert backend.keys_match(identity.relative_path) is True

    wrong = _backend(account_root, cache_root, "0123456789abcdef", fake_downloader)
    assert wrong.keys_match(identity.relative_path) is False
    assert wrong.keys_match(f"{IMAGE_DIR}/absent.dat") is False


def test_undecodable_original_falls_back_to_thumbnail(
    account_root, cache_root, aes_key, fake_downloader,
    image_factory, dat_factory, stager, record_from_identity,
):
    """An unknown original container degrades to the decodable _t.dat sibling."""
    original = stager(f"{IMAGE_DIR}/pair.dat", b"\x01\x02\x03\x04 unknown container \xfe")
    stager(f"{IMAGE_DIR}/pair_t.dat", dat_factory(image_factory(width=64, height=48), aes_key))
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)

    artifact = backend.decode_image(
        record_from_identity(original), variant=DecodeVariant.ORIGINAL
    )

    assert artifact.variant is DecodeVariant.THUMBNAIL
    output = Path(cache_root) / artifact.output_file.relative_path
    assert output.is_file()
    assert output.stat().st_size > 0


def test_undecodable_original_without_sibling_fails(
    account_root, cache_root, aes_key, fake_downloader, stager, record_from_identity
):
    original = stager(f"{IMAGE_DIR}/lonely.dat", b"\x01\x02\x03\x04 unknown container \xfe")
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)

    with pytest.raises(DecodeError) as exc_info:
        backend.decode_image(record_from_identity(original))
    assert exc_info.value.error.code.value == "DECODE_FAILED"


def test_close_clears_keys(account_root, cache_root, aes_key, fake_downloader):
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)
    backend.close()

    assert backend.keys.is_cleared is True


def test_availability_and_locate_delegate(
    account_root, cache_root, aes_key, fake_downloader,
    image_factory, dat_factory, stager, record_from_identity,
):
    identity = stager(f"{IMAGE_DIR}/delegate_t.dat", dat_factory(image_factory(), aes_key))
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)
    record = record_from_identity(identity)

    assert backend.availability(record).value == "thumbnail_only"
    located = backend.locate(record)
    assert located.exists is True
    assert located.is_thumbnail is True


def test_load_for_output_downscales_non_jpeg_giants():
    """PNG has no draft(); a stitched screenshot decoded full-size must still
    be shrunk before caching so the artifact stays in the preview budget."""
    import io

    from PIL import Image

    from wechat_cleaner.real_media.adapter import _load_for_output

    buffer = io.BytesIO()
    Image.new("RGB", (3000, 2000), (5, 10, 15)).save(buffer, format="PNG")

    image = _load_for_output(buffer.getvalue(), max_decode_px=1024)

    assert max(image.size) <= 1024


def test_load_for_output_keeps_small_non_jpeg_intact():
    import io

    from PIL import Image

    from wechat_cleaner.real_media.adapter import _load_for_output

    buffer = io.BytesIO()
    Image.new("RGB", (320, 240), (5, 10, 15)).save(buffer, format="PNG")

    image = _load_for_output(buffer.getvalue())

    assert image.size == (320, 240)


def test_export_original_returns_decrypted_payload(
    account_root, cache_root, aes_key, fake_downloader,
    image_factory, dat_factory, stager, record_from_identity,
):
    """Export returns the decrypted ORIGINAL bytes, never a re-encode."""
    payload = image_factory(width=80, height=60)
    identity = stager(f"{IMAGE_DIR}/orig.dat", dat_factory(payload, aes_key))
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)

    exported = backend.export_original(record_from_identity(identity))

    assert exported.payload == payload
    assert exported.image_format == "JPEG"
    assert (exported.width, exported.height) == (80, 60)
    assert exported.is_original is True


def test_export_original_refuses_non_image(
    account_root, cache_root, aes_key, fake_downloader, stager, record_from_identity
):
    identity = stager("msg/video/2026-02/clip.mp4", b"\x00\x00\x00\x18ftypmp42")
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)

    with pytest.raises(DecodeError) as exc_info:
        backend.export_original(record_from_identity(identity, media_type=MediaType.VIDEO))
    assert exc_info.value.error.code.value == "DECODER_UNAVAILABLE"


def test_export_original_refuses_missing_source(
    account_root, cache_root, aes_key, fake_downloader, record_from_identity
):
    from wechat_cleaner.domain.contracts import FileIdentity

    identity = FileIdentity(
        relative_path=f"{IMAGE_DIR}/gone.dat", byte_size=5, modified_time_ns=1
    )
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)

    with pytest.raises(DecodeError) as exc_info:
        backend.export_original(record_from_identity(identity))
    assert exc_info.value.error.code.value == "DECODE_FAILED"


# ---------------------------------------------------------------------------
# preview cache hit path: a decoded (session, media, variant, edge) artifact
# is an instant read instead of a full decrypt + re-encode
# ---------------------------------------------------------------------------

_CACHE_SESSION = uuid.uuid5(uuid.NAMESPACE_URL, "cache-hit-tests")


def test_decode_image_cache_hit_skips_redecode(
    account_root, cache_root, aes_key, fake_downloader,
    image_factory, dat_factory, stager, record_from_identity, monkeypatch,
):
    import wechat_cleaner.real_media.adapter as adapter_module

    payload = image_factory(width=80, height=60)
    identity = stager(f"{IMAGE_DIR}/orig.dat", dat_factory(payload, aes_key))
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)
    record = record_from_identity(identity)

    calls: list[int] = []
    real_decode = adapter_module.decode_image_bytes

    def counting_decode(*args, **kwargs):
        calls.append(1)
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(adapter_module, "decode_image_bytes", counting_decode)

    first = backend.decode_image(record, cache_session_id=_CACHE_SESSION)
    assert len(calls) == 1
    second = backend.decode_image(record, cache_session_id=_CACHE_SESSION)

    assert len(calls) == 1, "the second decode must be a cache hit"
    assert second.output_file == first.output_file
    assert second.media_id == record.media_id
    assert second.output_format == "PNG"


def test_decode_image_cache_key_includes_edge_px(
    account_root, cache_root, aes_key, fake_downloader,
    image_factory, dat_factory, stager, record_from_identity, monkeypatch,
):
    import wechat_cleaner.real_media.adapter as adapter_module

    payload = image_factory(width=80, height=60)
    identity = stager(f"{IMAGE_DIR}/orig.dat", dat_factory(payload, aes_key))
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)
    record = record_from_identity(identity)

    calls: list[int] = []
    real_decode = adapter_module.decode_image_bytes

    def counting_decode(*args, **kwargs):
        calls.append(1)
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(adapter_module, "decode_image_bytes", counting_decode)

    small = backend.decode_image(record, max_edge_px=256, cache_session_id=_CACHE_SESSION)
    large = backend.decode_image(record, max_edge_px=512, cache_session_id=_CACHE_SESSION)

    assert len(calls) == 2, "a different edge length must not hit the 256px cache"
    assert small.output_file.relative_path != large.output_file.relative_path


def test_decode_image_cache_is_per_session(
    account_root, cache_root, aes_key, fake_downloader,
    image_factory, dat_factory, stager, record_from_identity, monkeypatch,
):
    import wechat_cleaner.real_media.adapter as adapter_module

    payload = image_factory(width=80, height=60)
    identity = stager(f"{IMAGE_DIR}/orig.dat", dat_factory(payload, aes_key))
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)
    record = record_from_identity(identity)

    calls: list[int] = []
    real_decode = adapter_module.decode_image_bytes

    def counting_decode(*args, **kwargs):
        calls.append(1)
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(adapter_module, "decode_image_bytes", counting_decode)

    other_session = uuid.uuid5(uuid.NAMESPACE_URL, "another-session")
    backend.decode_image(record, cache_session_id=_CACHE_SESSION)
    backend.decode_image(record, cache_session_id=other_session)

    assert len(calls) == 2, "different sessions must not share cache entries"


def test_decode_image_cache_reads_legacy_name(
    account_root, cache_root, aes_key, fake_downloader,
    image_factory, dat_factory, stager, record_from_identity, monkeypatch,
):
    import wechat_cleaner.real_media.adapter as adapter_module
    from wechat_cleaner.decoder.cache import output_stem, session_dir

    payload = image_factory(width=80, height=60)
    identity = stager(f"{IMAGE_DIR}/orig.dat", dat_factory(payload, aes_key))
    backend = _backend(account_root, cache_root, aes_key, fake_downloader)
    record = record_from_identity(identity)

    calls: list[int] = []
    real_decode = adapter_module.decode_image_bytes

    def counting_decode(*args, **kwargs):
        calls.append(1)
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(adapter_module, "decode_image_bytes", counting_decode)

    first = backend.decode_image(record, cache_session_id=_CACHE_SESSION)
    # Simulate a file written before the edge suffix existed.
    target_dir = session_dir(Path(cache_root), _CACHE_SESSION)
    legacy = target_dir / f"{output_stem(record.media_id, DecodeVariant.THUMBNAIL)}.png"
    (target_dir / Path(first.output_file.relative_path).name).rename(legacy)

    second = backend.decode_image(record, cache_session_id=_CACHE_SESSION)

    assert len(calls) == 1, "the legacy-named cache file must still be read"
    # The contract normalizes separators to backslashes; compare in that form.
    assert second.output_file.relative_path.replace("\\", "/") == (
        legacy.relative_to(Path(cache_root)).as_posix()
    )


# ---------------------------------------------------------------------------
# thread safety: the upstream downloader is single-threaded native state
# ---------------------------------------------------------------------------
# The album grid warms thumbnails on a thread pool.  Every decode funnels
# through one shared upstream MediaDownloader, which wraps the SQLCipher
# connection and its own call state; concurrent use corrupted native memory and
# faulted the real Windows process with a bare access violation at first paint.


def test_concurrent_decodes_never_enter_the_downloader_together(
    account_root, cache_root, aes_key, fake_downloader,
    image_factory, dat_factory, stager, record_from_identity,
):
    import threading
    import time

    from wechat_cleaner.real_media import adapter as adapter_module

    payload = image_factory(width=64, height=48)
    records = []
    for index in range(8):
        identity = stager(
            f"{IMAGE_DIR}/concurrent-{index}.dat", dat_factory(payload, aes_key)
        )
        records.append(record_from_identity(identity))

    backend = _backend(account_root, cache_root, aes_key, fake_downloader)

    overlap: list[int] = []
    guard = threading.Lock()
    active = [0]
    real_decode = adapter_module.decode_image_bytes

    def observing_decode(*args, **kwargs):
        with guard:
            active[0] += 1
            if active[0] > 1:
                overlap.append(active[0])
        try:
            # Hold the window open long enough that a second thread would be
            # caught here if the serialisation were missing.
            time.sleep(0.02)
            return real_decode(*args, **kwargs)
        finally:
            with guard:
                active[0] -= 1

    adapter_module.decode_image_bytes = observing_decode
    try:
        threads = [
            threading.Thread(target=backend.decode_image, args=(record,))
            for record in records
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        adapter_module.decode_image_bytes = real_decode

    assert overlap == [], f"decrypt ran concurrently: saw {overlap}"
    assert len(fake_downloader.calls) == len(records)
