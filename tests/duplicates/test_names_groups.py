"""Unit tests for normalized filenames and in-memory grouping (D1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from wechat_cleaner.domain.contracts import (
    FileIdentity,
    MappingConfidence,
    MediaRecord,
    MediaType,
)
from wechat_cleaner.duplicates import (
    LEVEL_S1,
    LEVEL_S2,
    duplicate_key,
    normalize_filename,
    same_name_groups,
)


def _record(relative_path: str, size: int = 100, *, cache: bool = False) -> MediaRecord:
    return MediaRecord(
        account_id="wxid_demo_alpha",
        file=FileIdentity(
            relative_path=relative_path, byte_size=size, modified_time_ns=1
        ),
        media_type=MediaType.FILE,
        observed_at=datetime(2026, 10, 1, tzinfo=UTC),
        mapping_confidence=MappingConfidence.UNMAPPED,
        mapping_reason="filesystem scan only",
        is_regenerable_cache=cache,
    )


# --- normalize_filename -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("photo.jpg", "photo.jpg"),
        ("photo (1).jpg", "photo.jpg"),
        ("photo(2).jpg", "photo.jpg"),
        ("photo (12).jpg", "photo.jpg"),
        ("photo (1) (2).jpg", "photo.jpg"),
        ("photo (1) (3).jpg", "photo.jpg"),
        ("设计方案 (3).pdf", "设计方案.pdf"),
        ("设计方案(3).pdf", "设计方案.pdf"),
        ("archive (3)", "archive"),
        ("(1).jpg", "(1).jpg"),  # would empty the stem: kept intact
        ("photo.tar.gz", "photo.tar.gz"),  # only the last counter strips
        ("photo (draft).jpg", "photo (draft).jpg"),  # non-numeric counter stays
    ],
)
def test_normalize_filename_strips_trailing_counters(raw: str, expected: str) -> None:
    assert normalize_filename(raw) == expected


def test_duplicate_key_folds_case_and_uses_basename_only() -> None:
    assert duplicate_key(_record(r"msg\file\2026-05\Report (1).PDF")) == duplicate_key(
        _record(r"msg\attach\8a8b\2026-02\Rec\aa\F\3\report.pdf")
    )


# --- same_name_groups --------------------------------------------------------


def test_groups_same_normalized_names_across_directories() -> None:
    records = (
        _record(r"msg\file\2026-05\设计.pdf", 500),
        _record(r"msg\attach\aaaa\2026-01\Rec\bb\F\3\设计 (1).pdf", 500),
        _record(r"msg\attach\bbbb\2026-02\Rec\cc\F\3\设计(2).pdf", 500),
        _record(r"msg\file\2026-05\唯一.txt", 10),
    )
    index = same_name_groups(records)

    assert len(index) == 1
    group = index.groups[0]
    assert group.display_name == "设计.pdf"
    assert len(group.members) == 3
    # Same size → one S2 subgroup covering all three.
    assert group.best_level == LEVEL_S2
    assert len(group.size_subgroups) == 1 and len(group.size_subgroups[0]) == 3
    for record in group.members:
        assert index.level_of(record.media_id) == LEVEL_S2
    unique = records[3]
    assert index.level_of(unique.media_id) is None
    assert index.group_of(unique.media_id) is None


def test_same_name_different_sizes_split_into_size_subgroups() -> None:
    records = (
        _record(r"a\clip.mp4", 100),
        _record(r"b\clip (1).mp4", 100),
        _record(r"c\clip (2).mp4", 200),
    )
    index = same_name_groups(records)

    group = index.groups[0]
    assert len(group.members) == 3
    assert len(group.size_subgroups) == 1
    assert {r.file.relative_path for r in group.size_subgroups[0]} == {
        r"a\clip.mp4", r"b\clip (1).mp4"
    }
    assert index.level_of(records[0].media_id) == LEVEL_S2
    assert index.level_of(records[1].media_id) == LEVEL_S2
    assert index.level_of(records[2].media_id) == LEVEL_S1  # unique size: name only


def test_regenerable_cache_records_are_excluded_by_default() -> None:
    records = (
        _record(r"a\photo.jpg", 100),
        _record(r"b\photo (1).jpg", 100, cache=True),
    )
    assert len(same_name_groups(records)) == 0
    assert len(same_name_groups(records, include_cache=True)) == 1


def test_unique_names_and_single_records_produce_no_groups() -> None:
    assert len(same_name_groups(())) == 0
    assert len(same_name_groups((_record(r"a\only.txt", 1),))) == 0
    # Same name but two members with distinct sizes and no pair: S1 group only.
    index = same_name_groups((_record(r"a\x.bin", 1), _record(r"b\x.bin", 2)))
    assert len(index) == 1
    assert index.groups[0].best_level == LEVEL_S1
    assert index.groups[0].size_subgroups == ()
