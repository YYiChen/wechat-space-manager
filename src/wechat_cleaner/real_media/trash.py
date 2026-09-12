"""Recycle-Bin-only removal for source media files (Phase 4 write capability).

Safety contract, deliberately narrow:

* Windows-only - the Beta ships for Windows; other platforms refuse.
* The file is moved to the **Recycle Bin** via the Shell (``FOF_ALLOWUNDO``);
  permanent deletion does not exist in this module, in any code path.
* The caller MUST re-verify the on-disk identity (byte size + ``mtime_ns``)
  against the scan record immediately before the shell call (TOCTOU guard);
  a mismatch aborts the operation with ``FILE_IDENTITY_MISMATCH``.
* Every success produces a :class:`RecycleReceipt`; the caller journalizes it
  (JSON lines) under the application cache so the user keeps an audit trail.
* This module never walks directories and never accepts glob patterns: one
  call, one file, decided by a human in the UI.
"""

from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path

from wechat_cleaner.domain.contracts import ContractErrorCode, FileIdentity, MediaRecord

from ..decoder.formats import DecoderFailure
from .locate import resolve_source_path

__all__ = ["RecycleReceipt", "recycle_media_source", "send_to_recycle_bin"]

_FOF_ALLOWUNDO = 0x40
_FOF_NOCONFIRMATION = 0x10
_FOF_SILENT = 0x4
_FOF_NOERRORUI = 0x400
_FO_DELETE = 3


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def send_to_recycle_bin(path: str) -> None:
    """Move one file to the Recycle Bin via the Windows Shell.

    Raises ``OSError`` on any shell failure or user abort.  Non-Windows
    platforms are refused outright: this Beta only ships for Windows.
    """
    if os.name != "nt":
        raise OSError("recycle-bin removal is only implemented for Windows")
    # SHFileOperationW wants a double-NUL-terminated path list.
    buffer = ctypes.create_unicode_buffer(path + "\0")
    operation = _SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = _FO_DELETE
    operation.pFrom = ctypes.cast(buffer, ctypes.c_wchar_p)
    operation.pTo = None
    operation.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    operation.fAnyOperationsAborted = False
    operation.hNameMappings = None
    operation.lpszProgressTitle = None
    code = int(ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation)))
    if code != 0 or operation.fAnyOperationsAborted:
        raise OSError(
            f"shell recycle failed: code={code} aborted={operation.fAnyOperationsAborted}"
        )


def _identity_matches(source: Path, expected: FileIdentity) -> bool:
    try:
        stat = source.stat()
    except OSError:
        return False
    return stat.st_size == expected.byte_size and stat.st_mtime_ns == expected.modified_time_ns


def recycle_media_source(
    account_root: str,
    media: MediaRecord,
    *,
    receipts_path: Path,
) -> RecycleReceipt:
    """Move one scanned media record's source file to the Recycle Bin.

    Re-verifies the on-disk identity against the scan record (TOCTOU guard),
    asks the Shell to recycle the file, and journalizes a receipt.  Raises
    ``DecoderFailure`` with a user-readable message on every refusal.
    """
    source = resolve_source_path(account_root, media.file.relative_path)
    if not source.is_file():
        raise DecoderFailure(
            ContractErrorCode.INVALID_PATH, "源文件已缺失（可能已被移动或删除）"
        )
    if not _identity_matches(source, media.file):
        raise DecoderFailure(
            ContractErrorCode.FILE_IDENTITY_MISMATCH,
            "文件在扫描后发生了变化（大小或修改时间不一致），为安全起见已中止；请重新扫描后再试",
        )
    send_to_recycle_bin(str(source))
    if source.exists():
        raise DecoderFailure(
            ContractErrorCode.DECODE_FAILED, "回收站操作未生效，文件仍在原位；请重试"
        )
    receipt = RecycleReceipt(
        recycled_at=datetime.now(UTC).isoformat(),
        media_id=str(media.media_id),
        relative_path=media.file.relative_path,
        byte_size=media.file.byte_size,
        modified_time_ns=media.file.modified_time_ns,
    )
    receipts_path.parent.mkdir(parents=True, exist_ok=True)
    with open(receipts_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt.to_journal(), ensure_ascii=True) + "\n")
    return receipt


class RecycleReceipt:
    """One recycled file, journaled for the user's audit trail."""

    __slots__ = ("recycled_at", "media_id", "relative_path", "byte_size", "modified_time_ns")

    def __init__(
        self,
        *,
        recycled_at: str,
        media_id: str,
        relative_path: str,
        byte_size: int,
        modified_time_ns: int,
    ) -> None:
        self.recycled_at = recycled_at
        self.media_id = media_id
        self.relative_path = relative_path
        self.byte_size = byte_size
        self.modified_time_ns = modified_time_ns

    def to_journal(self) -> dict:
        return {
            "recycled_at": self.recycled_at,
            "media_id": self.media_id,
            "relative_path": self.relative_path,
            "byte_size": self.byte_size,
            "modified_time_ns": self.modified_time_ns,
        }
