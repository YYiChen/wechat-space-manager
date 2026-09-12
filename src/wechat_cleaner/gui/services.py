"""Public application-facing ports used by the desktop workflow.

The GUI deliberately talks to this small port instead of importing scanner,
mapper, decoder or an executor. ``ApplicationFacadeAdapter`` is the production
adapter around the headless application workflow; ``FakeApplicationFacade`` is
kept for explicit synthetic tests and demo mode only.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TypeVar
from uuid import UUID, uuid5

from platformdirs import user_data_path

from wechat_cleaner.application import (
    ApplicationFacade as WorkflowApplicationFacade,
)
from wechat_cleaner.application import (
    WorkflowError,
)
from wechat_cleaner.cleanup import (
    ExecutionOutcome,
    PlanBuildResult,
    PreflightReport,
    PreflightStatus,
    TargetPreflight,
    build_plan,
    build_receipt,
)
from wechat_cleaner.domain import (
    AccountRef,
    CleanupPlan,
    CleanupReceipt,
    ContactKind,
    ContactRef,
    FilterResultPage,
    FilterSpec,
    MappingConfidence,
    MediaRecord,
    OwnershipStatus,
    SelectionScope,
    SelectionSnapshot,
)
from wechat_cleaner.domain.contracts import CleanupDisposition, FileIdentity, MediaType
from wechat_cleaner.filtering import (
    FilterResult,
    build_selection_snapshot,
    filter_records,
)

_T = TypeVar("_T")


class FacadeError(RuntimeError):
    """A user-safe error returned by an application facade."""


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    """Result of the explicit confirmation boundary.

    The fake adapter never mutates files.  A real application facade may
    return a successful receipt after its own executor and safety gates run.
    """

    accepted: bool
    message: str
    receipt: CleanupReceipt | None = None


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    available: bool
    message: str


@dataclass(frozen=True, slots=True)
class DesktopFacadeConfig:
    """Explicit filesystem configuration for the production GUI adapter."""

    index_path: Path
    source_root: Path | None = None
    database_copy: Path | None = None
    account_id: str | None = None
    copy_root: Path | None = None
    cache_root: Path | None = None
    recovery_root: Path | None = None


class ApplicationFacadeAdapter:
    """Adapt the headless workflow facade to the GUI-safe port.

    The adapter only accepts source/database paths supplied by an explicit
    desktop configuration. It never exposes the workflow object to widgets and
    never performs file mutation itself.
    """

    def __init__(self, config: DesktopFacadeConfig) -> None:
        self.config = config
        # ``LocalMetadataIndex`` owns a thread-affine sqlite connection.  The
        # GUI runs tasks on Qt worker threads, so keep the real workflow on one
        # dedicated Python worker and marshal every call through it.
        self._workflow: WorkflowApplicationFacade | None = None
        self._worker = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="wechat-space-manager-workflow",
        )
        self._closed = False
        self._loaded = False

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._workflow is not None:
                self._invoke(lambda workflow: workflow.close())
        finally:
            self._closed = True
            self._worker.shutdown(wait=True)

    def load_records(self) -> tuple[MediaRecord, ...]:
        return self._invoke(self._load_records)

    def query(self, spec: FilterSpec, *, cursor: str | None = None) -> FilterResult:
        return self._invoke(lambda workflow: self._query(workflow, spec, cursor=cursor))

    def preview(self, media_id: UUID) -> str:
        return self._invoke(lambda workflow: self._preview(workflow, media_id))

    def select(
        self,
        result: FilterResult,
        scope: SelectionScope,
        *,
        selected_media_ids: tuple[UUID, ...] = (),
    ) -> SelectionSnapshot:
        # Visible results include auditable exclusions.  A page/result
        # selection must therefore contain only the safe subset; manual
        # selection keeps the facade's explicit rejection for an excluded row.
        eligible_ids = {record.media_id for record in result.eligible_records}
        if scope is SelectionScope.CURRENT_PAGE:
            result = replace(
                result,
                records=tuple(
                    record for record in result.records if record.media_id in eligible_ids
                ),
            )
        elif scope is SelectionScope.CURRENT_RESULT:
            eligible_records = tuple(
                record for record in result.all_records if record.media_id in eligible_ids
            )
            result = replace(
                result,
                all_records=eligible_records,
                records=eligible_records,
            )
        return self._invoke(
            lambda workflow: workflow.select(
                result,
                scope,
                selected_media_ids=selected_media_ids,
            )
        )

    def build_plan(
        self,
        selection: SelectionSnapshot,
        *,
        disposition: CleanupDisposition,
    ) -> PlanBuildResult:
        return self._invoke(
            lambda workflow: workflow.build_plan(selection, disposition=disposition)
        )

    def preflight(self, plan: CleanupPlan) -> PreflightReport:
        return self._invoke(lambda workflow: workflow.preflight(plan))

    def dry_run(self, plan: CleanupPlan) -> CleanupReceipt:
        return self._invoke(lambda workflow: workflow.dry_run(plan))

    def confirm(self, plan: CleanupPlan) -> ConfirmationResult:
        return self._invoke(self._confirm, plan)

    def recovery(self, receipt: CleanupReceipt) -> RecoveryResult:
        return self._invoke(self._recovery, receipt)

    def _load_records(self, workflow: WorkflowApplicationFacade) -> tuple[MediaRecord, ...]:
        self._ensure_loaded(workflow)
        return workflow.load_records()

    def _query(
        self,
        workflow: WorkflowApplicationFacade,
        spec: FilterSpec,
        *,
        cursor: str | None,
    ) -> FilterResult:
        self._ensure_loaded(workflow)
        return workflow.query(spec, cursor=cursor)

    def _preview(self, workflow: WorkflowApplicationFacade, media_id: UUID) -> str:
        self._ensure_loaded(workflow)
        if self.config.copy_root is None or self.config.cache_root is None:
            raise FacadeError("预览尚未配置副本根目录和缓存目录。")
        artifact = workflow.preview(
            media_id,
            copy_root=os.fspath(self.config.copy_root),
            cache_root=os.fspath(self.config.cache_root),
        )
        return f"已生成{artifact.variant.value}预览（缓存会话 {artifact.cache_session_id}）。"

    def _confirm(
        self,
        workflow: WorkflowApplicationFacade,
        plan: CleanupPlan,
    ) -> ConfirmationResult:
        result = workflow.confirm(plan)
        return ConfirmationResult(result.accepted, result.message, result.receipt)

    def _recovery(
        self,
        workflow: WorkflowApplicationFacade,
        receipt: CleanupReceipt,
    ) -> RecoveryResult:
        result = workflow.recovery(receipt)
        return RecoveryResult(result.available, result.message)

    def _ensure_loaded(self, workflow: WorkflowApplicationFacade) -> None:
        if self._loaded:
            return
        if self.config.source_root is not None:
            registration = workflow.register_scan(self.config.source_root)
            if self.config.database_copy is not None:
                account_id = self.config.account_id
                if account_id is None:
                    if len(registration.scans) != 1:
                        raise FacadeError("数据库副本对应多个账号，请显式配置 account_id。")
                    account_id = registration.scans[0].account.account_id
                workflow.import_database_copy(
                    self.config.database_copy,
                    account_id=account_id,
                )
        self._loaded = True

    def _invoke(self, operation: Callable[..., _T], *args: object) -> _T:
        if self._closed:
            raise FacadeError("应用门面已关闭。")

        def run() -> _T:
            if self._workflow is None:
                self._workflow = WorkflowApplicationFacade(
                    self.config.index_path,
                    recovery_root=self.config.recovery_root,
                )
            return operation(self._workflow, *args)

        try:
            return self._worker.submit(run).result()
        except FacadeError:
            raise
        except WorkflowError as exc:
            raise FacadeError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - redact unexpected worker details
            raise FacadeError("应用工作流操作失败，请检查配置后重试。") from exc


def create_default_application_facade() -> ApplicationFacadeAdapter:
    """Create the production facade with an empty, local metadata index."""

    root = user_data_path("wechat-space-manager", appauthor=False)
    return ApplicationFacadeAdapter(DesktopFacadeConfig(index_path=root / "index.sqlite"))


def create_application_facade_from_args(argv: list[str] | None = None) -> ApplicationFacadePort:
    """Build the real adapter, or an explicit fake demo adapter, from GUI args."""

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--index-path", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--database-copy", type=Path)
    parser.add_argument("--account-id")
    parser.add_argument("--copy-root", type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--recovery-root", type=Path)
    parsed, _unknown = parser.parse_known_args(argv or [])
    if parsed.demo:
        return FakeApplicationFacade()
    if all(
        value is None
        for value in (
            parsed.index_path,
            parsed.source_root,
            parsed.database_copy,
            parsed.account_id,
            parsed.copy_root,
            parsed.cache_root,
            parsed.recovery_root,
        )
    ):
        return create_default_application_facade()
    index_path = parsed.index_path or (
        user_data_path("wechat-space-manager", appauthor=False) / "index.sqlite"
    )
    return ApplicationFacadeAdapter(
        DesktopFacadeConfig(
            index_path=index_path,
            source_root=parsed.source_root,
            database_copy=parsed.database_copy,
            account_id=parsed.account_id,
            copy_root=parsed.copy_root,
            cache_root=parsed.cache_root,
            recovery_root=parsed.recovery_root,
        )
    )


class ApplicationFacadePort(Protocol):
    """Public, side-effect-bounded service boundary consumed by the GUI."""

    def load_records(self) -> tuple[MediaRecord, ...]: ...

    def query(self, spec: FilterSpec, *, cursor: str | None = None) -> FilterResult: ...

    def preview(self, media_id: UUID) -> str: ...

    def select(
        self,
        result: FilterResult,
        scope: SelectionScope,
        *,
        selected_media_ids: tuple[UUID, ...] = (),
    ) -> SelectionSnapshot: ...

    def build_plan(
        self,
        selection: SelectionSnapshot,
        *,
        disposition: CleanupDisposition,
    ) -> PlanBuildResult: ...

    def preflight(self, plan: CleanupPlan) -> PreflightReport: ...

    def dry_run(self, plan: CleanupPlan) -> CleanupReceipt: ...

    def confirm(self, plan: CleanupPlan) -> ConfirmationResult: ...

    def recovery(self, receipt: CleanupReceipt) -> RecoveryResult: ...


class FakeApplicationFacade:
    """Deterministic fake facade used by the shell and local GUI tests."""

    def __init__(self, records: tuple[MediaRecord, ...] | None = None) -> None:
        self.account = _demo_account()
        self._records = records or _demo_records(self.account)

    def load_records(self) -> tuple[MediaRecord, ...]:
        return self._records

    def query(self, spec: FilterSpec, *, cursor: str | None = None) -> FilterResult:
        return filter_records(self._records, spec, cursor=cursor)

    def preview(self, media_id: UUID) -> str:
        if media_id not in {record.media_id for record in self._records}:
            raise FacadeError("预览目标不在当前索引中。")
        return "演示门面仅返回预览占位，不解码或写入文件。"

    def select(
        self,
        result: FilterResult,
        scope: SelectionScope,
        *,
        selected_media_ids: tuple[UUID, ...] = (),
    ) -> SelectionSnapshot:
        selected = None
        if scope is SelectionScope.MANUAL:
            by_id = {record.media_id: record for record in result.all_records}
            selected = tuple(
                by_id[media_id] for media_id in selected_media_ids if media_id in by_id
            )
        return build_selection_snapshot(
            result,
            scope,
            selected_records=selected,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )

    def build_plan(
        self,
        selection: SelectionSnapshot,
        *,
        disposition: CleanupDisposition,
    ) -> PlanBuildResult:
        chosen = tuple(
            record
            for record in self._records
            if record.media_id in set(selection.selected_media_ids)
        )
        result = build_plan(self.account, chosen, disposition=disposition)
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

    def preflight(self, plan: CleanupPlan) -> PreflightReport:
        """Return synthetic READY checks without touching the filesystem."""

        return PreflightReport(
            plan_id=plan.plan_id,
            checks=tuple(
                TargetPreflight(
                    target_id=target.target_id,
                    status=PreflightStatus.READY,
                    current=target.file,
                )
                for target in plan.targets
            ),
        )

    def dry_run(self, plan: CleanupPlan) -> CleanupReceipt:
        outcomes = tuple(
            ExecutionOutcome(
                target_id=target.target_id,
                status="dry_run",
                actual_reclaim_bytes=0,
            )
            for target in plan.targets
        )
        return build_receipt(plan, outcomes, executor_id="fake-dry-run")

    def confirm(self, plan: CleanupPlan) -> ConfirmationResult:
        del plan
        return ConfirmationResult(
            accepted=False,
            message="演示门面未连接执行器，未执行任何文件操作；未删除或移动任何文件。",
        )

    def recovery(self, receipt: CleanupReceipt) -> RecoveryResult:
        if receipt.status.value == "dry_run":
            return RecoveryResult(False, "试运行没有产生可恢复的文件操作。")
        return RecoveryResult(False, "当前门面没有可恢复的执行记录。")


def _demo_account() -> AccountRef:
    return AccountRef(
        account_id="wxid_synthetic_gui",
        account_root=r"C:\Synthetic\Wechat",
        display_name="演示账号",
        source_version="synthetic-gui-1",
    )


def _demo_records(account: AccountRef) -> tuple[MediaRecord, ...]:
    scan_id = uuid5(UUID("00000000-0000-0000-0000-000000000001"), "gui-scan")
    manifest = hashlib.sha256(b"synthetic-gui-manifest").hexdigest()
    records: list[MediaRecord] = []
    base = datetime(2026, 9, 1, tzinfo=UTC)
    for index in range(13):
        contact_id = f"synthetic_contact_{index % 4:02d}"
        contact = ContactRef(
            account_id=account.account_id,
            contact_id=contact_id,
            kind=ContactKind.GROUP if index % 3 == 0 else ContactKind.DIRECT,
            session_hash=hashlib.md5(contact_id.encode(), usedforsecurity=False).hexdigest(),
            display_name=f"演示联系人 {index % 4 + 1}",
            remark=None,
        )
        media_type = (MediaType.IMAGE, MediaType.VIDEO, MediaType.FILE)[index % 3]
        relative = (
            f"msg\\attach\\{contact_id}\\2026-09\\synthetic-{index:02d}."
            f"{('jpg', 'mp4', 'dat')[index % 3]}"
        )
        records.append(
            MediaRecord(
                media_id=uuid5(scan_id, f"media-{index}"),
                account_id=account.account_id,
                file=FileIdentity(
                    relative_path=relative,
                    byte_size=(index + 1) * 1024,
                    modified_time_ns=1_700_000_000_000_000_000 + index,
                    sha256=None,
                ),
                media_type=media_type,
                observed_at=base + timedelta(hours=index),
                contact=contact,
                message_local_id=10_000 + index,
                mapping_confidence=MappingConfidence.HIGH,
                mapping_reason="synthetic deterministic contact mapping",
                message_time=base + timedelta(hours=index),
                ownership_status=OwnershipStatus.MAPPED,
                record_key=f"gui-record-{index:02d}",
                scan_id=scan_id,
                scan_manifest_sha256=manifest,
            )
        )
    return tuple(records)


def default_filter_spec(*, page_size: int = 5) -> FilterSpec:
    """Return the conservative GUI default filter."""

    return FilterSpec(page_size=page_size, include_protected=False, include_unmapped=False)


def page_summary(page: FilterResultPage) -> str:
    return (
        f"共 {page.summary.total_count} 项，"
        f"可清理 {page.summary.eligible_count} 项，"
        f"预计 {page.summary.eligible_bytes / 1024:.1f} KiB"
    )
