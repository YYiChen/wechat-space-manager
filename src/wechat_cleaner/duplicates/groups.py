"""In-memory duplicate grouping from already-scanned records (R1, zero IO).

The grouping runs over whatever records are currently loaded — the whole
account, or a folder-focus subtree — so the detection range follows the
app's focus state with no extra parameters (see F-20261008-001).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from uuid import UUID

from wechat_cleaner.domain.contracts import MediaRecord

from .model import LEVEL_S1, LEVEL_S2, DuplicateGroup, DuplicateIndex
from .names import duplicate_key

__all__ = ["same_name_groups"]


def same_name_groups(
    records: Iterable[MediaRecord], *, include_cache: bool = False
) -> DuplicateIndex:
    """Group records by normalized filename; refine same-size subgroups.

    Regenerable-cache records are excluded by default: they are re-creatable
    by design and carry no cleanup or dedup meaning.  Only groups with two or
    more members are reported; unique names never appear in the index.
    """

    by_key: dict[str, list[MediaRecord]] = defaultdict(list)
    display: dict[str, str] = {}
    for record in records:
        if not include_cache and record.is_regenerable_cache:
            continue
        key = duplicate_key(record)
        if key not in display:
            display[key] = _basename(record)
        by_key[key].append(record)

    groups: list[DuplicateGroup] = []
    level_by_id: dict[UUID, str] = {}
    for key in sorted(by_key):
        members = tuple(
            sorted(by_key[key], key=lambda item: item.file.relative_path.casefold())
        )
        if len(members) < 2:
            continue

        by_size: dict[int, list[MediaRecord]] = defaultdict(list)
        for record in members:
            by_size[record.file.byte_size].append(record)
        size_subgroups = tuple(
            tuple(sorted(same_size, key=lambda item: item.file.relative_path.casefold()))
            for _size, same_size in sorted(by_size.items())
            if len(same_size) >= 2
        )

        groups.append(
            DuplicateGroup(
                normalized_name=key,
                display_name=display[key],
                members=members,
                size_subgroups=size_subgroups,
            )
        )
        for record in members:
            level_by_id[record.media_id] = LEVEL_S1
        for subgroup in size_subgroups:
            for record in subgroup:
                level_by_id[record.media_id] = LEVEL_S2

    return DuplicateIndex(groups=tuple(groups), _level_by_media_id=level_by_id)


def _basename(record: MediaRecord) -> str:
    return record.file.relative_path.replace("\\", "/").rpartition("/")[2]
