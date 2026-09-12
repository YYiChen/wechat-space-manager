from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wechat_cleaner.cleanup import (
    CleanupSafetyError,
    ExecutionOutcome,
    PreflightStatus,
    build_plan,
    build_receipt,
    preflight,
)
from wechat_cleaner.domain.contracts import (
    AccountRef,
    CleanupDisposition,
    ContractError,
    ContractErrorCode,
    MappingConfidence,
    MediaType,
    ReceiptStatus,
)

NOW = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)


def stage_file(root: Path, relative_path: str, payload: bytes):
    path = root.joinpath(*relative_path.split("\\"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    info = path.stat()
    from wechat_cleaner.domain.contracts import FileIdentity

    return FileIdentity(
        relative_path=relative_path,
        byte_size=info.st_size,
        modified_time_ns=info.st_mtime_ns,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def make_record(
    account,
    identity,
    *,
    media_type=MediaType.IMAGE,
    confidence=MappingConfidence.HIGH,
    cache=False,
):
    from wechat_cleaner.domain.contracts import MediaRecord

    return MediaRecord(
        account_id=account.account_id,
        file=identity,
        media_type=media_type,
        observed_at=NOW,
        mapping_confidence=confidence,
        mapping_reason="synthetic fixture mapping",
        is_regenerable_cache=cache,
        message_local_id=1 if confidence is MappingConfidence.EXACT else None,
    )


def test_build_plan_is_deterministic_and_exact(account, account_root: Path) -> None:
    first = make_record(account, stage_file(account_root, r"msg\image\b.bin", b"1234"))
    second = make_record(
        account,
        stage_file(account_root, r"msg\image\a.bin", b"12"),
        media_type=MediaType.VIDEO,
    )

    result = build_plan(account, [first, second])

    assert result.rejected == ()
    assert result.plan is not None
    assert [target.file.relative_path for target in result.plan.targets] == [
        r"msg\image\a.bin",
        r"msg\image\b.bin",
    ]
    assert result.plan.summary.target_count == 2
    assert result.plan.summary.expected_reclaim_bytes == 6
    assert dict(result.plan.summary.by_media_type) == {
        MediaType.IMAGE: 4,
        MediaType.VIDEO: 2,
    }
    with pytest.raises(ValueError):
        result.plan.targets = ()  # type: ignore[misc]


def test_build_plan_rejects_unsafe_records_and_all_rejected_has_no_plan(
    account, account_root: Path
) -> None:
    records = [
        make_record(
            account,
            stage_file(account_root, r"msg\image\low.bin", b"x"),
            confidence=MappingConfidence.LOW,
        ),
        make_record(
            account,
            stage_file(account_root, r"db_storage\message.db", b"x"),
        ),
        make_record(
            account,
            stage_file(account_root, r"cache\preview.bin", b"x"),
            cache=True,
        ),
        make_record(
            account,
            stage_file(account_root, r"misc\unknown.bin", b"x"),
            media_type=MediaType.OTHER,
        ),
    ]

    result = build_plan(account, records)

    assert result.plan is None
    assert [error.code for error in result.rejected] == [
        ContractErrorCode.MAPPING_UNAVAILABLE,
        ContractErrorCode.EXECUTION_BLOCKED,
        ContractErrorCode.EXECUTION_BLOCKED,
        ContractErrorCode.EXECUTION_BLOCKED,
    ]
    assert all(
        "relative_path" in dict(error.context) and ":\\" not in dict(error.context)["relative_path"]
        for error in result.rejected
    )


def test_build_plan_requires_explicit_cache_and_permanent_ack(account, account_root: Path) -> None:
    cache = make_record(
        account,
        stage_file(account_root, r"cache\preview.bin", b"cache"),
        cache=True,
    )
    excluded = build_plan(account, [cache])
    assert excluded.plan is None
    assert excluded.rejected[0].code is ContractErrorCode.EXECUTION_BLOCKED

    included = build_plan(account, [cache], include_regenerable_cache=True)
    assert included.plan is not None
    assert included.plan.summary.includes_regenerable_cache

    permanent = build_plan(
        account,
        [make_record(account, stage_file(account_root, r"msg\image\one.bin", b"x"))],
        disposition=CleanupDisposition.PERMANENT,
    )
    assert permanent.plan is None
    assert permanent.rejected[0].code is ContractErrorCode.PLAN_INVALID


def test_build_plan_rejects_account_mismatch_and_duplicates(account, account_root: Path) -> None:
    identity = stage_file(account_root, r"msg\image\same.bin", b"x")
    first = make_record(account, identity)
    duplicate = make_record(account, identity)
    foreign = make_record(
        AccountRef(account_id="wxid_foreign", account_root=str(account_root)),
        identity,
    )

    result = build_plan(account, [first, duplicate, foreign])

    assert result.plan is not None
    assert len(result.plan.targets) == 1
    assert [error.code for error in result.rejected] == [
        ContractErrorCode.PLAN_INVALID,
        ContractErrorCode.ACCOUNT_MISMATCH,
    ]


def test_preflight_matches_identity_without_mutation(account, account_root: Path) -> None:
    identity = stage_file(account_root, r"msg\image\one.bin", b"payload")
    result = build_plan(account, [make_record(account, identity)])
    assert result.plan is not None
    before = (account_root / "msg" / "image" / "one.bin").read_bytes()

    report = preflight(result.plan, account_root)

    assert report.can_execute
    assert report.checks[0].status is PreflightStatus.READY
    assert report.checks[0].current == identity
    assert (account_root / "msg" / "image" / "one.bin").read_bytes() == before


@pytest.mark.parametrize("mutation", ["size", "mtime", "hash"])
def test_preflight_marks_changed_identity_stale(account, account_root: Path, mutation: str) -> None:
    identity = stage_file(account_root, r"msg\image\one.bin", b"payload")
    result = build_plan(account, [make_record(account, identity)])
    assert result.plan is not None
    target = account_root / "msg" / "image" / "one.bin"
    if mutation == "size":
        target.write_bytes(b"changed-size")
    elif mutation == "mtime":
        info = target.stat()
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 10_000_000))
    else:
        target.write_bytes(b"payload!")

    report = preflight(result.plan, account_root)

    assert not report.can_execute
    assert report.checks[0].status is PreflightStatus.STALE
    assert report.checks[0].error is not None
    assert report.checks[0].error.code is ContractErrorCode.FILE_IDENTITY_MISMATCH


def test_preflight_blocks_running_wechat_before_file_access(account, account_root: Path) -> None:
    identity = stage_file(account_root, r"msg\image\one.bin", b"payload")
    result = build_plan(account, [make_record(account, identity)])
    assert result.plan is not None

    report = preflight(result.plan, account_root / "missing", wechat_running=True)

    assert not report.can_execute
    assert report.checks[0].status is PreflightStatus.BLOCKED
    assert report.checks[0].error is not None
    assert report.checks[0].error.code is ContractErrorCode.WECHAT_RUNNING


def test_preflight_rejects_invalid_or_mismatched_root(account, account_root: Path) -> None:
    identity = stage_file(account_root, r"msg\image\one.bin", b"payload")
    result = build_plan(account, [make_record(account, identity)])
    assert result.plan is not None

    with pytest.raises(CleanupSafetyError) as mismatch:
        preflight(result.plan, account_root.parent)
    assert mismatch.value.code is ContractErrorCode.ACCOUNT_MISMATCH

    with pytest.raises(CleanupSafetyError) as missing:
        preflight(result.plan, account_root / "missing")
    assert missing.value.code is ContractErrorCode.ACCOUNT_MISMATCH


def test_preflight_rejects_symlink_target_when_supported(account, account_root: Path) -> None:
    outside = account_root.parent / "outside.bin"
    outside.write_bytes(b"outside")
    link = account_root / "msg" / "image" / "link.bin"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    identity = stage_file(account_root, r"msg\image\placeholder.bin", b"x")
    result = build_plan(account, [make_record(account, identity)])
    assert result.plan is not None
    unsafe_target = result.plan.targets[0].model_copy(
        update={"file": identity.model_copy(update={"relative_path": r"msg\image\link.bin"})}
    )
    unsafe_plan = result.plan.model_copy(update={"targets": (unsafe_target,)})

    report = preflight(unsafe_plan, account_root)

    assert report.checks[0].status is PreflightStatus.BLOCKED
    assert report.checks[0].error is not None
    assert report.checks[0].error.code in {
        ContractErrorCode.INVALID_PATH,
        ContractErrorCode.PATH_OUTSIDE_ACCOUNT_ROOT,
        ContractErrorCode.FILE_IDENTITY_MISMATCH,
    }


def test_build_receipt_requires_exact_coverage_and_bounds(account, account_root: Path) -> None:
    first = make_record(account, stage_file(account_root, r"msg\image\a.bin", b"123"))
    second = make_record(account, stage_file(account_root, r"msg\image\b.bin", b"1234"))
    result = build_plan(account, [first, second])
    assert result.plan is not None
    first_id, second_id = [target.target_id for target in result.plan.targets]

    receipt = build_receipt(
        result.plan,
        [
            ExecutionOutcome(first_id, ReceiptStatus.SUCCEEDED, 3),
            ExecutionOutcome(second_id, ReceiptStatus.SKIPPED),
        ],
        executor_id="synthetic-executor",
    )
    assert receipt.status is ReceiptStatus.SKIPPED
    assert receipt.total_reclaimed_bytes == 3
    assert [item.target_id for item in receipt.items] == [first_id, second_id]

    with pytest.raises(CleanupSafetyError) as missing:
        build_receipt(
            result.plan,
            [ExecutionOutcome(first_id, ReceiptStatus.SUCCEEDED, 3)],
            executor_id="synthetic-executor",
        )
    assert missing.value.code is ContractErrorCode.PLAN_INVALID

    with pytest.raises(CleanupSafetyError) as too_large:
        build_receipt(
            result.plan,
            [
                ExecutionOutcome(first_id, ReceiptStatus.SUCCEEDED, 4),
                ExecutionOutcome(second_id, ReceiptStatus.SKIPPED),
            ],
            executor_id="synthetic-executor",
        )
    assert too_large.value.code is ContractErrorCode.FILE_IDENTITY_MISMATCH


def test_build_receipt_failed_item_requires_error_and_no_bytes(account, account_root: Path) -> None:
    identity = stage_file(account_root, r"msg\image\one.bin", b"payload")
    result = build_plan(account, [make_record(account, identity)])
    assert result.plan is not None
    target_id = result.plan.targets[0].target_id

    with pytest.raises(CleanupSafetyError):
        build_receipt(
            result.plan,
            [ExecutionOutcome(target_id, ReceiptStatus.FAILED)],
            executor_id="synthetic-executor",
        )

    error = ContractError(
        code=ContractErrorCode.EXECUTION_BLOCKED,
        message=r"C:\private\chat text should not enter a receipt",
        context=(("absolute_path", r"C:\private\secret"),),
    )
    receipt = build_receipt(
        result.plan,
        [ExecutionOutcome(target_id, ReceiptStatus.FAILED, error=error)],
        executor_id="synthetic-executor",
    )
    assert receipt.status is ReceiptStatus.FAILED
    assert receipt.items[0].error is not None
    assert receipt.items[0].error.code is error.code
    assert receipt.items[0].error.message != error.message
    assert dict(receipt.items[0].error.context) == {"relative_path": r"msg\image\one.bin"}

    with pytest.raises(CleanupSafetyError):
        build_receipt(
            result.plan,
            [ExecutionOutcome(target_id, ReceiptStatus.FAILED, 1, error=error)],
            executor_id="synthetic-executor",
        )


def test_build_receipt_rejects_unsafe_executor_identifier(account, account_root: Path) -> None:
    identity = stage_file(account_root, r"msg\image\one.bin", b"payload")
    result = build_plan(account, [make_record(account, identity)])
    assert result.plan is not None
    target_id = result.plan.targets[0].target_id

    with pytest.raises(CleanupSafetyError) as error:
        build_receipt(
            result.plan,
            [ExecutionOutcome(target_id, ReceiptStatus.DRY_RUN)],
            executor_id=r"C:\executor",
        )
    assert error.value.code is ContractErrorCode.PLAN_INVALID
