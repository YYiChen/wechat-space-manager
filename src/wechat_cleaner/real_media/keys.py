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

# Upstream 1.3.x mirrors its key cache into this directory when it can find one.
_KEYS_DIR_ENV = "WECHATAUTO_KEYS_DIR"

# Upstream 1.3.x account self-heal hooks (see _account_pinned_session_class).
_HEAL_HOOKS = ("_try_other_accounts", "_select_account_by_keys", "_all_key_candidates")
_PINNED_SESSION_CLASSES: dict[type, type] = {}


def require_upstream() -> tuple[Any, Any]:
    """Import the optional upstream backend lazily; degrade cleanly if absent.

    Upstream 1.3.x brings two side effects this project must not inherit:

    * ``wechatauto.logger`` creates ``wechatauto_logs/app_<date>.log`` in the
      current working directory on the first log call, and its records carry
      account identifiers and absolute database paths — R2 forbids private data
      in logs, so file logging is switched off before the first import;
    * ``WeChatDB._save_keys`` mirrors the key cache into a user-level directory
      (see :func:`open_source_session`, which redirects it into the cache root).
    """
    try:
        from wechatauto.param import WxParam  # noqa: PLC0415
        from wechatauto.db import WeChatDB  # noqa: PLC0415
        from wechatauto.media import MediaDownloader  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise DecoderFailure(
            ContractErrorCode.DECODER_UNAVAILABLE,
            "the optional real-media backend is not installed",
        ) from exc
    WxParam.ENABLE_FILE_LOGGER = False
    return WeChatDB, MediaDownloader


def derive_image_keys(cfg_dword: int, wxid: str) -> tuple[str, int]:
    """Deterministic derivation documented by the upstream adapter.

    ``imageAesKey = MD5(str(cfgDword) + wxid)[:16]`` and ``xorKey = cfgDword & 0xFF``.
    """
    if not cfg_dword:
        raise DecoderFailure(ContractErrorCode.DECODER_UNAVAILABLE, "cfgDword is unavailable")
    aes_key = hashlib.md5(f"{cfg_dword}{wxid}".encode()).hexdigest()[:16]
    return aes_key, cfg_dword & 0xFF


def _account_pinned_session_class(base: type) -> type:
    """Return a ``WeChatDB`` variant that skips upstream's account self-heal.

    Upstream 1.3.x re-derives keys for *every* account directory whenever a
    single database stays keyless.  Real accounts always carry structurally
    keyless databases (``biz``, ``fmessage_new``, ``wcfinder``, ``websearch`` …),
    so the heal runs on every construction: measured on a 35-database account,
    35.4 s per connect of which 26.2 s was 8252 PBKDF2 derivations, versus 10.0 s
    with the heal disabled.

    The heal exists to recover from a *wrong account guess*.  This project
    always passes the account directory the user picked and validates every
    database by page-1 HMAC, so with an explicit account the search only costs
    latency.  Unknown upstream pins keep the plain class, and the override is a
    no-op if any hook disappears.
    """
    cached = _PINNED_SESSION_CLASSES.get(base)
    if cached is not None:
        return cached
    if not all(hasattr(base, hook) for hook in _HEAL_HOOKS):
        _PINNED_SESSION_CLASSES[base] = base
        return base

    class AccountPinned(base):  # type: ignore[misc, valid-type]
        """WeChatDB that trusts the caller's account instead of guessing."""

        def _try_other_accounts(self) -> bool:
            return False

        def _select_account_by_keys(self, candidates: Any) -> bool:
            return False

        def _all_key_candidates(self) -> set[Any]:
            return set()

    _PINNED_SESSION_CLASSES[base] = AccountPinned
    return AccountPinned


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
    session_class = WeChatDB if account is None else _account_pinned_session_class(WeChatDB)
    # 1.3.x mirrors keys.json into %LOCALAPPDATA%\wechatauto_keys unless this is
    # set.  Keep every cache artifact inside the app-owned root so "clear cache"
    # really clears it, and restore the caller's environment afterwards.
    previous_keys_dir = os.environ.get(_KEYS_DIR_ENV)
    os.environ[_KEYS_DIR_ENV] = os.path.join(workdir, "wechatauto-keys")
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink):
            return session_class(db_dir=db_root, workdir=workdir, account=account)
    finally:
        if previous_keys_dir is None:
            os.environ.pop(_KEYS_DIR_ENV, None)
        else:
            os.environ[_KEYS_DIR_ENV] = previous_keys_dir
        if log_sink is not None:
            log_sink(sink.getvalue())
