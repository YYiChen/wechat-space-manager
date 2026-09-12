"""Safety and recovery tests for the synthetic cleanup executor."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wechat_cleaner.cleanup import build_plan
from wechat_cleaner.domain.contracts import (
    AccountRef,
    CleanupDisposition,
    CleanupExecutionMode,
    ContactKind,
    ContactRef,
    FileIdentity,
    MappingConfidence,
    MediaRecord,
    MediaType,
    OwnershipStatus,
    ReceiptStatus,
)
from wechat_cleaner.executor import (
    ExecutorError,
    ExecutorErrorCode,
    RecoverableExecutor,
)


def _make_plan(
    root: Path,
    relative_paths: tuple[str, ...] = (r"msg\attach\friend\item.dat",),
    *,
    disposition: CleanupDisposition = CleanupDisposition.QUARANTINE,
):
    account = AccountRef(account_id="wxid_synthetic_exec", account_root=str(root))
    records = []
    for index, relative_path in enumerate(relative_paths):
        path = root.joinpath(*relative_path.split("\\"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"synthetic-{index}".encode())
        info = path.stat()
        records.append(
            MediaRecord(
                account_id=account.account_id,
                file=FileIdentity(
                    relative_path=relative_path,
                    byte_size=info.st_size,
                    modified_time_ns=info.st_mtime_ns,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                ),
                media_type=MediaType.IMAGE,
                observed_at=datetime.now(UTC),
                contact=ContactRef(
                    account_id=account.account_id,
                    contact_id="synthetic_friend",
                    kind=ContactKind.DIRECT,
                    session_hash=None,
                ),
                message_local_id=1000 + index,
                mapping_confidence=MappingConfidence.HIGH,
                mapping_reason="synthetic executor test",
                ownership_status=OwnershipStatus.MAPPED,
            )
        )
    result = build_plan(account, records, disposition=disposition)
    assert result.plan is not None
    return result.plan


def test_dry_run_never_moves_and_separates_processed_from_reclaimed(tmp_path: Path) -> None:
    root = tmp_path / "account"
    root.mkdir()
    plan = _make_plan(root)
    executor = RecoverableExecutor()

    result = executor.execute(plan, mode=CleanupExecutionMode.DRY_RUN)

    source = root / "msg" / "attach" / "friend" / "item.dat"
    assert source.is_file()
    assert result.receipt.status is ReceiptStatus.DRY_RUN
    assert result.receipt.processed_bytes == source.stat().st_size
    assert result.receipt.total_reclaimed_bytes == 0
    assert result.execution.reclaimed_bytes == 0


def test_quarantine_is_recoverable_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "account"
    recovery = tmp_path / "recovery"
    root.mkdir()
    plan = _make_plan(root)
    executor = RecoverableExecutor()
    digest = executor.plan_digest(plan)

    result = executor.execute(
        plan,
        mode=CleanupExecutionMode.QUARANTINE,
        recovery_root=recovery,
        expected_plan_digest=digest,
        confirm=True,
    )
    source = root / "msg" / "attach" / "friend" / "item.dat"
    assert not source.exists()
    assert result.receipt.status is ReceiptStatus.SUCCEEDED
    assert result.receipt.total_reclaimed_bytes == 0
    locator = result.receipt.items[0].recovery
    assert locator is not None
    assert locator.relative_path is not None

    retry = executor.execute(
        plan,
        mode=CleanupExecutionMode.QUARANTINE,
        recovery_root=recovery,
        expected_plan_digest=digest,
        confirm=True,
    )
    assert retry.receipt.status is ReceiptStatus.SUCCEEDED
    assert retry.receipt.items[0].recovery is not None

    restored = executor.restore(result.receipt)
    assert restored.restored_count == 1
    assert restored.failed_count == 0
    assert source.is_file()
    assert restored.receipt.items[0].recovery is not None
    assert restored.receipt.items[0].recovery.state.value == "restored"


def test_recycle_bin_mode_preserves_same_name_conflicts(tmp_path: Path) -> None:
    root = tmp_path / "account"
    recovery = tmp_path / "recovery"
    root.mkdir()
    plan = _make_plan(
        root,
        (r"msg\a\same.dat", r"msg\b\same.dat"),
        disposition=CleanupDisposition.RECYCLE_BIN,
    )
    executor = RecoverableExecutor()
    result = executor.execute(
        plan,
        mode=CleanupExecutionMode.RECYCLE_BIN,
        recovery_root=recovery,
        expected_plan_digest=executor.plan_digest(plan),
        confirm=True,
    )
    locators = [item.recovery for item in result.receipt.items]
    assert all(locator is not None for locator in locators)
    assert len({locator.relative_path for locator in locators if locator}) == 2
    assert all("recycle-bin" in (locator.relative_path or "") for locator in locators if locator)


def test_confirmation_process_and_stale_identity_gates(tmp_path: Path) -> None:
    root = tmp_path / "account"
    root.mkdir()
    plan = _make_plan(root)
    executor = RecoverableExecutor()
    with pytest.raises(ExecutorError) as missing_confirmation:
        executor.execute(
            plan,
            mode=CleanupExecutionMode.QUARANTINE,
            recovery_root=tmp_path / "recovery",
            expected_plan_digest=executor.plan_digest(plan),
        )
    assert missing_confirmation.value.code == ExecutorErrorCode.EXECUTION_BLOCKED
    with pytest.raises(ExecutorError) as stale:
        executor.execute(
            plan,
            mode=CleanupExecutionMode.QUARANTINE,
            recovery_root=tmp_path / "recovery",
            expected_plan_digest="0" * 64,
            confirm=True,
        )
    assert stale.value.code == ExecutorErrorCode.PLAN_STALE
    with pytest.raises(ExecutorError) as running:
        executor.execute(plan, wechat_running=True)
    assert running.value.code == ExecutorErrorCode.EXECUTION_BLOCKED

    def mutate(_target, _index):
        (root / "msg" / "attach" / "friend" / "item.dat").write_bytes(b"changed")

    changed = RecoverableExecutor(before_target=mutate).execute(
        plan, mode=CleanupExecutionMode.DRY_RUN
    )
    assert changed.receipt.status is ReceiptStatus.DRY_RUN
    changed_result = RecoverableExecutor(before_target=mutate).execute(
        plan,
        mode=CleanupExecutionMode.QUARANTINE,
        recovery_root=tmp_path / "recovery-2",
        expected_plan_digest=executor.plan_digest(plan),
        confirm=True,
    )
    assert changed_result.receipt.status is ReceiptStatus.FAILED


def test_partial_failure_and_receipt_write_failure_are_not_success(tmp_path: Path) -> None:
    root = tmp_path / "account"
    recovery = tmp_path / "recovery"
    root.mkdir()
    plan = _make_plan(root, (r"msg\a\one.dat", r"msg\b\two.dat"))

    def remove_second(_target, index):
        if index == 1:
            (root / "msg" / "b" / "two.dat").unlink()

    result = RecoverableExecutor(before_target=remove_second).execute(
        plan,
        mode=CleanupExecutionMode.QUARANTINE,
        recovery_root=recovery,
        expected_plan_digest=RecoverableExecutor().plan_digest(plan),
        confirm=True,
    )
    assert result.receipt.status is ReceiptStatus.FAILED
    assert any(item.status is ReceiptStatus.FAILED for item in result.receipt.items)

    root2 = tmp_path / "account2"
    root2.mkdir()
    plan2 = _make_plan(root2)
    with pytest.raises(ExecutorError) as sink_error:
        RecoverableExecutor().execute(
            plan2,
            mode=CleanupExecutionMode.QUARANTINE,
            recovery_root=tmp_path / "recovery2",
            expected_plan_digest=RecoverableExecutor().plan_digest(plan2),
            confirm=True,
            receipt_sink=lambda _receipt: (_ for _ in ()).throw(RuntimeError("disk full")),
        )
    assert sink_error.value.code == ExecutorErrorCode.RECEIPT_WRITE_FAILED
