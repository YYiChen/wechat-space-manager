from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from wechat_cleaner.domain.contracts import (
    PUBLIC_CONTRACTS,
    AccountRef,
    CleanupDisposition,
    CleanupExecution,
    CleanupExecutionMode,
    CleanupPlan,
    CleanupPlanSummary,
    CleanupReceipt,
    CleanupReceiptItem,
    CleanupTarget,
    CompatibilityMode,
    CompatibilityPolicy,
    ContactKind,
    ContactRef,
    DatabaseEvidenceEnvelope,
    DecodeRequest,
    DecodeVariant,
    EvidenceKind,
    FileIdentity,
    FilterResultPage,
    FilterResultSummary,
    FilterSpec,
    MappingConfidence,
    MediaRecord,
    MediaRecordLineage,
    MediaType,
    ReceiptStatus,
    RecoveryLocator,
    RecoveryLocatorKind,
    RecoveryState,
    ScanLineage,
    ScanManifest,
    SelectionScope,
    SelectionSnapshot,
)

ACCOUNT = AccountRef(account_id="wxid_demo", account_root=r"D:\xwechat_files\wxid_demo")
NOW = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)
FILE = FileIdentity(relative_path=r"msg\attach\aabb\1", byte_size=4096, modified_time_ns=123)


def exact_media() -> MediaRecord:
    return MediaRecord(
        account_id=ACCOUNT.account_id,
        file=FILE,
        media_type=MediaType.IMAGE,
        observed_at=NOW,
        contact=ContactRef(
            account_id=ACCOUNT.account_id,
            contact_id="wxid_contact",
            kind=ContactKind.DIRECT,
            session_hash="a" * 32,
        ),
        message_local_id=99,
        mapping_confidence=MappingConfidence.EXACT,
        mapping_reason="database local_id and media identity agree",
    )


class ContractTests(unittest.TestCase):
    def test_synthetic_fixtures_validate(self) -> None:
        fixture_dir = Path(__file__).resolve().parents[1] / "fixtures_synthetic" / "contracts"
        scan_json = (fixture_dir / "scan-manifest.json").read_text(encoding="utf-8")
        media_json = (fixture_dir / "media-record.json").read_text(encoding="utf-8")
        plan_json = (fixture_dir / "cleanup-plan.json").read_text(encoding="utf-8")
        ScanManifest.model_validate_json(scan_json)
        MediaRecord.model_validate_json(media_json)
        CleanupPlan.model_validate_json(plan_json)

    def test_relative_path_rejects_traversal_and_absolutes(self) -> None:
        for unsafe in (r"..\db_storage\MSG.db", r"D:\outside\file", r"msg\\attach\\item"):
            with self.assertRaises(ValidationError):
                FileIdentity(relative_path=unsafe, byte_size=0, modified_time_ns=0)

    def test_exact_mapping_requires_local_message_id(self) -> None:
        with self.assertRaisesRegex(ValidationError, "exact mapping requires message_local_id"):
            MediaRecord(
                account_id=ACCOUNT.account_id,
                file=FILE,
                media_type=MediaType.IMAGE,
                observed_at=NOW,
                mapping_confidence=MappingConfidence.EXACT,
                mapping_reason="test",
            )

    def test_decode_thumbnail_requires_size_and_cache_is_absolute(self) -> None:
        with self.assertRaisesRegex(ValidationError, "thumbnail decoding requires max_edge_px"):
            DecodeRequest(
                media=exact_media(),
                variant=DecodeVariant.THUMBNAIL,
                cache_root=r"D:\cache",
            )
        with self.assertRaisesRegex(ValidationError, "cache_root must be an absolute"):
            DecodeRequest(
                media=exact_media(),
                variant=DecodeVariant.THUMBNAIL,
                cache_root="cache",
                max_edge_px=256,
            )

    def test_cleanup_plan_is_immutable_and_summary_is_exact(self) -> None:
        media = exact_media()
        target = CleanupTarget(
            media_id=media.media_id,
            account_id=media.account_id,
            file=media.file,
            media_type=media.media_type,
            mapping_confidence=media.mapping_confidence,
            reason="user selected a dated image",
        )
        plan = CleanupPlan(
            account=ACCOUNT,
            disposition=CleanupDisposition.RECYCLE_BIN,
            targets=(target,),
            summary=CleanupPlanSummary(
                target_count=1,
                expected_reclaim_bytes=4096,
                by_media_type=((MediaType.IMAGE, 4096),),
            ),
        )
        with self.assertRaises(ValidationError):
            plan.disposition = CleanupDisposition.PERMANENT
        with self.assertRaisesRegex(ValidationError, "summary.expected_reclaim_bytes"):
            CleanupPlan(
                account=ACCOUNT,
                disposition=CleanupDisposition.RECYCLE_BIN,
                targets=(target,),
                summary=CleanupPlanSummary(
                    target_count=1,
                    expected_reclaim_bytes=1,
                    by_media_type=((MediaType.IMAGE, 1),),
                ),
            )

    def test_low_confidence_never_reaches_cleanup_plan(self) -> None:
        with self.assertRaisesRegex(ValidationError, "low-confidence"):
            CleanupTarget(
                media_id=exact_media().media_id,
                account_id=ACCOUNT.account_id,
                file=FILE,
                media_type=MediaType.IMAGE,
                mapping_confidence=MappingConfidence.LOW,
                reason="heuristic only",
            )

    def test_compatibility_mode_stays_read_only_for_legacy(self) -> None:
        with self.assertRaisesRegex(ValidationError, "cannot allow mutation"):
            CompatibilityPolicy(mode=CompatibilityMode.READ_ONLY_LEGACY, allow_mutation=True)

    def test_all_public_contracts_export_schema(self) -> None:
        for name, model in PUBLIC_CONTRACTS.items():
            schema = model.model_json_schema()
            self.assertEqual(schema["type"], "object", name)
            self.assertIn("schema_version", schema["properties"], name)

    def test_checked_in_schema_artifacts_have_stable_ids(self) -> None:
        schema_dir = Path(__file__).resolve().parents[2] / "contracts" / "jsonschema"
        for name in PUBLIC_CONTRACTS:
            artifact = schema_dir / f"{name}.schema.json"
            self.assertTrue(artifact.is_file(), artifact)
            version = str(PUBLIC_CONTRACTS[name].model_fields["schema_version"].default)
            self.assertIn(
                f'"$id": "https://wechat-space-manager.local/schema/{name}/{version}.json"',
                artifact.read_text(encoding="utf-8"),
            )

    def test_phase3_contract_fixtures_round_trip(self) -> None:
        fixture_dir = Path(__file__).resolve().parents[1] / "fixtures_synthetic" / "contracts"
        fixtures = {
            "scan-lineage": ScanLineage,
            "media-record-lineage": MediaRecordLineage,
            "filter-spec": FilterSpec,
            "filter-result-summary": FilterResultSummary,
            "filter-result-page": FilterResultPage,
            "selection-snapshot": SelectionSnapshot,
            "database-evidence-envelope": DatabaseEvidenceEnvelope,
            "cleanup-execution": CleanupExecution,
        }
        for name, model in fixtures.items():
            value = model.model_validate_json((fixture_dir / f"{name}.json").read_text())
            self.assertEqual(model.model_validate_json(value.model_dump_json()), value, name)

    def test_phase3_media_lineage_rejects_mismatch(self) -> None:
        scan = ScanLineage(
            scan_id=UUID("11111111-1111-4111-8111-111111111111"),
            account=ACCOUNT,
            scanned_at=NOW,
            scanner_version="2.0.0",
            manifest_sha256="b" * 64,
        )
        media = MediaRecord(
            account_id=ACCOUNT.account_id,
            file=FILE,
            media_type=MediaType.IMAGE,
            observed_at=NOW,
            mapping_confidence=MappingConfidence.MEDIUM,
            mapping_reason="synthetic lineage",
        )
        with self.assertRaisesRegex(ValidationError, "record.account_id"):
            MediaRecordLineage(
                record=media.model_copy(update={"account_id": "wxid_other"}),
                scan=scan,
                record_key="record-1",
            )

    def test_phase3_filter_and_selection_boundaries(self) -> None:
        with self.assertRaisesRegex(ValidationError, "min_bytes"):
            FilterSpec(min_bytes=20, max_bytes=10)
        with self.assertRaisesRegex(ValidationError, "datetime"):
            FilterSpec(message_time_from=datetime(2026, 9, 9))
        with self.assertRaisesRegex(ValidationError, "expires_at"):
            SelectionSnapshot(
                scope=SelectionScope.MANUAL,
                account_ids=(ACCOUNT.account_id,),
                scan_id=UUID("11111111-1111-4111-8111-111111111111"),
                scan_manifest_sha256="b" * 64,
                filter_digest="c" * 64,
                selected_media_ids=(UUID("22222222-2222-4222-8222-222222222222"),),
                selected_record_keys=("record-1",),
                selected_count=1,
                selected_bytes=10,
                selection_digest="d" * 64,
                created_at=NOW,
                expires_at=NOW,
            )

    def test_phase3_database_evidence_is_redacted_and_scoped(self) -> None:
        with self.assertRaisesRegex(ValidationError, "relative_path"):
            DatabaseEvidenceEnvelope(
                account=ACCOUNT,
                database_copy_id="copy-1",
                database_copy_sha256="a" * 64,
                database_schema_version="synthetic-1",
                adapter_version="0.1",
                evidence=(
                    {
                        "account_id": ACCOUNT.account_id,
                        "evidence_kind": EvidenceKind.MEDIA_PATH,
                        "source_table": "message",
                        "source_row_digest": "a" * 64,
                        "relative_path": r"D:\\outside\\file",
                    },
                ),
            )

    def test_phase3_cleanup_execution_separates_processed_and_reclaimed_bytes(self) -> None:
        locator = RecoveryLocator(
            recovery_id="recovery-1",
            kind=RecoveryLocatorKind.QUARANTINE,
            state=RecoveryState.AVAILABLE,
            relative_path=r"quarantine\recovery-1",
        )
        execution = CleanupExecution(
            plan_id=UUID("33333333-3333-4333-8333-333333333333"),
            mode=CleanupExecutionMode.QUARANTINE,
            status=ReceiptStatus.SUCCEEDED,
            processed_bytes=100,
            reclaimed_bytes=0,
            recovery=(locator,),
        )
        self.assertEqual(execution.reclaimed_bytes, 0)
        with self.assertRaisesRegex(ValidationError, "reclaimed_bytes"):
            CleanupExecution(
                plan_id=execution.plan_id,
                mode=CleanupExecutionMode.QUARANTINE,
                status=ReceiptStatus.SUCCEEDED,
                processed_bytes=1,
                reclaimed_bytes=2,
            )

    def test_phase3_cleanup_plan_binds_lineage_and_expiry(self) -> None:
        media = exact_media()
        target = CleanupTarget(
            media_id=media.media_id,
            account_id=media.account_id,
            file=media.file,
            media_type=media.media_type,
            mapping_confidence=media.mapping_confidence,
            reason="synthetic selected target",
        )
        plan = CleanupPlan(
            account=ACCOUNT,
            disposition=CleanupDisposition.QUARANTINE,
            targets=(target,),
            created_at=NOW,
            summary=CleanupPlanSummary(
                target_count=1,
                expected_reclaim_bytes=FILE.byte_size,
                by_media_type=((MediaType.IMAGE, FILE.byte_size),),
            ),
            scan_id=UUID("11111111-1111-4111-8111-111111111111"),
            scan_manifest_sha256="b" * 64,
            filter_digest="c" * 64,
            selection_digest="d" * 64,
            expires_at=NOW + timedelta(hours=1),
        )
        self.assertEqual(plan.selection_digest, "d" * 64)
        with self.assertRaisesRegex(ValidationError, "expires_at"):
            CleanupPlan(
                account=ACCOUNT,
                disposition=CleanupDisposition.QUARANTINE,
                targets=(target,),
                created_at=NOW,
                summary=plan.summary,
                expires_at=NOW,
            )

    def test_phase3_receipt_records_processed_bytes_and_recovery(self) -> None:
        locator = RecoveryLocator(
            recovery_id="recovery-2",
            kind=RecoveryLocatorKind.QUARANTINE,
            state=RecoveryState.AVAILABLE,
            relative_path=r"quarantine\recovery-2",
        )
        target_id = UUID("99999999-9999-4999-8999-999999999999")
        receipt = CleanupReceipt(
            plan_id=UUID("33333333-3333-4333-8333-333333333333"),
            account=ACCOUNT,
            status=ReceiptStatus.DRY_RUN,
            items=(
                CleanupReceiptItem(
                    target_id=target_id,
                    status=ReceiptStatus.DRY_RUN,
                    processed_bytes=4096,
                    recovery=locator,
                ),
            ),
            total_reclaimed_bytes=0,
            executor_id="synthetic-executor",
            execution_mode=CleanupExecutionMode.DRY_RUN,
            processed_bytes=4096,
            recovery=(locator,),
        )
        self.assertEqual(receipt.processed_bytes, 4096)
        self.assertEqual(receipt.total_reclaimed_bytes, 0)
