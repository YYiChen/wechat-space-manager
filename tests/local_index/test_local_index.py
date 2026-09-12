from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from wechat_cleaner.domain.contracts import (
    ContactKind,
    ContactRef,
    FileIdentity,
    FileStatus,
    FilterSortKey,
    FilterSpec,
    MappingConfidence,
    MediaRecord,
    MediaType,
    OwnershipStatus,
)
from wechat_cleaner.local_index import (
    CURRENT_SCHEMA_VERSION,
    IndexMigrationError,
    IndexOpenError,
    IndexQueryError,
    LocalMetadataIndex,
)

NOW = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)


def make_record(
    number: int,
    *,
    account_id: str = "wxid_index_alpha",
    media_type: MediaType = MediaType.IMAGE,
    byte_size: int | None = None,
    confidence: MappingConfidence = MappingConfidence.EXACT,
    ownership: OwnershipStatus = OwnershipStatus.MAPPED,
    file_status: FileStatus = FileStatus.PRESENT,
) -> MediaRecord:
    media_id = UUID(int=number + 1)
    contact = ContactRef(
        account_id=account_id,
        contact_id=f"{account_id}_contact_{number % 4}",
        kind=ContactKind.DIRECT if number % 2 else ContactKind.GROUP,
    )
    if confidence is MappingConfidence.UNMAPPED or ownership is OwnershipStatus.UNMAPPED:
        contact = None
    protection = ("protected synthetic root",) if ownership is OwnershipStatus.PROTECTED else ()
    conflicts = ("conflicting synthetic row",) if ownership is OwnershipStatus.CONFLICT else ()
    return MediaRecord(
        media_id=media_id,
        account_id=account_id,
        file=FileIdentity(
            relative_path=f"msg\\attach\\session_{number:06d}\\file_{number:06d}.dat",
            byte_size=byte_size if byte_size is not None else number % 17,
            modified_time_ns=number + 1,
            sha256=(f"{number + 1:064x}"),
        ),
        media_type=media_type,
        observed_at=NOW + timedelta(seconds=number),
        contact=contact,
        message_local_id=number if confidence is not MappingConfidence.UNMAPPED else None,
        mapping_confidence=confidence,
        mapping_reason="synthetic index record",
        record_key=f"index-record-{number:06d}",
        message_time=NOW - timedelta(days=number % 365),
        file_status=file_status,
        ownership_status=ownership,
        protection_reasons=protection,
        conflict_reasons=conflicts,
    )


def test_upsert_cursor_pagination_and_aggregate_are_stable(tmp_path: Path) -> None:
    path = tmp_path / "metadata.sqlite"
    records = [
        make_record(number, account_id="wxid_index_alpha" if number % 2 else "wxid_index_beta")
        for number in range(40)
    ]
    with LocalMetadataIndex.open(path) as index:
        assert index.upsert_records((record for record in records), batch_size=7) == len(records)
        spec = FilterSpec(
            include_unmapped=True,
            include_conflicts=True,
            include_protected=True,
            sort_by=FilterSortKey.BYTES,
            descending=False,
            page_size=7,
        )
        collected: list[UUID] = []
        cursor = None
        while True:
            page = index.query_page(spec, cursor=cursor)
            collected.extend(item.media_id for item in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        expected = [
            record.media_id
            for record in sorted(
                records, key=lambda item: (item.file.byte_size, str(item.media_id))
            )
        ]
        assert collected == expected
        assert len(collected) == len(set(collected)) == len(records)
        assert page.total_count == 40
        assert page.total_bytes == sum(record.file.byte_size for record in records)

        buckets = index.aggregate(spec, group_by="media_type")
        assert sum(bucket.count for bucket in buckets) == len(records)
        assert sum(bucket.total_bytes for bucket in buckets) == page.total_bytes
        assert {bucket.key for bucket in index.aggregate(spec, group_by="account_id")} == {
            "wxid_index_alpha",
            "wxid_index_beta",
        }


def test_filter_digest_binds_cursor_and_default_policy_excludes_unsafe_states(
    tmp_path: Path,
) -> None:
    path = tmp_path / "metadata.sqlite"
    with LocalMetadataIndex.open(path) as index:
        index.upsert_records(
            [
                make_record(1),
                make_record(
                    2,
                    confidence=MappingConfidence.UNMAPPED,
                    ownership=OwnershipStatus.UNMAPPED,
                ),
                make_record(
                    3,
                    ownership=OwnershipStatus.PROTECTED,
                    confidence=MappingConfidence.MEDIUM,
                ),
                make_record(
                    4,
                    ownership=OwnershipStatus.CONFLICT,
                    confidence=MappingConfidence.MEDIUM,
                ),
            ]
        )
        default_page = index.query_page(FilterSpec(page_size=20))
        assert default_page.total_count == 1
        broad = FilterSpec(
            include_unmapped=True,
            include_protected=True,
            include_conflicts=True,
            page_size=2,
        )
        first = index.query_page(broad)
        assert first.next_cursor is not None
        changed = broad.model_copy(update={"descending": not broad.descending})
        with pytest.raises(IndexQueryError, match="cursor"):
            index.query_page(changed, cursor=first.next_cursor)


def test_migration_interrupt_rolls_back_and_next_open_recovers(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    LocalMetadataIndex.create_legacy_v1(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO media_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(UUID(int=99)),
                "wxid_legacy",
                "wxid_legacy_contact",
                "image",
                r"msg\legacy.dat",
                12,
                1,
                "a" * 64,
                NOW.isoformat().replace("+00:00", "Z"),
                99,
                "exact",
                "legacy synthetic row",
            ),
        )
    with pytest.raises(IndexMigrationError):
        LocalMetadataIndex.migrate(path, interrupt_after=1)
    with sqlite3.connect(path) as connection:
        version_after_failure = connection.execute(
            "SELECT value FROM index_meta WHERE key='schema_version'"
        ).fetchone()[0]
        columns_after_failure = {
            row[1] for row in connection.execute("PRAGMA table_info(media_records)")
        }
    assert version_after_failure == "1"
    assert "record_key" not in columns_after_failure

    with LocalMetadataIndex.open(path) as index:
        assert index.schema_version == CURRENT_SCHEMA_VERSION
        page = index.query_page(
            FilterSpec(
                include_unmapped=True,
                include_conflicts=True,
                include_protected=True,
                page_size=10,
            )
        )
        assert page.total_count == 1
        assert page.items[0].record_key.startswith("media-")


def test_corrupt_and_newer_indexes_fail_closed(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not sqlite")
    with pytest.raises(IndexOpenError):
        LocalMetadataIndex.open(corrupt)

    newer = tmp_path / "newer.sqlite"
    with sqlite3.connect(newer) as connection:
        connection.execute("CREATE TABLE index_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO index_meta VALUES ('schema_version', '99')")
    with pytest.raises(IndexOpenError, match="newer"):
        LocalMetadataIndex.open(newer)


def test_clear_deletes_only_index_and_leaves_source_sentinel_untouched(tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    source = tmp_path / "synthetic-source" / "wxid_source" / "msg" / "attach" / "one.dat"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source sentinel")
    before = source.read_bytes()
    with LocalMetadataIndex.open(index_path) as index:
        index.upsert_records([make_record(1)])
        receipt = index.clear()
        assert receipt.deleted_file is True
        assert receipt.removed_records == 1
    assert not index_path.exists()
    assert source.read_bytes() == before


def test_one_hundred_thousand_streaming_records_and_index_query(tmp_path: Path) -> None:
    path = tmp_path / "large.sqlite"

    def records():
        for number in range(100_000):
            yield make_record(
                number,
                account_id="wxid_index_alpha" if number % 2 else "wxid_index_beta",
                media_type=MediaType.IMAGE if number % 3 else MediaType.FILE,
                byte_size=number % 4096,
            )

    with LocalMetadataIndex.open(path) as index:
        assert index.upsert_records(records(), batch_size=2_000) == 100_000
        spec = FilterSpec(
            account_ids=("wxid_index_beta",),
            min_bytes=3000,
            include_unmapped=True,
            include_conflicts=True,
            include_protected=True,
            page_size=100,
        )
        started = time.perf_counter()
        page = index.query_page(spec)
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert page.total_count > 0
        assert len(page.items) == 100
        assert elapsed_ms < 300, f"query took {elapsed_ms:.1f} ms"
        assert index.stats().record_count == 100_000


def test_index_does_not_store_private_payload_columns(tmp_path: Path) -> None:
    path = tmp_path / "privacy.sqlite"
    with LocalMetadataIndex.open(path) as index:
        index.upsert_records([make_record(1)])
        tables = {
            row[0]
            for row in index._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        columns = {
            row[1]
            for table in tables
            for row in index._connection.execute(f"PRAGMA table_info({table})")
        }
    forbidden = {"body", "content", "secret", "password", "pixel"}
    assert not any(any(token in column.lower() for token in forbidden) for column in columns)
    assert "relative_path" in columns
