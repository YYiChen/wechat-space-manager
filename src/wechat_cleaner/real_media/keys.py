"""Real image key handling for WeChat 4.x media.

Image keys are machine-confidential: they live in memory only, are never
persisted, and never appear in logs, exceptions or artifacts.  Resolution order
mirrors the upstream adapter's documented preference — derive deterministically
from ``cfgDword`` first, since the process memory scan is the weakest path and
generally fails on WeChat 4.1.x.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from wechat_cleaner.domain.contracts import ContractErrorCode

from ..decoder.formats import DecoderFailure

__all__ = ["RealImageKeys", "derive_image_keys", "require_upstream"]

JPEG_LIKE_PREFIXES = (b"\xff\xd8\xff", b"\x89PNG", b"GIF87a", b"GIF89a", b"BM")


def require_upstream() -> tuple[Any, Any]:
    """Import the optional upstream backend lazily; degrade cleanly if absent."""
    try:
        from wechatauto.db import WeChatDB  # noqa: PLC0415
        from wechatauto.media import MediaDownloader  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise DecoderFailure(
            ContractErrorCode.DECODER_UNAVAILABLE,
            "the optional real-media backend is not installed",
        ) from exc
    return WeChatDB, MediaDownloader


def derive_image_keys(cfg_dword: int, wxid: str) -> tuple[str, int]:
    """Deterministic derivation documented by the upstream adapter.

    ``imageAesKey = MD5(str(cfgDword) + wxid)[:16]`` and ``xorKey = cfgDword & 0xFF``.
    """
    if not cfg_dword:
        raise DecoderFailure(ContractErrorCode.DECODER_UNAVAILABLE, "cfgDword is unavailable")
    aes_key = hashlib.md5(f"{cfg_dword}{wxid}".encode()).hexdigest()[:16]
    return aes_key, cfg_dword & 0xFF


def validate_aes_key(aes_key: str, probe_ciphertext: bytes) -> bool:
    """Validate a candidate AES key against a real ciphertext probe block."""
    if not probe_ciphertext:
        return False
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        decryptor = Cipher(algorithms.AES(aes_key.encode()), modes.ECB()).decryptor()
        plain_head = decryptor.update(probe_ciphertext) + decryptor.finalize()
    except Exception:  # noqa: BLE001 - any crypto failure means "not this key"
        return False
    return any(plain_head.startswith(prefix) for prefix in JPEG_LIKE_PREFIXES)


@dataclass(frozen=True, slots=True)
class RealImageKeys:
    """In-memory image keys; supports explicit zeroization and masked repr."""

    aes_key: str
    xor_key: int

    def __post_init__(self) -> None:
        if len(self.aes_key) != 16:
            raise ValueError("image AES key must be 16 characters")

    @classmethod
    def from_cfg_dword(cls, cfg_dword: int, wxid: str) -> RealImageKeys:
        aes_key, xor_key = derive_image_keys(cfg_dword, wxid)
        return cls(aes_key=aes_key, xor_key=xor_key)

    def __repr__(self) -> str:
        return "RealImageKeys(aes=***, xor=***)"

    __str__ = __repr__

    def clear(self) -> None:
        """Drop the key material reference (strings are immutable; helper for parity)."""
        object.__setattr__(self, "aes_key", "")
        object.__setattr__(self, "xor_key", 0)

    @property
    def is_cleared(self) -> bool:
        return not self.aes_key


def open_source_session(
    *,
    db_root: str,
    workdir: str,
    account: str | None = None,
    log_sink: Callable[[str], None] | None = None,
) -> Any:
    """Open a read-only upstream ``WeChatDB`` session against the real source.

    ``workdir`` must be inside the application cache root: the upstream adapter
    writes its database key cache there.  Upstream console chatter is captured so
    account identifiers never reach the caller's console or logs.
    """
    WeChatDB, _ = require_upstream()
    os.makedirs(workdir, exist_ok=True)
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink):
            return WeChatDB(db_dir=db_root, workdir=workdir, account=account)
    finally:
        if log_sink is not None:
            log_sink(sink.getvalue())
