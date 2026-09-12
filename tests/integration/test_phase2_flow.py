"""Cross-module contract flow over the public synthetic fixture only."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wechat_cleaner.domain.contracts import (
    AccountRef,
    CleanupDisposition,
    CleanupPlan,
    CleanupPlanSummary,
    CleanupTarget,
    ContactKind,
    ContactRef,
    DecodeRequest,
    DecodeVariant,
    FileIdentity,
    MappingConfidence,
    MediaType,
)
from wechat_cleaner.mapper import MediaCandidate, MessageEvidence, map_candidates
from wechat_cleaner.scanner import scan_account
from wechat_cleaner.ui.filters import by_confidence, summarize

NOW = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)


def _fixture_builder():
    fixture_dir = Path(__file__).parents[1] / "fixtures_synthetic"
    specification = importlib.util.spec_from_file_location(
        "integration_fixture_builder", fixture_dir / "build_fixture.py"
    )
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _mapped_record(tmp_path: Path):
    builder = _fixture_builder()
    root = tmp_path / "xwechat_files"
    builder.build_fixture(root)
    scan_result = scan_account(root / "wxid_synthetic_alpha_d729")
    scanned = next(
        item
        for item in scan_result.files
        if item.media_type is MediaType.IMAGE and not item.is_protected
    )
    candidate = MediaCandidate(
        account_id=scan_result.account.account_id,
        file=FileIdentity(
            relative_path=scanned.relative_path,
            byte_size=scanned.byte_size,
            modified_time_ns=scanned.modified_time_ns,
        ),
        media_type=scanned.media_type,
        observed_at=NOW,
        is_regenerable_cache=scanned.is_regenerable_cache,
    )
    contact = ContactRef(
        account_id=scan_result.account.account_id,
        contact_id="wxid_synthetic_contact",
        kind=ContactKind.DIRECT,
        session_hash="b" * 32,
    )
    evidence = MessageEvidence(
        account_id=scan_result.account.account_id,
        message_local_id=42,
        contact=contact,
        observed_at=NOW,
        media_paths=(scanned.relative_path,),
    )
    result = map_candidates(scan_result.account, [candidate], [evidence])
    assert not result.errors
    assert len(result.records) == 1
    return scan_result.account, result.records[0]


def test_scanner_mapper_ui_and_cleanup_contracts_compose(tmp_path: Path) -> None:
    account, record = _mapped_record(tmp_path)

    assert record.mapping_confidence is MappingConfidence.EXACT
    selected = by_confidence([record], (MappingConfidence.EXACT,))
    summary = summarize(selected)
    assert summary.total_count == 1
    assert summary.total_bytes == record.file.byte_size
    assert summary.by_media_type == ((MediaType.IMAGE.value, 1, record.file.byte_size),)

    request = DecodeRequest(
        media=record,
        variant=DecodeVariant.THUMBNAIL,
        cache_root=str(tmp_path / "decode-cache"),
        max_edge_px=128,
    )
    assert request.media.media_id == record.media_id

    target = CleanupTarget(
        media_id=record.media_id,
        account_id=account.account_id,
        file=record.file,
        media_type=record.media_type,
        mapping_confidence=record.mapping_confidence,
        reason="synthetic integration selection",
    )
    plan = CleanupPlan(
        account=account,
        disposition=CleanupDisposition.RECYCLE_BIN,
        targets=(target,),
        summary=CleanupPlanSummary(
            target_count=1,
            expected_reclaim_bytes=record.file.byte_size,
            by_media_type=((record.media_type, record.file.byte_size),),
        ),
    )
    assert plan.summary.expected_reclaim_bytes == record.file.byte_size


def test_unmapped_output_does_not_cross_cleanup_boundary() -> None:
    account_ref = AccountRef(account_id="wxid_integration", account_root=r"D:\synthetic\account")
    candidate = MediaCandidate(
        account_id=account_ref.account_id,
        file=FileIdentity(
            relative_path=r"msg\attach\unknown.dat", byte_size=10, modified_time_ns=1
        ),
        media_type=MediaType.IMAGE,
        observed_at=NOW,
    )
    result = map_candidates(account_ref, [candidate], [])
    assert result.records[0].mapping_confidence is MappingConfidence.UNMAPPED
    with pytest.raises(ValueError):
        CleanupTarget(
            media_id=result.records[0].media_id,
            account_id=account_ref.account_id,
            file=result.records[0].file,
            media_type=result.records[0].media_type,
            mapping_confidence=result.records[0].mapping_confidence,
            reason="must be rejected",
        )
