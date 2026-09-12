"""Phase 4 cross-module gate: real-readonly path stays read-only and anonymous.

These are public/synthetic assertions: they prove that the real-data backends
cannot reach the cleanup executor, that unmapped real records can never enter a
cleanup plan, and that the public gate script covers the new modules.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from wechat_cleaner.application import RealReadOnlySession
from wechat_cleaner.domain.contracts import (
    CleanupDisposition,
    CleanupPlan,
    CleanupPlanSummary,
    CleanupTarget,
    MappingConfidence,
    MediaType,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_verify_p4_gate_script_passes_static_checks():
    """The static half of the Phase 4 gate must pass without touching real data."""
    result = subprocess.run(
        [sys.executable, "scripts/verify_p4.py", "--skip-suite", "--require-passed"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["private_data_accessed"] is False
    assert payload["state"] == "passed"
    assert {check["name"] for check in payload["checks"]} >= {
        "imports_without_upstream",
        "privacy_patterns",
        "no_executor_coupling",
    }


def test_real_backends_do_not_import_executor_or_cleanup():
    """A read-only Beta must not be able to delete or move real files."""
    modules = [
        REPO_ROOT / "src" / "wechat_cleaner" / "real_db",
        REPO_ROOT / "src" / "wechat_cleaner" / "real_media",
    ]
    files = [path for module in modules for path in module.rglob("*.py")]
    files.append(REPO_ROOT / "src" / "wechat_cleaner" / "application" / "real_workflow.py")
    files.append(REPO_ROOT / "scripts" / "harness_real_readonly.py")

    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "wechat_cleaner.executor" not in text, path.name
        assert "wechat_cleaner.cleanup" not in text, path.name


def test_unmapped_real_records_can_never_enter_a_cleanup_plan():
    """The contract itself rejects UNMAPPED targets, so the real path is inert."""
    from uuid import uuid4

    from wechat_cleaner.domain.contracts import AccountRef, FileIdentity

    account = AccountRef(account_id="wxid_synthetic_gate", account_root="C:\\synthetic")
    target = CleanupTarget(
        media_id=uuid4(),
        account_id="wxid_synthetic_gate",
        file=FileIdentity(relative_path="msg/attach/x/2026-02/Img/a.dat", byte_size=10,
                          modified_time_ns=1),
        media_type=MediaType.IMAGE,
        mapping_confidence=MappingConfidence.HIGH,
        reason="synthetic gate target",
    )

    with pytest.raises(Exception) as exc_info:
        CleanupPlan(
            account=account,
            disposition=CleanupDisposition.QUARANTINE,
            targets=(
                CleanupTarget(
                    media_id=uuid4(),
                    account_id="wxid_synthetic_gate",
                    file=FileIdentity(relative_path="msg/attach/x/2026-02/Img/b.dat",
                                      byte_size=10, modified_time_ns=1),
                    media_type=MediaType.IMAGE,
                    mapping_confidence=MappingConfidence.UNMAPPED,
                    reason="unmapped must be rejected",
                ),
            ),
            summary=CleanupPlanSummary(
                target_count=1,
                expected_reclaim_bytes=10,
                by_media_type=((MediaType.IMAGE, 10),),
            ),
        )
    assert "low-confidence or unmapped" in str(exc_info.value)

    # The mapped equivalent is accepted, proving the contract is the gate.
    plan = CleanupPlan(
        account=account,
        disposition=CleanupDisposition.QUARANTINE,
        targets=(target,),
        summary=CleanupPlanSummary(
            target_count=1,
            expected_reclaim_bytes=target.file.byte_size,
            by_media_type=((MediaType.IMAGE, target.file.byte_size),),
        ),
    )
    assert plan.summary.target_count == 1


def test_real_session_exposes_no_deletion_entry_points():
    """The session surface is read-only plus preview-cache clearing."""
    surface = {name for name in dir(RealReadOnlySession) if not name.startswith("_")}

    assert "records" in surface
    assert "preview" in surface
    assert "manifest" in surface
    assert "clear_decoded_cache" in surface
    assert not any("delete" in name or "remove" in name or "move" in name for name in surface)


def test_harness_reports_only_anonymous_fields():
    """The harness output contract has no identifying field names."""
    text = (REPO_ROOT / "scripts" / "harness_real_readonly.py").read_text(encoding="utf-8")

    for forbidden in ("wxid", "contact_name", "message_body", "absolute_path"):
        assert forbidden not in text.lower().replace("no account identifier", "")
