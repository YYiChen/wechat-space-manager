"""Video covers: first-frame extraction and the adapter cache path."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

pytest.importorskip("imageio_ffmpeg", reason="video covers need the bundled ffmpeg")

from wechat_cleaner.domain.contracts import DecodeVariant, MediaType  # noqa: E402
from wechat_cleaner.real_media.video import video_clip_pngs, video_frame_png  # noqa: E402

VIDEO_DIR = "msg/video/2026-02"


def _write_real_mp4(target: Path) -> Path:
    """A tiny but genuine mp4, encoded by the same ffmpeg the product uses."""
    import imageio_ffmpeg

    target.parent.mkdir(parents=True, exist_ok=True)
    writer = imageio_ffmpeg.write_frames(
        str(target), (64, 48), fps=12, pix_fmt_out="yuv420p"
    )
    writer.send(None)
    for index in range(16):
        shade = 8 * index
        writer.send(bytes([shade % 256, 120, 200]) * (64 * 48))
    writer.close()
    assert target.is_file() and target.stat().st_size > 0
    return target


def test_video_frame_png_returns_a_decodable_image(tmp_path):
    import io

    from PIL import Image

    clip = _write_real_mp4(tmp_path / "clip.mp4")

    encoded = video_frame_png(clip, max_edge=48)

    assert encoded[:8] == b"\x89PNG\r\n\x1a\n"
    with Image.open(io.BytesIO(encoded)) as cover:
        cover.load()
        assert cover.width <= 48 and cover.height <= 48


def test_video_clip_samples_frames_across_the_stream(tmp_path):
    import io

    from PIL import Image

    clip = _write_real_mp4(tmp_path / "clip.mp4")

    frames, fps = video_clip_pngs(clip, max_frames=6, max_edge=48)

    # 16 source frames sampled down to at most 6, every one a real picture.
    assert 1 < len(frames) <= 6
    for encoded in frames:
        assert encoded[:8] == b"\x89PNG\r\n\x1a\n"
        with Image.open(io.BytesIO(encoded)) as frame:
            frame.load()
            assert frame.width <= 48 and frame.height <= 48
    assert fps > 0


def test_video_clip_rejects_missing_files(tmp_path):
    from wechat_cleaner.decoder.formats import DecoderFailure

    with pytest.raises(DecoderFailure):
        video_clip_pngs(tmp_path / "gone.mp4", max_frames=4)


def test_xor_wrapped_video_decodes_through_the_key_unwrap(
    account_root, cache_root, aes_key, fake_downloader, stager, record_from_identity
):
    """Attached videos wrapped in image-style XOR ``.dat`` still get a cover.

    The plain ffmpeg read fails on the wrapped bytes; the backend retries by
    unwrapping with the session image keys into a cache-side temp (the source
    file itself is never modified).
    """
    from wechat_cleaner.real_media import RealImageKeys, RealMediaBackend

    xor_key = 0x5A
    plain = _write_real_mp4(account_root / "plain.mp4").read_bytes()
    wrapped = bytes(value ^ xor_key for value in plain)
    identity = stager("msg/attach/aa11/2026-02/Video/wrapped.dat", wrapped)
    staged_path = account_root / "msg/attach/aa11/2026-02/Video/wrapped.dat"
    backend = RealMediaBackend(
        account_root=str(account_root),
        cache_root=str(cache_root),
        keys=RealImageKeys(aes_key=aes_key, xor_key=xor_key),
        downloader=fake_downloader,
    )
    record = record_from_identity(identity, media_type=MediaType.VIDEO)

    artifact = backend.decode_image(record)

    output = Path(cache_root) / artifact.output_file.relative_path
    assert output.is_file()
    assert staged_path.read_bytes() == wrapped, "the source must stay wrapped"
    assert list(Path(cache_root).rglob("video-plain-*.mp4")) == [], "temp is cleaned up"


def test_video_cover_goes_through_the_adapter_cache(
    account_root, cache_root, aes_key, fake_downloader, stager, record_from_identity
):
    from wechat_cleaner.real_media import RealImageKeys, RealMediaBackend

    clip = _write_real_mp4(Path(account_root) / VIDEO_DIR / "clip.mp4")
    identity = stager(f"{VIDEO_DIR}/clip.mp4", clip.read_bytes())
    backend = RealMediaBackend(
        account_root=str(account_root),
        cache_root=str(cache_root),
        keys=RealImageKeys(aes_key=aes_key, xor_key=0x88),
        downloader=fake_downloader,
    )
    session_id = uuid.uuid4()
    record = record_from_identity(identity, media_type=MediaType.VIDEO)

    artifact = backend.decode_image(record, cache_session_id=session_id)

    assert artifact.variant is DecodeVariant.THUMBNAIL
    output = Path(cache_root) / artifact.output_file.relative_path
    assert output.is_file()
    # Same (session, media, variant, edge) key: a cache hit, not a second
    # ffmpeg run.
    again = backend.decode_image(record, cache_session_id=session_id)
    assert again.output_file.relative_path == artifact.output_file.relative_path
