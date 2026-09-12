"""Recoverable, safety-gated cleanup executor.

This module is deliberately narrower than a general file manager.  It
consumes immutable :class:`CleanupPlan` objects, performs a complete
read-only preflight, and supports only dry-run plus moves into an explicitly
provided recovery store.  There is no permanent-delete operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from wechat_cleaner.cleanup import PreflightStatus, preflight
from wechat_cleaner.domain.contracts import (
    CleanupExecution,
    CleanupExecutionMode,
    CleanupPlan,
    CleanupReceipt,
    CleanupReceiptItem,
    CleanupTarget,
    ContractError,
    ContractErrorCode,
    ReceiptStatus,
    RecoveryLocator,
    RecoveryLocatorKind,
    RecoveryState,
)

_REPARSE_POINT_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class ExecutorErrorCode:
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    EXECUTION_BLOCKED = "EXECUTION_BLOCKED"
    PLAN_STALE = "PLAN_STALE"
    INVALID_PATH = "INVALID_PATH"
    FILE_IDENTITY_MISMATCH = "FILE_IDENTITY_MISMATCH"
    RECOVERY_ROOT_INVALID = "RECOVERY_ROOT_INVALID"
    RECEIPT_WRITE_FAILED = "RECEIPT_WRITE_FAILED"
    RESTORE_FAILED = "RESTORE_FAILED"


class ExecutorError(RuntimeError):
    """A generic, path-redacted execution failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    receipt: CleanupReceipt
    execution: CleanupExecution
    receipt_persisted: bool = True


@dataclass(frozen=True, slots=True)
class RestoreResult:
    receipt: CleanupReceipt
    restored_count: int
    failed_count: int
    messages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _RecoveryEntry:
    recovery_id: str
    source_relative_path: str
    destination: Path
    kind: RecoveryLocatorKind


class RecoverableExecutor:
    """Execute plans over a caller-owned synthetic/recovery directory."""

    def __init__(
        self,
        *,
        executor_id: str = "recoverable-executor-1",
        process_probe: Callable[[], bool] | None = None,
        before_target: Callable[[CleanupTarget, int], None] | None = None,
    ) -> None:
        _validate_executor_id(executor_id)
        self.executor_id = executor_id
        self.process_probe = process_probe
        self.before_target = before_target
        self._recoveries: dict[str, _RecoveryEntry] = {}
        self._executions: dict[tuple[UUID, UUID], _RecoveryEntry] = {}

    def plan_digest(self, plan: CleanupPlan) -> str:
        """Return the canonical confirmation digest for an immutable plan."""

        payload = plan.model_dump(mode="json")
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def execute(
        self,
        plan: CleanupPlan,
        *,
        mode: CleanupExecutionMode = CleanupExecutionMode.DRY_RUN,
        account_root: str | os.PathLike[str] | None = None,
        recovery_root: str | os.PathLike[str] | None = None,
        expected_plan_digest: str | None = None,
        confirm: bool = False,
        wechat_running: bool = False,
        receipt_sink: Callable[[CleanupReceipt], None] | None = None,
    ) -> ExecutionResult:
        """Run a plan after all safety gates pass.

        ``QUARANTINE`` and ``RECYCLE_BIN`` both move into the explicit
        recovery store.  The distinction is retained in the receipt and
        recovery locator; neither mode reports bytes as immediately reclaimed.
        """

        try:
            mode = CleanupExecutionMode(mode)
        except ValueError as exc:
            raise ExecutorError(
                ExecutorErrorCode.EXECUTION_BLOCKED,
                "unsupported execution mode",
            ) from exc
        started_at = datetime.now(UTC)
        root = _validated_account_root(plan, account_root)
        self._gate_plan(
            plan,
            mode=mode,
            expected_plan_digest=expected_plan_digest,
            confirm=confirm,
            wechat_running=wechat_running,
        )
        report = preflight(plan, root)
        if not report.can_execute and not self._retry_preflight_ok(plan, root, report):
            raise ExecutorError(
                ExecutorErrorCode.EXECUTION_BLOCKED,
                "plan preflight did not pass for every target",
            )

        if mode is CleanupExecutionMode.DRY_RUN:
            result = self._dry_run(plan, started_at)
            return _persist_receipt(result, receipt_sink)

        recovery = _validated_recovery_root(root, recovery_root)
        recovery.mkdir(parents=True, exist_ok=True)
        if not recovery.is_dir() or _is_reparse(recovery.lstat()):
            raise ExecutorError(
                ExecutorErrorCode.RECOVERY_ROOT_INVALID,
                "recovery root is not a regular directory",
            )
        result = self._move_targets(plan, mode, root, recovery, started_at)
        return _persist_receipt(result, receipt_sink)

    def _retry_preflight_ok(self, plan: CleanupPlan, root: Path, report: Any) -> bool:
        """Allow only exact, already-moved targets to be retried idempotently."""

        for target, check in zip(plan.targets, report.checks, strict=True):
            if check.status is PreflightStatus.READY:
                continue
            entry = self._executions.get((plan.plan_id, target.target_id))
            source = root.joinpath(*_path_parts(target.file.relative_path))
            if entry is None or source.exists() or not entry.destination.exists():
                return False
        return True

    def restore(
        self,
        receipt: CleanupReceipt,
        *,
        recovery_id: str | None = None,
    ) -> RestoreResult:
        """Restore one or all available recovery entries from this executor."""

        candidates = [
            item.recovery
            for item in receipt.items
            if item.recovery is not None
            and item.recovery.state is RecoveryState.AVAILABLE
            and (recovery_id is None or item.recovery.recovery_id == recovery_id)
        ]
        messages: list[str] = []
        restored = 0
        failed = 0
        updated_items: list[CleanupReceiptItem] = []
        selected = {locator.recovery_id for locator in candidates}
        for item in receipt.items:
            locator = item.recovery
            if locator is None or locator.recovery_id not in selected:
                updated_items.append(item)
                continue
            entry = self._recoveries.get(locator.recovery_id)
            if entry is None:
                failed += 1
                messages.append("恢复记录不在当前执行器会话中。")
                updated_items.append(item)
                continue
            source = Path(receipt.account.account_root) / Path(entry.source_relative_path)
            try:
                _validate_restore_paths(source, entry.destination, receipt.account.account_root)
                if source.exists():
                    raise ExecutorError(
                        ExecutorErrorCode.RESTORE_FAILED,
                        "恢复目标已存在，未覆盖现有文件",
                    )
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(entry.destination), str(source))
                restored += 1
                updated_locator = locator.model_copy(update={"state": RecoveryState.RESTORED})
                updated_items.append(item.model_copy(update={"recovery": updated_locator}))
            except (OSError, ExecutorError):
                failed += 1
                messages.append("恢复失败，原文件未被覆盖。")
                updated_items.append(item)
        updated_recovery = tuple(
            item.recovery for item in updated_items if item.recovery is not None
        )
        updated_receipt = receipt.model_copy(
            update={"items": tuple(updated_items), "recovery": updated_recovery}
        )
        return RestoreResult(
            receipt=updated_receipt,
            restored_count=restored,
            failed_count=failed,
            messages=tuple(messages),
        )

    def _gate_plan(
        self,
        plan: CleanupPlan,
        *,
        mode: CleanupExecutionMode,
        expected_plan_digest: str | None,
        confirm: bool,
        wechat_running: bool,
    ) -> None:
        if plan.disposition.value == "permanent":
            raise ExecutorError(
                ExecutorErrorCode.EXECUTION_BLOCKED,
                "permanent deletion is not supported",
            )
        if self.process_probe is not None:
            try:
                wechat_running = wechat_running or bool(self.process_probe())
            except Exception as exc:  # noqa: BLE001 - fail closed at the process boundary
                raise ExecutorError(
                    ExecutorErrorCode.EXECUTION_BLOCKED,
                    "WeChat process state could not be confirmed",
                ) from exc
        if wechat_running:
            raise ExecutorError(
                ExecutorErrorCode.EXECUTION_BLOCKED,
                "WeChat must be closed before cleanup execution",
            )
        if plan.expires_at is not None and plan.expires_at <= datetime.now(UTC):
            raise ExecutorError(ExecutorErrorCode.PLAN_STALE, "cleanup plan has expired")
        if mode is CleanupExecutionMode.DRY_RUN:
            if expected_plan_digest is not None and expected_plan_digest != self.plan_digest(plan):
                raise ExecutorError(
                    ExecutorErrorCode.PLAN_STALE,
                    "cleanup plan confirmation is stale",
                )
            return
        if not confirm:
            raise ExecutorError(
                ExecutorErrorCode.EXECUTION_BLOCKED,
                "explicit confirmation is required before moving files",
            )
        if expected_plan_digest != self.plan_digest(plan):
            raise ExecutorError(ExecutorErrorCode.PLAN_STALE, "cleanup plan confirmation is stale")
        expected_mode = {
            "quarantine": CleanupExecutionMode.QUARANTINE,
            "recycle_bin": CleanupExecutionMode.RECYCLE_BIN,
        }.get(plan.disposition.value)
        if expected_mode is not mode:
            raise ExecutorError(
                ExecutorErrorCode.EXECUTION_BLOCKED,
                "execution mode does not match the cleanup plan",
            )

    def _dry_run(self, plan: CleanupPlan, started_at: datetime) -> ExecutionResult:
        items = tuple(
            CleanupReceiptItem(
                target_id=target.target_id,
                status=ReceiptStatus.DRY_RUN,
                processed_bytes=target.file.byte_size,
            )
            for target in plan.targets
        )
        finished_at = datetime.now(UTC)
        receipt = CleanupReceipt(
            plan_id=plan.plan_id,
            account=plan.account,
            status=ReceiptStatus.DRY_RUN,
            items=items,
            total_reclaimed_bytes=0,
            executor_id=self.executor_id,
            execution_mode=CleanupExecutionMode.DRY_RUN,
            processed_bytes=sum(item.processed_bytes for item in items),
        )
        execution = CleanupExecution(
            plan_id=plan.plan_id,
            mode=CleanupExecutionMode.DRY_RUN,
            status=ReceiptStatus.DRY_RUN,
            started_at=started_at,
            finished_at=finished_at,
            processed_bytes=receipt.processed_bytes,
            reclaimed_bytes=0,
        )
        return ExecutionResult(receipt=receipt, execution=execution)

    def _move_targets(
        self,
        plan: CleanupPlan,
        mode: CleanupExecutionMode,
        root: Path,
        recovery_root: Path,
        started_at: datetime,
    ) -> ExecutionResult:
        folder = "quarantine" if mode is CleanupExecutionMode.QUARANTINE else "recycle-bin"
        destination_root = recovery_root / folder / str(plan.plan_id)
        destination_root.mkdir(parents=True, exist_ok=True)
        items: list[CleanupReceiptItem] = []
        for index, target in enumerate(plan.targets):
            if self.before_target is not None:
                self.before_target(target, index)
            item = self._move_one(plan, target, root, destination_root, mode)
            items.append(item)
        status = (
            ReceiptStatus.FAILED
            if any(item.status is ReceiptStatus.FAILED for item in items)
            else ReceiptStatus.SUCCEEDED
        )
        recovery = tuple(
            item.recovery for item in items if item.recovery is not None
        )
        finished_at = datetime.now(UTC)
        receipt = CleanupReceipt(
            plan_id=plan.plan_id,
            account=plan.account,
            status=status,
            items=tuple(items),
            total_reclaimed_bytes=sum(item.actual_reclaim_bytes for item in items),
            executor_id=self.executor_id,
            execution_mode=mode,
            processed_bytes=sum(item.processed_bytes for item in items),
            recovery=recovery,
        )
        execution = CleanupExecution(
            plan_id=plan.plan_id,
            mode=mode,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            processed_bytes=receipt.processed_bytes,
            reclaimed_bytes=receipt.total_reclaimed_bytes,
            recovery=recovery,
        )
        return ExecutionResult(receipt=receipt, execution=execution)

    def _move_one(
        self,
        plan: CleanupPlan,
        target: CleanupTarget,
        root: Path,
        destination_root: Path,
        mode: CleanupExecutionMode,
    ) -> CleanupReceiptItem:
        key = (plan.plan_id, target.target_id)
        known = self._executions.get(key)
        if known is not None and known.destination.exists():
            locator = _locator(known, RecoveryState.AVAILABLE)
            return CleanupReceiptItem(
                target_id=target.target_id,
                status=ReceiptStatus.SUCCEEDED,
                processed_bytes=target.file.byte_size,
                recovery=locator,
            )
        source = root.joinpath(*_path_parts(target.file.relative_path))
        try:
            _validate_source_identity(source, target)
        except ExecutorError as exc:
            return _failed_item(target, exc.code, str(exc))
        destination = _unique_destination(destination_root, source.name)
        try:
            shutil.move(str(source), str(destination))
        except OSError:
            return _failed_item(
                target,
                ExecutorErrorCode.EXECUTION_BLOCKED,
                "target could not be moved to the recovery store",
            )
        recovery_id = f"recovery-{uuid4().hex}"
        entry = _RecoveryEntry(
            recovery_id=recovery_id,
            source_relative_path=target.file.relative_path,
            destination=destination,
            kind=(
                RecoveryLocatorKind.QUARANTINE
                if mode is CleanupExecutionMode.QUARANTINE
                else RecoveryLocatorKind.RECYCLE_BIN
            ),
        )
        self._recoveries[recovery_id] = entry
        self._executions[key] = entry
        locator = _locator(
            entry,
            RecoveryState.AVAILABLE,
            recovery_root=destination_root.parent.parent,
        )
        return CleanupReceiptItem(
            target_id=target.target_id,
            status=ReceiptStatus.SUCCEEDED,
            processed_bytes=target.file.byte_size,
            recovery=locator,
        )


def _persist_receipt(
    result: ExecutionResult,
    sink: Callable[[CleanupReceipt], None] | None,
) -> ExecutionResult:
    if sink is None:
        return result
    try:
        sink(result.receipt)
    except Exception as exc:  # noqa: BLE001 - persistence is an explicit boundary
        raise ExecutorError(
            ExecutorErrorCode.RECEIPT_WRITE_FAILED,
            "cleanup receipt could not be persisted",
        ) from exc
    return result


def _validate_executor_id(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("executor_id must be a non-empty logical identifier")
    if "/" in value or "\\" in value:
        raise ValueError("executor_id must be a non-empty logical identifier")


def _validated_account_root(
    plan: CleanupPlan, supplied: str | os.PathLike[str] | None
) -> Path:
    expected = os.path.normcase(os.path.abspath(plan.account.account_root))
    value = plan.account.account_root if supplied is None else str(supplied)
    root = Path(value)
    actual = os.path.normcase(os.path.abspath(str(root)))
    if actual != expected or not root.is_absolute():
        raise ExecutorError(
            ExecutorErrorCode.ACCOUNT_MISMATCH,
            "account root does not match the plan",
        )
    try:
        info = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ExecutorError(
            ExecutorErrorCode.INVALID_PATH,
            "account root could not be inspected",
        ) from exc
    if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
        raise ExecutorError(
            ExecutorErrorCode.INVALID_PATH,
            "account root is not a regular directory",
        )
    if os.path.normcase(os.path.abspath(str(resolved))) != actual:
        raise ExecutorError(
            ExecutorErrorCode.INVALID_PATH,
            "account root resolves through a reparse point",
        )
    return root


def _validated_recovery_root(account_root: Path, supplied: str | os.PathLike[str] | None) -> Path:
    if supplied is None:
        raise ExecutorError(
            ExecutorErrorCode.RECOVERY_ROOT_INVALID,
            "an explicit recovery root is required for moving modes",
        )
    recovery = Path(supplied)
    account_abs = os.path.normcase(os.path.abspath(str(account_root)))
    recovery_abs = os.path.normcase(os.path.abspath(str(recovery)))
    try:
        if os.path.commonpath([account_abs, recovery_abs]) in {account_abs, recovery_abs}:
            raise ExecutorError(
                ExecutorErrorCode.RECOVERY_ROOT_INVALID,
                "recovery root must be outside the account root",
            )
    except ValueError as exc:
        raise ExecutorError(
            ExecutorErrorCode.RECOVERY_ROOT_INVALID,
            "recovery root is on an incompatible volume",
        ) from exc
    if recovery.exists():
        try:
            info = recovery.lstat()
        except OSError as exc:
            raise ExecutorError(
                ExecutorErrorCode.RECOVERY_ROOT_INVALID,
                "recovery root could not be inspected",
            ) from exc
        if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
            raise ExecutorError(
                ExecutorErrorCode.RECOVERY_ROOT_INVALID,
                "recovery root is not a regular directory",
            )
    return recovery


def _path_parts(relative_path: str) -> tuple[str, ...]:
    parts = tuple(relative_path.replace("/", "\\").split("\\"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ExecutorError(ExecutorErrorCode.INVALID_PATH, "target relative path is invalid")
    return parts


def _validate_source_identity(source: Path, target: CleanupTarget) -> None:
    try:
        info = source.lstat()
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise ExecutorError(
            ExecutorErrorCode.INVALID_PATH,
            "target file could not be inspected",
        ) from exc
    if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
        raise ExecutorError(ExecutorErrorCode.INVALID_PATH, "target is not a regular file")
    if resolved != source.resolve(strict=False):
        raise ExecutorError(
            ExecutorErrorCode.INVALID_PATH,
            "target resolves outside the account root",
        )
    if info.st_size != target.file.byte_size or info.st_mtime_ns != target.file.modified_time_ns:
        raise ExecutorError(
            ExecutorErrorCode.FILE_IDENTITY_MISMATCH,
            "target file identity changed since the plan was created",
        )
    if target.file.sha256 is not None and _sha256(source) != target.file.sha256:
        raise ExecutorError(
            ExecutorErrorCode.FILE_IDENTITY_MISMATCH,
            "target file identity changed since the plan was created",
        )


def _unique_destination(root: Path, name: str) -> Path:
    candidate = root / name
    if not candidate.exists():
        return candidate
    stem = Path(name).stem
    suffix = Path(name).suffix
    index = 1
    while True:
        candidate = root / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _locator(
    entry: _RecoveryEntry,
    state: RecoveryState,
    *,
    recovery_root: Path | None = None,
) -> RecoveryLocator:
    if recovery_root is None:
        # This branch is used only for idempotent retries; the registry keeps
        # the original destination, so derive a stable path from its parent.
        recovery_root = entry.destination.parents[2]
    relative = entry.destination.relative_to(recovery_root).as_posix()
    return RecoveryLocator(
        recovery_id=entry.recovery_id,
        kind=entry.kind,
        state=state,
        relative_path=relative,
    )


def _failed_item(target: CleanupTarget, code: str, message: str) -> CleanupReceiptItem:
    try:
        error_code = ContractErrorCode(code)
    except ValueError:
        error_code = ContractErrorCode.EXECUTION_BLOCKED
    return CleanupReceiptItem(
        target_id=target.target_id,
        status=ReceiptStatus.FAILED,
        processed_bytes=target.file.byte_size,
        error=ContractError(
            code=error_code,
            message=message,
            context=(("relative_path", target.file.relative_path),),
        ),
    )


def _validate_restore_paths(source: Path, destination: Path, account_root: str) -> None:
    account_abs = os.path.normcase(os.path.abspath(account_root))
    source_abs = os.path.normcase(os.path.abspath(str(source)))
    if os.path.commonpath([account_abs, source_abs]) != account_abs:
        raise ExecutorError(
            ExecutorErrorCode.INVALID_PATH,
            "restore target is outside account root",
        )
    if not destination.exists() or _is_reparse(destination.lstat()) or not destination.is_file():
        raise ExecutorError(ExecutorErrorCode.RESTORE_FAILED, "recovery item is unavailable")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(info: Any) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT_FLAG
    )
