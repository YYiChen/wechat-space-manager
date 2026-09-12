"""Decode pipeline: approved copy → verified pixels → bounded cache artifact.

Safety invariants (R2, see ``AGENTS.md``):

* inputs are *copies* staged below ``copy_root`` — the decoder never touches a
  live WeChat directory;
* the on-disk identity of the copy is re-checked against ``DecodeRequest``
  before decoding (size, mtime, optional sha256);
* key material exists only in memory, is zeroized after use, and is asserted
  absent from every persisted byte;
* cache output is size-limited, session-scoped, and path-contained;
* all failures surface as :class:`DecodeError` wrapping a contract
  ``ContractError`` whose messages never embed absolute paths.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from wechat_cleaner.domain.contracts import (
    ContractError,
    ContractErrorCode,
    DecodeArtifact,
    DecodeRequest,
    DecodeVariant,
    FileIdentity,
    MediaType,
)

from .cache import (
    atomic_write,
    ensure_within_cache,
    output_stem,
    purge_expired,
    session_dir,
    usage_bytes,
)
from .formats import IMAGE_MAGICS, DecoderFailure, _decode_plain, _decode_xor
from .key_material import KeyProvider, acquire_key

__all__ = ["DecodeError", "DecoderLimits", "decode"]

_CACHE_IMAGE_FORMAT = "PNG"
_CACHE_IMAGE_EXT = ".png"


class DecodeError(Exception):
    """Pipeline failure carrying the contract error for callers (e.g. UI)."""

    def __init__(self, error: ContractError) -> None:
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True, slots=True)
class DecoderLimits:
    """Module-level cache ceilings; enforced before and after encoding."""

    max_total_cache_bytes: int = 512 * 1024 * 1024
    max_single_output_bytes: int = 64 * 1024 * 1024


def _fail(code: ContractErrorCode, message: str, *, retryable: bool = False) -> DecodeError:
    return DecodeError(ContractError(code=code, message=message, retryable=retryable))


def _resolve_copy(copy_root: str | Path, relative_path: str) -> Path:
    root = Path(copy_root).resolve()
    candidate = ensure_within_cache(root, str(root / relative_path))
    return candidate


def _recheck_identity(source: Path, request: DecodeRequest) -> None:
    identity = request.media.file
    if not source.is_file():
        raise _fail(ContractErrorCode.FILE_IDENTITY_MISMATCH, "approved copy is missing")
    stat = source.stat()
    if stat.st_size != identity.byte_size:
        raise _fail(ContractErrorCode.FILE_IDENTITY_MISMATCH, "approved copy size mismatch")
    if stat.st_mtime_ns != identity.modified_time_ns:
        raise _fail(ContractErrorCode.FILE_IDENTITY_MISMATCH, "approved copy mtime mismatch")
    if identity.sha256 is not None:
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != identity.sha256:
            raise _fail(ContractErrorCode.FILE_IDENTITY_MISMATCH, "approved copy sha256 mismatch")


def _looks_plain(head: bytes) -> bool:
    return any(head.startswith(magic) for magic, _fmt, _label in IMAGE_MAGICS)


def decode(
    request: DecodeRequest,
    *,
    copy_root: str,
    key_provider: KeyProvider | None = None,
    limits: DecoderLimits | None = None,
    now: datetime | None = None,
    cache_session_id: uuid.UUID | None = None,
) -> DecodeArtifact:
    """Decode an approved media copy into the bounded cache.

    ``copy_root`` must point at a staging tree of copies maintained by the
    orchestration layer; relative paths inside ``request`` resolve against it.
    ``cache_session_id`` defaults to a fresh session (one cache subdirectory
    per decode call) and may be supplied by the caller to group outputs.
    """
    limits = limits or DecoderLimits()
    moment = now or datetime.now(UTC)
    session_id = cache_session_id or new_cache_session()

    if request.media.media_type not in {MediaType.IMAGE, MediaType.THUMBNAIL}:
        raise _fail(
            ContractErrorCode.DECODER_UNAVAILABLE,
            f"media_type {request.media.media_type.value} is not decodable in this version",
        )

    source = _resolve_copy(copy_root, request.media.file.relative_path)
    _recheck_identity(source, request)

    cache_root = Path(request.cache_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    purge_expired(cache_root, moment)

    key_material, owned_key = acquire_key(key_provider)
    try:
        payload = source.read_bytes()
        head = payload[:16]

        decoded = None
        if _looks_plain(head):
            decoded = _decode_plain(payload)
        else:
            try:
                decoded = _decode_xor(payload, key_material.expose() if key_material else None)
            except DecoderFailure as exc:
                if key_material is None and exc.code is ContractErrorCode.DECODE_FAILED:
                    raise DecoderFailure(
                        ContractErrorCode.DECODER_UNAVAILABLE,
                        "encrypted payload requires key material",
                    ) from exc
                raise
        del payload

        image = decoded.image
        if request.variant is DecodeVariant.THUMBNAIL:
            edge = request.max_edge_px
            if edge is not None:
                image.thumbnail((edge, edge))

        buffer = io.BytesIO()
        image.save(buffer, format=_CACHE_IMAGE_FORMAT)
        encoded = buffer.getvalue()
        decoder_id = decoded.decoder_id
        del image, decoded  # drop pixel buffers promptly; payload already released
        if len(encoded) > limits.max_single_output_bytes:
            raise _fail(ContractErrorCode.CACHE_LIMIT_EXCEEDED, "single output exceeds cache limit")

        target_dir = session_dir(cache_root, session_id)
        ensure_within_cache(cache_root, str(target_dir))
        stem = output_stem(request.media.media_id, request.variant)
        target = target_dir / f"{stem}{_CACHE_IMAGE_EXT}"

        if usage_bytes(cache_root) + len(encoded) > limits.max_total_cache_bytes:
            raise _fail(
                ContractErrorCode.CACHE_LIMIT_EXCEEDED, "cache total limit would be exceeded"
            )

        expires_at = request.retain_until
        artifact = DecodeArtifact(
            request_id=request.request_id,
            media_id=request.media.media_id,
            created_at=moment,
            cache_session_id=session_id,
            variant=request.variant,
            output_file=FileIdentity(
                relative_path=target.relative_to(cache_root.resolve()).as_posix(),
                byte_size=len(encoded),
                modified_time_ns=0,
            ),
            output_format=_CACHE_IMAGE_FORMAT,
            decoder_id=decoder_id,
            expires_at=expires_at,
        )

        sidecar = artifact.model_dump_json().encode("utf-8")
        if key_material is not None:
            secret = key_material.expose()
            if secret and (secret in encoded or secret.hex().encode() in sidecar):
                raise _fail(ContractErrorCode.DECODE_FAILED, "key leak detected; aborting write")

        atomic_write(target, encoded)
        stat = target.stat()
        artifact = artifact.model_copy(
            update={
                "output_file": artifact.output_file.model_copy(
                    update={"modified_time_ns": stat.st_mtime_ns}
                )
            }
        )
        sidecar = artifact.model_dump_json().encode("utf-8")
        atomic_write(target.with_suffix(_CACHE_IMAGE_EXT + ".artifact.json"), sidecar)
        return artifact
    except DecoderFailure as failure:
        raise DecodeError(
            ContractError(code=failure.code, message=failure.message, retryable=failure.retryable)
        ) from failure
    finally:
        if owned_key and key_material is not None:
            key_material.clear()


def new_cache_session() -> uuid.UUID:
    return uuid.uuid4()
