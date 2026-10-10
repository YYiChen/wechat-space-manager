"""Streaming MD5 of on-disk source bytes (R1, strictly read-only).

The hash runs over the encrypted ``.dat`` container bytes: identical
ciphertext implies identical plaintext, so "same file" is decided without
touching keys or decryption.  Before reading, the on-disk identity is
re-checked against the scanned record (size + mtime_ns, the FileIdentity
semantics) — a drifted or missing file is skipped with a reason, never
silently hashed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from wechat_cleaner.domain.contracts import FileIdentity, MediaRecord

from .model import DuplicateError, DuplicateGroup, GroupVerification

__all__ = ["file_md5", "verify_group"]

_MD5_CHUNK_BYTES = 1024 * 1024


def file_md5(
    record: MediaRecord,
    path: Path,
    *,
    chunk_bytes: int = _MD5_CHUNK_BYTES,
    progress: Callable[[int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> str:
    """Streaming MD5 hex digest of ``path`` with an identity re-check.

    ``progress`` receives cumulative bytes read.  ``cancel`` returning True
    aborts with a ``CANCELLED`` error.  Size or mtime drift since the scan
    raises ``IDENTITY_MISMATCH`` — the record is stale, hashing it would
    describe a file the user never saw.
    """

    identity: FileIdentity = record.file
    try:
        info = path.stat()
    except OSError as exc:
        raise DuplicateError("SOURCE_MISSING", f"source file could not be stat-ed: {exc}") from exc
    if info.st_size != identity.byte_size or info.st_mtime_ns != identity.modified_time_ns:
        raise DuplicateError(
            "IDENTITY_MISMATCH",
            "source file changed since the scan; identity re-check failed",
        )

    digest = hashlib.md5(usedforsecurity=False)
    read = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(chunk_bytes):
                if cancel is not None and cancel():
                    raise DuplicateError("CANCELLED", "duplicate verification was cancelled")
                digest.update(chunk)
                read += len(chunk)
                if progress is not None:
                    progress(read)
    except DuplicateError:
        raise
    except OSError as exc:
        raise DuplicateError("READ_FAILED", f"source file could not be read: {exc}") from exc
    return digest.hexdigest()


def verify_group(
    group: DuplicateGroup,
    resolve_path: Callable[[MediaRecord], Path],
    *,
    progress: Callable[[int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> GroupVerification:
    """Hash every member of a candidate group; derive the S3 verdict.

    ``resolve_path`` maps a record to its absolute source path (the facade
    passes ``resolve_source_path`` partially applied).  Members that cannot
    be hashed are listed in ``skipped`` with a reason and keep the group
    verdict at S2.
    """

    md5_by_path: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []
    for record in group.members:
        relative = record.file.relative_path
        try:
            md5_by_path[relative] = file_md5(
                record, resolve_path(record), progress=progress, cancel=cancel
            )
        except DuplicateError as exc:
            skipped.append((relative, exc.code))
        if cancel is not None and cancel():
            break

    content_identical = bool(md5_by_path) and not skipped and len(set(md5_by_path.values())) == 1
    return GroupVerification(
        group_key=group.normalized_name,
        content_identical=content_identical,
        md5_by_relative_path=md5_by_path,
        skipped=tuple(skipped),
    )
