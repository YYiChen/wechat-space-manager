"""Filter, sort and summarize behaviour over synthetic MediaRecords."""

from __future__ import annotations

from datetime import UTC, datetime

from wechat_cleaner.domain.contracts import MappingConfidence, MediaType
from wechat_cleaner.ui import (
    by_confidence,
    by_contact,
    by_media_type,
    by_min_bytes,
    by_time_range,
    sort_by,
    summarize,
)


def _records(record_factory):
    """Build one fixed triple: (exact image, low unmapped image, high video)."""
    return (
        record_factory(
            media_type=MediaType.IMAGE, confidence=MappingConfidence.EXACT, byte_size=500
        ),
        record_factory(
            media_type=MediaType.IMAGE,
            confidence=MappingConfidence.LOW,
            byte_size=3000,
            contact_id=None,
        ),
        record_factory(
            media_type=MediaType.VIDEO, confidence=MappingConfidence.HIGH, byte_size=2000
        ),
    )


def test_by_media_type(record_factory):
    image, _low, _video = _records(record_factory)
    records = _records(record_factory)  # same builder, different UUIDs; filter by type only
    picked = by_media_type(records, MediaType.IMAGE)
    assert {record.media_type for record in picked} == {MediaType.IMAGE}


def test_by_confidence_multiple_levels(record_factory):
    records = _records(record_factory)
    picked = by_confidence(records, [MappingConfidence.EXACT, MappingConfidence.LOW])
    assert [record.mapping_confidence for record in picked] == [
        MappingConfidence.EXACT,
        MappingConfidence.LOW,
    ]
    assert len(picked) == 2


def test_by_contact_excludes_unmapped(record_factory):
    records = _records(record_factory)
    picked = by_contact(records, "contact_alpha")
    assert len(picked) == 2  # exact image + high video; the LOW record has no contact
    assert all(record.contact is not None for record in picked)


def test_by_min_bytes(record_factory):
    records = _records(record_factory)
    picked = by_min_bytes(records, 2000)
    assert {record.file.byte_size for record in picked} == {3000, 2000}


def test_by_time_range_bounds(record_factory):
    early = record_factory(observed_at=datetime(2026, 1, 1, tzinfo=UTC))
    late = record_factory(observed_at=datetime(2026, 6, 1, tzinfo=UTC))
    picked = by_time_range((early, late), start=datetime(2026, 3, 1, tzinfo=UTC))
    assert {record.media_id for record in picked} == {late.media_id}


def test_sort_by_bytes_descending(record_factory):
    records = _records(record_factory)
    ordered = sort_by(records, "bytes", reverse=True)
    assert [record.file.byte_size for record in ordered] == [3000, 2000, 500]


def test_sort_by_confidence(record_factory):
    records = _records(record_factory)
    ordered = sort_by(records, "confidence")
    assert [record.mapping_confidence for record in ordered] == [
        MappingConfidence.LOW,
        MappingConfidence.HIGH,
        MappingConfidence.EXACT,
    ]


def test_summarize_aggregates(record_factory):
    summary = summarize(_records(record_factory))
    assert summary.total_count == 3
    assert summary.total_bytes == 5500
    by_type = dict((name, (count, size)) for name, count, size in summary.by_media_type)
    assert by_type["image"] == (2, 3500)
    assert by_type["video"] == (1, 2000)
    by_conf = dict(summary.by_confidence)
    assert by_conf["exact"] == 1 and by_conf["low"] == 1 and by_conf["high"] == 1


def test_empty_input_yields_zero_summary():
    summary = summarize(())
    assert summary.total_count == 0
    assert summary.total_bytes == 0
    assert summary.by_media_type == ()
    assert summary.regenerable_cache_count == 0
