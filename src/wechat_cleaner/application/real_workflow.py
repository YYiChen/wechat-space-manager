"""In-app real read-only session: discovery → inventory → records → preview.

This is the "user launches one application" path for real WeChat data. It
composes the existing scanner with the optional real adapters so that no manual
database copy, copy root or mapper JSON is required:

1. discover account roots under the data root (read-only);
2. open the real database adapter and report an anonymous inventory;
3. scan the account directory read-only and emit unmapped media records;
4. decode a selected record through the real media backend into the cache;
5. keep a session manifest so the whole session can be cleared at once.

No cleanup, move or delete capability is exposed.  Records produced here carry
``MappingConfidence.UNMAPPED`` until a real database mapping pass lands, so they
can never enter a cleanup plan.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wechat_cleaner.domain.contracts import (
    ContractErrorCode,
    DecodeArtifact,
    DecodeVariant,
    FileIdentity,
    MappingConfidence,
    MediaRecord,
    MediaType,
)
from wechat_cleaner.scanner import ScannerError, scan_account

from ..decoder.formats import DecoderFailure
from ..decoder.pipeline import DecoderLimits
from ..real_db import RealDatabaseAdapter, discover_accounts
from ..real_media import RealMediaBackend

__all__ = ["SESSION_VERSION", "RealReadOnlySession", "SessionManifest"]

SESSION_VERSION = "real-session-1"
# Decoded previews live in the shared decoder cache layout
# (``<cache_root>/sessions/<cache_session_id>/…``), used by both the synthetic
# decoder and the real media backend.  The session manifest and cache clearing
# therefore target ``sessions/`` rather than a private directory name.
_DECODED_DIRNAME = "sessions"
_KEY_CACHE_DIRNAME = "db-cache"

_SCANNABLE_TYPES = {
    MediaType.IMAGE,
    MediaType.THUMBNAIL,
    MediaType.VIDEO,
    MediaType.FILE,
    MediaType.VOICE,
}

# The Phase 2 scanner classifies synthetic layouts (``msg/<category>/``).  Real
# WeChat 4.x stores attachments as ``msg/attach/<hash>/<month>/Img/...``, which
# the scanner reports as ``OTHER``.  Until the scanner owner extends its rules,
# this session refines those paths locally so real media is not lost.
# Tracked as a cross-module gap in F-20260910-091.
_ATTACH_SEGMENT_TYPES = {
    "img": MediaType.IMAGE,
    "image": MediaType.IMAGE,
    "video": MediaType.VIDEO,
    "file": MediaType.FILE,
    "voice": MediaType.VOICE,
}
_THUMBNAIL_SUFFIXES = ("_t.dat", "_t_w.dat", "_t")

# The scanner maps every ``Rec`` path to IMAGE/THUMBNAIL, but real WeChat 4.x
# uses ``Rec`` (merged/forwarded chat-record attachments) for ARBITRARY file
# types - the probed account stores a 109 MB design PDF there, which used to
# surface as an "image" that failed preview with a decode error.  When the
# final filename carries an unambiguous extension, trust the extension over
# the directory guess.  ``.dat`` and extension-less names are left untouched.
_REC_ZONE_EXTENSIONS: dict[str, MediaType] = {
    ".pdf": MediaType.FILE,
    ".doc": MediaType.FILE,
    ".docx": MediaType.FILE,
    ".xls": MediaType.FILE,
    ".xlsx": MediaType.FILE,
    ".ppt": MediaType.FILE,
    ".pptx": MediaType.FILE,
    ".txt": MediaType.FILE,
    ".rtf": MediaType.FILE,
    ".csv": MediaType.FILE,
    ".zip": MediaType.FILE,
    ".rar": MediaType.FILE,
    ".7z": MediaType.FILE,
    ".mp4": MediaType.VIDEO,
    ".mov": MediaType.VIDEO,
    ".avi": MediaType.VIDEO,
    ".mkv": MediaType.VIDEO,
    ".flv": MediaType.VIDEO,
    ".wmv": MediaType.VIDEO,
    ".m4v": MediaType.VIDEO,
    ".3gp": MediaType.VIDEO,
    ".amr": MediaType.VOICE,
    ".wav": MediaType.VOICE,
    ".mp3": MediaType.VOICE,
    ".aac": MediaType.VOICE,
    ".flac": MediaType.VOICE,
    ".m4a": MediaType.VOICE,
    ".ogg": MediaType.VOICE,
    ".silk": MediaType.VOICE,
}


def _rec_zone_media_type(filename: str) -> MediaType | None:
    """Extension-based type for ``Rec`` attachments; ``None`` = keep the guess."""
    stem_separator, suffix = filename.rpartition(".")[1:]
    if not stem_separator:
        return None
    return _REC_ZONE_EXTENSIONS.get(f".{suffix.casefold()}")


def _refine_media_type(relative_path: str, classified: MediaType) -> MediaType:
    """Reclassify ``msg/attach/...`` paths that the synthetic scanner calls OTHER."""
    parts = relative_path.replace("\\", "/").casefold().split("/")
    if "rec" in parts:
        # Mixed-attachment zone: a clear extension outranks the directory rule.
        corrected = _rec_zone_media_type(parts[-1])
        if corrected is not None:
            return corrected
    if classified is not MediaType.OTHER:
        return classified
    if len(parts) < 3 or parts[0] != "msg" or parts[1] != "attach":
        return MediaType.OTHER
    for segment in parts[2:-1]:
        mapped = _ATTACH_SEGMENT_TYPES.get(segment)
        if mapped is None:
            continue
        if mapped is MediaType.IMAGE and parts[-1].endswith(_THUMBNAIL_SUFFIXES):
            return MediaType.THUMBNAIL
        return mapped
    return MediaType.OTHER


@dataclass(frozen=True, slots=True)
class SessionManifest:
    """Everything one real read-only run produced, ready for whole-session cleanup."""

    session_id: str
    created_at: datetime
    account_count: int
    database_count: int
    database_bytes: int
    keyed_count: int
    record_count: int
    preview_count: int
    decoded_cache_files: tuple[str, ...]
    key_cache_relative_paths: tuple[str, ...]

    def to_anonymous_dict(self) -> dict:
        """Aggregate-only view; safe for reports and logs."""
        return {
            "session_version": SESSION_VERSION,
            "account_count": self.account_count,
            "database_count": self.database_count,
            "database_bytes": self.database_bytes,
            "keyed_count": self.keyed_count,
            "record_count": self.record_count,
            "preview_count": self.preview_count,
            "decoded_cache_file_count": len(self.decoded_cache_files),
            "key_cache_file_count": len(self.key_cache_relative_paths),
            "created_at": self.created_at.isoformat(),
        }


class RealReadOnlySession:
    """One in-app read-only run over a real WeChat account."""

    def __init__(
        self,
        *,
        session_id: str,
        db_root: str,
        cache_root: str,
        account_root: str,
        adapter: Any,
        backend: Any,
        upstream: Any = None,
    ) -> None:
        self.session_id = session_id
        self.db_root = db_root
        self.cache_root = str(Path(cache_root).resolve())
        self.account_root = account_root
        self._adapter = adapter
        self._backend = backend
        # The keyed upstream session (when opened) backs the session mapping;
        # it stays in memory and is closed with the rest of the session.
        self._upstream = upstream
        self._created_at = datetime.now(UTC)
        self._preview_count = 0
        self._record_count = 0

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def start(
        cls,
        *,
        db_root: str,
        cache_root: str,
        account: str | None = None,
        session_id: str | None = None,
        adapter_factory: Callable[..., Any] | None = None,
        backend_factory: Callable[..., Any] | None = None,
    ) -> RealReadOnlySession:
        accounts = discover_accounts(db_root)
        if not accounts:
            raise DecoderFailure(
                ContractErrorCode.DECODER_UNAVAILABLE, "no account with db_storage was found"
            )
        target = _pick_account(accounts, account)
        identifier = session_id or f"real-session-{uuid.uuid4().hex[:12]}"
        Path(cache_root).mkdir(parents=True, exist_ok=True)

        if adapter_factory is None and backend_factory is None:
            # Production path: open ONE upstream session and share it between
            # the database adapter and the media backend.  The upstream
            # key-extraction/decryption pipeline is the slowest part of a
            # connect; paying it twice made every connect take twice as long.
            from ..real_media.keys import open_source_session  # noqa: PLC0415

            shared_session = open_source_session(
                db_root=db_root,
                workdir=str(Path(cache_root) / "db-cache"),
                account=target.account_id,
            )
            adapter = RealDatabaseAdapter.open(
                db_root=db_root,
                cache_root=cache_root,
                account=target.account_id,
                session_factory=lambda **_kwargs: shared_session,
            )
            backend = RealMediaBackend.open(
                db_root=db_root,
                account_root=target.root,
                cache_root=cache_root,
                account=target.account_id,
                # Preview-on-selection keeps every decoded image cached; the
                # user explicitly opted into a generous cache (2 GiB).
                limits=DecoderLimits(max_total_cache_bytes=2 * 1024**3),
                session=shared_session,
            )
        else:
            if adapter_factory is None:
                adapter = RealDatabaseAdapter.open(
                    db_root=db_root, cache_root=cache_root, account=target.account_id
                )
            else:
                adapter = adapter_factory(
                    db_root=db_root, cache_root=cache_root, account=target.account_id
                )

            if backend_factory is None:
                backend = RealMediaBackend.open(
                    db_root=db_root,
                    account_root=target.root,
                    cache_root=cache_root,
                    account=target.account_id,
                    # Preview-on-selection keeps every decoded image cached; the
                    # user explicitly opted into a generous cache (2 GiB).
                    limits=DecoderLimits(max_total_cache_bytes=2 * 1024**3),
                )
            else:
                backend = backend_factory(
                    db_root=db_root, account_root=target.root, cache_root=cache_root,
                    account=target.account_id,
                )

        production_path = adapter_factory is None and backend_factory is None
        return cls(
            session_id=identifier,
            db_root=db_root,
            cache_root=cache_root,
            account_root=target.root,
            adapter=adapter,
            backend=backend,
            upstream=shared_session if production_path else None,
        )

    # ------------------------------------------------------------------
    # read-only views
    # ------------------------------------------------------------------
    def database_summary(self) -> dict:
        return self._adapter.summary()

    def session_mapping(self) -> dict[str, dict]:
        """Attach-hash dir -> conversation facts for the session filter.

        Advisory data for the read-only window: failures degrade to an empty
        mapping (the GUI then labels chats by their hash prefix) and never
        block the scan.
        """
        try:
            from ..real_db.mapping import build_session_mapping  # noqa: PLC0415

            return build_session_mapping(self.account_root, self._upstream)
        except Exception:  # noqa: BLE001 - mapping is advisory for the UI
            return {}

    def records(
        self,
        *,
        limit: int | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> tuple[MediaRecord, ...]:
        """Media records from a read-only scan of the account directory.

        Records are deliberately ``UNMAPPED``: without a database mapping pass
        they must never be eligible for cleanup.  ``progress`` is forwarded to
        the scanner and receives the running file count during the scan.
        """
        try:
            result = scan_account(self.account_root, progress=progress)
        except ScannerError as exc:
            raise DecoderFailure(exc.code, "the account directory could not be scanned") from exc

        records = []
        for scanned in result.files:
            if scanned.is_protected:
                continue
            media_type = _refine_media_type(scanned.relative_path, scanned.media_type)
            if media_type not in _SCANNABLE_TYPES:
                continue
            records.append(
                MediaRecord(
                    account_id=result.account.account_id,
                    file=FileIdentity(
                        relative_path=scanned.relative_path,
                        byte_size=scanned.byte_size,
                        modified_time_ns=scanned.modified_time_ns,
                    ),
                    media_type=media_type,
                    observed_at=datetime.fromtimestamp(
                        scanned.modified_time_ns / 1_000_000_000, tz=UTC
                    ),
                    mapping_confidence=MappingConfidence.UNMAPPED,
                    mapping_reason="filesystem scan only; no database mapping pass yet",
                    is_regenerable_cache=scanned.is_regenerable_cache,
                )
            )
            if limit is not None and len(records) >= limit:
                break
        self._record_count = len(records)
        return tuple(records)

    def preview(
        self,
        record: MediaRecord,
        *,
        variant: DecodeVariant = DecodeVariant.THUMBNAIL,
        max_edge_px: int | None = 256,
    ) -> DecodeArtifact:
        """Decode one record through the real media backend into the cache."""
        artifact = self._backend.decode_image(
            record,
            variant=variant,
            max_edge_px=max_edge_px,
            cache_session_id=uuid.uuid5(uuid.NAMESPACE_URL, self.session_id),
        )
        self._preview_count += 1
        return artifact

    # ------------------------------------------------------------------
    # session management
    # ------------------------------------------------------------------
    def manifest(self) -> SessionManifest:
        root = Path(self.cache_root)
        decoded = tuple(
            sorted(
                path.relative_to(root).as_posix()
                for path in (root / _DECODED_DIRNAME).rglob("*")
                if path.is_file()
            )
        ) if (root / _DECODED_DIRNAME).exists() else ()
        key_caches = tuple(
            sorted(
                path.relative_to(root).as_posix()
                for path in (root / _KEY_CACHE_DIRNAME).rglob("*")
                if path.is_file()
            )
        ) if (root / _KEY_CACHE_DIRNAME).exists() else ()
        summary = self._adapter.summary()
        return SessionManifest(
            session_id=self.session_id,
            created_at=self._created_at,
            account_count=1,
            database_count=int(summary.get("database_count", 0)),
            database_bytes=int(summary.get("database_bytes", 0)),
            keyed_count=int(summary.get("keyed_count", 0)),
            record_count=self._record_count,
            preview_count=self._preview_count,
            decoded_cache_files=decoded,
            key_cache_relative_paths=key_caches,
        )

    def clear_decoded_cache(self) -> int:
        """Delete decoded previews only; database caches and keys are untouched."""
        root = Path(self.cache_root) / _DECODED_DIRNAME
        if not root.exists():
            return 0
        removed = sum(1 for path in root.rglob("*") if path.is_file())
        shutil.rmtree(root, ignore_errors=True)
        return removed

    def close(self) -> None:
        for component in (self._backend, self._adapter):
            closer = getattr(component, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001 - best-effort release
                    pass


def _pick_account(accounts, requested: str | None):
    if requested is None:
        return max(accounts, key=lambda item: item.database_bytes)
    for account in accounts:
        if account.account_id == requested:
            return account
    raise DecoderFailure(
        ContractErrorCode.DECODER_UNAVAILABLE, "the requested account was not found"
    )
