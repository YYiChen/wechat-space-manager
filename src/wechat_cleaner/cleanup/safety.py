"""Non-mutating cleanup planning and execution safety gates.

The only filesystem operation here is a read-only identity check during
``preflight``.  A separate, human-confirmed executor may consume a plan after
``PreflightReport.can_execute`` is true.  This module never deletes, moves to a
recycle bin, changes a database, starts/stops WeChat, or writes a receipt to
disk.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from wechat_cleaner.domain.contracts import (
    AccountRef,
    CleanupDisposition,
    CleanupPlan,
    CleanupPlanSummary,
    CleanupReceipt,
    CleanupReceiptItem,
    CleanupTarget,
    ContractError,
    ContractErrorCode,
    FileIdentity,
    MappingConfidence,
    MediaRecord,
    MediaType,
    ReceiptStatus,
)

_PROTECTED_ROOTS = frozenset({"db_storage", "sendtemp"})
_PROTECTED_BUSINESS = ("business", "favorite")
_CACHE_ROOT = "cache"
_REPARSE_POINT_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SAFE_CONFIDENCE = frozenset(
    {MappingConfidence.EXACT, MappingConfidence.HIGH, MappingConfidence.MEDIUM}
)


class PreflightStatus(StrEnum):
    """Per-target state returned by the read-only execution gate."""

    READY = "ready"
    STALE = "stale"
    BLOCKED = "blocked"


class CleanupSafetyError(ValueError):
    """A caller supplied an unsafe plan, root, or execution result."""

    def __init__(self, code: ContractErrorCode, message: str) -> None:
        self.code = code
        self.error = ContractError(code=code, message=message)
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PlanBuildResult:
    """Accepted immutable plan plus per-record rejections."""

    plan: CleanupPlan | None
    rejected: tuple[ContractError, ...] = ()

    @property
    def accepted_count(self) -> int:
        return 0 if self.plan is None else len(self.plan.targets)


@dataclass(frozen=True, slots=True)
class TargetPreflight:
    """Read-only identity check for one plan target."""

    target_id: UUID
    status: PreflightStatus
    current: FileIdentity | None = None
    error: ContractError | None = None


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """Results of checking every target immediately before execution."""

    plan_id: UUID
    checks: tuple[TargetPreflight, ...]

    @property
    def can_execute(self) -> bool:
        return bool(self.checks) and all(
            check.status is PreflightStatus.READY for check in self.checks
        )


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """Result supplied by an independent executor for one target.

    Constructing this value does not perform an operation.  ``build_receipt``
    validates it against the immutable plan before creating a receipt.
    """

    target_id: UUID
    status: ReceiptStatus
    actual_reclaim_bytes: int = 0
    error: ContractError | None = None


def build_plan(
    account: AccountRef,
    records: Iterable[MediaRecord],
    *,
    disposition: CleanupDisposition = CleanupDisposition.RECYCLE_BIN,
    include_regenerable_cache: bool = False,
    acknowledged_risk: bool = False,
) -> PlanBuildResult:
    """Build a deterministic, immutable plan from user-selected media records.

    Rejected records are returned as generic ``ContractError`` values.  Error
    context contains only the record's account-relative path; no absolute path,
    contact text, message text, or key material is copied into the result.
    """

    if disposition is CleanupDisposition.PERMANENT and not acknowledged_risk:
        return PlanBuildResult(
            plan=None,
            rejected=(
                _error(
                    ContractErrorCode.PLAN_INVALID,
                    "permanent cleanup requires explicit risk acknowledgement",
                ),
            ),
        )

    accepted: list[MediaRecord] = []
    rejected: list[ContractError] = []
    seen_media: set[UUID] = set()
    seen_paths: set[str] = set()

    for record in records:
        relative_path = record.file.relative_path
        path_key = relative_path.casefold()
        if record.account_id != account.account_id:
            rejected.append(
                _error(
                    ContractErrorCode.ACCOUNT_MISMATCH,
                    "media record belongs to a different account",
                    relative_path,
                )
            )
            continue
        if record.media_id in seen_media or path_key in seen_paths:
            rejected.append(
                _error(
                    ContractErrorCode.PLAN_INVALID,
                    "duplicate media or relative path cannot enter a cleanup plan",
                    relative_path,
                )
            )
            continue
        seen_media.add(record.media_id)
        seen_paths.add(path_key)

        if record.mapping_confidence not in _SAFE_CONFIDENCE:
            rejected.append(
                _error(
                    ContractErrorCode.MAPPING_UNAVAILABLE,
                    "low-confidence or unmapped media cannot be selected",
                    relative_path,
                )
            )
            continue
        parts = _parts(relative_path)
        if _is_protected(parts):
            rejected.append(
                _error(
                    ContractErrorCode.EXECUTION_BLOCKED,
                    "database, favourite, and temporary upload paths are protected",
                    relative_path,
                )
            )
            continue
        is_cache = _is_cache(parts)
        if record.is_regenerable_cache and not is_cache:
            rejected.append(
                _error(
                    ContractErrorCode.EXECUTION_BLOCKED,
                    "regenerable cache metadata must stay below the cache root",
                    relative_path,
                )
            )
            continue
        if is_cache:
            if not record.is_regenerable_cache or not include_regenerable_cache:
                rejected.append(
                    _error(
                        ContractErrorCode.EXECUTION_BLOCKED,
                        "cache targets require explicit cache inclusion",
                        relative_path,
                    )
                )
                continue
        elif record.media_type is MediaType.OTHER:
            rejected.append(
                _error(
                    ContractErrorCode.EXECUTION_BLOCKED,
                    "unknown media type is not eligible for cleanup",
                    relative_path,
                )
            )
            continue
        accepted.append(record)

    if not accepted:
        return PlanBuildResult(plan=None, rejected=tuple(rejected))

    accepted.sort(key=lambda item: item.file.relative_path.casefold())
    targets = tuple(
        CleanupTarget(
            media_id=record.media_id,
            account_id=record.account_id,
            file=record.file,
            media_type=record.media_type,
            mapping_confidence=record.mapping_confidence,
            reason=(
                "user-selected; "
                f"mapping={record.mapping_confidence.value}; media={record.media_type.value}"
            ),
        )
        for record in accepted
    )
    by_type: dict[MediaType, int] = {}
    for target in targets:
        by_type[target.media_type] = by_type.get(target.media_type, 0) + target.file.byte_size
    summary = CleanupPlanSummary(
        target_count=len(targets),
        expected_reclaim_bytes=sum(target.file.byte_size for target in targets),
        by_media_type=tuple(sorted(by_type.items(), key=lambda item: item[0].value)),
        includes_regenerable_cache=any(record.is_regenerable_cache for record in accepted),
    )
    try:
        plan = CleanupPlan(
            account=account,
            disposition=disposition,
            targets=targets,
            summary=summary,
            acknowledged_risk=acknowledged_risk,
        )
    except ValueError:
        # Do not expose Pydantic details, which can include caller-supplied data.
        return PlanBuildResult(
            plan=None,
            rejected=tuple(
                [
                    *rejected,
                    _error(ContractErrorCode.PLAN_INVALID, "cleanup plan validation failed"),
                ]
            ),
        )
    return PlanBuildResult(plan=plan, rejected=tuple(rejected))


def preflight(
    plan: CleanupPlan,
    account_root: str | os.PathLike[str] | None = None,
    *,
    wechat_running: bool = False,
) -> PreflightReport:
    """Re-check a plan against the account copy without changing the filesystem."""

    if wechat_running:
        checks = tuple(
            TargetPreflight(
                target_id=target.target_id,
                status=PreflightStatus.BLOCKED,
                error=_error(
                    ContractErrorCode.WECHAT_RUNNING,
                    "WeChat must be closed before cleanup execution",
                    target.file.relative_path,
                    retryable=True,
                ),
            )
            for target in plan.targets
        )
        return PreflightReport(plan_id=plan.plan_id, checks=checks)

    root = _validated_account_root(plan.account, account_root)
    checks = tuple(_check_target(root, target) for target in plan.targets)
    return PreflightReport(plan_id=plan.plan_id, checks=checks)


def build_receipt(
    plan: CleanupPlan,
    outcomes: Iterable[ExecutionOutcome],
    *,
    executor_id: str,
    status: ReceiptStatus | None = None,
) -> CleanupReceipt:
    """Normalize independent executor results into an append-only receipt.

    The executor must supply exactly one outcome for every plan target.  This
    function only validates and serializes those outcomes; it never performs or
    retries an operation.
    """

    if not isinstance(executor_id, str):
        raise CleanupSafetyError(
            ContractErrorCode.PLAN_INVALID,
            "executor_id must be a non-empty logical identifier",
        )
    normalized_executor = executor_id.strip()
    if not normalized_executor or "/" in normalized_executor or "\\" in normalized_executor:
        raise CleanupSafetyError(
            ContractErrorCode.PLAN_INVALID,
            "executor_id must be a non-empty logical identifier",
        )
    if len(normalized_executor) > 128:
        raise CleanupSafetyError(
            ContractErrorCode.PLAN_INVALID,
            "executor_id is too long",
        )

    target_by_id = {target.target_id: target for target in plan.targets}
    outcome_by_id: dict[UUID, ExecutionOutcome] = {}
    for outcome in outcomes:
        if outcome.target_id in outcome_by_id or outcome.target_id not in target_by_id:
            raise CleanupSafetyError(
                ContractErrorCode.PLAN_INVALID,
                "outcomes must cover each plan target exactly once",
            )
        try:
            outcome_status = ReceiptStatus(outcome.status)
        except (TypeError, ValueError) as exc:
            raise CleanupSafetyError(
                ContractErrorCode.PLAN_INVALID,
                "invalid execution outcome status",
            ) from exc
        if outcome_status not in {
            ReceiptStatus.DRY_RUN,
            ReceiptStatus.SUCCEEDED,
            ReceiptStatus.SKIPPED,
            ReceiptStatus.FAILED,
        }:
            raise CleanupSafetyError(
                ContractErrorCode.PLAN_INVALID,
                "planned is not a completed execution outcome",
            )
        if not isinstance(outcome.actual_reclaim_bytes, int) or isinstance(
            outcome.actual_reclaim_bytes, bool
        ):
            raise CleanupSafetyError(
                ContractErrorCode.PLAN_INVALID,
                "actual reclaimed bytes must be an integer",
            )
        if outcome.actual_reclaim_bytes < 0:
            raise CleanupSafetyError(
                ContractErrorCode.PLAN_INVALID,
                "actual reclaimed bytes cannot be negative",
            )
        target = target_by_id[outcome.target_id]
        if outcome.actual_reclaim_bytes > target.file.byte_size:
            raise CleanupSafetyError(
                ContractErrorCode.FILE_IDENTITY_MISMATCH,
                "actual reclaimed bytes cannot exceed planned size",
            )
        if outcome_status is ReceiptStatus.FAILED and outcome.error is None:
            raise CleanupSafetyError(
                ContractErrorCode.PLAN_INVALID,
                "failed outcomes must include a generic error",
            )
        if outcome_status is not ReceiptStatus.SUCCEEDED and outcome.actual_reclaim_bytes:
            raise CleanupSafetyError(
                ContractErrorCode.PLAN_INVALID,
                "only succeeded outcomes may reclaim bytes",
            )
        outcome_by_id[outcome.target_id] = ExecutionOutcome(
            target_id=outcome.target_id,
            status=outcome_status,
            actual_reclaim_bytes=outcome.actual_reclaim_bytes,
            error=outcome.error,
        )

    if set(outcome_by_id) != set(target_by_id):
        raise CleanupSafetyError(
            ContractErrorCode.PLAN_INVALID,
            "outcomes must cover each plan target exactly once",
        )

    ordered_outcomes = [outcome_by_id[target.target_id] for target in plan.targets]
    derived_status = _derive_receipt_status(ordered_outcomes)
    receipt_status = derived_status if status is None else status
    if not isinstance(receipt_status, ReceiptStatus):
        raise CleanupSafetyError(ContractErrorCode.PLAN_INVALID, "invalid receipt status")
    if receipt_status is not derived_status:
        raise CleanupSafetyError(
            ContractErrorCode.PLAN_INVALID,
            "receipt status does not match target outcomes",
        )

    items = tuple(
        CleanupReceiptItem(
            target_id=outcome.target_id,
            status=outcome.status,
            actual_reclaim_bytes=outcome.actual_reclaim_bytes,
            error=_sanitize_error(outcome.error, target_by_id[outcome.target_id])
            if outcome.error is not None
            else None,
        )
        for outcome in ordered_outcomes
    )
    return CleanupReceipt(
        plan_id=plan.plan_id,
        account=plan.account,
        status=receipt_status,
        items=items,
        total_reclaimed_bytes=sum(item.actual_reclaim_bytes for item in items),
        executor_id=normalized_executor,
    )


def _derive_receipt_status(outcomes: Iterable[ExecutionOutcome]) -> ReceiptStatus:
    statuses = {outcome.status for outcome in outcomes}
    if ReceiptStatus.FAILED in statuses:
        return ReceiptStatus.FAILED
    if statuses == {ReceiptStatus.DRY_RUN}:
        return ReceiptStatus.DRY_RUN
    if statuses == {ReceiptStatus.SUCCEEDED}:
        return ReceiptStatus.SUCCEEDED
    if statuses == {ReceiptStatus.SKIPPED}:
        return ReceiptStatus.SKIPPED
    return ReceiptStatus.SKIPPED


def _check_target(root: Path, target: CleanupTarget) -> TargetPreflight:
    relative_path = target.file.relative_path
    parts = _parts(relative_path)
    if _is_protected(parts):
        return _blocked(
            target,
            ContractErrorCode.EXECUTION_BLOCKED,
            "protected path cannot be executed",
        )

    candidate = root.joinpath(*parts)
    root_abs = os.path.normcase(os.path.abspath(str(root)))
    candidate_abs = os.path.normcase(os.path.abspath(str(candidate)))
    try:
        if os.path.commonpath([root_abs, candidate_abs]) != root_abs:
            return _blocked(
                target,
                ContractErrorCode.PATH_OUTSIDE_ACCOUNT_ROOT,
                "target path is outside account root",
            )
    except ValueError:
        return _blocked(
            target,
            ContractErrorCode.PATH_OUTSIDE_ACCOUNT_ROOT,
            "target path is outside account root",
        )

    try:
        if _path_is_reparse(candidate):
            return _blocked(
                target,
                ContractErrorCode.INVALID_PATH,
                "reparse points cannot be executed",
            )
        resolved_root = root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
        resolved_root_norm = os.path.normcase(os.path.abspath(str(resolved_root)))
        resolved_candidate_norm = os.path.normcase(os.path.abspath(str(resolved_candidate)))
        if resolved_root_norm != root_abs or os.path.commonpath(
            [resolved_root_norm, resolved_candidate_norm]
        ) != resolved_root_norm:
            return _blocked(
                target,
                ContractErrorCode.PATH_OUTSIDE_ACCOUNT_ROOT,
                "target path resolves outside account root",
            )
        info = candidate.lstat()
        if _is_reparse_info(info) or not stat.S_ISREG(info.st_mode):
            return _blocked(
                target,
                ContractErrorCode.INVALID_PATH,
                "target is not a regular file",
            )
    except (OSError, ValueError):
        return _blocked(
            target,
            ContractErrorCode.INVALID_PATH,
            "target file could not be inspected",
        )

    try:
        actual_sha = _sha256(candidate) if target.file.sha256 is not None else None
    except OSError:
        return _blocked(
            target,
            ContractErrorCode.INVALID_PATH,
            "target file could not be hashed",
        )
    current = FileIdentity(
        relative_path=relative_path,
        byte_size=info.st_size,
        modified_time_ns=info.st_mtime_ns,
        sha256=actual_sha,
    )
    if (
        current.byte_size != target.file.byte_size
        or current.modified_time_ns != target.file.modified_time_ns
        or (
            target.file.sha256 is not None
            and current.sha256 != target.file.sha256
        )
    ):
        return TargetPreflight(
            target_id=target.target_id,
            status=PreflightStatus.STALE,
            current=current,
            error=_error(
                ContractErrorCode.FILE_IDENTITY_MISMATCH,
                "target file identity changed since the plan was created",
                relative_path,
            ),
        )
    return TargetPreflight(
        target_id=target.target_id,
        status=PreflightStatus.READY,
        current=current,
    )


def _validated_account_root(account: AccountRef, supplied: str | os.PathLike[str] | None) -> Path:
    expected = os.path.normcase(os.path.abspath(account.account_root))
    value = account.account_root if supplied is None else str(supplied)
    path = Path(value)
    if not path.is_absolute():
        raise CleanupSafetyError(ContractErrorCode.INVALID_PATH, "account root must be absolute")
    actual = os.path.normcase(os.path.abspath(str(path)))
    if actual != expected:
        raise CleanupSafetyError(
            ContractErrorCode.ACCOUNT_MISMATCH,
            "account root does not match the plan account",
        )
    try:
        if not path.exists() or not path.is_dir() or _path_is_reparse(path):
            raise CleanupSafetyError(
                ContractErrorCode.INVALID_PATH,
                "account root must be an existing non-reparse directory",
            )
        resolved = path.resolve(strict=True)
        if os.path.normcase(os.path.abspath(str(resolved))) != actual:
            raise CleanupSafetyError(
                ContractErrorCode.INVALID_PATH,
                "account root resolves through a reparse point",
            )
    except OSError as exc:
        raise CleanupSafetyError(
            ContractErrorCode.INVALID_PATH,
            "account root could not be inspected",
        ) from exc
    return path


def _parts(relative_path: str) -> tuple[str, ...]:
    return tuple(relative_path.replace("/", "\\").split("\\"))


def _is_protected(parts: tuple[str, ...]) -> bool:
    lowered = tuple(part.casefold() for part in parts)
    return bool(lowered) and (
        lowered[0] in _PROTECTED_ROOTS or lowered[:2] == _PROTECTED_BUSINESS
    )


def _is_cache(parts: tuple[str, ...]) -> bool:
    return bool(parts) and parts[0].casefold() == _CACHE_ROOT


def _blocked(
    target: CleanupTarget,
    code: ContractErrorCode,
    message: str,
) -> TargetPreflight:
    return TargetPreflight(
        target_id=target.target_id,
        status=PreflightStatus.BLOCKED,
        error=_error(code, message, target.file.relative_path),
    )


def _error(
    code: ContractErrorCode,
    message: str,
    relative_path: str | None = None,
    *,
    retryable: bool = False,
) -> ContractError:
    context = () if relative_path is None else (("relative_path", relative_path),)
    return ContractError(code=code, message=message, retryable=retryable, context=context)


def _sanitize_error(error: ContractError, target: CleanupTarget) -> ContractError:
    """Keep executor-supplied errors generic and relative-path-only."""

    if not isinstance(error, ContractError):
        return _error(
            ContractErrorCode.UNKNOWN,
            "executor reported an unrecognized failure",
            target.file.relative_path,
        )
    return _error(
        error.code,
        "executor reported a failure for the selected target",
        target.file.relative_path,
        retryable=error.retryable,
    )


def _path_is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    return _is_reparse_info(info)


def _is_reparse_info(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT_FLAG
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
