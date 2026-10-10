"""Duplicate-detection models (module-local, deliberately not contracts).

These are display-oriented signals for the read-only album: they never feed a
CleanupPlan and carry no cleanup semantics.  If a future consumer needs them
inside the contract layer, the contracts Agent must promote them explicitly
(see plan/ITER-20261010-duplicate-detection.md, section 2.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from wechat_cleaner.domain.contracts import MediaRecord

__all__ = [
    "LEVEL_S1",
    "LEVEL_S2",
    "DuplicateError",
    "DuplicateGroup",
    "DuplicateIndex",
    "GroupVerification",
]

# S1: normalized name matches (same stripped filename, any size).
LEVEL_S1 = "S1"
# S2: S1 and byte_size matches (cheap candidate gate before hashing).
LEVEL_S2 = "S2"


class DuplicateError(ValueError):
    """A rejected duplicate-detection step, with a stable machine code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    """Every record whose normalized filename matches (an S1 group).

    ``size_subgroups`` holds the S2 refinements: members that additionally
    share one byte size, in groups of two or more.  A record may appear in
    exactly one S1 group and at most one S2 subgroup.
    """

    normalized_name: str
    display_name: str
    members: tuple[MediaRecord, ...]
    size_subgroups: tuple[tuple[MediaRecord, ...], ...] = ()

    @property
    def best_level(self) -> str:
        """The strongest level any member reaches inside this group."""
        return LEVEL_S2 if self.size_subgroups else LEVEL_S1


@dataclass(frozen=True, slots=True)
class DuplicateIndex:
    """Lookup structure the UI consumes: per-record level plus the groups."""

    groups: tuple[DuplicateGroup, ...] = ()
    _level_by_media_id: dict[UUID, str] = field(default_factory=dict, repr=False)

    def level_of(self, media_id: UUID) -> str | None:
        """``"S2"``/``"S1"`` for members, ``None`` for unique records.

        The strongest signal wins: a record in a same-size subgroup reports
        S2 even though it also belongs to the enclosing S1 group.
        """
        return self._level_by_media_id.get(media_id)

    def group_of(self, media_id: UUID) -> DuplicateGroup | None:
        for group in self.groups:
            if any(record.media_id == media_id for record in group.members):
                return group
        return None

    def __len__(self) -> int:
        return len(self.groups)


@dataclass(frozen=True, slots=True)
class GroupVerification:
    """MD5 verdict for one candidate group (the S3 signal).

    ``content_identical`` is ``True`` only when **every** member was hashed
    successfully and all digests match; any skipped member keeps the verdict
    at S2 (honesty over convenience: "couldn't read one file" must not be
    reported as "provably the same file").
    """

    group_key: str
    content_identical: bool
    md5_by_relative_path: dict[str, str]
    skipped: tuple[tuple[str, str], ...] = ()
