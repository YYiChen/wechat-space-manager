"""Read-only location and availability for real media files.

Nothing here copies, decrypts or mutates: it answers "what does the source
directory actually contain for this record" so the UI can state
``original_available`` / ``thumbnail_only`` / ``missing`` honestly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from wechat_cleaner.domain.contracts import ContractErrorCode, MediaRecord, MediaType

from ..decoder.formats import DecoderFailure

__all__ = [
    "MediaAvailability",
    "LocatedMedia",
    "resolve_source_path",
    "locate",
    "availability",
    "original_sibling",
    "thumbnail_sibling",
]

THUMBNAIL_SUFFIX = "_t.dat"
THUMB_WIDE_SUFFIX = "_t_W.dat"
ORIGINAL_SUFFIX = ".dat"


class MediaAvailability(StrEnum):
    """What the source directory holds for one mapped record."""

    ORIGINAL_AVAILABLE = "original_available"
    THUMBNAIL_ONLY = "thumbnail_only"
    MISSING = "missing"
    UNDECODABLE = "undecodable"


@dataclass(frozen=True, slots=True)
class LocatedMedia:
    relative_path: str
    exists: bool
    byte_size: int
    is_thumbnail: bool


def resolve_source_path(account_root: str | Path, relative_path: str) -> Path:
    """Resolve a record path below the account root; refuse escapes.

    Normalization is done at string level (``os.path.normpath``) rather than
    ``Path.resolve()``: resolving touches the filesystem on Windows and a
    100k-record availability pass would take minutes in the UI thread.  The
    scanner already rejects reparse points, so symlink expansion is not needed
    here; ``..``-based escapes are still collapsed and rejected by prefix check.
    """
    root = Path(account_root)
    root_norm = os.path.normcase(os.path.normpath(str(root)))
    candidate = Path(os.path.normpath(os.path.join(str(root), relative_path)))
    candidate_norm = os.path.normcase(str(candidate))
    if candidate_norm != root_norm and not candidate_norm.startswith(root_norm + os.sep):
        raise DecoderFailure(
            ContractErrorCode.INVALID_PATH, "media path escapes the approved account root"
        )
    return candidate


def locate(account_root: str | Path, media: MediaRecord) -> LocatedMedia:
    path = resolve_source_path(account_root, media.file.relative_path)
    try:
        info = os.stat(path)
    except OSError:
        return LocatedMedia(
            relative_path=media.file.relative_path,
            exists=False,
            byte_size=0,
            is_thumbnail=path.name.endswith(THUMBNAIL_SUFFIX),
        )
    return LocatedMedia(
        relative_path=media.file.relative_path,
        exists=True,
        byte_size=info.st_size,
        is_thumbnail=path.name.endswith(THUMBNAIL_SUFFIX),
    )


def availability(account_root: str | Path, media: MediaRecord) -> MediaAvailability:
    """Classify availability, distinguishing original from thumbnail-only."""
    located = locate(account_root, media)
    if not located.exists:
        return MediaAvailability.MISSING
    if media.media_type is MediaType.THUMBNAIL:
        return MediaAvailability.THUMBNAIL_ONLY
    if media.media_type is MediaType.IMAGE and located.is_thumbnail:
        original = _original_sibling(account_root, media)
        if original.is_file():
            return MediaAvailability.ORIGINAL_AVAILABLE
        return MediaAvailability.THUMBNAIL_ONLY
    return MediaAvailability.ORIGINAL_AVAILABLE


def _original_sibling(account_root: str | Path, media: MediaRecord) -> Path:
    """Map ``<md5>_t.dat`` to its original ``<md5>.dat`` sibling path."""
    return original_sibling(account_root, media.file.relative_path)


def original_sibling(account_root: str | Path, relative_path: str) -> Path:
    """Map a ``<md5>_t.dat`` path to its original ``<md5>.dat`` sibling."""
    path = resolve_source_path(account_root, relative_path)
    if not path.name.endswith(THUMBNAIL_SUFFIX):
        return path
    return path.with_name(path.name[: -len(THUMBNAIL_SUFFIX)] + ORIGINAL_SUFFIX)


def thumbnail_sibling(account_root: str | Path, relative_path: str) -> Path | None:
    """Map an original path to a decodable thumbnail sibling, if one exists.

    Real accounts keep two thumbnail families next to originals: the AES
    ``<md5>_t.dat`` and the XOR ``<md5>_t_W.dat``.  Either is an honest
    fallback when the original container cannot be decoded; ``None`` means
    neither exists and the caller reports the failure as-is.
    """
    path = resolve_source_path(account_root, relative_path)
    if path.name.endswith(THUMBNAIL_SUFFIX):
        return path
    stem = path.name.removesuffix(ORIGINAL_SUFFIX)
    for suffix in (THUMBNAIL_SUFFIX, THUMB_WIDE_SUFFIX):
        candidate = path.with_name(f"{stem}{suffix}")
        if candidate.is_file():
            return candidate
    return None
