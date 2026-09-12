"""Synthetic end-to-end acceptance tests for the application workflow facade."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wechat_cleaner.application import (
    ApplicationFacade,
    CancellationToken,
    WorkflowCancelled,
    WorkflowError,
)
from wechat_cleaner.domain.contracts import (
    AccountRef,
    CleanupDisposition,
    FilterSpec,
    MappingConfidence,
    MediaType,
    SelectionScope,
)
from wechat_cleaner.executor import RecoverableExecutor

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures_synthetic"


def _fixture_builder():
    path = FIXTURE_DIR / "build_phase3_fixture.py"
    spec = importlib.util.spec_from_file_location("workflow_fixture_builder", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load workflow fixture builder")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    _fixture_builder().build_phase3_fixture(tmp_path / "fixture", seed=20260909)
    return tmp_path / "fixture", tmp_path / "fixture" / "accounts"


def _map_all_accounts(facade: ApplicationFacade, fixture_root: Path) -> None:
    facade.scan(fixture_root / "accounts")
    facade.import_database_copy(
        fixture_root / "databases" / "synthetic-v1.sqlite",
        account_id="wxid_synthetic_alpha_p3",
    )
    facade.import_database_copy(
        fixture_root / "databases" / "synthetic-v2.sqlite",
        account_id="wxid_synthetic_beta_p3",
    )


def test_application_workflow_runs_scan_query_plan_dry_run_execute_and_restore(
    tmp_path: Path,
) -> None:
    fixture_root, accounts_root = _prepare(tmp_path)
    with ApplicationFacade(
        tmp_path / "index.sqlite",
        recovery_root=tmp_path / "recovery",
    ) as facade:
        _map_all_accounts(facade, fixture_root)
        result = facade.query(
            FilterSpec(
                account_ids=("wxid_synthetic_alpha_p3",),
                mapping_confidences=(
                    MappingConfidence.EXACT,
                    MappingConfidence.HIGH,
                    MappingConfidence.MEDIUM,
                ),
                media_types=(MediaType.IMAGE, MediaType.THUMBNAIL),
                include_unmapped=False,
                page_size=20,
            )
        )
        assert result.summary.eligible_count >= 2

        selection = facade.select(result, SelectionScope.CURRENT_RESULT)
        built = facade.build_plan(selection, disposition=CleanupDisposition.QUARANTINE)
        assert built.plan is not None
        assert not built.rejected
        plan = built.plan
        assert plan.account.account_root == str(accounts_root / "wxid_synthetic_alpha_p3").replace(
            "/", "\\"
        )
        assert facade.preflight(plan).can_execute
        dry_run = facade.dry_run(plan)
        assert dry_run.status.value == "dry_run"

        confirmed = facade.confirm(plan)
        assert confirmed.accepted
        assert confirmed.receipt is not None
        assert confirmed.receipt.status.value == "succeeded"
        assert all(
            not (
                accounts_root / "wxid_synthetic_alpha_p3" / target.file.relative_path
            ).exists()
            for target in plan.targets
        )

        restored = facade.recovery(confirmed.receipt)
        assert restored.available
        assert restored.restore is not None
        assert restored.restore.restored_count == len(plan.targets)
        assert all(
            (
                accounts_root / "wxid_synthetic_alpha_p3" / target.file.relative_path
            ).exists()
            for target in plan.targets
        )


def test_application_workflow_cancellation_does_not_commit_index(tmp_path: Path) -> None:
    fixture_root, _ = _prepare(tmp_path)
    token = CancellationToken()

    def cancel_on_first_progress(_event: object) -> None:
        token.cancel()

    with ApplicationFacade(tmp_path / "index.sqlite") as facade:
        with pytest.raises(WorkflowCancelled) as error:
            facade.scan(
                fixture_root / "accounts",
                cancel=token,
                progress=cancel_on_first_progress,
            )
        assert error.value.code == "CANCELLED"
        assert facade.load_records() == ()
        assert facade.query_index_page().total_count == 0


def test_application_workflow_rejects_expired_selection_and_preflight_drift(
    tmp_path: Path,
) -> None:
    fixture_root, accounts_root = _prepare(tmp_path)
    with ApplicationFacade(tmp_path / "index.sqlite") as facade:
        _map_all_accounts(facade, fixture_root)
        result = facade.query(
            FilterSpec(
                account_ids=("wxid_synthetic_alpha_p3",),
                mapping_confidences=(MappingConfidence.EXACT,),
                media_types=(MediaType.IMAGE, MediaType.THUMBNAIL),
                page_size=20,
            )
        )
        selection = facade.select(result, SelectionScope.CURRENT_RESULT)
        expired = selection.model_copy(
            update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
        )
        with pytest.raises(WorkflowError) as stale:
            facade.build_plan(expired)
        assert stale.value.code == "SELECTION_STALE"

        built = facade.build_plan(selection)
        assert built.plan is not None
        spoofed = built.plan.model_copy(
            update={
                "account": AccountRef(
                    account_id="wxid_synthetic_alpha_p3",
                    account_root=r"C:\Synthetic\unregistered-account",
                )
            }
        )
        with pytest.raises(WorkflowError) as unregistered:
            facade.preflight(spoofed)
        assert unregistered.value.code == "ACCOUNT_MISMATCH"

        target = built.plan.targets[0]
        target_path = accounts_root / "wxid_synthetic_alpha_p3" / target.file.relative_path
        target_path.write_bytes(target_path.read_bytes() + b"drift")
        report = facade.preflight(built.plan)
        assert not report.can_execute


def test_application_workflow_rejects_unsupported_database_copy(tmp_path: Path) -> None:
    fixture_root, _ = _prepare(tmp_path)
    with ApplicationFacade(tmp_path / "index.sqlite") as facade:
        facade.scan(fixture_root / "accounts")
        with pytest.raises(WorkflowError) as error:
            facade.import_database_copy(
                fixture_root / "databases" / "synthetic-legacy-rejected.sqlite",
                account_id="wxid_synthetic_alpha_p3",
            )
        assert error.value.code == "UNSUPPORTED_SCHEMA"


def test_application_workflow_surfaces_partial_execution_and_keeps_receipt(
    tmp_path: Path,
) -> None:
    fixture_root, accounts_root = _prepare(tmp_path)
    account_root = accounts_root / "wxid_synthetic_alpha_p3"
    holder: dict[str, tuple[Path, ...]] = {}

    def fail_second_target(_target: object, index: int) -> None:
        if index == 1:
            holder["targets"][1].unlink()

    executor = RecoverableExecutor(before_target=fail_second_target)
    with ApplicationFacade(
        tmp_path / "index.sqlite",
        recovery_root=tmp_path / "recovery",
        executor=executor,
    ) as facade:
        _map_all_accounts(facade, fixture_root)
        result = facade.query(
            FilterSpec(
                account_ids=("wxid_synthetic_alpha_p3",),
                media_types=(MediaType.IMAGE, MediaType.THUMBNAIL),
                mapping_confidences=(MappingConfidence.EXACT,),
                page_size=20,
            )
        )
        selection = facade.select(result, SelectionScope.CURRENT_RESULT)
        built = facade.build_plan(selection)
        assert built.plan is not None
        holder["targets"] = tuple(
            account_root / target.file.relative_path for target in built.plan.targets
        )

        confirmed = facade.confirm(built.plan)
        assert not confirmed.accepted
        assert confirmed.receipt is not None
        assert confirmed.receipt.status.value == "failed"
        assert confirmed.receipt.items[0].recovery is not None


def test_application_workflow_reopens_durable_index_for_queries(tmp_path: Path) -> None:
    fixture_root, _ = _prepare(tmp_path)
    index_path = tmp_path / "index.sqlite"
    with ApplicationFacade(index_path) as facade:
        _map_all_accounts(facade, fixture_root)
        expected = len(facade.load_records())
        assert expected > 0

    with ApplicationFacade(index_path) as reopened:
        assert len(reopened.load_records()) == expected
        assert (
            reopened.query(
                FilterSpec(
                    include_protected=True,
                    include_conflicts=True,
                    include_unmapped=True,
                    page_size=20,
                )
            ).summary.total_count
            == expected
        )
