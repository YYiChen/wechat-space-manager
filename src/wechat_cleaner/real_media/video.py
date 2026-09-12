"""Video covers: the first decodable frame, cached like a grid thumbnail.

WeChat 4.x stores videos as plain ``.mp4`` under ``msg/video/<YYYY-MM>/``.  The
album needs a cover image for them the way it needs thumbnails for photos; a
frame extracted by the bundled ffmpeg (via ``imageio-ffmpeg``, which the frozen
package already ships) is the honest stand-in — it is what the file actually
looks like, not an invented icon.

Known limitation: if the stream carries a rotation matrix the frame may be
extracted unrotated (ffmpeg's metadata is not parsed here).  A sideways cover
is still a usable cover; the alternative would be inventing nothing at all.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from wechat_cleaner.decoder.formats import DecoderFailure
from wechat_cleaner.domain.contracts import ContractErrorCode

__all__ = ["video_frame_png", "video_clip_pngs", "VIDEO_PREVIEW_FORMAT"]

VIDEO_PREVIEW_FORMAT = "PNG"
# Seek slightly into the file: frame 0 of some phone videos is black, and a
# cover of a black rectangle helps nobody.
_DEFAULT_OFFSET_SECONDS = 0.5


def video_frame_png(video_path: str | Path, *, max_edge: int = 256) -> bytes:
    """Extract one frame and return it as encoded PNG bytes.

    Raises :class:`DecoderFailure` (``DECODE_FAILED``) when ffmpeg cannot read
    the file or yields no frame — callers already speak that contract.
    """
    try:
        import imageio_ffmpeg  # imported lazily: heavy dependency, CLI-only path
    except Exception as import_error:  # noqa: BLE001 - degrade to the contract
        raise DecoderFailure(
            ContractErrorCode.DECODE_FAILED,
            f"video decoding is unavailable: {type(import_error).__name__}",
        ) from import_error

    try:
        frames = imageio_ffmpeg.read_frames(
            str(video_path),
            input_params=["-ss", f"{_DEFAULT_OFFSET_SECONDS:.2f}"],
            output_params=["-pix_fmt", "rgb24"],
        )
        try:
            meta = next(frames)
            width, height = (int(value) for value in meta["size"])
            raw = next(frames)
        finally:
            frames.close()
    except StopIteration:
        raise DecoderFailure(
            ContractErrorCode.DECODE_FAILED, "video yields no decodable frame"
        ) from None
    except DecoderFailure:
        raise
    except Exception as exc:  # noqa: BLE001 - surface as the decode contract
        raise DecoderFailure(
            ContractErrorCode.DECODE_FAILED,
            f"video frame extraction failed: {type(exc).__name__}",
        ) from exc

    expected = width * height * 3
    if len(raw) < expected:
        raise DecoderFailure(
            ContractErrorCode.DECODE_FAILED,
            f"video frame is truncated ({len(raw)} of {expected} bytes)",
        )

    image = Image.frombytes("RGB", (width, height), raw[:expected])
    if max_edge > 0:
        image.thumbnail((max_edge, max_edge))
    buffer = io.BytesIO()
    image.save(buffer, format=VIDEO_PREVIEW_FORMAT)
    return buffer.getvalue()


# In-app clip playback samples this many frames at most: enough to recognise
# motion, small enough that a phone video's whole decode stays a short worker
# step.  Frames are downscaled by ffmpeg itself so Pillow never sees 4K.
_CLIP_MAX_FRAMES = 48
_CLIP_SCALE_WIDTH = 480


def video_clip_pngs(
    video_path: str | Path,
    *,
    max_frames: int = _CLIP_MAX_FRAMES,
    max_edge: int = _CLIP_SCALE_WIDTH,
) -> tuple[list[bytes], float]:
    """Sample up to ``max_frames`` PNG frames across the whole video.

    Returns ``(frames, fps)``: each frame is PNG bytes at ``max_edge`` width,
    spread from the start to the end of the stream (stride-sampled, so a short
    clip plays contiguously and a long one still previews its full span), and
    ``fps`` is the stream rate the UI paces playback with.  Raises
    :class:`DecoderFailure` (``DECODE_FAILED``) when ffmpeg cannot read the
    file or yields no frame.
    """
    try:
        import imageio_ffmpeg  # imported lazily: heavy dependency, CLI-only path
    except Exception as import_error:  # noqa: BLE001 - degrade to the contract
        raise DecoderFailure(
            ContractErrorCode.DECODE_FAILED,
            f"video decoding is unavailable: {type(import_error).__name__}",
        ) from import_error

    try:
        total_frames, _seconds = imageio_ffmpeg.count_frames_and_secs(str(video_path))
    except Exception:  # noqa: BLE001 - fall back to head sampling below
        total_frames = 0
    stride = max(1, int(total_frames) // max(1, int(max_frames))) if total_frames else 1

    try:
        frames = imageio_ffmpeg.read_frames(
            str(video_path),
            output_params=[
                "-pix_fmt",
                "rgb24",
                "-vf",
                f"scale={int(max_edge)}:-2",
            ],
        )
        try:
            meta = next(frames)
            width, height = (int(value) for value in meta["size"])
            try:
                fps = float(meta.get("fps", 0) or 0)
            except (TypeError, ValueError):
                fps = 0.0
            picked: list[bytes] = []
            for position, raw in enumerate(frames):
                if position % stride:
                    continue
                expected = width * height * 3
                if len(raw) < expected:
                    continue
                image = Image.frombytes("RGB", (width, height), raw[:expected])
                buffer = io.BytesIO()
                image.save(buffer, format=VIDEO_PREVIEW_FORMAT)
                picked.append(buffer.getvalue())
                if len(picked) >= max(1, int(max_frames)):
                    break
        finally:
            frames.close()
    except StopIteration:
        raise DecoderFailure(
            ContractErrorCode.DECODE_FAILED, "video yields no decodable frame"
        ) from None
    except DecoderFailure:
        raise
    except Exception as exc:  # noqa: BLE001 - surface as the decode contract
        raise DecoderFailure(
            ContractErrorCode.DECODE_FAILED,
            f"video clip extraction failed: {type(exc).__name__}",
        ) from exc

    if not picked:
        raise DecoderFailure(
            ContractErrorCode.DECODE_FAILED, "video yields no decodable frame"
        )
    return picked, (fps if fps > 0 else 10.0)
