"""Synthetic acceptance tests for the read-only database-copy adapter."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from wechat_cleaner.db_adapter import (
    DatabaseAdapterError,
    DatabaseAdapterErrorCode,
    DatabaseCopyAdapter,
)
from wechat_cleaner.domain.contracts import AccountRef

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures_synthetic"


def _builder():
    path = FIXTURE_DIR / "build_phase3_fixture.py"
    spec = importlib.util.spec_from_file_location("phase3_db_fixture_builder", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load fixture builder")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _account(account_id: str) -> AccountRef:
    return AccountRef(
        account_id=account_id,
        account_root=rf"C:\Synthetic\{account_id}",
        display_name=None,
        source_version="synthetic",
    )


def test_reads_v1_copy_read_only_and_redacted(tmp_path: Path) -> None:
    _builder().build_phase3_fixture(tmp_path, seed=41011)
    database = tmp_path / "databases" / "synthetic-v1.sqlite"
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    envelope = DatabaseCopyAdapter().read_copy(database, _account("wxid_synthetic_alpha_p3"))

    assert envelope.database_schema_version == "synthetic-v1"
    assert envelope.database_copy_sha256 == before
    assert envelope.evidence
    assert {item.account_id for item in envelope.evidence} == {"wxid_synthetic_alpha_p3"}
    assert all(item.source_table == "messages" for item in envelope.evidence)
    payload = json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False)
    assert "body" not in payload.lower()
    assert "secret" not in payload.lower()
    assert str(tmp_path) not in payload
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_reads_v2_joined_session_copy(tmp_path: Path) -> None:
    _builder().build_phase3_fixture(tmp_path, seed=41012)
    database = tmp_path / "databases" / "synthetic-v2.sqlite"

    envelope = DatabaseCopyAdapter().read(database, _account("wxid_synthetic_beta_p3"))

    assert envelope.database_schema_version == "synthetic-v2"
    assert envelope.evidence
    assert all(item.source_table == "media_messages" for item in envelope.evidence)
    assert any(item.contact is not None for item in envelope.evidence)
    assert all(
        item.contact is None or item.contact.display_name is None
        for item in envelope.evidence
    )


def test_rejects_unknown_schema_and_corrupt_copy(tmp_path: Path) -> None:
    _builder().build_phase3_fixture(tmp_path, seed=41013)
    adapter = DatabaseCopyAdapter()
    legacy = tmp_path / "databases" / "synthetic-legacy-rejected.sqlite"
    with pytest.raises(DatabaseAdapterError) as legacy_error:
        adapter.detect_schema(legacy)
    assert legacy_error.value.code == DatabaseAdapterErrorCode.UNSUPPORTED_SCHEMA

    broken = tmp_path / "broken.sqlite"
    broken.write_bytes(b"not sqlite")
    with pytest.raises(DatabaseAdapterError) as broken_error:
        adapter.detect_schema(broken)
    assert broken_error.value.code in {
        DatabaseAdapterErrorCode.CORRUPT_DATABASE,
        DatabaseAdapterErrorCode.READ_FAILED,
    }


def test_reports_conflicting_duplicate_media_evidence(tmp_path: Path) -> None:
    _builder().build_phase3_fixture(tmp_path, seed=41014)
    source = tmp_path / "databases" / "synthetic-v1.sqlite"
    duplicate = tmp_path / "duplicate.sqlite"
    duplicate.write_bytes(source.read_bytes())
    with sqlite3.connect(duplicate) as connection:
        row = connection.execute(
            "SELECT local_id, account_id, contact_id, contact_kind, media_id, "
            "relative_path, media_type, message_time, byte_size, mapping_confidence, "
            "file_status, ownership_status, duplicate_group, source_row_digest "
            "FROM messages WHERE account_id = ? LIMIT 1",
            ("wxid_synthetic_alpha_p3",),
        ).fetchone()
        assert row is not None
        values = list(row)
        values[0] = 9999
        values[2] = "synthetic_conflicting_contact"
        values[-1] = "f" * 64
        connection.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        connection.commit()

    envelope = DatabaseCopyAdapter().read_copy(
        duplicate, _account("wxid_synthetic_alpha_p3")
    )
    assert envelope.conflicts
    assert any(conflict.field_name == "media_id" for conflict in envelope.conflicts)
