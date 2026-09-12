"""Disposable local SQLite index for privacy-safe media metadata.

The index is deliberately not a database adapter and never receives a source
directory.  It stores only fields needed by filtering and review: relative
file identity, timestamps, media type, mapping/ownership state, and minimal
contact identifiers.  Message bodies, database keys, absolute paths and
decoded pixels have no columns in this schema.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from wechat_cleaner.domain.contracts import (
    ContactKind,
    ContactRef,
    FileStatus,
    FilterSortKey,
    FilterSpec,
    MappingConfidence,
    MediaRecord,
    MediaType,
    OwnershipStatus,
)

CURRENT_SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
_UTC_EPOCH = "1970-01-01T00:00:00Z"
_CURSOR_VERSION = 1
_GROUP_FIELDS = {
    "account_id": "account_id",
    "contact_id": "COALESCE(contact_id, 'unmapped')",
    "media_type": "media_type",
    "mapping_confidence": "mapping_confidence",
    "file_status": "file_status",
    "ownership_status": "ownership_status",
    "year": "substr(COALESCE(message_time, observed_at), 1, 4)",
    "month": "substr(COALESCE(message_time, observed_at), 1, 7)",
}
_SORT_FIELDS = {
    FilterSortKey.BYTES: "byte_size",
    FilterSortKey.MESSAGE_TIME: "COALESCE(message_time, '')",
    FilterSortKey.OBSERVED_AT: "observed_at",
    FilterSortKey.MEDIA_ID: "media_id",
}


class IndexErrorBase(RuntimeError):
    """Base class for errors that fail closed without exposing local paths."""


class IndexOpenError(IndexErrorBase):
    """The index is missing, corrupt, unsupported or unavailable."""


class IndexMigrationError(IndexErrorBase):
    """A schema migration failed and was rolled back."""


class IndexQueryError(IndexErrorBase):
    """A filter, grouping key or opaque cursor is invalid."""


class IndexWriteError(IndexErrorBase):
    """A batch upsert could not be committed."""


# Public-friendly alias; the more specific subclasses remain catchable.
LocalIndexError = IndexErrorBase


@dataclass(frozen=True, slots=True)
class AggregateBucket:
    key: str
    count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class IndexSummary:
    total_count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class IndexPage:
    items: tuple[MediaRecord, ...]
    page_size: int
    next_cursor: str | None
    total_count: int
    total_bytes: int
    filter_digest: str

    @property
    def records(self) -> tuple[MediaRecord, ...]:
        """Alias used by scanner/UI consumers that call page entries records."""

        return self.items

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None

    @property
    def summary(self) -> IndexSummary:
        return IndexSummary(total_count=self.total_count, total_bytes=self.total_bytes)


@dataclass(frozen=True, slots=True)
class IndexStats:
    schema_version: int
    record_count: int
    total_bytes: int
    account_count: int
    database_bytes: int
    journal_mode: str


@dataclass(frozen=True, slots=True)
class ClearReceipt:
    removed_records: int
    removed_bytes: int
    deleted_file: bool


_CREATE_META = """
CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_CREATE_RECORDS = """
CREATE TABLE IF NOT EXISTS media_records (
    media_id TEXT PRIMARY KEY,
    record_key TEXT NOT NULL UNIQUE,
    record_schema_version TEXT NOT NULL,
    account_id TEXT NOT NULL,
    contact_id TEXT,
    contact_kind TEXT,
    media_type TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    modified_time_ns INTEGER NOT NULL CHECK (modified_time_ns >= 0),
    sha256 TEXT,
    observed_at TEXT NOT NULL,
    message_time TEXT,
    message_local_id INTEGER,
    mapping_confidence TEXT NOT NULL,
    mapping_reason TEXT NOT NULL,
    file_status TEXT NOT NULL,
    ownership_status TEXT NOT NULL,
    is_regenerable_cache INTEGER NOT NULL DEFAULT 0 CHECK (is_regenerable_cache IN (0, 1)),
    scan_id TEXT,
    scan_manifest_sha256 TEXT,
    original_media_id TEXT,
    protection_reasons_json TEXT NOT NULL DEFAULT '[]',
    conflict_reasons_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
)
"""

_CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_media_records_account ON media_records(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_media_records_contact ON media_records(contact_id)",
    "CREATE INDEX IF NOT EXISTS idx_media_records_observed ON media_records(observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_media_records_message ON media_records(message_time)",
    "CREATE INDEX IF NOT EXISTS idx_media_records_bytes ON media_records(byte_size)",
    "CREATE INDEX IF NOT EXISTS idx_media_records_status "
    "ON media_records(file_status, ownership_status)",
    "CREATE INDEX IF NOT EXISTS idx_media_records_account_bytes "
    "ON media_records(account_id, byte_size, media_id)",
    "CREATE INDEX IF NOT EXISTS idx_media_records_account_observed "
    "ON media_records(account_id, observed_at, media_id)",
    "CREATE INDEX IF NOT EXISTS idx_media_records_account_message "
    "ON media_records(account_id, message_time, media_id)",
)

_UPSERT_COLUMNS = (
    "media_id",
    "record_key",
    "record_schema_version",
    "account_id",
    "contact_id",
    "contact_kind",
    "media_type",
    "relative_path",
    "byte_size",
    "modified_time_ns",
    "sha256",
    "observed_at",
    "message_time",
    "message_local_id",
    "mapping_confidence",
    "mapping_reason",
    "file_status",
    "ownership_status",
    "is_regenerable_cache",
    "scan_id",
    "scan_manifest_sha256",
    "original_media_id",
    "protection_reasons_json",
    "conflict_reasons_json",
    "updated_at",
)
_UPSERT_SQL = f"""
INSERT INTO media_records ({', '.join(_UPSERT_COLUMNS)})
VALUES ({', '.join('?' for _ in _UPSERT_COLUMNS)})
ON CONFLICT(media_id) DO UPDATE SET
    record_key=excluded.record_key,
    record_schema_version=excluded.record_schema_version,
    account_id=excluded.account_id,
    contact_id=excluded.contact_id,
    contact_kind=excluded.contact_kind,
    media_type=excluded.media_type,
    relative_path=excluded.relative_path,
    byte_size=excluded.byte_size,
    modified_time_ns=excluded.modified_time_ns,
    sha256=excluded.sha256,
    observed_at=excluded.observed_at,
    message_time=excluded.message_time,
    message_local_id=excluded.message_local_id,
    mapping_confidence=excluded.mapping_confidence,
    mapping_reason=excluded.mapping_reason,
    file_status=excluded.file_status,
    ownership_status=excluded.ownership_status,
    is_regenerable_cache=excluded.is_regenerable_cache,
    scan_id=excluded.scan_id,
    scan_manifest_sha256=excluded.scan_manifest_sha256,
    original_media_id=excluded.original_media_id,
    protection_reasons_json=excluded.protection_reasons_json,
    conflict_reasons_json=excluded.conflict_reasons_json,
    updated_at=excluded.updated_at
"""


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise IndexWriteError("timestamps must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IndexOpenError("index contains an invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IndexOpenError("index contains a timezone-naive timestamp")
    return parsed.astimezone(UTC)


def _json_tuple(value: str, label: str) -> tuple[str, ...]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise IndexOpenError(f"index contains invalid {label}") from exc
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise IndexOpenError(f"index contains invalid {label}")
    return tuple(payload)


def _filter_digest(spec: FilterSpec) -> str:
    payload = json.dumps(
        spec.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _encode_cursor(
    *,
    filter_digest: str,
    sort_by: FilterSortKey,
    descending: bool,
    sort_value: Any,
    media_id: str,
) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "filter_digest": filter_digest,
        "sort_by": sort_by.value,
        "descending": descending,
        "sort_value": sort_value,
        "media_id": media_id,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> dict[str, Any]:
    if not value or len(value) > 1024:
        raise IndexQueryError("cursor is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise IndexQueryError("cursor is invalid") from exc
    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise IndexQueryError("cursor version is unsupported")
    if not isinstance(payload.get("media_id"), str) or not payload["media_id"]:
        raise IndexQueryError("cursor identity is invalid")
    if not isinstance(payload.get("filter_digest"), str):
        raise IndexQueryError("cursor filter binding is invalid")
    return payload


def _path_sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal"))


class LocalMetadataIndex:
    """A transaction-safe, disposable SQLite index over ``MediaRecord`` data."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self._path = path
        self._connection = connection
        self._closed = False

    @property
    def path(self) -> Path:
        """The caller-selected index path; never serialized into index rows."""

        return self._path

    @property
    def schema_version(self) -> int:
        self._ensure_open()
        return _read_schema_version(self._connection)

    @classmethod
    def open(cls, path: str | Path) -> LocalMetadataIndex:
        """Open or create an index, auto-migrating supported older schemas."""

        target = Path(path)
        if target.name in {"", ".", ".."}:
            raise IndexOpenError("index path is invalid")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            connection = _connect(target)
            tables = _table_names(connection)
            if not tables:
                _create_current_schema(connection)
            else:
                version = _read_schema_version(connection)
                if version > CURRENT_SCHEMA_VERSION:
                    raise IndexOpenError("index schema is newer than this application")
                if version < LEGACY_SCHEMA_VERSION:
                    raise IndexOpenError("index schema is unsupported")
                if version == LEGACY_SCHEMA_VERSION:
                    _migrate_v1_to_v2(connection)
                _ensure_current_indexes(connection)
            return cls(target, connection)
        except IndexErrorBase:
            try:
                connection.close()  # type: ignore[has-type]
            except UnboundLocalError:
                pass
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            try:
                connection.close()  # type: ignore[has-type]
            except UnboundLocalError:
                pass
            raise IndexOpenError("index could not be opened") from exc

    @classmethod
    def create_legacy_v1(cls, path: str | Path) -> None:
        """Create a small v1 fixture for migration tests and compatibility tools."""

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = _connect(target)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(_CREATE_META)
            connection.execute(
                """
                CREATE TABLE media_records (
                    media_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    contact_id TEXT,
                    media_type TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    modified_time_ns INTEGER NOT NULL,
                    sha256 TEXT,
                    observed_at TEXT NOT NULL,
                    message_local_id INTEGER,
                    mapping_confidence TEXT NOT NULL,
                    mapping_reason TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO index_meta(key, value) VALUES ('schema_version', '1')"
            )
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        except (OSError, sqlite3.DatabaseError) as exc:
            try:
                connection.rollback()  # type: ignore[has-type]
                connection.close()  # type: ignore[has-type]
            except UnboundLocalError:
                pass
            raise IndexOpenError("legacy index could not be created") from exc
        connection.close()

    @classmethod
    def migrate(cls, path: str | Path, *, interrupt_after: int | None = None) -> int:
        """Migrate a v1 index; ``interrupt_after`` is a rollback test hook."""

        target = Path(path)
        try:
            connection = _connect(target)
            version = _read_schema_version(connection)
            if version > CURRENT_SCHEMA_VERSION:
                raise IndexMigrationError("index schema is newer than this application")
            if version == LEGACY_SCHEMA_VERSION:
                _migrate_v1_to_v2(connection, interrupt_after=interrupt_after)
            elif version != CURRENT_SCHEMA_VERSION:
                raise IndexMigrationError("index schema is unsupported")
            return _read_schema_version(connection)
        except IndexErrorBase:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise IndexMigrationError("index migration failed") from exc
        finally:
            try:
                connection.close()  # type: ignore[has-type]
            except UnboundLocalError:
                pass

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def __enter__(self) -> LocalMetadataIndex:
        self._ensure_open()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def upsert_records(
        self, records: Iterable[MediaRecord], *, batch_size: int = 1_000
    ) -> int:
        """Stream records in bounded batches and commit them atomically."""

        self._ensure_open()
        if batch_size < 1 or batch_size > 50_000:
            raise IndexWriteError("batch_size must be between 1 and 50000")
        inserted = 0
        batch: list[tuple[Any, ...]] = []
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            for record in records:
                if not isinstance(record, MediaRecord):
                    raise IndexWriteError("upsert input must contain MediaRecord values")
                batch.append(self._record_values(record))
                if len(batch) >= batch_size:
                    self._connection.executemany(_UPSERT_SQL, batch)
                    inserted += len(batch)
                    batch.clear()
            if batch:
                self._connection.executemany(_UPSERT_SQL, batch)
                inserted += len(batch)
            self._connection.execute(
                "INSERT INTO index_meta(key, value) VALUES ('last_upsert_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_utc_iso(datetime.now(UTC)),),
            )
            self._connection.commit()
            return inserted
        except (IndexErrorBase, sqlite3.DatabaseError, OSError) as exc:
            self._connection.rollback()
            if isinstance(exc, IndexErrorBase):
                raise
            raise IndexWriteError("index batch could not be committed") from exc

    def query_page(
        self,
        spec: FilterSpec | None = None,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> IndexPage:
        """Return a stable page bound to its filter digest and sort order."""

        self._ensure_open()
        spec = spec or FilterSpec()
        size = page_size if page_size is not None else spec.page_size
        if size < 1 or size > 500:
            raise IndexQueryError("page_size must be between 1 and 500")
        digest = _filter_digest(spec)
        filter_sql, filter_params = self._filter_clause(spec)
        sort_expr = _SORT_FIELDS[spec.sort_by]
        total = self._connection.execute(
            f"SELECT COUNT(*) AS count, COALESCE(SUM(byte_size), 0) AS bytes "
            f"FROM media_records WHERE {filter_sql}",
            filter_params,
        ).fetchone()
        cursor_sql = ""
        cursor_params: list[Any] = []
        if cursor is not None:
            payload = _decode_cursor(cursor)
            if (
                payload.get("filter_digest") != digest
                or payload.get("sort_by") != spec.sort_by.value
                or payload.get("descending") is not spec.descending
            ):
                raise IndexQueryError("cursor does not match the requested filter")
            cursor_value = payload.get("sort_value")
            cursor_media_id = payload["media_id"]
            operator = "<" if spec.descending else ">"
            cursor_sql = f" AND ({sort_expr} {operator} ? OR ({sort_expr} = ? AND media_id > ?))"
            cursor_params = [cursor_value, cursor_value, cursor_media_id]
        direction = "DESC" if spec.descending else "ASC"
        rows = self._connection.execute(
            f"SELECT *, {sort_expr} AS _sort_value FROM media_records "
            f"WHERE {filter_sql}{cursor_sql} "
            f"ORDER BY {sort_expr} {direction}, media_id ASC LIMIT ?",
            [*filter_params, *cursor_params, size + 1],
        ).fetchall()
        has_more = len(rows) > size
        page_rows = rows[:size]
        items = tuple(self._row_to_record(row) for row in page_rows)
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = _encode_cursor(
                filter_digest=digest,
                sort_by=spec.sort_by,
                descending=spec.descending,
                sort_value=last["_sort_value"],
                media_id=last["media_id"],
            )
        return IndexPage(
            items=items,
            page_size=size,
            next_cursor=next_cursor,
            total_count=int(total["count"]),
            total_bytes=int(total["bytes"]),
            filter_digest=digest,
        )

    def aggregate(
        self, spec: FilterSpec | None = None, *, group_by: str = "media_type"
    ) -> tuple[AggregateBucket, ...]:
        """Group matching metadata by a fixed, injection-safe dimension."""

        self._ensure_open()
        spec = spec or FilterSpec()
        try:
            group_expr = _GROUP_FIELDS[group_by]
        except KeyError as exc:
            raise IndexQueryError("unsupported aggregate dimension") from exc
        filter_sql, params = self._filter_clause(spec)
        rows = self._connection.execute(
            f"SELECT {group_expr} AS bucket_key, COUNT(*) AS count, "
            f"COALESCE(SUM(byte_size), 0) AS total_bytes FROM media_records "
            f"WHERE {filter_sql} GROUP BY {group_expr} ORDER BY bucket_key ASC",
            params,
        ).fetchall()
        return tuple(
            AggregateBucket(
                key=str(row["bucket_key"]),
                count=int(row["count"]),
                total_bytes=int(row["total_bytes"]),
            )
            for row in rows
        )

    def summarize(self, spec: FilterSpec | None = None) -> IndexSummary:
        page = self.query_page(spec, page_size=1)
        return page.summary

    def stats(self) -> IndexStats:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(byte_size), 0) AS bytes, "
            "COUNT(DISTINCT account_id) AS accounts FROM media_records"
        ).fetchone()
        page_count = int(self._connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(self._connection.execute("PRAGMA page_size").fetchone()[0])
        journal_mode = str(self._connection.execute("PRAGMA journal_mode").fetchone()[0])
        database_bytes = self._path.stat().st_size if self._path.exists() else 0
        return IndexStats(
            schema_version=self.schema_version,
            record_count=int(row["count"]),
            total_bytes=int(row["bytes"]),
            account_count=int(row["accounts"]),
            database_bytes=max(database_bytes, page_count * page_size),
            journal_mode=journal_mode,
        )

    def clear(self, *, delete_file: bool = True) -> ClearReceipt:
        """Delete only this disposable index; never receives a source path."""

        self._ensure_open()
        row = self._connection.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(byte_size), 0) AS bytes FROM media_records"
        ).fetchone()
        removed = ClearReceipt(
            removed_records=int(row["count"]),
            removed_bytes=int(row["bytes"]),
            deleted_file=delete_file and self._path != Path(":memory:"),
        )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute("DELETE FROM media_records")
            self._connection.commit()
            if delete_file and self._path != Path(":memory:"):
                self.close()
                self._path.unlink(missing_ok=True)
                for sidecar in _path_sidecars(self._path):
                    sidecar.unlink(missing_ok=True)
            return removed
        except (OSError, sqlite3.DatabaseError) as exc:
            try:
                self._connection.rollback()
            except sqlite3.Error:
                pass
            raise IndexWriteError("index clear failed") from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise IndexOpenError("index is closed")

    def _record_values(self, record: MediaRecord) -> tuple[Any, ...]:
        record_key = record.record_key or f"media-{record.media_id.hex}"
        contact_id = record.contact.contact_id if record.contact else None
        contact_kind = record.contact.kind.value if record.contact else None
        return (
            str(record.media_id),
            record_key,
            record.schema_version,
            record.account_id,
            contact_id,
            contact_kind,
            record.media_type.value,
            record.file.relative_path,
            record.file.byte_size,
            record.file.modified_time_ns,
            record.file.sha256,
            _utc_iso(record.observed_at),
            _utc_iso(record.message_time) if record.message_time else None,
            record.message_local_id,
            record.mapping_confidence.value,
            record.mapping_reason,
            record.file_status.value,
            record.ownership_status.value,
            int(record.is_regenerable_cache),
            str(record.scan_id) if record.scan_id else None,
            record.scan_manifest_sha256,
            str(record.original_media_id) if record.original_media_id else None,
            json.dumps(record.protection_reasons, ensure_ascii=False, separators=(",", ":")),
            json.dumps(record.conflict_reasons, ensure_ascii=False, separators=(",", ":")),
            _utc_iso(datetime.now(UTC)),
        )

    def _filter_clause(self, spec: FilterSpec) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []

        def add_in(column: str, values: Iterable[Any]) -> None:
            values = tuple(values)
            if values:
                clauses.append(f"{column} IN ({', '.join('?' for _ in values)})")
                params.extend(value.value if hasattr(value, "value") else value for value in values)

        add_in("account_id", spec.account_ids)
        add_in("contact_id", spec.contact_ids)
        add_in("media_type", spec.media_types)
        add_in("mapping_confidence", spec.mapping_confidences)
        add_in("file_status", spec.file_statuses)
        add_in("ownership_status", spec.ownership_statuses)
        if not spec.include_protected:
            clauses.append("ownership_status <> ?")
            params.append(OwnershipStatus.PROTECTED.value)
        if not spec.include_conflicts:
            clauses.append("ownership_status <> ?")
            params.append(OwnershipStatus.CONFLICT.value)
        if not spec.include_unmapped:
            clauses.append("ownership_status <> ?")
            params.append(OwnershipStatus.UNMAPPED.value)
        if spec.message_time_from is not None:
            clauses.append("message_time >= ?")
            params.append(_utc_iso(spec.message_time_from))
        if spec.message_time_to is not None:
            clauses.append("message_time <= ?")
            params.append(_utc_iso(spec.message_time_to))
        if spec.observed_time_from is not None:
            clauses.append("observed_at >= ?")
            params.append(_utc_iso(spec.observed_time_from))
        if spec.observed_time_to is not None:
            clauses.append("observed_at <= ?")
            params.append(_utc_iso(spec.observed_time_to))
        if spec.min_bytes is not None:
            clauses.append("byte_size >= ?")
            params.append(spec.min_bytes)
        if spec.max_bytes is not None:
            clauses.append("byte_size <= ?")
            params.append(spec.max_bytes)
        return " AND ".join(clauses) or "1=1", params

    def _row_to_record(self, row: sqlite3.Row) -> MediaRecord:
        try:
            contact = None
            if row["contact_id"]:
                contact = ContactRef(
                    account_id=row["account_id"],
                    contact_id=row["contact_id"],
                    kind=ContactKind(row["contact_kind"] or ContactKind.UNKNOWN.value),
                )
            return MediaRecord(
                schema_version=row["record_schema_version"],
                media_id=UUID(row["media_id"]),
                account_id=row["account_id"],
                file={
                    "relative_path": row["relative_path"],
                    "byte_size": row["byte_size"],
                    "modified_time_ns": row["modified_time_ns"],
                    "sha256": row["sha256"],
                },
                media_type=MediaType(row["media_type"]),
                observed_at=_parse_utc(row["observed_at"]),
                contact=contact,
                message_local_id=row["message_local_id"],
                mapping_confidence=MappingConfidence(row["mapping_confidence"]),
                mapping_reason=row["mapping_reason"],
                original_media_id=UUID(row["original_media_id"])
                if row["original_media_id"]
                else None,
                is_regenerable_cache=bool(row["is_regenerable_cache"]),
                record_key=row["record_key"],
                scan_id=UUID(row["scan_id"]) if row["scan_id"] else None,
                scan_manifest_sha256=row["scan_manifest_sha256"],
                message_time=_parse_utc(row["message_time"]) if row["message_time"] else None,
                file_status=FileStatus(row["file_status"]),
                ownership_status=OwnershipStatus(row["ownership_status"]),
                protection_reasons=_json_tuple(
                    row["protection_reasons_json"], "protection reasons"
                ),
                conflict_reasons=_json_tuple(row["conflict_reasons_json"], "conflict reasons"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise IndexOpenError("index contains an invalid media record") from exc


def _connect(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA journal_mode=DELETE")
        return connection
    except sqlite3.DatabaseError as exc:
        raise IndexOpenError("index database is not a readable SQLite file") from exc


def _table_names(connection: sqlite3.Connection) -> set[str]:
    try:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    except sqlite3.DatabaseError as exc:
        raise IndexOpenError("index catalog is unreadable") from exc


def _read_schema_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute(
            "SELECT value FROM index_meta WHERE key='schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise IndexOpenError("index schema marker is missing") from exc
    if row is None:
        raise IndexOpenError("index schema marker is missing")
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise IndexOpenError("index schema marker is invalid") from exc


def _ensure_current_indexes(connection: sqlite3.Connection) -> None:
    try:
        for statement in _CREATE_INDEXES:
            connection.execute(statement)
    except sqlite3.DatabaseError as exc:
        raise IndexOpenError("index indexes are unavailable") from exc


def _create_current_schema(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(_CREATE_META)
        connection.execute(_CREATE_RECORDS)
        _ensure_current_indexes(connection)
        connection.execute(
            "INSERT INTO index_meta(key, value) VALUES ('schema_version', ?)",
            (str(CURRENT_SCHEMA_VERSION),),
        )
        connection.execute("PRAGMA user_version=2")
        connection.commit()
    except sqlite3.DatabaseError as exc:
        connection.rollback()
        raise IndexOpenError("index schema could not be created") from exc


def _migrate_v1_to_v2(
    connection: sqlite3.Connection, *, interrupt_after: int | None = None
) -> None:
    additions = (
        ("record_key", "TEXT NOT NULL DEFAULT ''"),
        ("record_schema_version", "TEXT NOT NULL DEFAULT '1.0'"),
        ("contact_kind", "TEXT"),
        ("message_time", "TEXT"),
        ("file_status", "TEXT NOT NULL DEFAULT 'present'"),
        ("ownership_status", "TEXT NOT NULL DEFAULT 'mapped'"),
        ("is_regenerable_cache", "INTEGER NOT NULL DEFAULT 0"),
        ("scan_id", "TEXT"),
        ("scan_manifest_sha256", "TEXT"),
        ("original_media_id", "TEXT"),
        ("protection_reasons_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("conflict_reasons_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("updated_at", f"TEXT NOT NULL DEFAULT '{_UTC_EPOCH}'"),
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(media_records)")
        }
        step = 0
        for column, definition in additions:
            if column in existing:
                continue
            if interrupt_after is not None and step >= interrupt_after:
                raise RuntimeError("migration interrupted by test hook")
            connection.execute(f"ALTER TABLE media_records ADD COLUMN {column} {definition}")
            step += 1
        connection.execute(
            "UPDATE media_records SET record_key='media-' || replace(media_id, '-', '') "
            "WHERE record_key=''"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_media_records_record_key "
            "ON media_records(record_key)"
        )
        connection.execute(
            "UPDATE media_records SET contact_kind='unknown' "
            "WHERE contact_id IS NOT NULL AND contact_kind IS NULL"
        )
        _ensure_current_indexes(connection)
        connection.execute(
            "INSERT INTO index_meta(key, value) VALUES ('schema_version', '2') "
            "ON CONFLICT(key) DO UPDATE SET value='2'"
        )
        connection.execute("PRAGMA user_version=2")
        connection.commit()
    except Exception as exc:
        connection.rollback()
        if isinstance(exc, IndexMigrationError):
            raise
        raise IndexMigrationError("index migration was rolled back") from exc
