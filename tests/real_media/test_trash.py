"""Recycle-bin removal: TOCTOU guard, journalized receipts, shell stub.

The real Shell call is stubbed in unit tests; one integration test performs an
actual Recycle Bin move of a synthetic file (Windows only) to prove the ctypes
wiring end-to-end.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wechat_cleaner.decoder.formats import DecoderFailure
from wechat_cleaner.domain.contracts import (
    ContractErrorCode,
    FileIdentity,
    MappingConfidence,
    MediaRecord,
    MediaType,
)
from wechat_cleaner.real_media import trash
from wechat_cleaner.real_media.trash import recycle_media_source

ACCOUNT = Path(r"C:\synthetic-account")


def _record(path: str, size: int, mtime_ns: int) -> MediaRecord:
    return MediaRecord(
        account_id="wxid_demo",
        file=FileIdentity(
            relative_path=path, byte_size=size, modified_time_ns=mtime_ns
        ),
        media_type=MediaType.IMAGE,
        observed_at=datetime(2026, 9, 1, tzinfo=UTC),
        mapping_confidence=MappingConfidence.UNMAPPED,
        mapping_reason="scan",
    )


def test_recycle_success_appends_receipt(
    account_root, stager, image_factory, monkeypatch, tmp_path
):
    identity = stager("msg/attach/aa/2026-02/Img/beef.dat", image_factory())
    record = _record(identity.relative_path, identity.byte_size, identity.modified_time_ns)
    calls: list[str] = []

    def fake_recycle(path: str) -> None:
        calls.append(path)
        os.unlink(path)  # simulate the shell moving the file away

    monkeypatch.setattr(trash, "send_to_recycle_bin", fake_recycle)
    receipts = tmp_path / "receipts.jsonl"

    receipt = recycle_media_source(str(account_root), record, receipts_path=receipts)

    assert calls == [str(account_root / identity.relative_path)]
    assert receipt.relative_path == identity.relative_path
    journal = [json.loads(line) for line in receipts.read_text(encoding="utf-8").splitlines()]
    assert journal == [receipt.to_journal()]


def test_recycle_refuses_when_identity_drifted(
    account_root, stager, image_factory, monkeypatch, tmp_path
):
    identity = stager("msg/attach/aa/2026-02/Img/cafe.dat", image_factory())
    drifted = _record(
        identity.relative_path, identity.byte_size, identity.modified_time_ns + 1
    )
    monkeypatch.setattr(trash, "send_to_recycle_bin", lambda p: pytest.fail("must not run"))
    receipts = tmp_path / "receipts.jsonl"

    with pytest.raises(DecoderFailure) as exc_info:
        recycle_media_source(str(account_root), drifted, receipts_path=receipts)

    assert exc_info.value.code is ContractErrorCode.FILE_IDENTITY_MISMATCH
    assert not receipts.exists()


def test_recycle_refuses_when_source_missing(account_root, tmp_path):
    ghost = _record("msg/attach/aa/2026-02/Img/ghost.dat", 10, 1)
    receipts = tmp_path / "receipts.jsonl"

    with pytest.raises(DecoderFailure) as exc_info:
        recycle_media_source(str(account_root), ghost, receipts_path=receipts)

    assert exc_info.value.code is ContractErrorCode.INVALID_PATH


@pytest.mark.skipif(os.name != "nt", reason="Recycle Bin is Windows-only")
def test_real_shell_recycle_moves_synthetic_file(tmp_path):
    target = tmp_path / "wechat-space-manager-selftest.tmp"
    target.write_bytes(b"selftest")
    trash.send_to_recycle_bin(str(target))
    assert not target.exists()


import os  # noqa: E402
