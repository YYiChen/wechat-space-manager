"""Real-media backend: read-only source in, verified pixels in app cache out.

This is an **optional** backend.  The synthetic ``decoder`` remains the default
and is untouched; this module isolates every upstream call so version drift,
exceptions and key caches stay inside one adapter.  Rules enforced here:

* the source account directory is opened read-only and never written;
* key material stays in memory (``RealImageKeys``) and is never persisted by us;
* outputs go only below ``cache_root`` via the shared decoder cache helpers;
* failures surface as ``DecodeError`` carrying a contract error code.
"""

from __future__ import annotations

import io
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wechat_cleaner.domain.contracts import (
    ContractError,
    ContractErrorCode,
    DecodeArtifact,
    DecodeVariant,
    FileIdentity,
    MediaRecord,
    MediaType,
)

from ..decoder.cache import (
    atomic_write,
    ensure_within_cache,
    output_stem,
    purge_expired,
    session_dir,
    usage_bytes,
)
from ..decoder.formats import DecoderFailure
from ..decoder.pipeline import DecodeError, DecoderLimits
from .images import (
    MediaFormat,
    decode_image_bytes,
    detect_format,
    probe_ciphertext,
    self_decode_file,
    verify_pixels,
)
from .keys import RealImageKeys, open_source_session, require_upstream, validate_aes_key
from .locate import (
    LocatedMedia,
    MediaAvailability,
    availability,
    locate,
    resolve_source_path,
    thumbnail_sibling,
)
from .video import video_frame_png

__all__ = ["RealMediaBackend"]

BACKEND_ID = "real-media-1"
_CACHE_FORMAT = "PNG"
_CACHE_EXT = ".png"
# Full-cache sweeps (purge_expired + usage_bytes) walk every cached file;
# preview-on-selection would otherwise stat thousands of files per click.
_MAINTENANCE_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ExportedOriginal:
    """Decrypted original bytes ready to be written where the user chooses.

    ``is_original`` is ``False`` when the source body was damaged and the
    payload degraded to the EXIF-embedded fallback - callers must surface that
    honestly instead of silently relabeling a stand-in as the original.
    """

    payload: bytes
    image_format: str
    width: int
    height: int
    is_original: bool
    container: MediaFormat
    relative_path: str


class RealMediaBackend:
    """Read-only backend over a real WeChat account directory."""

    def __init__(
        self,
        *,
        account_root: str,
        cache_root: str,
        keys: RealImageKeys,
        downloader,
        limits: DecoderLimits | None = None,
        session_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self.account_root = str(Path(account_root).resolve())
        self.cache_root = str(Path(cache_root).resolve())
        self.keys = keys
        self.limits = limits or DecoderLimits()
        self._downloader = downloader
        self._session_factory = session_factory
        self._last_maintenance = 0.0
        self._cached_usage = 0
        # The upstream downloader wraps a single SQLCipher connection and its
        # own call state; it is NOT safe to drive from several threads at once.
        # The grid decodes thumbnails on a pool, so every decrypt is serialised
        # through this lock.  The expensive part of a decode (Pillow resize /
        # re-encode) happens outside it, so warming stays parallel in practice.
        self._decrypt_lock = threading.Lock()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def open(
        cls,
        *,
        db_root: str,
        account_root: str,
        cache_root: str,
        account: str | None = None,
        limits: DecoderLimits | None = None,
        session: Any = None,
    ) -> RealMediaBackend:
        """Open a read-only upstream session and resolve image keys from cfgDword.

        ``session`` optionally injects an already-opened upstream ``WeChatDB``
        so the database adapter and this backend share one session instead of
        paying the key-extraction/decryption pipeline twice per connect.
        """
        workdir = str(Path(cache_root) / "db-cache")
        if session is None:
            session = open_source_session(db_root=db_root, workdir=workdir, account=account)
        cfg_dword = getattr(session, "cfg_dword", None)
        if not cfg_dword:
            raise _fail(ContractErrorCode.DECODER_UNAVAILABLE, "cfgDword could not be extracted")
        keys = RealImageKeys.from_cfg_dword(cfg_dword, session.wxid)
        _, MediaDownloader = require_upstream()
        downloader = MediaDownloader(session, save_dir=cache_root, cfg_dword=cfg_dword)
        return cls(
            account_root=account_root,
            cache_root=cache_root,
            keys=keys,
            downloader=downloader,
            limits=limits,
        )

    def close(self) -> None:
        self.keys.clear()
        closer = getattr(self._downloader, "db", None)
        if closer is not None and hasattr(closer, "close"):
            try:
                closer.close()
            except Exception:  # noqa: BLE001 - best-effort release
                pass

    # ------------------------------------------------------------------
    # read-only inspection
    # ------------------------------------------------------------------
    def availability(self, media: MediaRecord) -> MediaAvailability:
        return availability(self.account_root, media)

    def locate(self, media: MediaRecord) -> LocatedMedia:
        return locate(self.account_root, media)

    def keys_match(self, relative_path: str) -> bool:
        """Confirm the in-memory keys decrypt the given source file's probe block."""
        source = resolve_source_path(self.account_root, relative_path)
        if not source.is_file():
            return False
        with source.open("rb") as handle:
            header = handle.read(32)
        container = detect_format(header)
        probe = probe_ciphertext(header, container)
        if not probe:
            return False
        return validate_aes_key(self.keys.aes_key, probe)

    def _maybe_maintain_cache(self, cache_root: Path, now: datetime) -> None:
        """Throttle full-cache sweeps to one per interval (see module constant)."""
        monotonic = time.monotonic()
        if monotonic - self._last_maintenance < _MAINTENANCE_INTERVAL_SECONDS:
            return
        purge_expired(cache_root, now)
        self._cached_usage = usage_bytes(cache_root)
        self._last_maintenance = monotonic

    def _thumbnail_fallback(self, media: MediaRecord):
        """Fall back to the ``_t.dat`` sibling when an original cannot be decoded.

        Real accounts store an original and its thumbnail side by side; upstream
        support covers the thumbnail container today, so a failed original
        degrades to the thumbnail and the artifact reports ``thumbnail``.
        """
        if media.media_type is MediaType.THUMBNAIL:
            return None
        candidate = thumbnail_sibling(self.account_root, media.file.relative_path)
        if candidate is None or str(candidate) == str(
            resolve_source_path(self.account_root, media.file.relative_path)
        ):
            return None
        return candidate, DecodeVariant.THUMBNAIL

    # ------------------------------------------------------------------
    # decoding
    # ------------------------------------------------------------------
    def decode_image(
        self,
        media: MediaRecord,
        *,
        variant: DecodeVariant = DecodeVariant.THUMBNAIL,
        max_edge_px: int | None = None,
        retain_until: datetime | None = None,
        cache_session_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> DecodeArtifact:
        """Decrypt one real media record into the bounded cache.

        If the original is unavailable but a thumbnail exists, the thumbnail is
        returned and the artifact's ``variant`` reports ``thumbnail`` truthfully.
        Videos decode their first usable frame as a cover image (same cache,
        ``thumbnail`` variant); only files and voice notes have no picture.
        """
        if media.media_type is MediaType.VIDEO:
            return self._decode_video_preview(
                media,
                max_edge_px=max_edge_px,
                retain_until=retain_until,
                cache_session_id=cache_session_id,
                now=now,
            )
        if media.media_type not in {MediaType.IMAGE, MediaType.THUMBNAIL}:
            raise _fail(
                ContractErrorCode.DECODER_UNAVAILABLE,
                f"media_type {media.media_type.value} is not decodable by this backend",
            )

        state = self.availability(media)
        if state is MediaAvailability.MISSING:
            raise _fail(ContractErrorCode.DECODE_FAILED, "source media is missing")
        if state is MediaAvailability.UNDECODABLE:
            raise _fail(ContractErrorCode.DECODE_FAILED, "source media is not decodable")

        effective_variant = variant
        if variant is DecodeVariant.ORIGINAL and state is MediaAvailability.THUMBNAIL_ONLY:
            effective_variant = DecodeVariant.THUMBNAIL

        cache_root = Path(self.cache_root)
        if cache_session_id is not None:
            # Cache hit path: a previously decoded (session, media, variant,
            # edge) artifact is an instant file read instead of a full
            # decrypt + re-encode.  This is what makes browsing warm rows
            # feel instant and background warming worthwhile.
            hit = self._cache_hit(
                cache_root, cache_session_id, media, effective_variant, max_edge_px
            )
            if hit is not None:
                return hit

        source = resolve_source_path(self.account_root, media.file.relative_path)
        try:
            # Serialised: the upstream downloader is single-threaded native
            # state, and the grid warms cells from several threads at once.
            with self._decrypt_lock:
                payload, container, _is_original = decode_image_bytes(
                    str(source),
                    downloader=self._downloader,
                    aes_key=self.keys.aes_key,
                    xor_key=self.keys.xor_key,
                )
            pixels = verify_pixels(payload, container=container)
        except DecoderFailure as failure:
            fallback = self._thumbnail_fallback(media)
            if fallback is None:
                raise _fail(failure.code, failure.message) from failure
            source, effective_variant = fallback
            try:
                with self._decrypt_lock:
                    payload, container, _is_original = decode_image_bytes(
                        str(source),
                        downloader=self._downloader,
                        aes_key=self.keys.aes_key,
                        xor_key=self.keys.xor_key,
                    )
                pixels = verify_pixels(payload, container=container)
            except DecoderFailure as fallback_failure:
                raise _fail(fallback_failure.code, fallback_failure.message) from fallback_failure

        image = _load_for_output(pixels.payload)
        if effective_variant is DecodeVariant.THUMBNAIL and max_edge_px is not None:
            image.thumbnail((max_edge_px, max_edge_px))

        buffer = io.BytesIO()
        image.save(buffer, format=_CACHE_FORMAT)
        encoded = buffer.getvalue()
        del image

        return self._store_output(
            media,
            encoded=encoded,
            variant=effective_variant,
            max_edge_px=max_edge_px,
            retain_until=retain_until,
            cache_session_id=cache_session_id,
            now=now,
        )

    def _decode_video_preview(
        self,
        media: MediaRecord,
        *,
        max_edge_px: int | None,
        retain_until: datetime | None,
        cache_session_id: uuid.UUID | None,
        now: datetime | None,
    ) -> DecodeArtifact:
        """Decode a video's cover frame into the same bounded cache.

        ffmpeg runs as its own subprocess (no shared native state), so this
        deliberately stays outside ``_decrypt_lock`` — extracting a frame must
        not stall the concurrent image decodes the grid warms with.
        """
        variant = DecodeVariant.THUMBNAIL
        edge = max_edge_px or 256
        cache_root = Path(self.cache_root)
        if cache_session_id is not None:
            hit = self._cache_hit(cache_root, cache_session_id, media, variant, edge)
            if hit is not None:
                return hit

        state = self.availability(media)
        if state is MediaAvailability.MISSING:
            raise _fail(ContractErrorCode.DECODE_FAILED, "source media is missing")
        source = resolve_source_path(self.account_root, media.file.relative_path)
        try:
            encoded = video_frame_png(source, max_edge=edge)
        except DecoderFailure:
            # Attached videos may be wrapped in the same ``.dat`` encryption
            # as images (XOR or V2 AES with the image keys) instead of plain
            # mp4.  Decrypt to a cache-side temp file and read that; the
            # WeChat source itself is never touched.
            decrypted = self.decrypted_video_temp(media)
            if decrypted is None:
                raise _fail(
                    ContractErrorCode.DECODE_FAILED,
                    "video source is not a readable mp4, even after key unwrap",
                ) from None
            try:
                encoded = video_frame_png(decrypted, max_edge=edge)
            except DecoderFailure as failure:
                raise _fail(failure.code, failure.message) from failure
            finally:
                decrypted.unlink(missing_ok=True)

        return self._store_output(
            media,
            encoded=encoded,
            variant=variant,
            max_edge_px=edge,
            retain_until=retain_until,
            cache_session_id=cache_session_id,
            now=now,
        )

    def decrypted_video_temp(self, media: MediaRecord) -> Path | None:
        """Unwrap a ``.dat``-encrypted video into a cache-side temp mp4.

        Some attached videos share the image containers (single-byte XOR or
        V2 AES under the session image keys) instead of plain mp4 bytes.
        Returns the temp path when the unwrapped bytes look like an mp4
        (``ftyp`` box), else ``None``.  The caller deletes the temp file; keys
        stay in memory and the WeChat source is only ever read.
        """
        try:
            source = resolve_source_path(self.account_root, media.file.relative_path)
        except DecoderFailure:
            return None
        if source.suffix.casefold() != ".dat":
            return None
        try:
            data = source.read_bytes()
        except OSError:
            return None
        if len(data) < 16 or len(data) > 256 * 1024 * 1024:
            return None
        candidates: list[bytes] = []
        xor_key = getattr(self.keys, "xor_key", None)
        if xor_key is not None:
            try:
                table = bytes(byte ^ int(xor_key) for byte in range(256))
            except (TypeError, ValueError):
                table = b""
            if table:
                candidates.append(data.translate(table))
        try:
            decoded = self_decode_file(
                str(source), aes_key=self.keys.aes_key, xor_key=self.keys.xor_key
            )
        except DecoderFailure:
            decoded = None
        if decoded is not None:
            candidates.append(decoded[0])
        for payload in candidates:
            if len(payload) >= 8 and payload[4:8] == b"ftyp":
                temp = Path(self.cache_root) / f"video-plain-{uuid.uuid4().hex}.mp4"
                try:
                    temp.parent.mkdir(parents=True, exist_ok=True)
                    temp.write_bytes(payload)
                except OSError:
                    return None
                return temp
        return None

    def _store_output(
        self,
        media: MediaRecord,
        *,
        encoded: bytes,
        variant: DecodeVariant,
        max_edge_px: int | None,
        retain_until: datetime | None,
        cache_session_id: uuid.UUID | None,
        now: datetime | None,
    ) -> DecodeArtifact:
        """Write encoded output into the bounded cache and build the artifact.

        Shared by image and video decodes: the budget checks, the atomic write
        and the artifact contract are identical for both.
        """
        cache_root = Path(self.cache_root)
        cache_root.mkdir(parents=True, exist_ok=True)
        now_dt = now or _utcnow()
        self._maybe_maintain_cache(cache_root, now_dt)
        if len(encoded) > self.limits.max_single_output_bytes:
            raise _fail(ContractErrorCode.CACHE_LIMIT_EXCEEDED, "single output exceeds cache limit")
        if self._cached_usage + len(encoded) > self.limits.max_total_cache_bytes:
            # Refresh the stale counter before refusing, so throttled
            # maintenance cannot wrongly reject a small artifact.
            self._cached_usage = usage_bytes(cache_root)
            self._last_maintenance = time.monotonic()
            if self._cached_usage + len(encoded) > self.limits.max_total_cache_bytes:
                raise _fail(
                    ContractErrorCode.CACHE_LIMIT_EXCEEDED, "cache total limit would be exceeded"
                )
        self._cached_usage += len(encoded)

        session_id = cache_session_id or self._session_factory()
        target_dir = session_dir(cache_root, session_id)
        ensure_within_cache(cache_root, str(target_dir))
        stem = output_stem(media.media_id, variant)
        target = target_dir / f"{stem}-e{max_edge_px or 0}{_CACHE_EXT}"
        atomic_write(target, encoded)

        artifact = DecodeArtifact(
            request_id=uuid.uuid4(),
            media_id=media.media_id,
            created_at=now or _utcnow(),
            cache_session_id=session_id,
            variant=variant,
            output_file=FileIdentity(
                relative_path=target.relative_to(cache_root).as_posix(),
                byte_size=len(encoded),
                modified_time_ns=target.stat().st_mtime_ns,
            ),
            output_format=_CACHE_FORMAT,
            decoder_id=BACKEND_ID,
            expires_at=retain_until,
        )
        return artifact

    def _cache_hit(
        self,
        cache_root: Path,
        session_id: uuid.UUID,
        media: MediaRecord,
        variant: DecodeVariant,
        max_edge_px: int | None,
    ) -> DecodeArtifact | None:
        """Return the cached artifact for (session, media, variant, edge).

        The edge length is part of the key so a 256px preview can never
        satisfy a 512px request.  Files written before the edge suffix existed
        keep the legacy name and are still read, so an existing cache stays
        usable; a legacy hit is not rewritten (the next decode writes the new
        name and the old file ages out with the cache).
        """
        target_dir = session_dir(cache_root, session_id)
        stem = output_stem(media.media_id, variant)
        candidates = [target_dir / f"{stem}-e{max_edge_px or 0}{_CACHE_EXT}"]
        # Legacy name (written before the edge suffix existed): always probed
        # as a fallback so an existing cache keeps working.
        candidates.append(target_dir / f"{stem}{_CACHE_EXT}")
        for target in candidates:
            try:
                stat_result = target.stat()
            except OSError:
                continue
            return DecodeArtifact(
                request_id=uuid.uuid4(),
                media_id=media.media_id,
                created_at=datetime.fromtimestamp(
                    stat_result.st_mtime_ns / 1_000_000_000, tz=UTC
                ),
                cache_session_id=session_id,
                variant=variant,
                output_file=FileIdentity(
                    relative_path=target.relative_to(cache_root).as_posix(),
                    byte_size=stat_result.st_size,
                    modified_time_ns=stat_result.st_mtime_ns,
                ),
                output_format=_CACHE_FORMAT,
                decoder_id=BACKEND_ID,
            )
        return None

    def export_original(self, media: MediaRecord) -> ExportedOriginal:
        """Decrypt a source file into ORIGINAL image bytes (no re-encode).

        Unlike :meth:`decode_image` this bypasses the preview cache entirely:
        the returned payload is what the caller writes to the user-chosen
        export path.  A damaged body is NOT silently exported as the original —
        the payload degrades to the EXIF-embedded fallback and ``is_original``
        reports that.  No thumbnail fallback here: exporting a stand-in under
        the name of the original would be dishonest.
        """
        if media.media_type not in {MediaType.IMAGE, MediaType.THUMBNAIL}:
            raise _fail(
                ContractErrorCode.DECODER_UNAVAILABLE,
                f"media_type {media.media_type.value} is not decodable by this backend",
            )

        state = self.availability(media)
        if state is MediaAvailability.MISSING:
            raise _fail(ContractErrorCode.DECODE_FAILED, "source media is missing")
        if state is MediaAvailability.UNDECODABLE:
            raise _fail(ContractErrorCode.DECODE_FAILED, "source media is not decodable")

        source = resolve_source_path(self.account_root, media.file.relative_path)
        try:
            payload, container, is_original = decode_image_bytes(
                str(source),
                downloader=self._downloader,
                aes_key=self.keys.aes_key,
                xor_key=self.keys.xor_key,
            )
            pixels = verify_pixels(payload, container=container)
        except DecoderFailure as failure:
            raise _fail(failure.code, failure.message) from failure

        return ExportedOriginal(
            payload=pixels.payload,
            image_format=pixels.image_format,
            width=pixels.width,
            height=pixels.height,
            is_original=is_original,
            container=container,
            relative_path=media.file.relative_path,
        )


def _load_for_output(payload: bytes, *, max_decode_px: int = 2048):
    from PIL import Image, ImageFile  # noqa: PLC0415 - optional dependency

    ImageFile.LOAD_TRUNCATED_IMAGES = True
    # Real accounts hold stitched screenshots/panoramas far beyond Pillow's
    # 179-megapixel bomb guard (see images._relax_pillow_bomb_guard); inputs
    # are local files capped by the decoder's source-size limit.
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(io.BytesIO(payload)) as image:
        if (image.format or "").upper() == "JPEG":
            # draft() downgrades the decode resolution for JPEG before load(),
            # keeping multi-megapixel originals inside a sane memory budget.
            image.draft("RGB", (max_decode_px, max_decode_px))
        image.load()
        if (image.format or "").upper() != "JPEG":
            # PNG/WebP have no draft(): the decode above is unavoidably
            # full-size, so shrink the bitmap before caching to keep the
            # artifact inside the same preview budget as drafted JPEGs.
            image.thumbnail((max_decode_px, max_decode_px))
        return image.copy()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _fail(code: ContractErrorCode, message: str, *, retryable: bool = False) -> DecodeError:
    return DecodeError(ContractError(code=code, message=message, retryable=retryable))
