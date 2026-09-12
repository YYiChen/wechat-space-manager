"""Headless application workflow facade.

This module is the only orchestration boundary the desktop GUI needs.  It
composes the public scanner, database-copy adapter, mapper, local index,
filtering, cleanup planning and recoverable executor APIs without exposing
arbitrary filesystem operations to callers.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

from wechat_cleaner.cleanup import (
    PlanBuildResult,
    PreflightReport,
    build_plan,
    preflight,
)
from wechat_cleaner.db_adapter import DatabaseAdapterError, DatabaseCopyAdapter
from wechat_cleaner.domain.contracts import (
    AccountRef,
    CleanupDisposition,
    CleanupPlan,
    CleanupReceipt,
    DatabaseEvidenceEnvelope,
    DecodeArtifact,
    DecodeRequest,
    DecodeVariant,
    FileIdentity,
    FilterSpec,
    MappingConfidence,
    MediaRecord,
    OwnershipStatus,
    ReceiptStatus,
    ScanManifest,
    SelectionScope,
    SelectionSnapshot,
)
from wechat_cleaner.executor import (
    ExecutionResult,
    ExecutorError,
    RecoverableExecutor,
    RestoreResult,
)
from wechat_cleaner.filtering import (
    FilterResult,
    build_selection_snapshot,
    filter_records,
)
from wechat_cleaner.local_index import IndexErrorBase, IndexPage, LocalMetadataIndex
from wechat_cleaner.mapper import MediaCandidate, MessageEvidence, map_candidates
from wechat_cleaner.scanner import ScannedFile, ScanResult, scan


class WorkflowError(RuntimeError):
    """A redacted, stable application-boundary failure."""

    def __init__(self, code: str, message: str, *, correlation_id: UUID | None = None) -> None:
        self.code = code
        self.correlation_id = correlation_id
        super().__init__(message)


class WorkflowCancelled(WorkflowError):
    """An operation stopped before it committed a new index or plan state."""

    def __init__(self, correlation_id: UUID) -> None:
        super().__init__("CANCELLED", "operation cancelled", correlation_id=correlation_id)


class CancellationToken:
    """Cooperative cancellation token safe to pass across worker boundaries."""

    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    operation: str
    correlation_id: UUID
    completed: int
    total: int
    message: str


ProgressCallback = Callable[[ProgressEvent], None]


@dataclass(frozen=True, slots=True)
class ScanRegistration:
    correlation_id: UUID
    scans: tuple[ScanResult, ...]
    records: tuple[MediaRecord, ...]


@dataclass(frozen=True, slots=True)
class DatabaseImport:
    correlation_id: UUID
    envelope: DatabaseEvidenceEnvelope
    records: tuple[MediaRecord, ...]


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    accepted: bool
    message: str
    execution: ExecutionResult | None = None
    receipt: CleanupReceipt | None = None


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    available: bool
    message: str
    restore: RestoreResult | None = None


_MEDIA_NAMESPACE = UUID("3a3c5d3b-4ee3-4fd0-9b5f-7f79fd1d9301")


class ApplicationFacade:
    """Compose the Phase 3 services behind a GUI-safe public facade."""

    def __init__(
        self,
        index_path: str | os.PathLike[str],
        *,
        database_adapter: DatabaseCopyAdapter | None = None,
        executor: RecoverableExecutor | None = None,
        recovery_root: str | os.PathLike[str] | None = None,
        decoder: Callable[..., DecodeArtifact] | None = None,
    ) -> None:
        self._index = LocalMetadataIndex.open(index_path)
        self._database_adapter = database_adapter or DatabaseCopyAdapter()
        self._executor = executor or RecoverableExecutor()
        self._recovery_root = Path(recovery_root) if recovery_root is not None else None
        self._decoder = decoder
        self._scans: dict[str, ScanResult] = {}
        self._records: dict[str, tuple[MediaRecord, ...]] = {}
        self._envelopes: dict[str, DatabaseEvidenceEnvelope] = {}
        self._last_result: FilterResult | None = None
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._index.close()
            self._closed = True

    def __enter__(self) -> ApplicationFacade:
        self._ensure_open()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def register_scan(
        self,
        root: str | os.PathLike[str],
        *,
        scanner_version: str | None = None,
        progress: ProgressCallback | None = None,
        cancel: CancellationToken | None = None,
        correlation_id: UUID | None = None,
    ) -> ScanRegistration:
        """Scan, map against any imported evidence, then atomically index records."""

        self._ensure_open()
        correlation = correlation_id or uuid4()
        operation = "scan"
        self._emit(progress, operation, correlation, 0, 1, "scanning")
        self._check_cancel(cancel, correlation)
        try:
            scans = scan(root, **({"scanner_version": scanner_version} if scanner_version else {}))
        except Exception as exc:  # noqa: BLE001 - convert module errors at the facade boundary
            raise WorkflowError(
                "SCAN_FAILED", "scan could not be completed", correlation_id=correlation
            ) from exc
        self._check_cancel(cancel, correlation)
        records: list[MediaRecord] = []
        pending_scans = dict(self._scans)
        pending_records = dict(self._records)
        for index, result in enumerate(scans, start=1):
            self._check_cancel(cancel, correlation)
            mapped = self._map_scan(result, cancel=cancel, correlation_id=correlation)
            records.extend(mapped)
            pending_scans[result.account.account_id] = result
            pending_records[result.account.account_id] = tuple(mapped)
            self._emit(
                progress, operation, correlation, index, len(scans), result.account.account_id
            )
        self._check_cancel(cancel, correlation)
        if records:
            self._upsert_records(records, correlation)
        self._scans = pending_scans
        self._records = pending_records
        self._emit(progress, operation, correlation, len(scans), len(scans), "indexed")
        return ScanRegistration(correlation, tuple(scans), tuple(records))

    # Short aliases make the facade convenient for adapters and tests.
    scan = register_scan

    def import_database_copy(
        self,
        database_copy: str | os.PathLike[str],
        *,
        account_id: str,
        database_copy_id: str | None = None,
        progress: ProgressCallback | None = None,
        cancel: CancellationToken | None = None,
        correlation_id: UUID | None = None,
    ) -> DatabaseImport:
        """Read a database copy, remap the registered scan, then index atomically."""

        self._ensure_open()
        correlation = correlation_id or uuid4()
        self._emit(progress, "database-import", correlation, 0, 1, "reading copy")
        self._check_cancel(cancel, correlation)
        result = self._scans.get(account_id)
        if result is None:
            raise WorkflowError(
                "ACCOUNT_NOT_SCANNED",
                "account must be scanned before database evidence is imported",
                correlation_id=correlation,
            )
        try:
            envelope = self._database_adapter.read_copy(
                database_copy,
                result.account,
                database_copy_id=database_copy_id,
            )
        except DatabaseAdapterError as exc:
            raise WorkflowError(exc.code, str(exc), correlation_id=correlation) from exc
        self._check_cancel(cancel, correlation)
        records = self._map_scan(
            result,
            envelope=envelope,
            cancel=cancel,
            correlation_id=correlation,
        )
        self._check_cancel(cancel, correlation)
        if records:
            self._upsert_records(records, correlation)
        self._envelopes[account_id] = envelope
        self._records[account_id] = tuple(records)
        self._emit(progress, "database-import", correlation, 1, 1, "indexed")
        return DatabaseImport(correlation, envelope, tuple(records))

    def load_records(self) -> tuple[MediaRecord, ...]:
        """Return the current session records, falling back to the durable index."""

        self._ensure_open()
        if self._records:
            return tuple(record for values in self._records.values() for record in values)
        return self._load_index_records()

    def query(
        self,
        spec: FilterSpec | None = None,
        *,
        cursor: str | None = None,
    ) -> FilterResult:
        """Query the indexed session through the same precise filter engine as GUI."""

        self._ensure_open()
        spec = spec or FilterSpec()
        records = self.load_records()
        result = filter_records(records, spec, cursor=cursor)
        self._last_result = result
        return result

    def query_index_page(
        self,
        spec: FilterSpec | None = None,
        *,
        cursor: str | None = None,
    ) -> IndexPage:
        """Expose bounded index pagination for headless consumers."""

        self._ensure_open()
        try:
            return self._index.query_page(spec or FilterSpec(), cursor=cursor)
        except IndexErrorBase as exc:
            raise WorkflowError(
                "INDEX_QUERY_FAILED", "metadata index query could not complete"
            ) from exc

    def select(
        self,
        result: FilterResult | None = None,
        scope: SelectionScope = SelectionScope.CURRENT_PAGE,
        *,
        selected_media_ids: tuple[UUID, ...] = (),
        expires_at: datetime | None = None,
    ) -> SelectionSnapshot:
        self._ensure_open()
        result = result or self._last_result
        if result is None:
            raise WorkflowError("NO_QUERY", "a filter result is required before selection")
        chosen = None
        if scope is SelectionScope.MANUAL:
            by_id = {record.media_id: record for record in result.all_records}
            chosen = tuple(by_id[media_id] for media_id in selected_media_ids if media_id in by_id)
        try:
            return build_selection_snapshot(
                result,
                scope,
                selected_records=chosen,
                expires_at=expires_at or _default_expiry(),
            )
        except ValueError as exc:
            raise WorkflowError("SELECTION_INVALID", "selection could not be created") from exc

    def build_cleanup_plan(
        self,
        selection: SelectionSnapshot,
        *,
        disposition: CleanupDisposition = CleanupDisposition.QUARANTINE,
        include_regenerable_cache: bool = False,
    ) -> PlanBuildResult:
        self._ensure_open()
        if selection.expires_at <= datetime.now(UTC):
            raise WorkflowError("SELECTION_STALE", "selection snapshot has expired")
        all_records = self.load_records()
        selected_ids = set(selection.selected_media_ids)
        chosen = tuple(record for record in all_records if record.media_id in selected_ids)
        if (
            len(chosen) != selection.selected_count
            or sum(record.file.byte_size for record in chosen) != selection.selected_bytes
            or {record.record_key for record in chosen}
            != set(selection.selected_record_keys)
            or any(
                record.scan_id != selection.scan_id
                or record.scan_manifest_sha256 != selection.scan_manifest_sha256
                for record in chosen
            )
        ):
            raise WorkflowError(
                "SELECTION_STALE", "selection no longer matches the indexed records"
            )
        result = build_plan(
            selection_account(chosen, self._scans),
            chosen,
            disposition=disposition,
            include_regenerable_cache=include_regenerable_cache,
        )
        if result.plan is not None:
            result = PlanBuildResult(
                plan=result.plan.model_copy(
                    update={
                        "scan_id": selection.scan_id,
                        "scan_manifest_sha256": selection.scan_manifest_sha256,
                        "filter_digest": selection.filter_digest,
                        "selection_digest": selection.selection_digest,
                        "expires_at": selection.expires_at,
                    }
                ),
                rejected=result.rejected,
            )
        return result

    # GUI-compatible name.
    build_plan = build_cleanup_plan

    def preflight(self, plan: CleanupPlan) -> PreflightReport:
        self._ensure_open()
        self._validate_plan_scope(plan)
        try:
            return preflight(plan, plan.account.account_root)
        except Exception as exc:  # noqa: BLE001 - no path-bearing exception crosses facade
            raise WorkflowError("PREFLIGHT_FAILED", "cleanup preflight could not complete") from exc

    def dry_run(
        self,
        plan: CleanupPlan,
        *,
        progress: ProgressCallback | None = None,
        cancel: CancellationToken | None = None,
        correlation_id: UUID | None = None,
    ) -> CleanupReceipt:
        self._ensure_open()
        correlation = correlation_id or uuid4()
        self._validate_plan_scope(plan, correlation)
        self._emit(progress, "dry-run", correlation, 0, 1, "preflight")
        self._check_cancel(cancel, correlation)
        try:
            result = self._executor.execute(plan, mode="dry_run")
        except ExecutorError as exc:
            raise WorkflowError(exc.code, str(exc), correlation_id=correlation) from exc
        self._emit(progress, "dry-run", correlation, 1, 1, "complete")
        return result.receipt

    def confirm(
        self,
        plan: CleanupPlan,
        *,
        progress: ProgressCallback | None = None,
        cancel: CancellationToken | None = None,
        correlation_id: UUID | None = None,
    ) -> ConfirmationResult:
        self._ensure_open()
        correlation = correlation_id or uuid4()
        self._validate_plan_scope(plan, correlation)
        self._check_cancel(cancel, correlation)
        if self._recovery_root is None:
            return ConfirmationResult(False, "未配置受控恢复目录，未执行任何文件操作。")
        disposition = plan.disposition.value
        mode = "quarantine" if disposition == "quarantine" else "recycle_bin"
        self._emit(progress, "execute", correlation, 0, len(plan.targets), "confirmed")
        try:
            result = self._executor.execute(
                plan,
                mode=mode,
                account_root=plan.account.account_root,
                recovery_root=self._recovery_root,
                expected_plan_digest=self._executor.plan_digest(plan),
                confirm=True,
            )
        except ExecutorError as exc:
            raise WorkflowError(exc.code, str(exc), correlation_id=correlation) from exc
        self._emit(
            progress, "execute", correlation, len(plan.targets), len(plan.targets), "complete"
        )
        succeeded = result.receipt.status is ReceiptStatus.SUCCEEDED
        message = (
            "执行完成，已生成恢复收据。"
            if succeeded
            else "执行未完全成功，收据已保留；可检查失败项并恢复已移动文件。"
        )
        return ConfirmationResult(succeeded, message, result, result.receipt)

    def recovery(self, receipt: CleanupReceipt) -> RecoveryResult:
        self._ensure_open()
        result = self._executor.restore(receipt)
        if result.failed_count:
            return RecoveryResult(False, "部分恢复失败，未覆盖现有文件。", result)
        return RecoveryResult(True, f"已恢复 {result.restored_count} 项。", result)

    def preview_request(
        self,
        media_id: UUID,
        *,
        cache_root: str,
        variant: DecodeVariant = DecodeVariant.THUMBNAIL,
        max_edge_px: int = 1_024,
    ) -> DecodeRequest:
        record = self._record_by_id(media_id)
        return DecodeRequest(
            media=record,
            variant=variant,
            cache_root=cache_root,
            max_edge_px=max_edge_px if variant is DecodeVariant.THUMBNAIL else None,
        )

    def preview(
        self,
        media_id: UUID,
        *,
        copy_root: str,
        cache_root: str,
        variant: DecodeVariant = DecodeVariant.THUMBNAIL,
        max_edge_px: int = 1_024,
        key_provider: Callable[..., Any] | None = None,
    ) -> DecodeArtifact:
        if self._decoder is None:
            from wechat_cleaner.decoder import decode as decoder
        else:
            decoder = self._decoder
        request = self.preview_request(
            media_id,
            cache_root=cache_root,
            variant=variant,
            max_edge_px=max_edge_px,
        )
        try:
            return decoder(request, copy_root=copy_root, key_provider=key_provider)
        except Exception as exc:  # noqa: BLE001 - decoder errors are redacted at facade boundary
            raise WorkflowError("PREVIEW_FAILED", "preview could not be generated") from exc

    def _map_scan(
        self,
        result: ScanResult,
        *,
        envelope: DatabaseEvidenceEnvelope | None = None,
        cancel: CancellationToken | None,
        correlation_id: UUID,
    ) -> tuple[MediaRecord, ...]:
        active_envelope = (
            envelope if envelope is not None else self._envelopes.get(result.account.account_id)
        )
        evidence = _message_evidence(active_envelope)
        candidates = tuple(
            _candidate(result.account, result.manifest, item) for item in result.files
        )
        mapped = map_candidates(result.account, candidates, evidence)
        conflict_local_ids = _conflict_local_ids(active_envelope)
        records: list[MediaRecord] = []
        for index, record in enumerate(mapped.records, start=1):
            self._check_cancel(cancel, correlation_id)
            records.append(
                _enrich_record(
                    record,
                    result.account,
                    result.manifest,
                    result.files[index - 1],
                    conflict_local_ids,
                )
            )
        return tuple(records)

    def _record_by_id(self, media_id: UUID) -> MediaRecord:
        for records in self._records.values():
            for record in records:
                if record.media_id == media_id:
                    return record
        for record in self._load_index_records():
            if record.media_id == media_id:
                return record
        raise WorkflowError("MEDIA_NOT_FOUND", "media record is not registered")

    def _load_index_records(self) -> tuple[MediaRecord, ...]:
        """Materialize durable rows through bounded pages for a reopened facade."""

        spec = FilterSpec(
            page_size=500,
            include_protected=True,
            include_conflicts=True,
            include_unmapped=True,
            descending=False,
        )
        records: list[MediaRecord] = []
        cursor: str | None = None
        try:
            while True:
                page = self._index.query_page(spec, cursor=cursor)
                records.extend(page.items)
                if page.next_cursor is None:
                    return tuple(records)
                cursor = page.next_cursor
        except IndexErrorBase as exc:
            raise WorkflowError(
                "INDEX_QUERY_FAILED", "metadata index query could not complete"
            ) from exc

    def _upsert_records(
        self, records: list[MediaRecord] | tuple[MediaRecord, ...], correlation: UUID
    ) -> None:
        try:
            self._index.upsert_records(records)
        except IndexErrorBase as exc:
            raise WorkflowError(
                "INDEX_WRITE_FAILED",
                "metadata index update could not complete",
                correlation_id=correlation,
            ) from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise WorkflowError("CLOSED", "application facade is closed")

    def _validate_plan_scope(
        self, plan: CleanupPlan, correlation_id: UUID | None = None
    ) -> None:
        """Reject plans that were not produced from this facade's registered scan."""

        registered = self._scans.get(plan.account.account_id)
        if registered is None:
            raise WorkflowError(
                "ACCOUNT_NOT_SCANNED",
                "account must be scanned in this workflow session before plan use",
                correlation_id=correlation_id,
            )
        if registered.account.account_root.casefold() != plan.account.account_root.casefold():
            raise WorkflowError(
                "ACCOUNT_MISMATCH",
                "plan account root does not match the registered scan",
                correlation_id=correlation_id,
            )
        if (
            plan.scan_id != registered.manifest.scan_id
            or plan.scan_manifest_sha256 != registered.manifest.manifest_sha256
        ):
            raise WorkflowError(
                "PLAN_STALE",
                "plan scan lineage does not match the registered scan",
                correlation_id=correlation_id,
            )
        known = {
            record.media_id: record
            for records in self._records.values()
            for record in records
        }
        if any(
            (known.get(target.media_id) is None)
            or known[target.media_id].file != target.file
            or target.account_id != plan.account.account_id
            for target in plan.targets
        ):
            raise WorkflowError(
                "PLAN_STALE",
                "plan targets no longer match the registered records",
                correlation_id=correlation_id,
            )

    @staticmethod
    def _emit(
        callback: ProgressCallback | None,
        operation: str,
        correlation: UUID,
        completed: int,
        total: int,
        message: str,
    ) -> None:
        if callback is not None:
            callback(ProgressEvent(operation, correlation, completed, total, message))

    @staticmethod
    def _check_cancel(token: CancellationToken | None, correlation: UUID) -> None:
        if token is not None and token.cancelled:
            raise WorkflowCancelled(correlation)


class ApplicationWorkflow(ApplicationFacade):
    """Descriptive alias used by orchestration code."""


WorkflowFacade = ApplicationFacade


def selection_account(
    records: tuple[MediaRecord, ...], scans: dict[str, ScanResult] | None = None
) -> AccountRef:
    if not records:
        raise WorkflowError("SELECTION_INVALID", "selection has no records")
    account_ids = {record.account_id for record in records}
    if len(account_ids) != 1:
        raise WorkflowError("ACCOUNT_MISMATCH", "selection contains multiple accounts")
    account_id = next(iter(account_ids))
    if scans and account_id in scans:
        return scans[account_id].account
    raise WorkflowError(
        "ACCOUNT_NOT_SCANNED",
        "account must be scanned in this workflow session before cleanup planning",
    )


def _default_expiry() -> datetime:
    return datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=15)


def _candidate(account: AccountRef, manifest: ScanManifest, item: ScannedFile) -> MediaCandidate:
    return MediaCandidate(
        account_id=account.account_id,
        file=FileIdentity(
            relative_path=item.relative_path,
            byte_size=item.byte_size,
            modified_time_ns=item.modified_time_ns,
            sha256=None,
        ),
        media_type=item.media_type,
        observed_at=manifest.scanned_at,
        is_regenerable_cache=item.is_regenerable_cache,
    )


def _message_evidence(envelope: DatabaseEvidenceEnvelope | None) -> tuple[MessageEvidence, ...]:
    if envelope is None:
        return ()
    values: list[MessageEvidence] = []
    for item in envelope.evidence:
        if item.contact is None or item.message_local_id is None:
            continue
        observed_at = item.message_time or envelope.read_at
        values.append(
            MessageEvidence(
                account_id=item.account_id,
                message_local_id=item.message_local_id,
                contact=item.contact,
                observed_at=observed_at,
                media_paths=(item.relative_path,) if item.relative_path else (),
                media_name=(
                    item.relative_path.replace("/", "\\").rsplit("\\", maxsplit=1)[-1]
                    if item.relative_path
                    else None
                ),
                byte_size=item.byte_size,
                source=f"database:{envelope.database_schema_version}",
            )
        )
    return tuple(values)


def _conflict_local_ids(envelope: DatabaseEvidenceEnvelope | None) -> frozenset[int]:
    if envelope is None or not envelope.conflicts:
        return frozenset()
    digests = {
        digest for conflict in envelope.conflicts for digest in conflict.candidate_digests
    }
    return frozenset(
        item.message_local_id
        for item in envelope.evidence
        if item.message_local_id is not None and item.source_row_digest in digests
    )


def _enrich_record(
    record: MediaRecord,
    account: AccountRef,
    manifest: ScanManifest,
    scanned: ScannedFile,
    conflict_local_ids: frozenset[int],
) -> MediaRecord:
    record_id = uuid5(
        _MEDIA_NAMESPACE,
        f"{account.account_id}:{record.file.relative_path.casefold()}",
    )
    digest = hashlib.sha256(record.file.relative_path.casefold().encode()).hexdigest()[:32]
    record_key = f"media-{digest}"
    update: dict[str, Any] = {
        "media_id": record_id,
        "record_key": record_key,
        "scan_id": manifest.scan_id,
        "scan_manifest_sha256": manifest.manifest_sha256,
        "is_regenerable_cache": scanned.is_regenerable_cache,
    }
    if scanned.is_protected:
        update.update(
            ownership_status=OwnershipStatus.PROTECTED,
            protection_reasons=("scanner marked protected path",),
        )
    elif record.message_local_id in conflict_local_ids:
        update.update(
            ownership_status=OwnershipStatus.CONFLICT,
            conflict_reasons=("database evidence contains conflicting candidates",),
        )
    elif scanned.is_regenerable_cache:
        update.update(
            contact=None,
            message_local_id=None,
            mapping_confidence=MappingConfidence.UNMAPPED,
            mapping_reason="regenerable cache has no message ownership",
            ownership_status=OwnershipStatus.UNMAPPED,
        )
    elif record.mapping_confidence is MappingConfidence.UNMAPPED:
        update["ownership_status"] = OwnershipStatus.UNMAPPED
    else:
        update["ownership_status"] = OwnershipStatus.MAPPED
    return record.model_copy(update=update)
