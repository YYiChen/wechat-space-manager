"""Read-only facade for real WeChat data, consumed by the desktop window.

This facade intentionally exposes **no cleanup, plan, move or delete
capability**: the Phase 4 Beta is read-only, and the only mutations it performs
are removals inside the application's own cache root.

Original vs preview semantics
-----------------------------
A preview is a decoded *product* of a source media file.  Clearing a preview
removes only the cached copy; the WeChat source file is never touched.  The
reverse relation matters for the future cleanup phase: when an original is
eventually removed (by the independent executor, with per-file confirmation),
its preview should be cleared with it — never the other way round.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from wechat_cleaner.application import RealReadOnlySession
from wechat_cleaner.domain.contracts import DecodeVariant, MediaRecord, MediaType
from wechat_cleaner.real_db import discover_accounts, session_dir_of
from wechat_cleaner.real_media.locate import resolve_source_path
from wechat_cleaner.real_media.trash import recycle_media_source

READ_ONLY_NOTICE = (
    "只读模式：仅浏览与预览，不会删除、移动或修改微信源文件。"
    "「清除缓存」只作用于本应用自己的缓存目录。"
)

# 「智能」预览的原图上限。文件大小在扫描时就已知道（无需任何磁盘访问），所以
# 这个决定是零成本的：小图整段解密 + 缩放都很快，直接给原图画质；更大的文件
# 仍走缩略图，等待时间不随选择变化。
AUTO_ORIGINAL_MAX_BYTES = 1000 * 1024

# Only true originals benefit from the auto upgrade: a "缩略图" record *is* the
# small version, so decoding it as "original" would be the same work under a
# different cache key.
_AUTO_ORIGINAL_TYPES = (MediaType.IMAGE,)


def resolve_preview_variant(record: MediaRecord, variant: str) -> DecodeVariant:
    """Map the UI choice onto a decode variant.

    ``auto`` decides from what is already known about the record (its type and
    size): a small picture is shown at original quality, everything else uses
    the thumbnail.  An unknown variant string degrades to the thumbnail rather
    than raising - a preview is best-effort by contract.
    """
    if variant == "auto":
        if (
            record.media_type in _AUTO_ORIGINAL_TYPES
            and record.file.byte_size <= AUTO_ORIGINAL_MAX_BYTES
        ):
            return DecodeVariant.ORIGINAL
        return DecodeVariant.THUMBNAIL
    try:
        return DecodeVariant(variant)
    except ValueError:
        return DecodeVariant.THUMBNAIL



class FacadeError(RuntimeError):
    """A user-facing failure raised by the read-only facade."""


@dataclass(frozen=True, slots=True)
class AccountOption:
    """One selectable account; the identifier is only used to open a session."""

    account_id: str
    database_count: int
    database_bytes: int
    is_largest: bool = False


@dataclass(frozen=True, slots=True)
class PreviewOutcome:
    """Preview attempt result: either an artifact path or a readable reason."""

    ok: bool
    output_path: str = ""
    variant: str = ""
    decoder_id: str = ""
    error_code: str = ""
    error_message: str = ""
    output_bytes: int = 0


@dataclass(frozen=True, slots=True)
class CacheStatus:
    total_bytes: int
    file_count: int
    decoded_count: int
    key_cache_count: int


@dataclass(frozen=True, slots=True)
class VideoClipOutcome:
    """Sampled frames of one video for in-app play/pause preview.

    ``frames`` are PNG bytes sampled across the whole stream (see
    ``real_media.video.video_clip_pngs``); the window paces them with ``fps``.
    This is a lightweight stand-in, not a media player: no audio, no seeking.
    """

    ok: bool
    frames: tuple[bytes, ...] = ()
    fps: float = 10.0
    relative_path: str = ""
    error_code: str = ""
    error_message: str = ""


@dataclass(frozen=True, slots=True)
class RecycleOutcome:
    """Result of moving one source file to the Recycle Bin."""

    ok: bool
    error_code: str = ""
    error_message: str = ""
    relative_path: str = ""
    byte_size: int = 0


@dataclass(frozen=True, slots=True)
class ExportOutcome:
    """Result of exporting one decrypted original to a user-chosen path.

    ``payload`` carries the decrypted image bytes; the window writes them to
    the export target inside its worker thread.  ``is_original`` is ``False``
    when the source body was damaged and the payload degraded to the
    EXIF-embedded fallback - the UI must say so instead of passing a stand-in
    off as the original.
    """

    ok: bool
    payload: bytes = b""
    image_format: str = ""
    width: int = 0
    height: int = 0
    is_original: bool = True
    relative_path: str = ""
    byte_size: int = 0
    written_path: str = ""
    error_code: str = ""
    error_message: str = ""


class RealReadOnlyPort(Protocol):
    """The complete surface the read-only window may call."""

    def discover(self, db_root: str) -> tuple[AccountOption, ...]: ...

    def connect(self, db_root: str, cache_root: str, account: str | None = None) -> dict: ...

    def database_summary(self) -> dict: ...

    def load_records(
        self, limit: int | None = None, progress: Callable[[int], None] | None = None
    ) -> tuple[MediaRecord, ...]: ...

    def session_mapping(self) -> dict[str, dict]: ...

    def availability(self, record: MediaRecord) -> str: ...

    def source_path(self, record: MediaRecord) -> str: ...

    def video_clip(self, record: MediaRecord) -> VideoClipOutcome: ...

    def preview(
        self, record: MediaRecord, variant: str = "thumbnail", max_edge_px: int = 256
    ) -> PreviewOutcome: ...

    def recycle_record(self, record: MediaRecord) -> RecycleOutcome: ...

    def export_original(self, record: MediaRecord) -> ExportOutcome: ...

    def cache_status(self) -> CacheStatus: ...

    def clear_decoded_cache(self) -> int: ...

    def clear_all_local_cache(self) -> int: ...

    def manifest(self) -> dict: ...

    def close(self) -> None: ...


class RealSessionFacade:
    """Facade over :class:`RealReadOnlySession` with UI-friendly shapes."""

    def __init__(
        self,
        *,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._session_factory = session_factory or RealReadOnlySession.start
        self._session: Any | None = None
        self._record_index: dict[uuid.UUID, MediaRecord] = {}
        self.cache_root: str | None = None

    # ------------------------------------------------------------------
    # wizard
    # ------------------------------------------------------------------
    def discover(self, db_root: str) -> tuple[AccountOption, ...]:
        try:
            accounts = discover_accounts(db_root)
        except Exception as exc:  # noqa: BLE001 - surfaced as a UI message
            raise FacadeError(f"无法读取微信数据目录：{type(exc).__name__}") from exc
        if not accounts:
            raise FacadeError("未在该目录下找到包含 db_storage 的账号")
        largest = max(account.database_bytes for account in accounts)
        return tuple(
            AccountOption(
                account_id=account.account_id,
                database_count=account.database_count,
                database_bytes=account.database_bytes,
                is_largest=account.database_bytes == largest,
            )
            for account in accounts
        )

    def connect(self, db_root: str, cache_root: str, account: str | None = None) -> dict:
        self.close()
        self.cache_root = str(Path(cache_root))
        try:
            self._session = self._session_factory(
                db_root=db_root, cache_root=cache_root, account=account
            )
        except Exception as exc:  # noqa: BLE001
            code = getattr(getattr(exc, "error", None), "code", None)
            raise FacadeError(
                f"连接失败：{getattr(code, 'value', None) or type(exc).__name__}"
            ) from exc
        return self.database_summary()

    # ------------------------------------------------------------------
    # read-only views
    # ------------------------------------------------------------------
    def database_summary(self) -> dict:
        return self._require_session().database_summary()

    def load_records(
        self, limit: int | None = None, progress: Callable[[int], None] | None = None
    ) -> tuple[MediaRecord, ...]:
        records = self._require_session().records(limit=limit, progress=progress)
        self._record_index = {record.media_id: record for record in records}
        return records

    def session_mapping(self) -> dict[str, dict]:
        session = self._require_session()
        try:
            return session.session_mapping()
        except Exception:  # noqa: BLE001 - mapping is advisory for the UI
            return {}

    def record(self, media_id: uuid.UUID) -> MediaRecord | None:
        return self._record_index.get(media_id)

    def availability(self, record: MediaRecord) -> str:
        session = self._require_session()
        try:
            return session._backend.availability(record).value
        except Exception:  # noqa: BLE001 - availability is advisory for the UI
            return "unknown"

    def source_path(self, record: MediaRecord) -> str:
        """Absolute on-disk path of a record's source file (UI use only).

        The caller opens or reveals it locally; the value is never written to
        receipts, logs or the index, which only ever carry relative paths.
        """
        session = self._require_session()
        try:
            return str(resolve_source_path(session.account_root, record.file.relative_path))
        except Exception as exc:  # noqa: BLE001 - surfaced as a UI message
            raise FacadeError(f"无法定位源文件：{type(exc).__name__}") from exc

    def video_clip(self, record: MediaRecord) -> VideoClipOutcome:
        """Sample frames of one video for in-app play/pause preview."""
        from wechat_cleaner.real_media.video import video_clip_pngs

        session = self._require_session()
        if record.media_type is not MediaType.VIDEO:
            return VideoClipOutcome(
                ok=False,
                error_code="DECODER_UNAVAILABLE",
                error_message="该记录不是视频",
                relative_path=record.file.relative_path,
            )
        try:
            source = resolve_source_path(session.account_root, record.file.relative_path)
            picked, fps = video_clip_pngs(source)
        except Exception:  # noqa: BLE001 - a .dat wrap gets one unwrap retry below
            picked, fps = None, 0.0
        if picked is None:
            # Same encrypted-container retry as the cover path: unwrap with
            # the image keys into a cache-side temp, sample it, delete it.
            try:
                unwrapped = session._backend.decrypted_video_temp(record)
            except Exception:  # noqa: BLE001 - surfaced as a UI outcome below
                unwrapped = None
            if unwrapped is None:
                return VideoClipOutcome(
                    ok=False,
                    error_code="DECODE_FAILED",
                    error_message="该视频无法解码：源文件不是可读的 mp4，密钥解包也未成功",
                    relative_path=record.file.relative_path,
                )
            try:
                picked, fps = video_clip_pngs(unwrapped)
            except Exception as exc:  # noqa: BLE001 - failures are shown in the UI
                error = getattr(exc, "error", None)
                code = getattr(error, "code", None)
                return VideoClipOutcome(
                    ok=False,
                    error_code=getattr(code, "value", None) or type(exc).__name__,
                    error_message=getattr(error, "message", "") or str(exc)[:200],
                    relative_path=record.file.relative_path,
                )
            finally:
                try:
                    unwrapped.unlink(missing_ok=True)
                except OSError:
                    pass
        assert picked is not None  # set above, or returned early on failure
        return VideoClipOutcome(
            ok=True,
            frames=tuple(picked),
            fps=fps,
            relative_path=record.file.relative_path,
        )

    def preview(
        self, record: MediaRecord, variant: str = "auto", max_edge_px: int = 256
    ) -> PreviewOutcome:
        session = self._require_session()
        decode_variant = resolve_preview_variant(record, variant)
        try:
            artifact = session.preview(
                record, variant=decode_variant, max_edge_px=max_edge_px
            )
        except Exception as exc:  # noqa: BLE001 - failures are shown in the UI
            error = getattr(exc, "error", None)
            code = getattr(error, "code", None)
            return PreviewOutcome(
                ok=False,
                error_code=getattr(code, "value", None) or type(exc).__name__,
                error_message=getattr(error, "message", "") or str(exc)[:200],
            )
        root = Path(self.cache_root or ".")
        output = root / artifact.output_file.relative_path
        return PreviewOutcome(
            ok=True,
            output_path=str(output),
            variant=artifact.variant.value,
            decoder_id=artifact.decoder_id,
            output_bytes=artifact.output_file.byte_size,
        )

    # ------------------------------------------------------------------
    # recycle-bin removal (recoverable, audited)
    # ------------------------------------------------------------------
    def recycle_record(self, record: MediaRecord) -> RecycleOutcome:
        """Move one source file to the Recycle Bin after a TOCTOU re-check."""
        session = self._require_session()
        receipts = Path(self.cache_root or ".") / "recycle-receipts.jsonl"
        try:
            receipt = recycle_media_source(
                session.account_root, record, receipts_path=receipts
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            error = getattr(exc, "error", None)
            code = getattr(error, "code", None)
            return RecycleOutcome(
                ok=False,
                error_code=getattr(code, "value", None) or type(exc).__name__,
                error_message=getattr(error, "message", "") or str(exc)[:200],
                relative_path=record.file.relative_path,
                byte_size=record.file.byte_size,
            )
        return RecycleOutcome(
            ok=True,
            relative_path=receipt.relative_path,
            byte_size=receipt.byte_size,
        )

    # ------------------------------------------------------------------
    # original export (user-chosen destination, source untouched)
    # ------------------------------------------------------------------
    def export_original(self, record: MediaRecord) -> ExportOutcome:
        """Decrypt one record's source into ORIGINAL bytes for the export path.

        Writing the file is the window's job (it knows the chosen path); this
        only decrypts and reports.  Failures surface as readable outcomes, and
        a damaged source is reported as ``is_original=False`` so the UI can
        label the export honestly.
        """
        session = self._require_session()
        try:
            exported = session._backend.export_original(record)
        except Exception as exc:  # noqa: BLE001 - failures are shown in the UI
            error = getattr(exc, "error", None)
            code = getattr(error, "code", None)
            return ExportOutcome(
                ok=False,
                error_code=getattr(code, "value", None) or type(exc).__name__,
                error_message=getattr(error, "message", "") or str(exc)[:200],
                relative_path=record.file.relative_path,
                byte_size=record.file.byte_size,
            )
        return ExportOutcome(
            ok=True,
            payload=exported.payload,
            image_format=exported.image_format,
            width=exported.width,
            height=exported.height,
            is_original=exported.is_original,
            relative_path=exported.relative_path,
            byte_size=len(exported.payload),
        )

    # ------------------------------------------------------------------
    # cache control (application cache only)
    # ------------------------------------------------------------------
    def cache_status(self) -> CacheStatus:
        session = self._require_session()
        manifest = session.manifest()
        total = 0
        file_count = 0
        root = Path(self.cache_root or ".")
        for path in root.rglob("*"):
            if path.is_file():
                file_count += 1
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return CacheStatus(
            total_bytes=total,
            file_count=file_count,
            decoded_count=len(manifest.decoded_cache_files),
            key_cache_count=len(manifest.key_cache_relative_paths),
        )

    def clear_decoded_cache(self) -> int:
        """Remove decoded previews only; database caches and keys stay."""
        return int(self._require_session().clear_decoded_cache())

    def clear_all_local_cache(self) -> int:
        """Remove every file this application wrote under its own cache root."""
        root = Path(self.cache_root or ".")
        if not root.exists():
            return 0
        removed = 0
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink(missing_ok=True)
                removed += 1
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    continue
        return removed

    def manifest(self) -> dict:
        return self._require_session().manifest().to_anonymous_dict()

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None

    def _require_session(self):
        if self._session is None:
            raise FacadeError("尚未连接微信数据，请先运行自动检测")
        return self._session


class FakeRealSessionFacade:
    """Deterministic stand-in for tests and the demo mode."""

    def __init__(
        self,
        *,
        records: tuple[MediaRecord, ...] = (),
        preview_image_path: str = "",
        keyed_count: int = 27,
    ) -> None:
        self._records = records
        self._preview_image_path = preview_image_path
        self._keyed_count = keyed_count
        self._connected = False
        self._cleared: list[str] = []
        self._recycled: list[str] = []
        self._exported: list[str] = []
        self.export_payload: bytes | None = None
        self.export_format: str = "JPEG"
        self.export_is_original: bool = True
        # Synthetic clip frames (PNG bytes) served by ``video_clip`` in tests.
        self.clip_frames: list[bytes] = []
        self.clip_fps: float = 8.0
        # Whether ``preview`` serves a cover for videos (the real backend
        # extracts one via ffmpeg; the default keeps the old rejection so
        # tests that assert the failure path stay meaningful).
        self.video_preview_ok: bool = False
        self.cache_root = ""
        self.preview_variant = "thumbnail"
        # Session-filter demo data: display names / chatroom flags keyed by
        # attach dir name; entries absent here degrade to the hash prefix.
        self.session_names: dict[str, str] = {}
        self.session_chatrooms: set[str] = set()

    def discover(self, db_root: str) -> tuple[AccountOption, ...]:
        if not db_root:
            raise FacadeError("请先选择微信数据目录")
        return (
            AccountOption(account_id="wxid_demo_alpha", database_count=35,
                          database_bytes=1_381_945_344, is_largest=True),
            AccountOption(account_id="wxid_demo_beta", database_count=4, database_bytes=4096),
        )

    def connect(self, db_root: str, cache_root: str, account: str | None = None) -> dict:
        self._connected = True
        self.cache_root = cache_root
        return self.database_summary()

    def database_summary(self) -> dict:
        return {
            "database_count": 35,
            "database_bytes": 1_381_945_344,
            "keyed_count": self._keyed_count,
            "unkeyed_count": 35 - self._keyed_count,
            "account_count": 1,
        }

    def load_records(
        self, limit: int | None = None, progress: Callable[[int], None] | None = None
    ) -> tuple[MediaRecord, ...]:
        records = self._records if limit is None else self._records[:limit]
        if progress is not None and records:
            # Mirror the real facade's progress notifications once, so GUI
            # wiring is exercised in tests.
            progress(len(records))
        return records

    def session_mapping(self) -> dict[str, dict]:
        mapping: dict[str, dict] = {}
        for record in self._records:
            dir_name = session_dir_of(record.file.relative_path)
            if not dir_name or dir_name in mapping:
                continue
            mapping[dir_name] = {
                "name": self.session_names.get(dir_name, ""),
                "username": "",
                "is_chatroom": dir_name in self.session_chatrooms,
                "message_count": 0,
            }
        return mapping

    def availability(self, record: MediaRecord) -> str:
        if record.media_type is MediaType.THUMBNAIL:
            return "thumbnail_only"
        return "original_available"

    def source_path(self, record: MediaRecord) -> str:
        import os

        return os.path.join("C:\\synthetic-sources", record.file.relative_path)

    def video_clip(self, record: MediaRecord) -> VideoClipOutcome:
        if record.media_type is not MediaType.VIDEO:
            return VideoClipOutcome(
                ok=False,
                error_code="DECODER_UNAVAILABLE",
                error_message="该记录不是视频",
                relative_path=record.file.relative_path,
            )
        return VideoClipOutcome(
            ok=True,
            frames=tuple(self.clip_frames),
            fps=self.clip_fps,
            relative_path=record.file.relative_path,
        )

    def preview(self, record: MediaRecord, variant: str = "auto", max_edge_px: int = 256):
        if record.media_type is MediaType.VIDEO and not self.video_preview_ok:
            return PreviewOutcome(
                ok=False, error_code="DECODER_UNAVAILABLE", error_message="该类型暂不支持预览"
            )
        decode_variant = resolve_preview_variant(record, variant)
        self.preview_variant = decode_variant.value
        output_path = self._preview_image_path or (
            f"{self.cache_root}/sessions/demo/{record.media_id}.png"
        )
        return PreviewOutcome(
            ok=True, output_path=output_path,
            variant=decode_variant.value, decoder_id="fake-real-session", output_bytes=4096,
        )

    def recycle_record(self, record: MediaRecord) -> RecycleOutcome:
        self._recycled.append(record.file.relative_path)
        return RecycleOutcome(
            ok=True,
            relative_path=record.file.relative_path,
            byte_size=record.file.byte_size,
        )

    def export_original(self, record: MediaRecord) -> ExportOutcome:
        if record.media_type not in (MediaType.IMAGE, MediaType.THUMBNAIL):
            return ExportOutcome(
                ok=False,
                error_code="DECODER_UNAVAILABLE",
                error_message="该记录不是图片（视频 / 文件 / 语音不支持导出）",
                relative_path=record.file.relative_path,
                byte_size=record.file.byte_size,
            )
        payload = self.export_payload or b"fake-export-bytes"
        self._exported.append(record.file.relative_path)
        return ExportOutcome(
            ok=True,
            payload=payload,
            image_format=self.export_format,
            width=640,
            height=480,
            is_original=self.export_is_original,
            relative_path=record.file.relative_path,
            byte_size=len(payload),
        )

    def cache_status(self) -> CacheStatus:
        return CacheStatus(
            total_bytes=12_345_678, file_count=42, decoded_count=3, key_cache_count=1
        )

    def clear_decoded_cache(self) -> int:
        self._cleared.append("decoded")
        return 3

    def clear_all_local_cache(self) -> int:
        self._cleared.append("all")
        return 43

    def manifest(self) -> dict:
        return {"session_version": "real-session-1", "preview_count": 3}

    def close(self) -> None:
        self._connected = False
