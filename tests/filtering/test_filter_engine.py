from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

import pytest
from pydantic import ValidationError

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
    SelectionScope,
)
from wechat_cleaner.filtering import (
    ExclusionCode,
    FilterError,
    RetentionPolicy,
    build_selection_snapshot,
    evaluate_eligibility,
    filter_records,
)

NOW = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)
SCAN_A = UUID("11111111-1111-4111-8111-111111111111")
SCAN_B = UUID("22222222-2222-4222-8222-222222222222")


def make_record(
    key: str,
    *,
    account_id: str = "wxid_filter_a",
    path: str | None = None,
    media_type: MediaType = MediaType.IMAGE,
    size: int = 100,
    confidence: MappingConfidence = MappingConfidence.EXACT,
    file_status: FileStatus = FileStatus.PRESENT,
    ownership: OwnershipStatus = OwnershipStatus.MAPPED,
    contact_id: str | None = "wxid_filter_friend_a",
    contact_kind: ContactKind = ContactKind.DIRECT,
    message_time: datetime | None = NOW - timedelta(days=30),
    observed_at: datetime = NOW,
    scan_id: UUID | None = SCAN_A,
    scan_digest: str | None = "a" * 64,
    regenerable_cache: bool = False,
    conflict_reasons: tuple[str, ...] = (),
    protection_reasons: tuple[str, ...] = (),
) -> MediaRecord:
    if path is None:
        path = rf"msg\attach\{key}.dat"
    contact = (
        ContactRef(
            account_id=account_id,
            contact_id=contact_id,
            kind=contact_kind,
            session_hash="a" * 32,
        )
        if contact_id is not None
        else None
    )
    return MediaRecord(
        media_id=uuid5(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), key),
        account_id=account_id,
        file=FileIdentity(relative_path=path, byte_size=size, modified_time_ns=1),
        media_type=media_type,
        observed_at=observed_at,
        contact=contact,
        message_local_id=(
            int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big")
            if confidence is not MappingConfidence.UNMAPPED
            else None
        ),
        mapping_confidence=confidence,
        mapping_reason="synthetic filtering fixture",
        record_key=key,
        scan_id=scan_id,
        scan_manifest_sha256=scan_digest,
        message_time=message_time,
        file_status=file_status,
        ownership_status=ownership,
        protection_reasons=protection_reasons,
        conflict_reasons=conflict_reasons,
        is_regenerable_cache=regenerable_cache,
    )


@pytest.fixture
def records() -> tuple[MediaRecord, ...]:
    return (
        make_record("a-image-100", size=100),
        make_record("a-video-200", media_type=MediaType.VIDEO, size=200),
        make_record(
            "a-retained",
            size=300,
            message_time=NOW - timedelta(days=1),
        ),
        make_record("a-low", size=50, confidence=MappingConfidence.LOW),
        make_record(
            "a-changed",
            size=60,
            confidence=MappingConfidence.MEDIUM,
            file_status=FileStatus.CHANGED,
        ),
        make_record(
            "a-duplicate",
            size=70,
            confidence=MappingConfidence.MEDIUM,
            file_status=FileStatus.DUPLICATE,
        ),
        make_record(
            "a-protected",
            path=r"db_storage\message.db",
            confidence=MappingConfidence.UNMAPPED,
            ownership=OwnershipStatus.PROTECTED,
            contact_id=None,
            protection_reasons=("database storage",),
        ),
        make_record(
            "a-conflict",
            confidence=MappingConfidence.MEDIUM,
            ownership=OwnershipStatus.CONFLICT,
            conflict_reasons=("candidate contacts disagree",),
        ),
        make_record(
            "a-unmapped",
            confidence=MappingConfidence.UNMAPPED,
            ownership=OwnershipStatus.UNMAPPED,
            contact_id=None,
        ),
        make_record(
            "a-cache",
            path=r"cache\preview.bin",
            confidence=MappingConfidence.MEDIUM,
            contact_id=None,
            regenerable_cache=True,
        ),
        make_record(
            "a-unknown-location",
            path=r"mystery\payload.bin",
            confidence=MappingConfidence.MEDIUM,
        ),
        make_record(
            "b-image",
            account_id="wxid_filter_b",
            path=r"msg\attach\b-image.dat",
            size=400,
            scan_id=SCAN_B,
            scan_digest="b" * 64,
        ),
    )


def test_filters_use_or_within_field_and_and_across_fields(records) -> None:
    result = filter_records(
        records,
        FilterSpec(
            account_ids=("wxid_filter_a", "wxid_filter_b"),
            media_types=(MediaType.IMAGE, MediaType.VIDEO),
            min_bytes=100,
            include_protected=True,
            include_conflicts=True,
            include_unmapped=True,
            mapping_confidences=(MappingConfidence.EXACT, MappingConfidence.HIGH),
            sort_by=FilterSortKey.BYTES,
            descending=False,
        ),
    )

    assert [record.record_key for record in result.records] == [
        "a-image-100",
        "a-video-200",
        "a-retained",
        "b-image",
    ]
    assert result.summary.total_count == 4
    assert result.summary.total_bytes == 1000
    assert result.summary.eligible_count == 4
    assert result.summary.filter_digest == result.filter_digest


def test_pagination_is_stable_and_cursor_is_bound_to_spec(records) -> None:
    spec = FilterSpec(
        include_protected=True,
        include_conflicts=True,
        include_unmapped=True,
        page_size=3,
        sort_by=FilterSortKey.BYTES,
        descending=True,
    )
    first = filter_records(records, spec)
    assert first.next_cursor is not None
    assert len(first.records) == 3
    pages = [first.records]
    cursor = first.next_cursor
    while cursor is not None:
        page = filter_records(records, spec, cursor=cursor)
        pages.append(page.records)
        cursor = page.next_cursor
    assert tuple(item for page in pages for item in page) == first.all_records
    with pytest.raises(FilterError, match="cursor"):
        filter_records(records, spec.model_copy(update={"min_bytes": 1}), cursor=first.next_cursor)


def test_security_priority_and_visibility_flags(records) -> None:
    protected = next(record for record in records if record.record_key == "a-protected")
    decision = evaluate_eligibility(
        protected,
        retention=RetentionPolicy(retain_record_keys=(protected.record_key,)),
    )
    assert decision.codes[:2] == (ExclusionCode.PROTECTED, ExclusionCode.RETAINED)
    assert not decision.eligible

    hidden = filter_records(records, FilterSpec())
    assert "a-protected" not in {record.record_key for record in hidden.all_records}
    assert "a-conflict" not in {record.record_key for record in hidden.all_records}
    assert "a-unmapped" not in {record.record_key for record in hidden.all_records}
    assert {item.code for item in hidden.visibility_exclusions} >= {
        ExclusionCode.PROTECTED,
        ExclusionCode.CONFLICT,
        ExclusionCode.UNMAPPED,
    }

    visible_unsafe = filter_records(
        records,
        FilterSpec(include_protected=True, include_conflicts=True, include_unmapped=True),
    )
    assert "a-protected" in {record.record_key for record in visible_unsafe.all_records}
    assert "a-protected" in {item.record_key for item in visible_unsafe.exclusions}
    assert "a-protected" not in {record.record_key for record in visible_unsafe.eligible_records}


def test_retention_and_eligibility_reasons_are_aggregated(records) -> None:
    result = filter_records(
        records,
        FilterSpec(include_protected=True, include_conflicts=True, include_unmapped=True),
        retention=RetentionPolicy(
            retain_after=NOW - timedelta(days=7), allow_regenerable_cache=True
        ),
    )
    excluded_keys = {item.record_key for item in result.exclusions}
    assert {
        "a-retained",
        "a-low",
        "a-changed",
        "a-duplicate",
        "a-unknown-location",
    } <= excluded_keys
    assert "a-cache" in {record.record_key for record in result.eligible_records}
    assert "a-image-100" in {record.record_key for record in result.eligible_records}
    aggregate_keys = {item.key for item in result.summary.exclusion_reasons}
    assert {
        "retained",
        "low_confidence",
        "file_changed",
        "duplicate",
        "unknown_location",
    } <= aggregate_keys
    assert (
        result.summary.excluded_count + result.summary.eligible_count
        == result.summary.total_count
    )
    assert (
        result.summary.excluded_bytes + result.summary.eligible_bytes
        == result.summary.total_bytes
    )


def test_selection_scopes_are_distinct_and_bind_to_one_scan(records) -> None:
    safe_spec = FilterSpec(
        account_ids=("wxid_filter_a",),
        contact_ids=("wxid_filter_friend_a",),
        min_bytes=150,
        page_size=2,
        sort_by=FilterSortKey.BYTES,
        descending=False,
    )
    result = filter_records(records, safe_spec)
    expiry = NOW + timedelta(hours=1)
    page_snapshot = build_selection_snapshot(
        result, SelectionScope.CURRENT_PAGE, expires_at=expiry, now=NOW
    )
    result_snapshot = build_selection_snapshot(
        result, SelectionScope.CURRENT_RESULT, expires_at=expiry, now=NOW
    )
    manual_snapshot = build_selection_snapshot(
        result,
        SelectionScope.MANUAL,
        selected_records=(result.all_records[0],),
        expires_at=expiry,
        now=NOW,
    )
    assert page_snapshot.scope is SelectionScope.CURRENT_PAGE
    assert result_snapshot.scope is SelectionScope.CURRENT_RESULT
    assert manual_snapshot.scope is SelectionScope.MANUAL
    assert len(
        {
            page_snapshot.selection_digest,
            result_snapshot.selection_digest,
            manual_snapshot.selection_digest,
        }
    ) == 3
    assert page_snapshot.selected_count == len(page_snapshot.selected_media_ids)
    assert page_snapshot.selected_bytes == sum(
        record.file.byte_size for record in result.records
    )


def test_selection_rejects_unsafe_or_cross_lineage_records(records) -> None:
    result = filter_records(
        records,
        FilterSpec(include_protected=True, include_conflicts=True, include_unmapped=True),
    )
    unsafe = next(record for record in result.all_records if record.record_key == "a-low")
    with pytest.raises(FilterError, match="ineligible"):
        build_selection_snapshot(
            result,
            SelectionScope.MANUAL,
            selected_records=(unsafe,),
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
        )

    cross_lineage = filter_records(
        records,
        FilterSpec(include_protected=True, include_conflicts=True, include_unmapped=True),
    )
    safe_a = next(
        record
        for record in cross_lineage.eligible_records
        if record.account_id == "wxid_filter_a"
    )
    safe_b = next(
        record
        for record in cross_lineage.eligible_records
        if record.account_id == "wxid_filter_b"
    )
    with pytest.raises(FilterError, match="one scan lineage"):
        build_selection_snapshot(
            cross_lineage,
            SelectionScope.MANUAL,
            selected_records=(safe_a, safe_b),
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
        )


def test_duplicate_input_and_expiry_are_rejected(records) -> None:
    with pytest.raises(FilterError, match="duplicate media_id"):
        filter_records(records + (records[0],), FilterSpec())
    result = filter_records(records[:1], FilterSpec())
    with pytest.raises(ValidationError):
        build_selection_snapshot(
            result,
            SelectionScope.CURRENT_PAGE,
            expires_at=NOW,
            now=NOW,
        )
