"""Read-only adapters for supported decrypted database *copies*.

The adapter intentionally knows only the small, redacted metadata surface
needed by the mapper.  It never opens a live WeChat database, writes a
database, extracts keys, or returns message bodies/contact display names.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import stat
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from wechat_cleaner.domain.contracts import (
    AccountRef,
    ContactKind,
    ContactRef,
    DatabaseEvidence,
    DatabaseEvidenceConflict,
    DatabaseEvidenceEnvelope,
    EvidenceKind,
    FileIdentity,
    MappingConfidence,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")
_REPARSE_POINT_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class DatabaseAdapterError(ValueError):
    """A redacted, stable adapter failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class DatabaseAdapterErrorCode:
    INVALID_COPY = "INVALID_COPY"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    AMBIGUOUS_SCHEMA = "AMBIGUOUS_SCHEMA"
    CORRUPT_DATABASE = "CORRUPT_DATABASE"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    COPY_CHANGED = "COPY_CHANGED"
    READ_FAILED = "READ_FAILED"


@dataclass(frozen=True, slots=True)
class SchemaDefinition:
    schema_version: str
    sqlite_user_version: int
    required_tables: tuple[str, ...]
    required_columns: tuple[tuple[str, tuple[str, ...]], ...]


SUPPORTED_SCHEMAS: tuple[SchemaDefinition, ...] = (
    SchemaDefinition(
        schema_version="synthetic-v1",
        sqlite_user_version=301,
        required_tables=("messages",),
        required_columns=(
            (
                "messages",
                (
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
                ),
            ),
        ),
    ),
    SchemaDefinition(
        schema_version="synthetic-v2",
        sqlite_user_version=302,
        required_tables=("media_messages", "sessions"),
        required_columns=(
            (
                "media_messages",
                (
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
                ),
            ),
            ("sessions", ("session_id", "account_id", "contact_id", "contact_kind")),
        ),
    ),
)


class DatabaseCopyAdapter:
    """Read a supported database copy into a redacted evidence envelope."""

    adapter_version = "db-adapter-1.0"

    def __init__(self, *, adapter_version: str | None = None) -> None:
        self.adapter_version = adapter_version or self.adapter_version

    def detect_schema(self, database_copy: str | Path) -> SchemaDefinition:
        path = _validate_copy_path(database_copy)
        try:
            with _open_readonly(path) as connection:
                return _detect_schema(connection)
        except DatabaseAdapterError:
            raise
        except sqlite3.DatabaseError as exc:
            raise DatabaseAdapterError(
                DatabaseAdapterErrorCode.CORRUPT_DATABASE,
                "database copy could not be read",
            ) from exc
        except OSError as exc:
            raise DatabaseAdapterError(
                DatabaseAdapterErrorCode.READ_FAILED,
                "database copy could not be opened",
            ) from exc

    def read_copy(
        self,
        database_copy: str | Path,
        account: AccountRef,
        *,
        database_copy_id: str | None = None,
    ) -> DatabaseEvidenceEnvelope:
        """Read one account from a copy while checking identity before/after."""

        path = _validate_copy_path(database_copy)
        before = _copy_identity(path)
        try:
            with _open_readonly(path) as connection:
                schema = _detect_schema(connection)
                rows = tuple(_read_rows(connection, schema, account.account_id))
        except DatabaseAdapterError:
            raise
        except sqlite3.DatabaseError as exc:
            raise DatabaseAdapterError(
                DatabaseAdapterErrorCode.CORRUPT_DATABASE,
                "database copy could not be queried",
            ) from exc
        except (OSError, ValueError, TypeError) as exc:
            raise DatabaseAdapterError(
                DatabaseAdapterErrorCode.READ_FAILED,
                "database copy query failed validation",
            ) from exc
        after = _copy_identity(path)
        if before != after:
            raise DatabaseAdapterError(
                DatabaseAdapterErrorCode.COPY_CHANGED,
                "database copy changed during read",
            )
        digest = before.sha256
        copy_id = database_copy_id or f"db-{digest[:24]}"
        if not _SAFE_ID_RE.fullmatch(copy_id):
            raise DatabaseAdapterError(
                DatabaseAdapterErrorCode.INVALID_COPY,
                "database copy id is invalid",
            )
        evidence, conflicts = _build_evidence(rows, account)
        return DatabaseEvidenceEnvelope(
            account=account,
            database_copy_id=copy_id,
            database_copy_sha256=digest,
            database_schema_version=schema.schema_version,
            adapter_version=self.adapter_version,
            evidence=tuple(evidence),
            conflicts=tuple(conflicts),
        )

    # Alias used by callers that prefer a verb matching other adapters.
    read = read_copy
    adapt = read_copy


@dataclass(frozen=True, slots=True)
class _CopyIdentity:
    size: int
    modified_time_ns: int
    sha256: str


def _validate_copy_path(value: str | Path) -> Path:
    try:
        path = Path(value)
        resolved = path.resolve(strict=True)
        info = resolved.lstat()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise DatabaseAdapterError(
            DatabaseAdapterErrorCode.INVALID_COPY,
            "database copy is not a readable regular file",
        ) from exc
    if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
        raise DatabaseAdapterError(
            DatabaseAdapterErrorCode.INVALID_COPY,
            "database copy is not a readable regular file",
        )
    return resolved


def _copy_identity(path: Path) -> _CopyIdentity:
    try:
        info = path.stat()
        digest = _sha256(path)
    except OSError as exc:
        raise DatabaseAdapterError(
            DatabaseAdapterErrorCode.READ_FAILED,
            "database copy identity could not be checked",
        ) from exc
    return _CopyIdentity(info.st_size, info.st_mtime_ns, digest)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_readonly(path: Path) -> sqlite3.Connection:
    # ``immutable=1`` prevents SQLite from attempting journal/WAL writes.  URI
    # values are built from Path.as_uri rather than interpolated SQL.
    uri = f"{path.as_uri()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as exc:
        raise DatabaseAdapterError(
            DatabaseAdapterErrorCode.CORRUPT_DATABASE,
            "database copy could not be opened read-only",
        ) from exc


def _detect_schema(connection: sqlite3.Connection) -> SchemaDefinition:
    try:
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        table_rows = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        tables = {str(row[0]) for row in table_rows}
    except sqlite3.DatabaseError as exc:
        raise DatabaseAdapterError(
            DatabaseAdapterErrorCode.CORRUPT_DATABASE,
            "database schema could not be inspected",
        ) from exc

    matches: list[SchemaDefinition] = []
    for definition in SUPPORTED_SCHEMAS:
        if definition.sqlite_user_version != user_version:
            continue
        if not set(definition.required_tables).issubset(tables):
            continue
        if all(
            set(columns).issubset(_table_columns(connection, table))
            for table, columns in definition.required_columns
        ):
            matches.append(definition)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise DatabaseAdapterError(
            DatabaseAdapterErrorCode.AMBIGUOUS_SCHEMA,
            "database schema matches more than one supported definition",
        )
    raise DatabaseAdapterError(
        DatabaseAdapterErrorCode.UNSUPPORTED_SCHEMA,
        "database schema is not supported",
    )


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    quoted = '"' + table.replace('"', '""') + '"'
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted})")}


def _read_rows(
    connection: sqlite3.Connection,
    schema: SchemaDefinition,
    account_id: str,
) -> Iterable[dict[str, Any]]:
    if schema.schema_version == "synthetic-v1":
        query = (
            "SELECT local_id, account_id, contact_id, contact_kind, media_id, "
            "relative_path, media_type, message_time, byte_size, mapping_confidence, "
            "file_status, ownership_status, duplicate_group, source_row_digest "
            "FROM messages WHERE account_id = ? ORDER BY local_id"
        )
        for row in connection.execute(query, (account_id,)):
            yield dict(row)
        return
    query = (
        "SELECT m.local_id, m.account_id, s.contact_id, s.contact_kind, "
        "m.session_id, s.account_id AS session_account_id, m.media_id, "
        "m.media_relpath AS relative_path, m.media_type, m.sent_at AS message_time, "
        "m.size_bytes AS byte_size, m.mapping_confidence, m.file_status, "
        "m.ownership_status, m.duplicate_group, m.source_row_digest "
        "FROM media_messages AS m LEFT JOIN sessions AS s "
        "ON s.session_id = m.session_id WHERE m.account_id = ? ORDER BY m.local_id"
    )
    for row in connection.execute(query, (account_id,)):
        yield dict(row)


def _build_evidence(
    rows: Iterable[dict[str, Any]], account: AccountRef
) -> tuple[list[DatabaseEvidence], list[DatabaseEvidenceConflict]]:
    evidence: list[DatabaseEvidence] = []
    conflicts: list[DatabaseEvidenceConflict] = []
    by_media: defaultdict[UUID, list[str]] = defaultdict(list)
    by_path: defaultdict[str, list[str]] = defaultdict(list)
    session_conflicts: list[tuple[UUID | None, str, str]] = []
    for row in rows:
        normalized = _normalize_row(row, account.account_id)
        item = DatabaseEvidence(
            account_id=account.account_id,
            evidence_kind=EvidenceKind.MESSAGE,
            source_table=normalized["source_table"],
            source_row_digest=normalized["source_row_digest"],
            media_id=normalized["media_id"],
            contact=normalized["contact"],
            message_local_id=normalized["local_id"],
            message_time=normalized["message_time"],
            relative_path=normalized["relative_path"],
            byte_size=normalized["byte_size"],
            confidence=normalized["confidence"],
        )
        evidence.append(item)
        digest = normalized["source_row_digest"]
        media_id = normalized["media_id"]
        if media_id is not None:
            by_media[media_id].append(digest)
        if normalized["relative_path"] is not None:
            path_key = normalized["relative_path"].casefold()
            contact_key = normalized["contact_digest"]
            by_path[path_key].append(contact_key)
        if normalized["session_conflict"]:
            session_conflicts.append(
                (media_id, normalized["session_conflict"], digest)
            )

    for media_id, digests in by_media.items():
        unique = tuple(dict.fromkeys(digests))
        if len(unique) > 1:
            conflicts.append(
                _conflict(
                    account.account_id,
                    media_id,
                    "media_id",
                    unique,
                    "multiple database rows claim the same media identity",
                )
            )
    for _path_key, digests in by_path.items():
        unique = tuple(dict.fromkeys(digests))
        if len(unique) > 1:
            conflicts.append(
                _conflict(
                    account.account_id,
                    None,
                    "relative_path",
                    unique,
                    "multiple contact candidates claim one relative media path",
                )
            )
    for media_id, session_digest, row_digest in session_conflicts:
        conflicts.append(
            _conflict(
                account.account_id,
                media_id,
                "session.account_id",
                (row_digest, session_digest),
                "session account does not match media row account",
            )
        )
    return evidence, conflicts


def _normalize_row(row: dict[str, Any], account_id: str) -> dict[str, Any]:
    if row.get("account_id") != account_id:
        raise DatabaseAdapterError(
            DatabaseAdapterErrorCode.ACCOUNT_MISMATCH,
            "database row belongs to another account",
        )
    source_digest = row.get("source_row_digest")
    if not isinstance(source_digest, str) or not _SHA256_RE.fullmatch(source_digest):
        raise DatabaseAdapterError(
            DatabaseAdapterErrorCode.CORRUPT_DATABASE,
            "database row digest is invalid",
        )
    relative_path = row.get("relative_path")
    if relative_path is not None:
        try:
            relative_path = FileIdentity(
                relative_path=str(relative_path), byte_size=0, modified_time_ns=0
            ).relative_path
        except ValueError as exc:
            raise DatabaseAdapterError(
                DatabaseAdapterErrorCode.CORRUPT_DATABASE,
                "database row contains an invalid relative path",
            ) from exc
    media_id = _uuid_or_none(row.get("media_id"))
    message_time = _parse_time(row.get("message_time"))
    byte_size = row.get("byte_size")
    if byte_size is not None and (isinstance(byte_size, bool) or not isinstance(byte_size, int)):
        raise DatabaseAdapterError(
            DatabaseAdapterErrorCode.CORRUPT_DATABASE,
            "database row contains an invalid byte size",
        )
    if byte_size is not None and byte_size < 0:
        raise DatabaseAdapterError(
            DatabaseAdapterErrorCode.CORRUPT_DATABASE,
            "database row contains an invalid byte size",
        )
    try:
        confidence = MappingConfidence(str(row.get("mapping_confidence", "medium")))
    except ValueError as exc:
        raise DatabaseAdapterError(
            DatabaseAdapterErrorCode.CORRUPT_DATABASE,
            "database row contains an unsupported mapping confidence",
        ) from exc
    contact_id = row.get("contact_id")
    contact = None
    if contact_id is not None:
        kind = _contact_kind(row.get("contact_kind"))
        session_id = row.get("session_id")
        session_hash = (
            str(session_id)
            if isinstance(session_id, str) and _SESSION_HASH_RE.fullmatch(session_id)
            else None
        )
        contact = ContactRef(
            account_id=account_id,
            contact_id=str(contact_id),
            kind=kind,
            session_hash=session_hash,
        )
    session_conflict = None
    session_account = row.get("session_account_id")
    if session_account is not None and session_account != account_id:
        session_conflict = hashlib.sha256(str(session_account).encode("utf-8")).hexdigest()
    contact_digest = hashlib.sha256(
        f"{account_id}:{contact_id or ''}:{row.get('session_id') or ''}".encode()
    ).hexdigest()
    return {
        "source_table": "media_messages" if "session_id" in row else "messages",
        "source_row_digest": source_digest,
        "local_id": _int_or_none(row.get("local_id")),
        "media_id": media_id,
        "contact": contact,
        "message_time": message_time,
        "relative_path": relative_path,
        "byte_size": byte_size,
        "confidence": confidence,
        "contact_digest": contact_digest,
        "session_conflict": session_conflict,
    }


def _uuid_or_none(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise DatabaseAdapterError(
            DatabaseAdapterErrorCode.CORRUPT_DATABASE,
            "database row contains an invalid media identity",
        ) from exc


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DatabaseAdapterError(
            DatabaseAdapterErrorCode.CORRUPT_DATABASE,
            "database row contains an invalid local id",
        )
    return value


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DatabaseAdapterError(
            DatabaseAdapterErrorCode.CORRUPT_DATABASE,
            "database row contains an invalid message time",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DatabaseAdapterError(
            DatabaseAdapterErrorCode.CORRUPT_DATABASE,
            "database row contains an invalid message time",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DatabaseAdapterError(
            DatabaseAdapterErrorCode.CORRUPT_DATABASE,
            "database message time has no timezone",
        )
    return parsed.astimezone(UTC)


def _contact_kind(value: Any) -> ContactKind:
    try:
        return ContactKind(str(value))
    except ValueError:
        return ContactKind.UNKNOWN


def _conflict(
    account_id: str,
    media_id: UUID | None,
    field_name: str,
    candidate_digests: Iterable[str],
    reason: str,
) -> DatabaseEvidenceConflict:
    digests = tuple(dict.fromkeys(candidate_digests))
    return DatabaseEvidenceConflict(
        account_id=account_id,
        media_id=media_id,
        field_name=field_name,
        candidate_digests=digests,
        reason=reason,
    )


def _is_reparse(info: Any) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT_FLAG
    )
