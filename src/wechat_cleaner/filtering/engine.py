"""Pure filtering, eligibility and selection logic for Phase 3.

The engine operates only on :class:`~wechat_cleaner.domain.MediaRecord` values.
It never reads a path, opens a database, or mutates a record.  Visibility
filters and cleanup eligibility are deliberately separate: a user may ask to
see a protected or ambiguous record, but no include flag can make that record
eligible for a cleanup plan.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from wechat_cleaner.domain.contracts import (
    FileStatus,
    FilterAggregate,
    FilterResultPage,
    FilterResultSummary,
    FilterSortKey,
    FilterSpec,
    MappingConfidence,
    MediaRecord,
    OwnershipStatus,
    SelectionScope,
    SelectionSnapshot,
)


class FilterError(ValueError):
    """Raised when a query, cursor or selection cannot be safely evaluated."""


class ExclusionCode(StrEnum):
    """Stable, non-sensitive reasons why a record cannot be selected."""

    PROTECTED = "protected"
    RETAINED = "retained"
    LOW_CONFIDENCE = "low_confidence"
    UNMAPPED = "unmapped"
    CONFLICT = "conflict"
    UNKNOWN_OWNERSHIP = "unknown_ownership"
    UNKNOWN_LOCATION = "unknown_location"
    UNKNOWN_MEDIA_TYPE = "unknown_media_type"
    FILE_MISSING = "file_missing"
    FILE_CHANGED = "file_changed"
    DUPLICATE = "duplicate"
    CACHE_REQUIRES_OPT_IN = "cache_requires_opt_in"


_SAFE_CONFIDENCE = frozenset(
    {MappingConfidence.EXACT, MappingConfidence.HIGH, MappingConfidence.MEDIUM}
)
_KNOWN_ROOTS = frozenset({"msg", "cache", "business", "db_storage", "sendtemp", "all_users"})
_PROTECTED_ROOTS = frozenset({"all_users", "db_storage", "sendtemp"})
_CURSOR_VERSION = "f1"


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Explicit keep rules evaluated before ordinary eligibility rules.

    ``retain_after`` keeps records whose message time is on or after the
    threshold.  ID/contact rules are useful for a user-defined allowlist.  A
    regenerable cache remains ineligible until ``allow_regenerable_cache`` is
    explicitly enabled; that flag does not override protected paths or other
    safety checks.
    """

    retain_after: datetime | None = None
    retain_media_ids: tuple[UUID, ...] = ()
    retain_record_keys: tuple[str, ...] = ()
    retain_contact_ids: tuple[str, ...] = ()
    allow_regenerable_cache: bool = False

    def __post_init__(self) -> None:
        if self.retain_after is not None:
            if self.retain_after.tzinfo is None or self.retain_after.utcoffset() is None:
                raise FilterError("retention threshold must include a timezone")
            object.__setattr__(self, "retain_after", self.retain_after.astimezone(UTC))
        for field_name in ("retain_media_ids", "retain_record_keys", "retain_contact_ids"):
            values = tuple(getattr(self, field_name))
            if len(values) != len(set(values)):
                raise FilterError(f"{field_name} cannot contain duplicates")
            if field_name != "retain_media_ids" and any(not value.strip() for value in values):
                raise FilterError(f"{field_name} cannot contain empty values")
            object.__setattr__(self, field_name, values)


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    """Safety classification for one record, with priority-ordered reasons."""

    eligible: bool
    codes: tuple[ExclusionCode, ...] = ()
    messages: tuple[str, ...] = ()

    @property
    def primary_code(self) -> ExclusionCode | None:
        return self.codes[0] if self.codes else None


@dataclass(frozen=True, slots=True)
class FilterExclusion:
    """A structured, path-free explanation for one visible excluded record."""

    media_id: UUID
    record_key: str
    code: ExclusionCode
    reason: str
    all_codes: tuple[ExclusionCode, ...]
    byte_size: int


@dataclass(frozen=True, slots=True)
class FilterResult:
    """Full filter output plus the requested page and audit information."""

    all_records: tuple[MediaRecord, ...]
    records: tuple[MediaRecord, ...]
    eligible_records: tuple[MediaRecord, ...]
    exclusions: tuple[FilterExclusion, ...]
    visibility_exclusions: tuple[FilterExclusion, ...]
    summary: FilterResultSummary
    page: FilterResultPage
    filter_digest: str

    @property
    def items(self) -> tuple[MediaRecord, ...]:
        """Alias for page items used by GUI and CLI consumers."""

        return self.records

    @property
    def next_cursor(self) -> str | None:
        return self.page.next_cursor


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FilterError(f"{label} must include a timezone")
    return value.astimezone(UTC)


def _record_key(record: MediaRecord) -> str:
    return record.record_key or f"media-{record.media_id.hex}"


def _path_parts(record: MediaRecord) -> tuple[str, ...]:
    normalized = record.file.relative_path.replace("/", "\\")
    return tuple(part.casefold() for part in normalized.split("\\"))


def _is_protected(record: MediaRecord) -> bool:
    parts = _path_parts(record)
    return (
        record.ownership_status is OwnershipStatus.PROTECTED
        or bool(_PROTECTED_ROOTS.intersection(parts))
        or parts[:2] == ("business", "favorite")
    )


def _is_unknown_location(record: MediaRecord) -> bool:
    parts = _path_parts(record)
    return not parts or parts[0] not in _KNOWN_ROOTS


def _is_retained(record: MediaRecord, policy: RetentionPolicy) -> bool:
    if record.media_id in policy.retain_media_ids:
        return True
    if record.record_key and record.record_key in policy.retain_record_keys:
        return True
    if record.contact and record.contact.contact_id in policy.retain_contact_ids:
        return True
    return (
        policy.retain_after is not None
        and record.message_time is not None
        and record.message_time >= policy.retain_after
    )


def _append_reason(
    codes: list[ExclusionCode], messages: list[str], code: ExclusionCode, message: str
) -> None:
    if code not in codes:
        codes.append(code)
        messages.append(message)


def evaluate_eligibility(
    record: MediaRecord, *, retention: RetentionPolicy | None = None
) -> EligibilityDecision:
    """Classify a record without allowing include filters to weaken safety."""

    policy = retention or RetentionPolicy()
    codes: list[ExclusionCode] = []
    messages: list[str] = []

    # The order here is an intentional policy contract: protected, retained,
    # eligibility, and only then caller-selected include filters.
    if _is_protected(record):
        _append_reason(
            codes,
            messages,
            ExclusionCode.PROTECTED,
            "protected database, favourite, temporary or explicitly protected path",
        )
    if _is_retained(record, policy):
        _append_reason(
            codes, messages, ExclusionCode.RETAINED, "retention policy keeps this record"
        )
    if _is_unknown_location(record):
        _append_reason(
            codes,
            messages,
            ExclusionCode.UNKNOWN_LOCATION,
            "record location is outside known media roots",
        )
    if record.ownership_status is OwnershipStatus.CONFLICT:
        _append_reason(codes, messages, ExclusionCode.CONFLICT, "ownership evidence conflicts")
    elif record.ownership_status is OwnershipStatus.UNMAPPED:
        _append_reason(codes, messages, ExclusionCode.UNMAPPED, "record has no mapped ownership")
    elif record.ownership_status is OwnershipStatus.UNKNOWN:
        _append_reason(
            codes, messages, ExclusionCode.UNKNOWN_OWNERSHIP, "ownership state is unknown"
        )
    if record.mapping_confidence is MappingConfidence.LOW:
        _append_reason(codes, messages, ExclusionCode.LOW_CONFIDENCE, "mapping is low confidence")
    elif record.mapping_confidence is MappingConfidence.UNMAPPED:
        _append_reason(codes, messages, ExclusionCode.UNMAPPED, "mapping has no credible evidence")
    if record.file_status is FileStatus.MISSING:
        _append_reason(
            codes,
            messages,
            ExclusionCode.FILE_MISSING,
            "file was not present at observation",
        )
    elif record.file_status is FileStatus.CHANGED:
        _append_reason(
            codes,
            messages,
            ExclusionCode.FILE_CHANGED,
            "file identity changed after observation",
        )
    elif record.file_status is FileStatus.DUPLICATE:
        _append_reason(
            codes, messages, ExclusionCode.DUPLICATE, "duplicate candidate requires review"
        )
    if record.media_type.value == "other":
        _append_reason(
            codes,
            messages,
            ExclusionCode.UNKNOWN_MEDIA_TYPE,
            "media type is not supported",
        )
    parts = _path_parts(record)
    if parts and parts[0] == "cache" and (
        not policy.allow_regenerable_cache or not record.is_regenerable_cache
    ):
        _append_reason(
            codes,
            messages,
            ExclusionCode.CACHE_REQUIRES_OPT_IN,
            "cache records require explicit cache policy opt-in",
        )

    # Retention and every safety state are exclusions from execution.  An
    # explicitly enabled cache only removes the opt-in reason; it never clears
    # an unmapped, protected, changed, or otherwise unsafe reason.
    return EligibilityDecision(eligible=not codes, codes=tuple(codes), messages=tuple(messages))


def _visibility_exclusion(
    record: MediaRecord, spec: FilterSpec, decision: EligibilityDecision
) -> tuple[ExclusionCode, str] | None:
    if _is_protected(record) and not spec.include_protected:
        return ExclusionCode.PROTECTED, "protected records are hidden unless explicitly included"
    if (
        record.ownership_status is OwnershipStatus.CONFLICT
        and not spec.include_conflicts
    ):
        return ExclusionCode.CONFLICT, "conflict records are hidden unless explicitly included"
    if (
        (
            record.ownership_status in {OwnershipStatus.UNMAPPED, OwnershipStatus.UNKNOWN}
            or record.mapping_confidence is MappingConfidence.UNMAPPED
        )
        and not spec.include_unmapped
    ):
        return ExclusionCode.UNMAPPED, "unmapped records are hidden unless explicitly included"
    return None


def _matches_spec(record: MediaRecord, spec: FilterSpec) -> bool:
    if spec.account_ids and record.account_id not in spec.account_ids:
        return False
    if spec.contact_ids and (
        record.contact is None or record.contact.contact_id not in spec.contact_ids
    ):
        return False
    if spec.media_types and record.media_type not in spec.media_types:
        return False
    if spec.mapping_confidences and record.mapping_confidence not in spec.mapping_confidences:
        return False
    if spec.file_statuses and record.file_status not in spec.file_statuses:
        return False
    if spec.ownership_statuses and record.ownership_status not in spec.ownership_statuses:
        return False
    if spec.message_time_from is not None and (
        record.message_time is None or record.message_time < spec.message_time_from
    ):
        return False
    if spec.message_time_to is not None and (
        record.message_time is None or record.message_time > spec.message_time_to
    ):
        return False
    if spec.observed_time_from is not None and record.observed_at < spec.observed_time_from:
        return False
    if spec.observed_time_to is not None and record.observed_at > spec.observed_time_to:
        return False
    if spec.min_bytes is not None and record.file.byte_size < spec.min_bytes:
        return False
    if spec.max_bytes is not None and record.file.byte_size > spec.max_bytes:
        return False
    return True


def _sort_key(record: MediaRecord, spec: FilterSpec) -> tuple[object, str, str]:
    if spec.sort_by is FilterSortKey.BYTES:
        primary: object = record.file.byte_size
    elif spec.sort_by is FilterSortKey.MESSAGE_TIME:
        primary = record.message_time or datetime.min.replace(tzinfo=UTC)
    elif spec.sort_by is FilterSortKey.OBSERVED_AT:
        primary = record.observed_at
    else:
        primary = str(record.media_id)
    return primary, str(record.media_id), _record_key(record)


def filter_digest(spec: FilterSpec) -> str:
    """Return the SHA-256 digest that binds a result and selection snapshot."""

    payload = spec.model_dump(mode="json")
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _exclusion(
    record: MediaRecord, decision: EligibilityDecision, code: ExclusionCode | None = None,
    reason: str | None = None,
) -> FilterExclusion:
    primary = code or decision.primary_code
    if primary is None:
        raise FilterError("cannot create an exclusion for an eligible record")
    index = decision.codes.index(primary) if primary in decision.codes else 0
    return FilterExclusion(
        media_id=record.media_id,
        record_key=_record_key(record),
        code=primary,
        reason=reason or decision.messages[index],
        all_codes=decision.codes or (primary,),
        byte_size=record.file.byte_size,
    )


def _aggregate(records: Sequence[MediaRecord], key: str) -> tuple[FilterAggregate, ...]:
    buckets: dict[str, list[int]] = {}
    for record in records:
        if key == "media_type":
            value = record.media_type.value
        elif key == "confidence":
            value = record.mapping_confidence.value
        else:
            raise FilterError(f"unsupported aggregate key: {key}")
        bucket = buckets.setdefault(value, [0, 0])
        bucket[0] += 1
        bucket[1] += record.file.byte_size
    return tuple(
        FilterAggregate(key=name, count=values[0], total_bytes=values[1])
        for name, values in sorted(buckets.items())
    )


def _exclusion_aggregate(exclusions: Sequence[FilterExclusion]) -> tuple[FilterAggregate, ...]:
    buckets: dict[str, list[int]] = {}
    for item in exclusions:
        bucket = buckets.setdefault(item.code.value, [0, 0])
        bucket[0] += 1
        bucket[1] += item.byte_size
    return tuple(
        FilterAggregate(key=name, count=values[0], total_bytes=values[1])
        for name, values in sorted(buckets.items())
    )


def _decode_cursor(cursor: str, digest: str, total: int) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = base64.urlsafe_b64decode(padded.encode()).decode()
        version, cursor_digest, raw_offset = value.split(":", 2)
        offset = int(raw_offset)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise FilterError("cursor is invalid or expired") from exc
    if version != _CURSOR_VERSION or cursor_digest != digest or offset < 0 or offset > total:
        raise FilterError("cursor is invalid or expired")
    return offset


def _encode_cursor(digest: str, offset: int) -> str:
    value = f"{_CURSOR_VERSION}:{digest}:{offset}".encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _validate_input(records: Sequence[MediaRecord]) -> None:
    media_ids: set[UUID] = set()
    paths: set[tuple[str, str]] = set()
    for record in records:
        if record.media_id in media_ids:
            raise FilterError("input contains duplicate media_id")
        media_ids.add(record.media_id)
        path_key = (record.account_id, record.file.relative_path.casefold())
        if path_key in paths:
            raise FilterError("input contains duplicate account-relative path")
        paths.add(path_key)


def filter_records(
    records: Iterable[MediaRecord],
    spec: FilterSpec,
    *,
    retention: RetentionPolicy | None = None,
    cursor: str | None = None,
) -> FilterResult:
    """Apply OR-within-field/AND-across-fields filters and return a page.

    ``all_records`` and ``summary`` describe visible records after include
    flags.  ``eligible_records`` and ``exclusions`` describe the safe subset;
    visibility exclusions remain separately auditable and never enter the
    summary totals.
    """

    materialized = tuple(records)
    _validate_input(materialized)
    digest = filter_digest(spec)
    policy = retention or RetentionPolicy()
    visible: list[MediaRecord] = []
    decisions: dict[UUID, EligibilityDecision] = {}
    hidden: list[FilterExclusion] = []
    for record in materialized:
        if not _matches_spec(record, spec):
            continue
        decision = evaluate_eligibility(record, retention=policy)
        visibility = _visibility_exclusion(record, spec, decision)
        if visibility is not None:
            hidden.append(_exclusion(record, decision, visibility[0], visibility[1]))
            continue
        visible.append(record)
        decisions[record.media_id] = decision

    ordered = tuple(
        sorted(visible, key=lambda item: _sort_key(item, spec), reverse=spec.descending)
    )
    ordered_eligible = tuple(record for record in ordered if decisions[record.media_id].eligible)
    ordered_exclusions = tuple(
        _exclusion(record, decisions[record.media_id])
        for record in ordered
        if not decisions[record.media_id].eligible
    )
    total_bytes = sum(record.file.byte_size for record in ordered)
    eligible_bytes = sum(record.file.byte_size for record in ordered_eligible)
    summary = FilterResultSummary(
        total_count=len(ordered),
        total_bytes=total_bytes,
        eligible_count=len(ordered_eligible),
        eligible_bytes=eligible_bytes,
        excluded_count=len(ordered_exclusions),
        excluded_bytes=total_bytes - eligible_bytes,
        by_media_type=_aggregate(ordered, "media_type"),
        by_confidence=_aggregate(ordered, "confidence"),
        exclusion_reasons=_exclusion_aggregate(ordered_exclusions),
        filter_digest=digest,
    )
    offset = _decode_cursor(cursor, digest, len(ordered)) if cursor else 0
    page_items = ordered[offset : offset + spec.page_size]
    next_cursor = (
        _encode_cursor(digest, offset + spec.page_size)
        if offset + spec.page_size < len(ordered)
        else None
    )
    page = FilterResultPage(
        items=page_items,
        page_size=spec.page_size,
        next_cursor=next_cursor,
        summary=summary,
    )
    return FilterResult(
        all_records=ordered,
        records=page_items,
        eligible_records=ordered_eligible,
        exclusions=ordered_exclusions,
        visibility_exclusions=tuple(hidden),
        summary=summary,
        page=page,
        filter_digest=digest,
    )


def paginate_records(
    records: Iterable[MediaRecord],
    spec: FilterSpec,
    *,
    cursor: str | None = None,
    retention: RetentionPolicy | None = None,
) -> FilterResultPage:
    """Convenience API returning only the contract page."""

    return filter_records(records, spec, retention=retention, cursor=cursor).page


def _selection_records(
    result: FilterResult,
    scope: SelectionScope,
    selected: Iterable[MediaRecord] | None,
) -> tuple[MediaRecord, ...]:
    if scope is SelectionScope.CURRENT_PAGE:
        if selected is not None:
            raise FilterError("current_page selection does not accept manual records")
        return result.records
    if scope is SelectionScope.CURRENT_RESULT:
        if selected is not None:
            raise FilterError("current_result selection does not accept manual records")
        return result.all_records
    if selected is None:
        raise FilterError("manual selection requires selected records")
    return tuple(selected)


def build_selection_snapshot(
    result: FilterResult,
    scope: SelectionScope,
    *,
    expires_at: datetime,
    selected_records: Iterable[MediaRecord] | None = None,
    now: datetime | None = None,
) -> SelectionSnapshot:
    """Create an immutable, lineage-bound snapshot of eligible records only."""

    chosen = _selection_records(result, scope, selected_records)
    available = {record.media_id: record for record in result.all_records}
    if not chosen:
        raise FilterError("selection contains no visible records")
    if len({record.media_id for record in chosen}) != len(chosen):
        raise FilterError("selection contains duplicate media_id")
    if any(
        record.media_id not in available or available[record.media_id] != record
        for record in chosen
    ):
        raise FilterError("manual selection contains a record outside the filter result")
    policy_by_id = {item.media_id: item for item in result.eligible_records}
    if any(record.media_id not in policy_by_id for record in chosen):
        raise FilterError("selection contains ineligible records")

    scan_ids = {record.scan_id for record in chosen}
    manifests = {record.scan_manifest_sha256 for record in chosen}
    if len(scan_ids) != 1 or len(manifests) != 1 or None in scan_ids or None in manifests:
        raise FilterError("selection must bind to exactly one scan lineage")
    scan_id = next(iter(scan_ids))
    scan_manifest_sha256 = next(iter(manifests))
    assert scan_id is not None and scan_manifest_sha256 is not None
    created_at = _aware(now or datetime.now(UTC), "created_at")
    expiry = _aware(expires_at, "expires_at")
    selected_media_ids = tuple(record.media_id for record in chosen)
    selected_record_keys = tuple(_record_key(record) for record in chosen)
    selected_bytes = sum(record.file.byte_size for record in chosen)
    digest_payload = {
        "scope": scope.value,
        "account_ids": sorted({record.account_id for record in chosen}),
        "scan_id": str(scan_id),
        "scan_manifest_sha256": scan_manifest_sha256,
        "filter_digest": result.filter_digest,
        "selected_media_ids": [str(value) for value in selected_media_ids],
        "selected_record_keys": selected_record_keys,
        "selected_count": len(chosen),
        "selected_bytes": selected_bytes,
        "created_at": created_at.isoformat(),
        "expires_at": expiry.isoformat(),
    }
    selection_digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return SelectionSnapshot(
        scope=scope,
        account_ids=tuple(sorted({record.account_id for record in chosen})),
        scan_id=scan_id,
        scan_manifest_sha256=scan_manifest_sha256,
        filter_digest=result.filter_digest,
        selected_media_ids=selected_media_ids,
        selected_record_keys=selected_record_keys,
        selected_count=len(chosen),
        selected_bytes=selected_bytes,
        selection_digest=selection_digest,
        created_at=created_at,
        expires_at=expiry,
    )


# Names used by callers that prefer verb-oriented APIs.
apply_filter = filter_records
compute_filter_digest = filter_digest
create_selection_snapshot = build_selection_snapshot
