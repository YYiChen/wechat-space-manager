"""Safe, read-only inventory of an ``xwechat_files``-shaped directory.

The scanner deliberately stops at a public boundary: it discovers account roots,
records file identities and conservative categories, and emits a versioned
``ScanManifest``. It does not open databases, decode payloads, or mutate files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from wechat_cleaner.domain.contracts import (
    AccountRef,
    ContractError,
    ContractErrorCode,
    MediaType,
    ScanManifest,
)

DEFAULT_SCANNER_VERSION = "0.1.0"
ACCOUNT_DIRECTORY_RE = re.compile(r"^wxid_[A-Za-z0-9_@.-]+$")
PROTECTED_ROOTS = frozenset({"db_storage", "sendtemp"})
PROTECTED_BUSINESS_PATH = ("business", "favorite")
CACHE_ROOT = "cache"
_REPARSE_POINT_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
# Progress callback cadence: one call per this many discovered files.  A
# 100k-file account then emits ~200 callbacks instead of 100k, which keeps
# the cross-thread signal traffic negligible while still updating visibly.
_PROGRESS_EVERY = 512


@dataclass(frozen=True, slots=True)
class ScannedFile:
    """A conservative file candidate relative to one account root."""

    relative_path: str
    byte_size: int
    modified_time_ns: int
    media_type: MediaType
    is_protected: bool = False
    is_regenerable_cache: bool = False


@dataclass(frozen=True, slots=True)
class ScanResult:
    """The manifest and candidates for one discovered account."""

    account: AccountRef
    manifest: ScanManifest
    files: tuple[ScannedFile, ...]


class ScannerError(ValueError):
    """A rejected scan root or account discovery request."""

    def __init__(self, code: ContractErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


def scan(
    root: str | os.PathLike[str],
    *,
    scanner_version: str = DEFAULT_SCANNER_VERSION,
) -> tuple[ScanResult, ...]:
    """Discover and scan every account directly below ``root``.

    ``root`` may itself be an account directory. The function never follows a
    symlink or Windows reparse point, and all returned paths are relative to the
    corresponding account root.
    """

    root_path = _validated_directory(root)
    account_roots = discover_account_roots(root_path)
    if not account_roots:
        raise ScannerError(
            ContractErrorCode.INVALID_PATH,
            "selected root contains no supported wxid account directory",
        )
    return tuple(
        scan_account(account_root, scanner_version=scanner_version)
        for account_root in account_roots
    )


def discover_account_roots(root: str | os.PathLike[str]) -> tuple[Path, ...]:
    """Return supported account directories in deterministic name order."""

    root_path = _validated_directory(root)
    if _looks_like_account_directory(root_path.name):
        return (root_path,)

    account_roots: list[Path] = []
    try:
        with os.scandir(root_path) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        for entry in entries:
            if not _looks_like_account_directory(entry.name):
                continue
            try:
                if _entry_is_reparse(entry):
                    raise ScannerError(
                        ContractErrorCode.INVALID_PATH,
                        "account directory is a reparse point and cannot be scanned",
                    )
                if entry.is_dir(follow_symlinks=False):
                    account_roots.append(Path(entry.path))
            except OSError as exc:
                raise ScannerError(
                    ContractErrorCode.INVALID_PATH,
                    "account directory could not be inspected",
                ) from exc
    except OSError as exc:
        raise ScannerError(
            ContractErrorCode.INVALID_PATH,
            "selected root could not be enumerated",
        ) from exc
    return tuple(account_roots)


def scan_account(
    account_root: str | os.PathLike[str],
    *,
    scanner_version: str = DEFAULT_SCANNER_VERSION,
    progress: Callable[[int], None] | None = None,
) -> ScanResult:
    """Inventory one account directory without opening or changing its files.

    ``progress``, when given, is called periodically with the number of files
    discovered so far.  It is a read-only notification: it never influences
    what is scanned, and callers may use it to drive UI feedback from a
    worker thread.
    """

    root_path = _validated_directory(account_root)
    if not _looks_like_account_directory(root_path.name):
        raise ScannerError(
            ContractErrorCode.INVALID_PATH,
            "account root name must start with wxid_",
        )
    if not scanner_version.strip():
        raise ScannerError(ContractErrorCode.INVALID_PATH, "scanner_version cannot be empty")

    account = AccountRef(
        account_id=root_path.name,
        account_root=_windows_style(root_path),
    )
    files, errors = _walk_account(root_path, progress=progress)
    files = tuple(sorted(files, key=lambda item: item.relative_path.casefold()))
    errors = tuple(sorted(errors, key=_error_sort_key))
    indexed_roots = tuple(
        sorted({item.relative_path.split("\\", maxsplit=1)[0] for item in files}, key=str.casefold)
    )
    manifest = ScanManifest(
        account=account,
        scanner_version=scanner_version.strip(),
        indexed_roots=indexed_roots,
        file_count=len(files),
        total_bytes=sum(item.byte_size for item in files),
        manifest_sha256=_manifest_digest(account.account_id, files, errors),
        errors=errors,
    )
    return ScanResult(account=account, manifest=manifest, files=files)


def _walk_account(
    root: Path, progress: Callable[[int], None] | None = None
) -> tuple[list[ScannedFile], list[ContractError]]:
    files: list[ScannedFile] = []
    errors: list[ContractError] = []
    pending: list[tuple[Path, tuple[str, ...]]] = [(root, ())]

    while pending:
        current, parent_parts = pending.pop()
        try:
            if _path_is_reparse(current):
                errors.append(
                    _scan_error(ContractErrorCode.INVALID_PATH, parent_parts, reparse=True)
                )
                continue
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        except ScannerError:
            errors.append(_scan_error(ContractErrorCode.INVALID_PATH, parent_parts))
            continue
        except OSError:
            errors.append(_scan_error(ContractErrorCode.INVALID_PATH, parent_parts))
            continue

        for entry in entries:
            parts = (*parent_parts, entry.name)
            relative_path = "\\".join(parts)
            try:
                if _entry_is_reparse(entry):
                    errors.append(
                        _scan_error(ContractErrorCode.INVALID_PATH, parts, reparse=True)
                    )
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), parts))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                info = entry.stat(follow_symlinks=False)
                files.append(
                    ScannedFile(
                        relative_path=relative_path,
                        byte_size=info.st_size,
                        modified_time_ns=info.st_mtime_ns,
                        media_type=_classify_media(parts),
                        is_protected=_is_protected(parts),
                        is_regenerable_cache=_is_cache(parts),
                    )
                )
                if progress is not None and len(files) % _PROGRESS_EVERY == 0:
                    progress(len(files))
            except OSError:
                errors.append(_scan_error(ContractErrorCode.INVALID_PATH, parts))

    return files, errors


def _classify_media(parts: tuple[str, ...]) -> MediaType:
    lowered = tuple(part.casefold() for part in parts)
    if _is_protected(parts) or _is_cache(parts):
        return MediaType.OTHER
    if "rec" in lowered:
        return MediaType.THUMBNAIL if parts[-1].casefold().endswith("_t") else MediaType.IMAGE
    if len(lowered) >= 2 and lowered[0] == "msg":
        category = lowered[1]
        return {
            "image": MediaType.IMAGE,
            "video": MediaType.VIDEO,
            "file": MediaType.FILE,
            "voice": MediaType.VOICE,
        }.get(category, MediaType.OTHER)
    return MediaType.OTHER


def _is_protected(parts: tuple[str, ...]) -> bool:
    lowered = tuple(part.casefold() for part in parts)
    return lowered[0] in PROTECTED_ROOTS or lowered[:2] == PROTECTED_BUSINESS_PATH


def _is_cache(parts: tuple[str, ...]) -> bool:
    return bool(parts) and parts[0].casefold() == CACHE_ROOT


def _manifest_digest(
    account_id: str,
    files: Iterable[ScannedFile],
    errors: Iterable[ContractError],
) -> str:
    payload = {
        "account_id": account_id,
        "files": [
            {
                "relative_path": item.relative_path,
                "byte_size": item.byte_size,
                "modified_time_ns": item.modified_time_ns,
                "media_type": item.media_type.value,
                "is_protected": item.is_protected,
                "is_regenerable_cache": item.is_regenerable_cache,
            }
            for item in files
        ],
        "errors": [error.model_dump(mode="json") for error in errors],
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _scan_error(
    code: ContractErrorCode,
    parts: tuple[str, ...],
    *,
    reparse: bool = False,
) -> ContractError:
    relative = "\\".join(parts) if parts else "."
    message = "reparse point skipped" if reparse else "path could not be inspected"
    return ContractError(
        code=code,
        message=message,
        context=(("relative_path", relative),),
    )


def _error_sort_key(error: ContractError) -> str:
    return ";".join(f"{key}={value}" for key, value in error.context)


def _validated_directory(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ScannerError(ContractErrorCode.INVALID_PATH, "scan root must be absolute")
    path = Path(os.path.abspath(path))
    if _path_is_reparse(path):
        raise ScannerError(
            ContractErrorCode.INVALID_PATH,
            "scan root is a reparse point and cannot be scanned",
        )
    if not path.exists() or not path.is_dir():
        raise ScannerError(
            ContractErrorCode.INVALID_PATH,
            "scan root must be an existing directory",
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ScannerError(
            ContractErrorCode.INVALID_PATH,
            "scan root could not be resolved",
        ) from exc
    if os.path.normcase(str(resolved)) != os.path.normcase(str(path)):
        raise ScannerError(
            ContractErrorCode.INVALID_PATH,
            "scan root resolves through a reparse point and cannot be scanned",
        )
    return path


def _looks_like_account_directory(name: str) -> bool:
    return bool(ACCOUNT_DIRECTORY_RE.fullmatch(name))


def _windows_style(path: Path) -> str:
    return str(path).replace("/", "\\").rstrip("\\")


def _path_is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ScannerError(
            ContractErrorCode.INVALID_PATH,
            "scan root could not be inspected",
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT_FLAG)


def _entry_is_reparse(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True
    info = entry.stat(follow_symlinks=False)
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT_FLAG)
