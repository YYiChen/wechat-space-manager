"""Read-only filtering, sorting and summarizing of mapped media records.

Pure functions over public contracts: no filesystem access, no mutation, no
decoder calls.  The CLI layer and any future GUI consume these directly.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from wechat_cleaner.domain.contracts import (
    MappingConfidence,
    MediaRecord,
    MediaType,
)

SortKey = Literal["bytes", "time", "confidence"]

_CONFIDENCE_ORDER: dict[MappingConfidence, int] = {
    MappingConfidence.EXACT: 5,
    MappingConfidence.HIGH: 4,
    MappingConfidence.MEDIUM: 3,
    MappingConfidence.LOW: 2,
    MappingConfidence.UNMAPPED: 1,
}


def by_media_type(records: Iterable[MediaRecord], media_type: MediaType) -> tuple[MediaRecord, ...]:
    return tuple(record for record in records if record.media_type is media_type)


def by_confidence(
    records: Iterable[MediaRecord], levels: Iterable[MappingConfidence]
) -> tuple[MediaRecord, ...]:
    accepted = frozenset(levels)
    return tuple(record for record in records if record.mapping_confidence in accepted)


def by_contact(records: Iterable[MediaRecord], contact_id: str) -> tuple[MediaRecord, ...]:
    return tuple(
        record
        for record in records
        if record.contact is not None and record.contact.contact_id == contact_id
    )


def by_min_bytes(records: Iterable[MediaRecord], minimum: int) -> tuple[MediaRecord, ...]:
    return tuple(record for record in records if record.file.byte_size >= minimum)


def by_time_range(
    records: Iterable[MediaRecord],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[MediaRecord, ...]:
    selected: list[MediaRecord] = []
    for record in records:
        if start is not None and record.observed_at < start:
            continue
        if end is not None and record.observed_at > end:
            continue
        selected.append(record)
    return tuple(selected)


def sort_by(
    records: Iterable[MediaRecord], key: SortKey, *, reverse: bool = False
) -> tuple[MediaRecord, ...]:
    if key == "bytes":
        ordered = sorted(records, key=lambda record: record.file.byte_size, reverse=reverse)
    elif key == "time":
        ordered = sorted(records, key=lambda record: record.observed_at, reverse=reverse)
    elif key == "confidence":
        ordered = sorted(
            records,
            key=lambda record: _CONFIDENCE_ORDER[record.mapping_confidence],
            reverse=reverse,
        )
    else:  # pragma: no cover - Literal guards this
        raise ValueError(f"unsupported sort key: {key}")
    return tuple(ordered)


@dataclass(frozen=True, slots=True)
class Summary:
    """Aggregate view of a record selection; safe for JSON round-trips."""

    total_count: int
    total_bytes: int
    by_media_type: tuple[tuple[str, int, int], ...]  # (type, count, bytes)
    by_confidence: tuple[tuple[str, int], ...]  # (confidence, count)
    regenerable_cache_count: int


def summarize(records: Iterable[MediaRecord]) -> Summary:
    materialized = tuple(records)
    type_stats: dict[str, list[int]] = {}
    confidence_stats: dict[str, int] = {}
    total_bytes = 0
    regenerable = 0
    for record in materialized:
        type_name = record.media_type.value
        entry = type_stats.setdefault(type_name, [0, 0])
        entry[0] += 1
        entry[1] += record.file.byte_size
        confidence_name = record.mapping_confidence.value
        confidence_stats[confidence_name] = confidence_stats.get(confidence_name, 0) + 1
        total_bytes += record.file.byte_size
        if record.is_regenerable_cache:
            regenerable += 1
    return Summary(
        total_count=len(materialized),
        total_bytes=total_bytes,
        by_media_type=tuple(
            (name, stats[0], stats[1]) for name, stats in sorted(type_stats.items())
        ),
        by_confidence=tuple(sorted(confidence_stats.items())),
        regenerable_cache_count=regenerable,
    )
