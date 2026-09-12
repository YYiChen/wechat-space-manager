"""Internal, privacy-minimal inputs for the media mapping core.

Database adapters should translate their version-specific rows into these
objects before calling :class:`MediaMapper`.  The mapper never needs raw SQL,
message text, encryption material, or an absolute filesystem path.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from wechat_cleaner.domain.contracts import ContactRef, FileIdentity, MediaType

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def normalize_relative_path(value: str) -> str:
    """Normalize a database-relative path and reject traversal/absolute paths."""

    if not isinstance(value, str):
        raise TypeError("relative path must be a string")
    normalized = value.replace("/", "\\")
    segments = normalized.split("\\")
    if (
        not normalized
        or normalized.startswith(("\\", "/"))
        or re.match(r"^[A-Za-z]:", normalized)
        or any(segment in {"", ".", ".."} for segment in segments)
    ):
        raise ValueError("evidence paths must be non-empty relative paths without traversal")
    return normalized


def _normalize_many(values: Iterable[str]) -> tuple[str, ...]:
    """Normalize and de-duplicate paths while preserving adapter order."""

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_relative_path(value)
        key = normalized.casefold()
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return tuple(result)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class MediaCandidate:
    """One scanner candidate awaiting contact/message mapping."""

    account_id: str
    file: FileIdentity
    media_type: MediaType
    observed_at: datetime
    is_regenerable_cache: bool = False
    original_media_id: UUID | None = None

    def __post_init__(self) -> None:
        if not self.account_id.strip():
            raise ValueError("candidate account_id cannot be empty")
        object.__setattr__(self, "observed_at", _aware_utc(self.observed_at))


@dataclass(frozen=True, slots=True)
class MessageEvidence:
    """De-identified evidence emitted by a database adapter.

    ``media_paths`` and ``path_prefixes`` are preferred because they preserve a
    deterministic association.  ``media_name``/``byte_size`` are deliberately
    weaker correlation hints and can never become an exact cleanup mapping.
    """

    account_id: str
    message_local_id: int
    contact: ContactRef
    observed_at: datetime
    media_paths: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] = ()
    media_name: str | None = None
    byte_size: int | None = None
    sha256: str | None = None
    source: str = "database_copy"

    def __post_init__(self) -> None:
        if not self.account_id.strip():
            raise ValueError("evidence account_id cannot be empty")
        if self.message_local_id < 0:
            raise ValueError("message_local_id cannot be negative")
        if self.contact.account_id != self.account_id:
            raise ValueError("evidence contact must belong to evidence account")
        paths = _normalize_many(self.media_paths)
        prefixes = tuple(path.rstrip("\\") for path in _normalize_many(self.path_prefixes))
        if not paths and not prefixes and not self.media_name and not self.sha256:
            raise ValueError("evidence needs at least one correlation key")
        if self.media_name is not None:
            name = self.media_name.strip()
            if not name or "\\" in name or "/" in name:
                raise ValueError("media_name must be a non-empty basename")
            object.__setattr__(self, "media_name", name)
        if self.byte_size is not None and self.byte_size < 0:
            raise ValueError("byte_size cannot be negative")
        if self.sha256 is not None:
            digest = self.sha256.lower()
            if not _SHA256_RE.fullmatch(digest):
                raise ValueError("sha256 must be a lowercase 64-character hex digest")
            object.__setattr__(self, "sha256", digest)
        if not self.source.strip():
            raise ValueError("source cannot be empty")
        object.__setattr__(self, "observed_at", _aware_utc(self.observed_at))
        object.__setattr__(self, "media_paths", paths)
        object.__setattr__(self, "path_prefixes", prefixes)
