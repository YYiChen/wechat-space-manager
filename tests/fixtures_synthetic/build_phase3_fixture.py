"""Build deterministic, non-private Phase 3 filesystem and SQLite fixtures.

The generated tree is deliberately synthetic.  It contains relative paths,
opaque IDs, and sentinel bytes only; it never contains a real account root,
contact name, message body, encryption key, or decrypted media.  The SQLite
files model adapter inputs and are safe to delete after a test run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from wechat_cleaner.domain.contracts import (
    ContactKind,
    ContactRef,
    FileIdentity,
    FileStatus,
    MappingConfidence,
    MediaRecord,
    MediaType,
    OwnershipStatus,
)

DEFAULT_SEED = 20260909
FIXTURE_SCHEMA_VERSION = "1.0"
SCANNER_VERSION = "synthetic-scanner-3.0"
_NAMESPACE = uuid.UUID("2b4a5e9a-f5b1-4f90-9f84-2c3b0e2df301")


@dataclass(frozen=True)
class RecordSpec:
    """Portable metadata for one synthetic candidate.

    ``byte_size`` is the size observed by the scanner.  ``actual_size`` is
    used only for the generated file and intentionally differs for the
    ``changed`` sample.  Missing samples do not create a file.
    """

    record_key: str
    account_id: str
    contact_id: str | None
    contact_kind: ContactKind | None
    media_type: MediaType
    relative_path: str
    message_local_id: int | None
    message_time: str | None
    observed_at: str
    byte_size: int
    actual_size: int | None
    mapping_confidence: MappingConfidence
    file_status: FileStatus
    ownership_status: OwnershipStatus
    mapping_reason: str
    duplicate_group: str | None = None
    original_key: str | None = None
    protected_reason: str | None = None
    conflict_reason: str | None = None
    is_regenerable_cache: bool = False


SCHEMA_MATRIX: tuple[dict[str, Any], ...] = (
    {
        "schema_version": "synthetic-v1",
        "supported": True,
        "filename": "synthetic-v1.sqlite",
        "sqlite_user_version": 301,
        "tables": {
            "messages": [
                "local_id",
                "account_id",
                "contact_id",
                "contact_kind",
                "media_id",
                "relative_path",
                "media_type",
                "message_time",
                "byte_size",
                "mapping_confidence",
                "file_status",
                "ownership_status",
                "duplicate_group",
                "source_row_digest",
            ]
        },
        "required_columns": [
            "account_id",
            "contact_id",
            "relative_path",
            "media_type",
            "message_time",
        ],
        "rejection_reason": None,
    },
    {
        "schema_version": "synthetic-v2",
        "supported": True,
        "filename": "synthetic-v2.sqlite",
        "sqlite_user_version": 302,
        "tables": {
            "media_messages": [
                "local_id",
                "account_id",
                "session_id",
                "media_id",
                "media_relpath",
                "kind",
                "media_type",
                "sent_at",
                "size_bytes",
                "mapping_confidence",
                "file_status",
                "ownership_status",
                "duplicate_group",
                "source_row_digest",
            ],
            "sessions": ["session_id", "account_id", "contact_id", "contact_kind"],
        },
        "required_columns": [
            "account_id",
            "session_id",
            "media_relpath",
            "media_type",
            "sent_at",
        ],
        "rejection_reason": None,
    },
    {
        "schema_version": "synthetic-legacy-rejected",
        "supported": False,
        "filename": "synthetic-legacy-rejected.sqlite",
        "sqlite_user_version": 299,
        "tables": {"legacy_events": ["event_id", "account_id", "path_hint", "opaque_ref"]},
        "required_columns": ["account_id", "relative_path", "media_type", "message_time"],
        "rejection_reason": "required media columns are absent; adapter must refuse this schema",
    },
)


RECORD_SPECS: tuple[RecordSpec, ...] = (
    RecordSpec(
        "alpha-direct-image-zero",
        "wxid_synthetic_alpha_p3",
        "wxid_synthetic_alpha_friend_01",
        ContactKind.DIRECT,
        MediaType.IMAGE,
        r"msg\attach\wxid_synthetic_alpha_friend_01\2024-01\Image\alpha-zero.dat",
        1001,
        "2024-01-15T09:00:00Z",
        "2024-01-15T09:01:00Z",
        0,
        0,
        MappingConfidence.EXACT,
        FileStatus.PRESENT,
        OwnershipStatus.MAPPED,
        "synthetic-v1 local_id and relative path agree",
    ),
    RecordSpec(
        "alpha-direct-voice-one",
        "wxid_synthetic_alpha_p3",
        "wxid_synthetic_alpha_friend_01",
        ContactKind.DIRECT,
        MediaType.VOICE,
        r"msg\attach\wxid_synthetic_alpha_friend_01\2025-12\Voice\alpha-one.voice",
        1002,
        "2025-12-31T23:59:00Z",
        "2026-01-01T00:00:00Z",
        1,
        1,
        MappingConfidence.HIGH,
        FileStatus.PRESENT,
        OwnershipStatus.MAPPED,
        "synthetic session path and timestamp agree",
    ),
    RecordSpec(
        "alpha-group-original",
        "wxid_synthetic_alpha_p3",
        "group_synthetic_alpha_team",
        ContactKind.GROUP,
        MediaType.IMAGE,
        r"msg\attach\group_synthetic_alpha_team\2026-02\Rec\alpha-rec-0001\0",
        1003,
        "2026-02-20T12:00:00Z",
        "2026-02-20T12:01:00Z",
        4096,
        4096,
        MappingConfidence.EXACT,
        FileStatus.PRESENT,
        OwnershipStatus.MAPPED,
        "synthetic forwarded record identity agrees",
    ),
    RecordSpec(
        "alpha-group-thumbnail",
        "wxid_synthetic_alpha_p3",
        "group_synthetic_alpha_team",
        ContactKind.GROUP,
        MediaType.THUMBNAIL,
        r"msg\attach\group_synthetic_alpha_team\2026-02\Rec\alpha-rec-0001\0_t",
        1003,
        "2026-02-20T12:00:00Z",
        "2026-02-20T12:01:00Z",
        512,
        512,
        MappingConfidence.HIGH,
        FileStatus.PRESENT,
        OwnershipStatus.MAPPED,
        "synthetic thumbnail paired with original",
        original_key="alpha-group-original",
    ),
    RecordSpec(
        "alpha-cache-preview",
        "wxid_synthetic_alpha_p3",
        None,
        None,
        MediaType.OTHER,
        r"cache\alpha-preview.bin",
        None,
        None,
        "2026-02-20T12:02:00Z",
        123,
        123,
        MappingConfidence.UNMAPPED,
        FileStatus.PRESENT,
        OwnershipStatus.UNMAPPED,
        "regenerable cache has no message ownership",
        is_regenerable_cache=True,
    ),
    RecordSpec(
        "alpha-protected-db",
        "wxid_synthetic_alpha_p3",
        None,
        None,
        MediaType.OTHER,
        r"db_storage\alpha-message.db",
        None,
        None,
        "2026-02-20T12:03:00Z",
        149,
        149,
        MappingConfidence.UNMAPPED,
        FileStatus.PRESENT,
        OwnershipStatus.PROTECTED,
        "database storage is outside media cleanup scope",
        protected_reason="db_storage is always protected",
    ),
    RecordSpec(
        "beta-group-video-max",
        "wxid_synthetic_beta_p3",
        "group_synthetic_beta_team",
        ContactKind.GROUP,
        MediaType.VIDEO,
        r"msg\video\group_synthetic_beta_team\2023-03\beta-max.mp4",
        2001,
        "2023-03-01T08:30:00Z",
        "2023-03-01T08:31:00Z",
        1_048_576,
        1_048_576,
        MappingConfidence.HIGH,
        FileStatus.PRESENT,
        OwnershipStatus.MAPPED,
        "synthetic session and media identity agree",
    ),
    RecordSpec(
        "beta-direct-file",
        "wxid_synthetic_beta_p3",
        "wxid_synthetic_beta_friend_01",
        ContactKind.DIRECT,
        MediaType.FILE,
        r"msg\file\wxid_synthetic_beta_friend_01\2025-07\beta-report.pdf",
        2002,
        "2025-07-04T16:00:00Z",
        "2025-07-04T16:01:00Z",
        2048,
        2048,
        MappingConfidence.MEDIUM,
        FileStatus.PRESENT,
        OwnershipStatus.MAPPED,
        "synthetic date, size and session correlation",
    ),
    RecordSpec(
        "beta-duplicate-a",
        "wxid_synthetic_beta_p3",
        "group_synthetic_beta_team",
        ContactKind.GROUP,
        MediaType.FILE,
        r"msg\attach\group_synthetic_beta_team\2026-08\duplicate-a.dat",
        2003,
        "2026-08-08T08:00:00Z",
        "2026-08-08T08:01:00Z",
        64,
        64,
        MappingConfidence.MEDIUM,
        FileStatus.DUPLICATE,
        OwnershipStatus.MAPPED,
        "same synthetic SHA-256 as duplicate-b",
        duplicate_group="beta-duplicate-group-01",
    ),
    RecordSpec(
        "beta-duplicate-b",
        "wxid_synthetic_beta_p3",
        "group_synthetic_beta_team",
        ContactKind.GROUP,
        MediaType.FILE,
        r"msg\attach\group_synthetic_beta_team\2026-08\duplicate-b.dat",
        2004,
        "2026-08-08T08:02:00Z",
        "2026-08-08T08:03:00Z",
        64,
        64,
        MappingConfidence.MEDIUM,
        FileStatus.DUPLICATE,
        OwnershipStatus.MAPPED,
        "same synthetic SHA-256 as duplicate-a",
        duplicate_group="beta-duplicate-group-01",
    ),
    RecordSpec(
        "beta-missing-unmapped",
        "wxid_synthetic_beta_p3",
        None,
        None,
        MediaType.OTHER,
        r"msg\attach\unknown\2026-09\missing.dat",
        None,
        None,
        "2026-09-01T10:00:00Z",
        777,
        None,
        MappingConfidence.UNMAPPED,
        FileStatus.MISSING,
        OwnershipStatus.UNMAPPED,
        "database copy has no credible media ownership",
    ),
    RecordSpec(
        "beta-changed-low",
        "wxid_synthetic_beta_p3",
        "wxid_synthetic_beta_friend_01",
        ContactKind.DIRECT,
        MediaType.IMAGE,
        r"msg\attach\wxid_synthetic_beta_friend_01\2024-06\changed.dat",
        2005,
        "2024-06-06T06:00:00Z",
        "2026-09-01T10:01:00Z",
        1024,
        1025,
        MappingConfidence.LOW,
        FileStatus.CHANGED,
        OwnershipStatus.MAPPED,
        "heuristic association with changed file identity",
    ),
    RecordSpec(
        "beta-conflict",
        "wxid_synthetic_beta_p3",
        "wxid_synthetic_beta_friend_01",
        ContactKind.DIRECT,
        MediaType.IMAGE,
        r"msg\attach\wxid_synthetic_beta_friend_01\2024-06\conflict.dat",
        2006,
        "2024-06-06T06:05:00Z",
        "2026-09-01T10:02:00Z",
        2048,
        2048,
        MappingConfidence.MEDIUM,
        FileStatus.PRESENT,
        OwnershipStatus.CONFLICT,
        "two synthetic database rows disagree on contact",
        conflict_reason="candidate contact digests disagree",
    ),
    RecordSpec(
        "beta-protected-favourite",
        "wxid_synthetic_beta_p3",
        None,
        None,
        MediaType.OTHER,
        r"business\favorite\beta-favorite.dat",
        None,
        None,
        "2026-09-01T10:03:00Z",
        83,
        83,
        MappingConfidence.UNMAPPED,
        FileStatus.PRESENT,
        OwnershipStatus.PROTECTED,
        "favourite data is outside media cleanup scope",
        protected_reason="business/favorite is always protected",
    ),
    RecordSpec(
        "beta-protected-sendtemp",
        "wxid_synthetic_beta_p3",
        None,
        None,
        MediaType.OTHER,
        r"SendTemp\beta-upload.tmp",
        None,
        None,
        "2026-09-01T10:04:00Z",
        32,
        32,
        MappingConfidence.UNMAPPED,
        FileStatus.PRESENT,
        OwnershipStatus.PROTECTED,
        "in-progress send is outside media cleanup scope",
        protected_reason="SendTemp is always protected",
    ),
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _stable_uuid(seed: int, key: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"p3:{seed}:{key}")


def _stable_sha256(seed: int, key: str) -> str:
    return hashlib.sha256(f"p3:{seed}:{key}".encode()).hexdigest()


def _payload(seed: int, key: str, size: int) -> bytes:
    if size == 0:
        return b""
    prefix = f"SYNTHETIC_P3::{seed}::{key}::".encode("ascii")
    return (prefix * ((size // len(prefix)) + 1))[:size]


def _account_scan(seed: int, account_id: str) -> tuple[str, str]:
    scan_id = str(_stable_uuid(seed, f"scan:{account_id}"))
    manifest_sha = _stable_sha256(seed, f"manifest:{account_id}")
    return scan_id, manifest_sha


def _record_payload(spec: RecordSpec, seed: int, ids: dict[str, uuid.UUID]) -> dict[str, Any]:
    scan_id, scan_manifest_sha = _account_scan(seed, spec.account_id)
    contact = None
    if spec.contact_id is not None and spec.contact_kind is not None:
        contact = ContactRef(
            account_id=spec.account_id,
            contact_id=spec.contact_id,
            kind=spec.contact_kind,
            session_hash=_stable_sha256(seed, f"session:{spec.account_id}:{spec.contact_id}")[:32],
        ).model_dump(mode="json")
    modified_time_ns = int(
        datetime.fromisoformat(spec.observed_at.replace("Z", "+00:00")).timestamp()
    )
    file_identity = FileIdentity(
        relative_path=spec.relative_path,
        byte_size=spec.byte_size,
        modified_time_ns=modified_time_ns,
        sha256=_stable_sha256(seed, f"bytes:{spec.duplicate_group or spec.record_key}"),
    )
    record = MediaRecord(
        media_id=ids[spec.record_key],
        account_id=spec.account_id,
        file=file_identity,
        media_type=spec.media_type,
        observed_at=datetime.fromisoformat(spec.observed_at.replace("Z", "+00:00")),
        contact=contact,
        message_local_id=spec.message_local_id,
        mapping_confidence=spec.mapping_confidence,
        mapping_reason=spec.mapping_reason,
        original_media_id=ids[spec.original_key] if spec.original_key else None,
        is_regenerable_cache=spec.is_regenerable_cache,
        record_key=spec.record_key,
        scan_id=uuid.UUID(scan_id),
        scan_manifest_sha256=scan_manifest_sha,
        message_time=(
            datetime.fromisoformat(spec.message_time.replace("Z", "+00:00"))
            if spec.message_time
            else None
        ),
        file_status=spec.file_status,
        ownership_status=spec.ownership_status,
        protection_reasons=(spec.protected_reason,) if spec.protected_reason else (),
        conflict_reasons=(spec.conflict_reason,) if spec.conflict_reason else (),
    )
    return record.model_dump(mode="json")


def _write_sqlite(
    path: Path, schema: dict[str, Any], seed: int, records: list[dict[str, Any]]
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute(f"PRAGMA user_version={int(schema['sqlite_user_version'])}")
        if schema["schema_version"] == "synthetic-v1":
            connection.execute(
                """
                CREATE TABLE messages (
                    local_id INTEGER PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    contact_id TEXT,
                    contact_kind TEXT,
                    media_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    message_time TEXT,
                    byte_size INTEGER NOT NULL,
                    mapping_confidence TEXT NOT NULL,
                    file_status TEXT NOT NULL,
                    ownership_status TEXT NOT NULL,
                    duplicate_group TEXT,
                    source_row_digest TEXT NOT NULL
                )
                """
            )
            sql = (
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            rows = [
                (
                    index,
                    record["account_id"],
                    record["contact"]["contact_id"] if record["contact"] else None,
                    record["contact"]["kind"] if record["contact"] else None,
                    record["media_id"],
                    record["file"]["relative_path"],
                    record["media_type"],
                    record["message_time"],
                    record["file"]["byte_size"],
                    record["mapping_confidence"],
                    record["file_status"],
                    record["ownership_status"],
                    next(
                        spec.duplicate_group
                        for spec in RECORD_SPECS
                        if spec.record_key == record["record_key"]
                    ),
                    _stable_sha256(seed, f"row:{record['record_key']}"),
                )
                for index, record in enumerate(records, start=1)
            ]
            connection.executemany(sql, rows)
        elif schema["schema_version"] == "synthetic-v2":
            connection.executescript(
                """
                CREATE TABLE sessions (
                    session_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    contact_id TEXT,
                    contact_kind TEXT
                );
                CREATE TABLE media_messages (
                    local_id INTEGER PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    media_id TEXT NOT NULL,
                    media_relpath TEXT NOT NULL,
                    kind TEXT,
                    media_type TEXT NOT NULL,
                    sent_at TEXT,
                    size_bytes INTEGER NOT NULL,
                    mapping_confidence TEXT NOT NULL,
                    file_status TEXT NOT NULL,
                    ownership_status TEXT NOT NULL,
                    duplicate_group TEXT,
                    source_row_digest TEXT NOT NULL
                );
                """
            )
            session_rows: dict[str, tuple[str, str, str | None, str | None]] = {}
            rows = []
            for index, record in enumerate(records, start=1):
                contact = record["contact"]
                session_key = record["account_id"] + ":" + (
                    contact["contact_id"] if contact else "unmapped"
                )
                session_id = _stable_sha256(seed, f"session-row:{session_key}")[:32]
                session_rows[session_id] = (
                    session_id,
                    record["account_id"],
                    contact["contact_id"] if contact else None,
                    contact["kind"] if contact else None,
                )
                duplicate_group = next(
                    spec.duplicate_group
                    for spec in RECORD_SPECS
                    if spec.record_key == record["record_key"]
                )
                rows.append(
                    (
                        index,
                        record["account_id"],
                        session_id,
                        record["media_id"],
                        record["file"]["relative_path"],
                        contact["kind"] if contact else None,
                        record["media_type"],
                        record["message_time"],
                        record["file"]["byte_size"],
                        record["mapping_confidence"],
                        record["file_status"],
                        record["ownership_status"],
                        duplicate_group,
                        _stable_sha256(seed, f"row:{record['record_key']}"),
                    )
                )
            connection.executemany(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)", session_rows.values()
            )
            connection.executemany(
                "INSERT INTO media_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        else:
            connection.execute(
                "CREATE TABLE legacy_events ("
                "event_id INTEGER PRIMARY KEY, account_id TEXT, path_hint TEXT, opaque_ref TEXT)"
            )
            connection.execute(
                "INSERT INTO legacy_events VALUES (?, ?, ?, ?)",
                (1, "wxid_synthetic_legacy", r"legacy\opaque.bin", _stable_sha256(seed, "legacy")),
            )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_phase3_fixture(destination: Path, *, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Create the complete Phase 3 fixture and return a path-free report."""

    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"Refusing non-empty fixture destination: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    ids = {spec.record_key: _stable_uuid(seed, f"media:{spec.record_key}") for spec in RECORD_SPECS}
    records = [_record_payload(spec, seed, ids) for spec in RECORD_SPECS]
    records.sort(key=lambda value: value["record_key"])
    for spec in RECORD_SPECS:
        if spec.actual_size is None:
            continue
        target = destination / "accounts" / spec.account_id / spec.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            _payload(seed, spec.duplicate_group or spec.record_key, spec.actual_size)
        )

    records_path = destination / "media-records.json"
    records_path.write_bytes(
        _canonical_json({"schema_version": FIXTURE_SCHEMA_VERSION, "records": records})
    )
    database_entries = []
    for schema in SCHEMA_MATRIX:
        database_path = destination / "databases" / schema["filename"]
        digest = _write_sqlite(database_path, schema, seed, records)
        database_entries.append(
            {
                "schema_version": schema["schema_version"],
                "supported": schema["supported"],
                "relative_path": f"databases/{schema['filename']}",
                "sha256": digest,
                "rejection_reason": schema["rejection_reason"],
            }
        )

    accounts: dict[str, dict[str, Any]] = {}
    for record in records:
        account = accounts.setdefault(
            record["account_id"],
            {
                "account_id": record["account_id"],
                "scan_id": record["scan_id"],
                "scan_manifest_sha256": record["scan_manifest_sha256"],
                "contact_ids": [],
                "record_keys": [],
            },
        )
        if record["contact"] and record["contact"]["contact_id"] not in account["contact_ids"]:
            account["contact_ids"].append(record["contact"]["contact_id"])
        account["record_keys"].append(record["record_key"])

    sizes = [record["file"]["byte_size"] for record in records]
    coverage = {
        "accounts": sorted(accounts),
        "contact_kinds": sorted(
            {record["contact"]["kind"] for record in records if record["contact"]}
        ),
        "media_types": sorted({record["media_type"] for record in records}),
        "mapping_confidences": sorted({record["mapping_confidence"] for record in records}),
        "file_statuses": sorted({record["file_status"] for record in records}),
        "ownership_statuses": sorted({record["ownership_status"] for record in records}),
        "message_years": sorted(
            {record["message_time"][:4] for record in records if record["message_time"]}
        ),
        "message_months": sorted(
            {record["message_time"][:7] for record in records if record["message_time"]}
        ),
        "size_boundaries": {"min": min(sizes), "max": max(sizes), "one_byte": 1},
        "duplicate_groups": sorted(
            {
                spec.duplicate_group
                for spec in RECORD_SPECS
                if spec.duplicate_group is not None
            }
        ),
    }
    manifest = {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "fixture_kind": "phase3-filesystem-and-sqlite",
        "seed": seed,
        "privacy": {
            "contains_real_chat_data": False,
            "contains_real_contacts": False,
            "contains_real_media": False,
            "contains_absolute_paths": False,
            "contains_keys": False,
            "contains_message_bodies": False,
        },
        "records_file": "media-records.json",
        "accounts": list(accounts.values()),
        "coverage": coverage,
        "boundary_cases": [
            {"record_key": "alpha-direct-image-zero", "kind": "zero-byte"},
            {"record_key": "alpha-direct-voice-one", "kind": "one-byte"},
            {"record_key": "beta-group-video-max", "kind": "one-mebibyte"},
            {"record_key": "beta-changed-low", "kind": "changed-file-identity"},
            {"record_key": "beta-missing-unmapped", "kind": "missing-file"},
            {"record_key": "beta-duplicate-a", "kind": "duplicate-candidate"},
            {"record_key": "beta-duplicate-b", "kind": "duplicate-candidate"},
        ],
        "database_schema_matrix": [
            {
                "schema_version": schema["schema_version"],
                "supported": schema["supported"],
                "filename": schema["filename"],
                "required_columns": schema["required_columns"],
                "rejection_reason": schema["rejection_reason"],
            }
            for schema in SCHEMA_MATRIX
        ],
        "databases": database_entries,
        "summary": {
            "record_count": len(records),
            "materialized_file_count": sum(spec.actual_size is not None for spec in RECORD_SPECS),
            "materialized_file_bytes": sum(
                spec.actual_size or 0 for spec in RECORD_SPECS if spec.actual_size is not None
            ),
            "database_count": len(SCHEMA_MATRIX),
            "supported_database_count": sum(schema["supported"] for schema in SCHEMA_MATRIX),
            "rejected_database_count": sum(not schema["supported"] for schema in SCHEMA_MATRIX),
        },
    }
    manifest["manifest_sha256"] = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    (destination / "phase3-manifest.json").write_bytes(_canonical_json(manifest))
    (destination / "database-schema-matrix.json").write_bytes(
        _canonical_json({"schemas": SCHEMA_MATRIX})
    )
    return {
        "seed": seed,
        "manifest_sha256": manifest["manifest_sha256"],
        **manifest["summary"],
        "database_sha256": {
            entry["schema_version"]: entry["sha256"] for entry in database_entries
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    arguments = parser.parse_args()
    print(
        json.dumps(
            build_phase3_fixture(arguments.destination, seed=arguments.seed),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
