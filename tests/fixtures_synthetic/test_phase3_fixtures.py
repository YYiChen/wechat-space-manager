"""Acceptance checks for the deterministic Phase 3 synthetic fixture."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path

from wechat_cleaner.domain.contracts import MediaRecord

FIXTURE_DIR = Path(__file__).resolve().parent
BUILDER_PATH = FIXTURE_DIR / "build_phase3_fixture.py"


def _builder():
    specification = importlib.util.spec_from_file_location("phase3_fixture_builder", BUILDER_PATH)
    if specification is None or specification.loader is None:
        raise RuntimeError("unable to load phase3 fixture builder")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _read_manifest(root: Path) -> dict:
    return json.loads((root / "phase3-manifest.json").read_text(encoding="utf-8"))


def _read_records(root: Path) -> list[dict]:
    payload = json.loads((root / "media-records.json").read_text(encoding="utf-8"))
    return payload["records"]


def test_same_seed_has_same_content_and_summary(tmp_path: Path) -> None:
    builder = _builder()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_report = builder.build_phase3_fixture(first, seed=41001)
    second_report = builder.build_phase3_fixture(second, seed=41001)

    assert first_report == second_report
    assert _read_manifest(first) == _read_manifest(second)
    assert _read_records(first) == _read_records(second)
    for relative_path in (
        "media-records.json",
        "phase3-manifest.json",
        "database-schema-matrix.json",
    ):
        assert (first / relative_path).read_bytes() == (second / relative_path).read_bytes()
    for filename in (
        "synthetic-v1.sqlite",
        "synthetic-v2.sqlite",
        "synthetic-legacy-rejected.sqlite",
    ):
        assert (first / "databases" / filename).read_bytes() == (
            second / "databases" / filename
        ).read_bytes()


def test_catalog_covers_phase3_dimensions_and_cross_account_ids(tmp_path: Path) -> None:
    builder = _builder()
    root = tmp_path / "fixture"
    builder.build_phase3_fixture(root, seed=41002)
    manifest = _read_manifest(root)
    records = _read_records(root)
    coverage = manifest["coverage"]

    assert len(coverage["accounts"]) == 2
    assert set(coverage["contact_kinds"]) == {"direct", "group"}
    assert {"image", "video", "file", "thumbnail", "voice", "other"} <= set(
        coverage["media_types"]
    )
    assert {"exact", "high", "medium", "low", "unmapped"} <= set(
        coverage["mapping_confidences"]
    )
    assert {"present", "missing", "changed", "duplicate"} <= set(coverage["file_statuses"])
    assert {"mapped", "unmapped", "conflict", "protected"} <= set(
        coverage["ownership_statuses"]
    )
    assert len(coverage["message_years"]) >= 3
    assert len(coverage["message_months"]) >= 4
    assert coverage["size_boundaries"] == {"min": 0, "max": 1_048_576, "one_byte": 1}
    assert coverage["duplicate_groups"] == ["beta-duplicate-group-01"]

    media_ids = [record["media_id"] for record in records]
    record_keys = [record["record_key"] for record in records]
    contact_ids = [
        (record["account_id"], record["contact"]["contact_id"])
        for record in records
        if record["contact"] is not None
    ]
    assert len(media_ids) == len(set(media_ids))
    assert len(record_keys) == len(set(record_keys))
    assert len(contact_ids) >= len(set(contact_ids))
    unique_contact_ids = {contact_id for _, contact_id in contact_ids}
    assert len(unique_contact_ids) == 4
    contact_accounts: dict[str, set[str]] = {}
    for account_id, contact_id in contact_ids:
        contact_accounts.setdefault(contact_id, set()).add(account_id)
    assert all(len(account_ids) == 1 for account_ids in contact_accounts.values())
    assert all("synthetic_" in value for value in unique_contact_ids)
    assert all(
        not re.match(r"^(?:[A-Za-z]:)?[\\/]|^\\\\", record["file"]["relative_path"])
        for record in records
    )


def test_records_validate_and_boundary_samples_are_materialized_as_declared(tmp_path: Path) -> None:
    builder = _builder()
    root = tmp_path / "fixture"
    builder.build_phase3_fixture(root, seed=41003)
    manifest = _read_manifest(root)
    records = _read_records(root)
    by_key = {record["record_key"]: record for record in records}

    for record in records:
        MediaRecord.model_validate(record)
        assert "body" not in json.dumps(record).lower()
        assert "key" not in json.dumps(record).lower() or "record_key" in json.dumps(record).lower()

    assert by_key["alpha-direct-image-zero"]["file"]["byte_size"] == 0
    assert by_key["alpha-direct-voice-one"]["file"]["byte_size"] == 1
    assert by_key["beta-group-video-max"]["file"]["byte_size"] == 1_048_576
    missing_path = (
        root
        / "accounts"
        / "wxid_synthetic_beta_p3"
        / "msg"
        / "attach"
        / "unknown"
        / "2026-09"
        / "missing.dat"
    )
    assert not missing_path.exists()
    assert (
        root
        / "accounts"
        / "wxid_synthetic_beta_p3"
        / "msg"
        / "attach"
        / "wxid_synthetic_beta_friend_01"
        / "2024-06"
        / "changed.dat"
    ).stat().st_size == 1025
    duplicate_paths = [
        root / "accounts" / "wxid_synthetic_beta_p3" / record["file"]["relative_path"]
        for record in records
        if record["record_key"] in {"beta-duplicate-a", "beta-duplicate-b"}
    ]
    assert duplicate_paths[0].read_bytes() == duplicate_paths[1].read_bytes()
    assert manifest["boundary_cases"]


def test_database_matrix_has_two_supported_and_one_rejected_schema(tmp_path: Path) -> None:
    builder = _builder()
    root = tmp_path / "fixture"
    report = builder.build_phase3_fixture(root, seed=41004)
    manifest = _read_manifest(root)
    matrix = manifest["database_schema_matrix"]
    assert sum(entry["supported"] for entry in matrix) == 2
    assert sum(not entry["supported"] for entry in matrix) == 1
    assert report["supported_database_count"] == 2
    assert report["rejected_database_count"] == 1

    for entry in matrix:
        database = root / "databases" / entry["filename"]
        assert database.is_file()
        assert hashlib.sha256(database.read_bytes()).hexdigest() == report["database_sha256"][
            entry["schema_version"]
        ]
        with sqlite3.connect(database) as connection:
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            }
            columns = {
                table: {
                    row[1]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                for table in tables
            }
        schema = next(
            item
            for item in builder.SCHEMA_MATRIX
            if item["schema_version"] == entry["schema_version"]
        )
        expected_tables = set(schema["tables"])
        assert tables == expected_tables
        assert user_version == next(
            item["sqlite_user_version"]
            for item in builder.SCHEMA_MATRIX
            if item["schema_version"] == entry["schema_version"]
        )
        expected_columns = schema["tables"]
        for table, expected in expected_columns.items():
            assert columns[table] == set(expected)
        assert all("body" not in column.lower() for table in columns.values() for column in table)
        assert all("secret" not in column.lower() for table in columns.values() for column in table)
        assert all("key" not in column.lower() for table in columns.values() for column in table)

    rejected = next(entry for entry in matrix if not entry["supported"])
    assert rejected["rejection_reason"]
