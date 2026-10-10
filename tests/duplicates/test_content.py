"""Unit tests for the streaming content hash and group verdicts (D2).

All fixtures are synthetic files in ``tmp_path``; no real WeChat data.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wechat_cleaner.domain.contracts import (
    FileIdentity,
    MappingConfidence,
    MediaRecord,
    MediaType,
)
from wechat_cleaner.duplicates import (
    DuplicateError,
    file_md5,
    same_name_groups,
    verify_group,
)


def _record(relative_path: str, size: int, mtime_ns: int) -> MediaRecord:
    return MediaRecord(
        account_id="wxid_demo_alpha",
        file=FileIdentity(
            relative_path=relative_path, byte_size=size, modified_time_ns=mtime_ns
        ),
        media_type=MediaType.FILE,
        observed_at=datetime(2026, 10, 1, tzinfo=UTC),
        mapping_confidence=MappingConfidence.UNMAPPED,
        mapping_reason="filesystem scan only",
    )


def _materialize(root: Path, relative_path: str, payload: bytes) -> MediaRecord:
    target = root / relative_path.replace("\\", "/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    info = target.stat()
    return _record(relative_path, info.st_size, info.st_mtime_ns)


def test_file_md5_matches_reference_digest(tmp_path: Path) -> None:
    payload = b"\x89PNG synthetic payload" * 10_000
    record = _materialize(tmp_path, r"msg\file\2026-05\a.bin", payload)

    digest = file_md5(record, tmp_path / "msg/file/2026-05/a.bin")

    assert digest == hashlib.md5(payload, usedforsecurity=False).hexdigest()


def test_file_md5_reports_progress_in_cumulative_bytes(tmp_path: Path) -> None:
    payload = b"x" * (2 * 1024 * 1024 + 11)  # forces several 1 MiB chunks
    record = _materialize(tmp_path, r"a\big.bin", payload)
    seen: list[int] = []

    file_md5(record, tmp_path / "a/big.bin", progress=seen.append, chunk_bytes=1024 * 1024)

    assert seen == sorted(seen)
    assert seen[-1] == len(payload)


def test_file_md5_rejects_drifted_identity(tmp_path: Path) -> None:
    record = _materialize(tmp_path, r"a\same.bin", b"original payload")
    target = tmp_path / "a/same.bin"
    target.write_bytes(b"tampered!")  # size now differs from the record

    with pytest.raises(DuplicateError) as excinfo:
        file_md5(record, target)
    assert excinfo.value.code == "IDENTITY_MISMATCH"


def test_file_md5_rejects_missing_source(tmp_path: Path) -> None:
    record = _record(r"a\gone.bin", 10, 1)

    with pytest.raises(DuplicateError) as excinfo:
        file_md5(record, tmp_path / "a/gone.bin")
    assert excinfo.value.code == "SOURCE_MISSING"


def test_file_md5_honours_cancellation(tmp_path: Path) -> None:
    record = _materialize(tmp_path, r"a\big.bin", b"y" * (3 * 1024 * 1024))

    with pytest.raises(DuplicateError) as excinfo:
        file_md5(record, tmp_path / "a/big.bin", cancel=lambda: True)
    assert excinfo.value.code == "CANCELLED"


def test_verify_group_flags_identical_content(tmp_path: Path) -> None:
    payload = b"the very same document" * 100
    records = (
        _materialize(tmp_path, r"msg\file\2026-05\memo.pdf", payload),
        _materialize(tmp_path, r"msg\file\2026-06\memo (1).pdf", payload),
        _materialize(tmp_path, r"msg\file\2026-06\memo (2).pdf", payload),
    )
    group = same_name_groups(records).groups[0]
    assert group.best_level == "S2"  # candidate gate, not yet verified

    def resolve(record):
        return tmp_path / record.file.relative_path.replace("\\", "/")

    verdict = verify_group(group, resolve)

    assert verdict.content_identical is True
    assert len(verdict.md5_by_relative_path) == 3
    assert len(set(verdict.md5_by_relative_path.values())) == 1
    assert verdict.skipped == ()


def test_verify_group_keeps_s2_when_one_member_is_unreadable(tmp_path: Path) -> None:
    payload = b"same bytes here"
    records = (
        _materialize(tmp_path, r"a\doc.bin", payload),
        _materialize(tmp_path, r"b\doc (1).bin", payload),
    )
    (tmp_path / "b/doc (1).bin").unlink()  # vanished after the scan
    group = same_name_groups(records).groups[0]

    def resolve(record):
        return tmp_path / record.file.relative_path.replace("\\", "/")

    verdict = verify_group(group, resolve)

    assert verdict.content_identical is False
    assert len(verdict.md5_by_relative_path) == 1
    assert verdict.skipped[0][1] == "SOURCE_MISSING"


def test_verify_group_reports_distinct_content(tmp_path: Path) -> None:
    records = (
        _materialize(tmp_path, r"a\same.bin", b"one"),
        _materialize(tmp_path, r"b\same (1).bin", b"two"),
    )
    group = same_name_groups(records).groups[0]

    def resolve(record):
        return tmp_path / record.file.relative_path.replace("\\", "/")

    verdict = verify_group(group, resolve)

    assert verdict.content_identical is False
    assert len(verdict.md5_by_relative_path) == 2
    assert len(set(verdict.md5_by_relative_path.values())) == 2
