"""Versioned, side-effect-free contracts shared by scanners, UI and executors.

All paths supplied by an adapter are absolute Windows paths, except
``FileIdentity.relative_path`` which is deliberately constrained to a relative
path below the account root.  Cleanup contracts are plans only: execution is a
separate, non-agent component and must re-check identities immediately before
any mutation.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"
P3_SCHEMA_VERSION = "1.1"
SUPPORTED_SCHEMA_MAJOR = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SESSION_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
WINDOWS_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)")
SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_schema_version(value: str) -> str:
    """Accept only the supported major version at a public contract boundary."""
    try:
        major, minor = value.split(".", maxsplit=1)
        if not major.isdigit() or not minor.isdigit():
            raise ValueError
    except (AttributeError, ValueError):
        raise ValueError("schema_version must have the form '<major>.<minor>'") from None
    if int(major) != SUPPORTED_SCHEMA_MAJOR:
        raise ValueError(
            f"unsupported schema major {major}; supported major is {SUPPORTED_SCHEMA_MAJOR}"
        )
    return value


class ContractModel(BaseModel):
    """Base class for serializable contracts with strict, versioned input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    schema_version: str = Field(default=SCHEMA_VERSION, examples=[SCHEMA_VERSION])

    @field_validator("schema_version")
    @classmethod
    def supported_schema_version(cls, value: str) -> str:
        return _validate_schema_version(value)


class FrozenContractModel(ContractModel):
    """Contract whose fields cannot be reassigned after validation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class Phase3ContractModel(FrozenContractModel):
    """Version 1.1 contracts introduced for the desktop workflow."""

    schema_version: str = Field(default=P3_SCHEMA_VERSION, examples=[P3_SCHEMA_VERSION])


class CompatibilityMode(StrEnum):
    """How a caller may process a known schema variant."""

    STRICT = "strict"
    BACKWARD_READ = "backward_read"
    READ_ONLY_LEGACY = "read_only_legacy"


class CompatibilityPolicy(FrozenContractModel):
    mode: CompatibilityMode = CompatibilityMode.STRICT
    accepted_schema_versions: tuple[str, ...] = (SCHEMA_VERSION,)
    allow_mutation: bool = False

    @field_validator("accepted_schema_versions")
    @classmethod
    def validate_versions(cls, versions: tuple[str, ...]) -> tuple[str, ...]:
        if not versions:
            raise ValueError("accepted_schema_versions cannot be empty")
        return tuple(_validate_schema_version(version) for version in versions)

    @model_validator(mode="after")
    def legacy_mode_is_read_only(self) -> CompatibilityPolicy:
        if self.mode is CompatibilityMode.READ_ONLY_LEGACY and self.allow_mutation:
            raise ValueError("read_only_legacy compatibility mode cannot allow mutation")
        return self


class ContractErrorCode(StrEnum):
    SCHEMA_VERSION_UNSUPPORTED = "SCHEMA_VERSION_UNSUPPORTED"
    INVALID_PATH = "INVALID_PATH"
    PATH_OUTSIDE_ACCOUNT_ROOT = "PATH_OUTSIDE_ACCOUNT_ROOT"
    FILE_IDENTITY_MISMATCH = "FILE_IDENTITY_MISMATCH"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    MAPPING_UNAVAILABLE = "MAPPING_UNAVAILABLE"
    DECODER_UNAVAILABLE = "DECODER_UNAVAILABLE"
    DECODE_FAILED = "DECODE_FAILED"
    CACHE_LIMIT_EXCEEDED = "CACHE_LIMIT_EXCEEDED"
    WECHAT_RUNNING = "WECHAT_RUNNING"
    PLAN_INVALID = "PLAN_INVALID"
    PLAN_STALE = "PLAN_STALE"
    EXECUTION_BLOCKED = "EXECUTION_BLOCKED"
    UNKNOWN = "UNKNOWN"


class ContractError(FrozenContractModel):
    code: ContractErrorCode
    message: Annotated[str, Field(min_length=1, max_length=1_000)]
    retryable: bool = False
    context: tuple[tuple[str, str], ...] = ()


class ContactKind(StrEnum):
    DIRECT = "direct"
    GROUP = "group"
    OFFICIAL = "official"
    UNKNOWN = "unknown"


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    FILE = "file"
    THUMBNAIL = "thumbnail"
    VOICE = "voice"
    OTHER = "other"


class MappingConfidence(StrEnum):
    """Strength of evidence connecting a disk file to a WeChat message."""

    EXACT = "exact"  # database identity and on-disk identity both match
    HIGH = "high"  # strong deterministic derivation (e.g. known session path)
    MEDIUM = "medium"  # date/name/size correlation, no direct identity
    LOW = "low"  # heuristic association only; never an automatic cleanup target
    UNMAPPED = "unmapped"  # no credible contact/message association


class FileStatus(StrEnum):
    """Observed file state used by filtering and execution preflight."""

    PRESENT = "present"
    MISSING = "missing"
    CHANGED = "changed"
    DUPLICATE = "duplicate"


# Descriptive alias used by consumers that call the field a file observation status.
FileObservationStatus = FileStatus


class OwnershipStatus(StrEnum):
    """Whether a record can be attributed and offered for a plan."""

    MAPPED = "mapped"
    UNMAPPED = "unmapped"
    CONFLICT = "conflict"
    PROTECTED = "protected"
    UNKNOWN = "unknown"


class SelectionScope(StrEnum):
    CURRENT_PAGE = "current_page"
    CURRENT_RESULT = "current_result"
    MANUAL = "manual"


class FilterSortKey(StrEnum):
    BYTES = "bytes"
    MESSAGE_TIME = "message_time"
    OBSERVED_AT = "observed_at"
    MEDIA_ID = "media_id"


class EvidenceKind(StrEnum):
    MEDIA_PATH = "media_path"
    MEDIA_HASH = "media_hash"
    MESSAGE = "message"
    CONTACT = "contact"
    SESSION = "session"


class CleanupExecutionMode(StrEnum):
    DRY_RUN = "dry_run"
    QUARANTINE = "quarantine"
    RECYCLE_BIN = "recycle_bin"


class RecoveryLocatorKind(StrEnum):
    NONE = "none"
    QUARANTINE = "quarantine"
    RECYCLE_BIN = "recycle_bin"


class RecoveryState(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    AVAILABLE = "available"
    RESTORED = "restored"
    MISSING = "missing"
    UNKNOWN = "unknown"


class DecodeVariant(StrEnum):
    THUMBNAIL = "thumbnail"
    ORIGINAL = "original"


class CleanupDisposition(StrEnum):
    RECYCLE_BIN = "recycle_bin"
    QUARANTINE = "quarantine"
    PERMANENT = "permanent"


class ReceiptStatus(StrEnum):
    PLANNED = "planned"
    DRY_RUN = "dry_run"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"


class AccountRef(FrozenContractModel):
    """A WeChat account and its authoritative filesystem boundary."""

    account_id: Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_@.-]+$")]
    account_root: Annotated[str, Field(min_length=3, max_length=1024)]
    display_name: Annotated[str | None, Field(max_length=128)] = None
    source_version: Annotated[str | None, Field(max_length=64)] = None

    @field_validator("account_root")
    @classmethod
    def absolute_account_root(cls, value: str) -> str:
        normalized = value.replace("/", "\\").rstrip("\\")
        if not WINDOWS_ABSOLUTE_RE.match(normalized):
            raise ValueError("account_root must be an absolute Windows or UNC path")
        return normalized


class ContactRef(FrozenContractModel):
    account_id: Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_@.-]+$")]
    contact_id: Annotated[str, Field(min_length=1, max_length=256)]
    kind: ContactKind
    session_hash: Annotated[str | None, Field(default=None, pattern=r"^[0-9a-f]{32}$")]
    display_name: Annotated[str | None, Field(default=None, max_length=256)]
    remark: Annotated[str | None, Field(default=None, max_length=256)]


class FileIdentity(FrozenContractModel):
    """Stable facts rechecked before a cleanup executor touches a file."""

    relative_path: Annotated[str, Field(min_length=1, max_length=1024)]
    byte_size: Annotated[int, Field(ge=0)]
    modified_time_ns: Annotated[int, Field(ge=0)]
    sha256: Annotated[str | None, Field(default=None, pattern=r"^[0-9a-f]{64}$")]

    @field_validator("relative_path")
    @classmethod
    def account_relative_path(cls, value: str) -> str:
        normalized = value.replace("/", "\\")
        segments = normalized.split("\\")
        if (
            not normalized
            or WINDOWS_ABSOLUTE_RE.match(normalized)
            or any(segment in {"", ".", ".."} for segment in segments)
        ):
            raise ValueError("relative_path must stay below account_root without traversal")
        return normalized


class ScanManifest(FrozenContractModel):
    scan_id: UUID = Field(default_factory=uuid4)
    account: AccountRef
    scanned_at: datetime = Field(default_factory=_utc_now)
    scanner_version: Annotated[str, Field(min_length=1, max_length=64)]
    indexed_roots: tuple[str, ...] = ()
    file_count: Annotated[int, Field(ge=0)]
    total_bytes: Annotated[int, Field(ge=0)]
    manifest_sha256: Annotated[str | None, Field(default=None, pattern=r"^[0-9a-f]{64}$")]
    errors: tuple[ContractError, ...] = ()

    @field_validator("scanned_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scanned_at must include a timezone")
        return value.astimezone(UTC)

    @field_validator("indexed_roots")
    @classmethod
    def validate_indexed_roots(cls, roots: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            FileIdentity(relative_path=root, byte_size=0, modified_time_ns=0).relative_path
            for root in roots
        )


class ScanLineage(Phase3ContractModel):
    """Minimal immutable identity binding a record to one scanner observation."""

    scan_id: UUID
    account: AccountRef
    scanned_at: datetime
    scanner_version: Annotated[str, Field(min_length=1, max_length=64)]
    manifest_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    source_copy_id: Annotated[str | None, Field(default=None, min_length=1, max_length=128)] = None

    @field_validator("scanned_at")
    @classmethod
    def lineage_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scanned_at must include a timezone")
        return value.astimezone(UTC)


class MediaRecord(FrozenContractModel):
    media_id: UUID = Field(default_factory=uuid4)
    account_id: Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_@.-]+$")]
    file: FileIdentity
    media_type: MediaType
    observed_at: datetime
    contact: ContactRef | None = None
    message_local_id: Annotated[int | None, Field(default=None, ge=0)] = None
    mapping_confidence: MappingConfidence
    mapping_reason: Annotated[str, Field(min_length=1, max_length=512)]
    original_media_id: UUID | None = None
    is_regenerable_cache: bool = False
    # Phase 3 additions are optional so v1.0 serialized records remain readable.
    record_key: Annotated[str | None, Field(default=None, min_length=1, max_length=256)] = None
    scan_id: UUID | None = None
    scan_manifest_sha256: Annotated[
        str | None, Field(default=None, pattern=r"^[0-9a-f]{64}$")
    ] = None
    message_time: datetime | None = None
    file_status: FileStatus = FileStatus.PRESENT
    ownership_status: OwnershipStatus = OwnershipStatus.MAPPED
    protection_reasons: tuple[
        Annotated[str, Field(min_length=1, max_length=256)], ...
    ] = ()
    conflict_reasons: tuple[
        Annotated[str, Field(min_length=1, max_length=256)], ...
    ] = ()

    @field_validator("observed_at")
    @classmethod
    def observed_at_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        return value.astimezone(UTC)

    @field_validator("message_time")
    @classmethod
    def message_time_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("message_time must include a timezone")
        return value.astimezone(UTC)

    @field_validator("record_key")
    @classmethod
    def stable_record_key_is_safe(cls, value: str | None) -> str | None:
        if value is not None and not SAFE_KEY_RE.fullmatch(value):
            raise ValueError("record_key must contain only safe identifier characters")
        return value

    @model_validator(mode="after")
    def contact_belongs_to_account(self) -> MediaRecord:
        if self.contact is not None and self.contact.account_id != self.account_id:
            raise ValueError("contact.account_id must match media account_id")
        if self.mapping_confidence is MappingConfidence.EXACT and self.message_local_id is None:
            raise ValueError("exact mapping requires message_local_id")
        if self.mapping_confidence is MappingConfidence.UNMAPPED and self.contact is not None:
            raise ValueError("unmapped media cannot claim a contact")
        if (self.scan_id is None) != (self.scan_manifest_sha256 is None):
            raise ValueError("scan_id and scan_manifest_sha256 must be provided together")
        if self.ownership_status is OwnershipStatus.UNMAPPED and self.contact is not None:
            raise ValueError("unmapped ownership cannot claim a contact")
        if self.ownership_status is OwnershipStatus.CONFLICT and not self.conflict_reasons:
            raise ValueError("conflict ownership requires conflict_reasons")
        if self.ownership_status is OwnershipStatus.PROTECTED and not self.protection_reasons:
            raise ValueError("protected ownership requires protection_reasons")
        return self


class MediaRecordLineage(Phase3ContractModel):
    """A MediaRecord plus the scan lineage required by Phase 3 consumers."""

    record: MediaRecord
    scan: ScanLineage
    record_key: Annotated[str, Field(min_length=1, max_length=256, pattern=SAFE_KEY_RE.pattern)]

    @model_validator(mode="after")
    def record_matches_scan(self) -> MediaRecordLineage:
        if self.record.account_id != self.scan.account.account_id:
            raise ValueError("record.account_id must match scan.account.account_id")
        if self.record.scan_id is not None and self.record.scan_id != self.scan.scan_id:
            raise ValueError("record.scan_id must match scan.scan_id")
        if (
            self.record.scan_manifest_sha256 is not None
            and self.record.scan_manifest_sha256 != self.scan.manifest_sha256
        ):
            raise ValueError("record.scan_manifest_sha256 must match scan.manifest_sha256")
        if self.record.record_key is not None and self.record.record_key != self.record_key:
            raise ValueError("record.record_key must match lineage record_key")
        return self


class FilterSpec(Phase3ContractModel):
    """Deterministic filter definition: values within a field are OR, fields are AND."""

    spec_id: UUID = Field(default_factory=uuid4)
    account_ids: tuple[
        Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_@.-]+$")], ...
    ] = ()
    contact_ids: tuple[Annotated[str, Field(min_length=1, max_length=256)], ...] = ()
    media_types: tuple[MediaType, ...] = ()
    mapping_confidences: tuple[MappingConfidence, ...] = ()
    file_statuses: tuple[FileStatus, ...] = ()
    ownership_statuses: tuple[OwnershipStatus, ...] = ()
    message_time_from: datetime | None = None
    message_time_to: datetime | None = None
    observed_time_from: datetime | None = None
    observed_time_to: datetime | None = None
    min_bytes: Annotated[int | None, Field(default=None, ge=0)] = None
    max_bytes: Annotated[int | None, Field(default=None, ge=0)] = None
    include_protected: bool = False
    include_conflicts: bool = False
    include_unmapped: bool = False
    sort_by: FilterSortKey = FilterSortKey.BYTES
    descending: bool = True
    page_size: Annotated[int, Field(ge=1, le=500)] = 200

    @field_validator(
        "message_time_from",
        "message_time_to",
        "observed_time_from",
        "observed_time_to",
    )
    @classmethod
    def filter_time_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("filter datetime must include a timezone")
        return value.astimezone(UTC)

    @field_validator(
        "media_types",
        "mapping_confidences",
        "file_statuses",
        "ownership_statuses",
        "account_ids",
        "contact_ids",
    )
    @classmethod
    def filter_values_are_unique(cls, values: tuple) -> tuple:
        if len(set(values)) != len(values):
            raise ValueError("filter values cannot contain duplicates")
        return values

    @model_validator(mode="after")
    def filter_ranges_are_ordered(self) -> FilterSpec:
        if (
            self.min_bytes is not None
            and self.max_bytes is not None
            and self.min_bytes > self.max_bytes
        ):
            raise ValueError("min_bytes cannot exceed max_bytes")
        for lower, upper, label in (
            (self.message_time_from, self.message_time_to, "message_time"),
            (self.observed_time_from, self.observed_time_to, "observed_time"),
        ):
            if lower is not None and upper is not None and lower > upper:
                raise ValueError(f"{label}_from cannot exceed {label}_to")
        return self


class FilterAggregate(Phase3ContractModel):
    """One deterministic aggregate bucket in a result summary."""

    key: Annotated[str, Field(min_length=1, max_length=128)]
    count: Annotated[int, Field(ge=0)]
    total_bytes: Annotated[int, Field(ge=0)]


class FilterResultSummary(Phase3ContractModel):
    """Counts and bytes for both matched and safely eligible records."""

    total_count: Annotated[int, Field(ge=0)]
    total_bytes: Annotated[int, Field(ge=0)]
    eligible_count: Annotated[int, Field(ge=0)] = 0
    eligible_bytes: Annotated[int, Field(ge=0)] = 0
    excluded_count: Annotated[int, Field(ge=0)] = 0
    excluded_bytes: Annotated[int, Field(ge=0)] = 0
    by_media_type: tuple[FilterAggregate, ...] = ()
    by_confidence: tuple[FilterAggregate, ...] = ()
    exclusion_reasons: tuple[FilterAggregate, ...] = ()
    filter_digest: Annotated[str | None, Field(default=None, pattern=r"^[0-9a-f]{64}$")] = None

    @field_validator("by_media_type", "by_confidence", "exclusion_reasons")
    @classmethod
    def aggregate_keys_are_unique(
        cls, values: tuple[FilterAggregate, ...]
    ) -> tuple[FilterAggregate, ...]:
        keys = [item.key for item in values]
        if len(set(keys)) != len(keys):
            raise ValueError("aggregate keys cannot repeat")
        return values

    @model_validator(mode="after")
    def summary_totals_match(self) -> FilterResultSummary:
        if self.eligible_count + self.excluded_count != self.total_count:
            raise ValueError("eligible_count plus excluded_count must equal total_count")
        if self.eligible_bytes + self.excluded_bytes != self.total_bytes:
            raise ValueError("eligible_bytes plus excluded_bytes must equal total_bytes")
        return self


class FilterResultPage(Phase3ContractModel):
    """A bounded page; the next cursor is opaque to the GUI."""

    items: tuple[MediaRecord, ...] = ()
    page_size: Annotated[int, Field(ge=1, le=500)]
    next_cursor: Annotated[str | None, Field(default=None, min_length=1, max_length=256)] = None
    summary: FilterResultSummary

    @model_validator(mode="after")
    def page_does_not_exceed_limit(self) -> FilterResultPage:
        if len(self.items) > self.page_size:
            raise ValueError("items cannot exceed page_size")
        return self


class SelectionSnapshot(Phase3ContractModel):
    """Immutable selection bound to a scan and filter digest."""

    selection_id: UUID = Field(default_factory=uuid4)
    scope: SelectionScope
    account_ids: Annotated[
        tuple[str, ...], Field(min_length=1, max_length=128)
    ]
    scan_id: UUID
    scan_manifest_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    filter_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    selected_media_ids: Annotated[tuple[UUID, ...], Field(min_length=1)]
    selected_record_keys: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=256)], ...], Field(min_length=1)
    ]
    selected_count: Annotated[int, Field(ge=1)]
    selected_bytes: Annotated[int, Field(ge=0)]
    selection_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    created_at: datetime = Field(default_factory=_utc_now)
    expires_at: datetime

    @field_validator("created_at", "expires_at")
    @classmethod
    def snapshot_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("snapshot datetime must include a timezone")
        return value.astimezone(UTC)

    @field_validator("account_ids", "selected_record_keys")
    @classmethod
    def snapshot_values_are_unique(cls, values: tuple) -> tuple:
        if len(set(values)) != len(values):
            raise ValueError("snapshot values cannot contain duplicates")
        return values

    @model_validator(mode="after")
    def snapshot_is_non_expired(self) -> SelectionSnapshot:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if self.selected_count != len(self.selected_media_ids):
            raise ValueError("selected_count must equal selected_media_ids length")
        if self.selected_count != len(self.selected_record_keys):
            raise ValueError("selected_count must equal selected_record_keys length")
        if self.scope is SelectionScope.MANUAL and not self.selected_media_ids:
            raise ValueError("manual selection requires selected_media_ids")
        return self


class DatabaseEvidence(Phase3ContractModel):
    """A redacted, structured database observation; no message body is allowed."""

    evidence_id: UUID = Field(default_factory=uuid4)
    account_id: Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_@.-]+$")]
    evidence_kind: EvidenceKind
    source_table: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_]+$")]
    source_row_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    media_id: UUID | None = None
    contact: ContactRef | None = None
    message_local_id: Annotated[int | None, Field(default=None, ge=0)] = None
    message_time: datetime | None = None
    relative_path: Annotated[str | None, Field(default=None, min_length=1, max_length=1024)] = None
    byte_size: Annotated[int | None, Field(default=None, ge=0)] = None
    confidence: MappingConfidence = MappingConfidence.MEDIUM

    @field_validator("message_time")
    @classmethod
    def evidence_time_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("message_time must include a timezone")
        return value.astimezone(UTC)

    @field_validator("relative_path")
    @classmethod
    def evidence_path_is_relative(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return FileIdentity(relative_path=value, byte_size=0, modified_time_ns=0).relative_path

    @model_validator(mode="after")
    def evidence_contact_matches_account(self) -> DatabaseEvidence:
        if self.contact is not None and self.contact.account_id != self.account_id:
            raise ValueError("evidence contact.account_id must match account_id")
        if self.evidence_kind is EvidenceKind.MEDIA_PATH and self.relative_path is None:
            raise ValueError("media_path evidence requires relative_path")
        if self.evidence_kind is EvidenceKind.MESSAGE and self.message_local_id is None:
            raise ValueError("message evidence requires message_local_id")
        return self


class DatabaseEvidenceConflict(Phase3ContractModel):
    """Conflict metadata represented by digests rather than raw database values."""

    conflict_id: UUID = Field(default_factory=uuid4)
    account_id: Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_@.-]+$")]
    media_id: UUID | None = None
    field_name: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")]
    candidate_digests: Annotated[tuple[str, ...], Field(min_length=2, max_length=32)]
    reason: Annotated[str, Field(min_length=1, max_length=512)]

    @field_validator("candidate_digests")
    @classmethod
    def conflict_digests_are_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("candidate_digests cannot repeat")
        if any(not SHA256_RE.fullmatch(value) for value in values):
            raise ValueError("candidate_digests must be SHA-256 values")
        return values


class DatabaseEvidenceEnvelope(Phase3ContractModel):
    """Read-only output of a database-copy adapter, safe to persist as metadata."""

    envelope_id: UUID = Field(default_factory=uuid4)
    account: AccountRef
    database_copy_id: Annotated[
        str, Field(min_length=1, max_length=128, pattern=SAFE_KEY_RE.pattern)
    ]
    database_copy_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    database_schema_version: Annotated[str, Field(min_length=1, max_length=64)]
    adapter_version: Annotated[str, Field(min_length=1, max_length=64)]
    read_at: datetime = Field(default_factory=_utc_now)
    evidence: tuple[DatabaseEvidence, ...] = ()
    conflicts: tuple[DatabaseEvidenceConflict, ...] = ()

    @field_validator("read_at")
    @classmethod
    def envelope_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("read_at must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def envelope_account_matches_items(self) -> DatabaseEvidenceEnvelope:
        account_id = self.account.account_id
        if any(item.account_id != account_id for item in self.evidence):
            raise ValueError("all evidence must belong to envelope.account")
        if any(item.account_id != account_id for item in self.conflicts):
            raise ValueError("all conflicts must belong to envelope.account")
        if len({item.evidence_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("evidence_id values must be unique")
        if len({item.conflict_id for item in self.conflicts}) != len(self.conflicts):
            raise ValueError("conflict_id values must be unique")
        return self


class DecodeRequest(FrozenContractModel):
    request_id: UUID = Field(default_factory=uuid4)
    media: MediaRecord
    variant: DecodeVariant
    cache_root: Annotated[str, Field(min_length=3, max_length=1024)]
    max_edge_px: Annotated[int | None, Field(default=None, ge=16, le=16_384)]
    retain_until: datetime | None = None

    @field_validator("cache_root")
    @classmethod
    def absolute_cache_root(cls, value: str) -> str:
        normalized = value.replace("/", "\\").rstrip("\\")
        if not WINDOWS_ABSOLUTE_RE.match(normalized):
            raise ValueError("cache_root must be an absolute Windows or UNC path")
        return normalized

    @model_validator(mode="after")
    def thumbnail_has_size_limit(self) -> DecodeRequest:
        if self.variant is DecodeVariant.THUMBNAIL and self.max_edge_px is None:
            raise ValueError("thumbnail decoding requires max_edge_px")
        return self


class DecodeArtifact(FrozenContractModel):
    request_id: UUID
    media_id: UUID
    created_at: datetime = Field(default_factory=_utc_now)
    cache_session_id: UUID
    variant: DecodeVariant
    output_file: FileIdentity
    output_format: Annotated[str, Field(min_length=2, max_length=16, pattern=r"^[A-Za-z0-9]+$")]
    decoder_id: Annotated[str, Field(min_length=1, max_length=128)]
    expires_at: datetime | None = None


class RecoveryLocator(Phase3ContractModel):
    """Opaque, non-absolute locator for a recoverable operation result."""

    recovery_id: Annotated[str, Field(min_length=1, max_length=128, pattern=SAFE_KEY_RE.pattern)]
    kind: RecoveryLocatorKind
    state: RecoveryState
    relative_path: Annotated[str | None, Field(default=None, min_length=1, max_length=1024)] = None

    @field_validator("relative_path")
    @classmethod
    def recovery_path_is_relative(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return FileIdentity(relative_path=value, byte_size=0, modified_time_ns=0).relative_path

    @model_validator(mode="after")
    def locator_kind_matches_state(self) -> RecoveryLocator:
        if self.kind is RecoveryLocatorKind.NONE and self.state is RecoveryState.AVAILABLE:
            raise ValueError("none recovery locator cannot be available")
        if self.kind is not RecoveryLocatorKind.NONE and self.relative_path is None:
            raise ValueError("recoverable locator requires a relative_path")
        return self


class CleanupExecution(Phase3ContractModel):
    """Execution summary that separates processed bytes from reclaimed bytes."""

    execution_id: UUID = Field(default_factory=uuid4)
    plan_id: UUID
    mode: CleanupExecutionMode
    status: ReceiptStatus
    started_at: datetime = Field(default_factory=_utc_now)
    finished_at: datetime | None = None
    processed_bytes: Annotated[int, Field(ge=0)] = 0
    reclaimed_bytes: Annotated[int, Field(ge=0)] = 0
    recovery: tuple[RecoveryLocator, ...] = ()

    @field_validator("started_at", "finished_at")
    @classmethod
    def execution_time_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("execution datetime must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def execution_totals_are_valid(self) -> CleanupExecution:
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.reclaimed_bytes > self.processed_bytes:
            raise ValueError("reclaimed_bytes cannot exceed processed_bytes")
        if len({locator.recovery_id for locator in self.recovery}) != len(self.recovery):
            raise ValueError("recovery_id values must be unique")
        return self


class CleanupTarget(FrozenContractModel):
    """One exact on-disk target included in an approved cleanup plan."""

    target_id: UUID = Field(default_factory=uuid4)
    media_id: UUID
    account_id: Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_@.-]+$")]
    file: FileIdentity
    media_type: MediaType
    mapping_confidence: MappingConfidence
    reason: Annotated[str, Field(min_length=1, max_length=512)]

    @model_validator(mode="after")
    def target_is_safe_enough_to_offer(self) -> CleanupTarget:
        if self.mapping_confidence in {MappingConfidence.LOW, MappingConfidence.UNMAPPED}:
            raise ValueError("low-confidence or unmapped media cannot enter a cleanup plan")
        return self


class CleanupPlanSummary(FrozenContractModel):
    target_count: Annotated[int, Field(ge=1)]
    expected_reclaim_bytes: Annotated[int, Field(ge=0)]
    by_media_type: tuple[tuple[MediaType, int], ...]
    includes_regenerable_cache: bool = False

    @field_validator("by_media_type")
    @classmethod
    def media_type_summary_is_unique(
        cls, values: tuple[tuple[MediaType, int], ...]
    ) -> tuple[tuple[MediaType, int], ...]:
        seen: set[MediaType] = set()
        for media_type, byte_count in values:
            if media_type in seen:
                raise ValueError("by_media_type cannot repeat a media type")
            if byte_count < 0:
                raise ValueError("by_media_type byte counts cannot be negative")
            seen.add(media_type)
        return values


class CleanupPlan(FrozenContractModel):
    """Immutable proposal.  Constructing this model never deletes a file."""

    plan_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=_utc_now)
    account: AccountRef
    disposition: CleanupDisposition
    targets: tuple[CleanupTarget, ...]
    summary: CleanupPlanSummary
    policy: CompatibilityPolicy = Field(default_factory=CompatibilityPolicy)
    acknowledged_risk: bool = False
    scan_id: UUID | None = None
    scan_manifest_sha256: Annotated[
        str | None, Field(default=None, pattern=r"^[0-9a-f]{64}$")
    ] = None
    filter_digest: Annotated[str | None, Field(default=None, pattern=r"^[0-9a-f]{64}$")] = None
    selection_digest: Annotated[str | None, Field(default=None, pattern=r"^[0-9a-f]{64}$")] = None
    expires_at: datetime | None = None

    @field_validator("created_at")
    @classmethod
    def created_at_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value.astimezone(UTC)

    @field_validator("expires_at")
    @classmethod
    def expiry_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def plan_matches_exact_target_set(self) -> CleanupPlan:
        if not self.targets:
            raise ValueError("cleanup plan requires at least one target")
        if len({target.target_id for target in self.targets}) != len(self.targets):
            raise ValueError("cleanup targets must have unique target_id values")
        if len({target.file.relative_path for target in self.targets}) != len(self.targets):
            raise ValueError("cleanup plan cannot target a file twice")
        if any(target.account_id != self.account.account_id for target in self.targets):
            raise ValueError("all cleanup targets must belong to plan.account")
        if self.summary.target_count != len(self.targets):
            raise ValueError("summary.target_count must equal target count")
        expected = sum(target.file.byte_size for target in self.targets)
        if self.summary.expected_reclaim_bytes != expected:
            raise ValueError("summary.expected_reclaim_bytes must equal target byte sizes")
        actual_by_type: dict[MediaType, int] = {}
        for target in self.targets:
            actual_by_type[target.media_type] = (
                actual_by_type.get(target.media_type, 0) + target.file.byte_size
            )
        if dict(self.summary.by_media_type) != actual_by_type:
            raise ValueError("summary.by_media_type must exactly match targets")
        if self.disposition is CleanupDisposition.PERMANENT and not self.acknowledged_risk:
            raise ValueError("permanent cleanup plans require acknowledged_risk")
        if (self.scan_id is None) != (self.scan_manifest_sha256 is None):
            raise ValueError("scan_id and scan_manifest_sha256 must be provided together")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if self.selection_digest is not None and self.filter_digest is None:
            raise ValueError("selection_digest requires filter_digest")
        return self


class CleanupReceiptItem(FrozenContractModel):
    target_id: UUID
    status: ReceiptStatus
    actual_reclaim_bytes: Annotated[int, Field(ge=0)] = 0
    processed_bytes: Annotated[int, Field(ge=0)] = 0
    error: ContractError | None = None
    recovery: RecoveryLocator | None = None

    @model_validator(mode="after")
    def failed_item_has_error(self) -> CleanupReceiptItem:
        if self.status is ReceiptStatus.FAILED and self.error is None:
            raise ValueError("failed receipt item must include error")
        return self


class CleanupReceipt(FrozenContractModel):
    """Append-only execution evidence; executors may create it, planners may not."""

    receipt_id: UUID = Field(default_factory=uuid4)
    plan_id: UUID
    account: AccountRef
    recorded_at: datetime = Field(default_factory=_utc_now)
    status: ReceiptStatus
    items: tuple[CleanupReceiptItem, ...]
    total_reclaimed_bytes: Annotated[int, Field(ge=0)]
    executor_id: Annotated[str, Field(min_length=1, max_length=128)]
    execution_mode: CleanupExecutionMode | None = None
    processed_bytes: Annotated[int, Field(ge=0)] = 0
    recovery: tuple[RecoveryLocator, ...] = ()

    @model_validator(mode="after")
    def receipt_reclaim_total_matches_items(self) -> CleanupReceipt:
        if self.total_reclaimed_bytes != sum(item.actual_reclaim_bytes for item in self.items):
            raise ValueError("total_reclaimed_bytes must equal receipt item totals")
        if self.processed_bytes < sum(item.processed_bytes for item in self.items):
            raise ValueError("processed_bytes cannot be less than item processed bytes")
        if len({locator.recovery_id for locator in self.recovery}) != len(self.recovery):
            raise ValueError("recovery_id values must be unique")
        return self


PUBLIC_CONTRACTS: dict[str, type[ContractModel]] = {
    "account-ref": AccountRef,
    "contact-ref": ContactRef,
    "scan-manifest": ScanManifest,
    "media-record": MediaRecord,
    "decode-request": DecodeRequest,
    "decode-artifact": DecodeArtifact,
    "cleanup-plan": CleanupPlan,
    "cleanup-receipt": CleanupReceipt,
    "contract-error": ContractError,
    "compatibility-policy": CompatibilityPolicy,
    "scan-lineage": ScanLineage,
    "media-record-lineage": MediaRecordLineage,
    "filter-spec": FilterSpec,
    "filter-aggregate": FilterAggregate,
    "filter-result-summary": FilterResultSummary,
    "filter-result-page": FilterResultPage,
    "selection-snapshot": SelectionSnapshot,
    "database-evidence": DatabaseEvidence,
    "database-evidence-conflict": DatabaseEvidenceConflict,
    "database-evidence-envelope": DatabaseEvidenceEnvelope,
    "recovery-locator": RecoveryLocator,
    "cleanup-execution": CleanupExecution,
}
