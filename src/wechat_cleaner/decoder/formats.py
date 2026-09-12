"""Format probing and decoding adapters.

Adapters are original to this project (see ``docs/reuse-boundary.md``: no
third-party decoder source is vendored).  The first version covers:

* plain images readable by Pillow (JPEG/PNG/GIF/BMP/WEBP),
* XOR-obfuscated image blobs such as legacy WeChat ``*.dat`` attachments:
  single-byte keys are auto-derived from known magic numbers, explicit
  multi-byte keys are supplied in memory by the caller and applied as a
  repeating-key XOR.

Real WeChat 4.x image formats are intentionally *not* claimed as supported
until validated against an approved private fixture; unknown payloads raise
``DECODER_UNAVAILABLE``/``DECODE_FAILED`` instead of guessing.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from wechat_cleaner.domain.contracts import ContractErrorCode

__all__ = [
    "DecoderFailure",
    "DecodedImage",
    "IMAGE_MAGICS",
    "plain_image_adapter",
    "xor_dat_adapter",
]


class DecoderFailure(Exception):
    """Decoder-side failure carrying a contract error code."""

    def __init__(self, code: ContractErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class DecodedImage:
    """A verified, in-memory decoded image plus provenance for the artifact."""

    image: object  # PIL.Image.Image; typed loosely to keep Pillow a lazy import
    decoder_id: str
    was_encrypted: bool


IMAGE_MAGICS: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png", "xor-png"),
    (b"\xff\xd8\xff", "jpeg", "xor-jpeg"),
    (b"GIF87a", "gif", "xor-gif"),
    (b"GIF89a", "gif", "xor-gif"),
    (b"BM", "bmp", "xor-bmp"),
)


def _require_pillow():
    try:
        from PIL import Image  # noqa: PLC0415 - lazy import keeps Pillow optional
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise DecoderFailure(
            ContractErrorCode.DECODER_UNAVAILABLE,
            "Pillow is not installed; image decoding is unavailable",
        ) from exc
    return Image


def _load_verified(payload: bytes) -> object:
    """Open, integrity-verify and fully load an image from bytes."""
    image_mod = _require_pillow()
    with image_mod.open(io.BytesIO(payload)) as probe:
        probe.verify()
    with image_mod.open(io.BytesIO(payload)) as reloaded:
        reloaded.load()
        return reloaded.copy()


def _decode_plain(payload: bytes) -> DecodedImage:
    try:
        return DecodedImage(
            image=_load_verified(payload), decoder_id="plain-image", was_encrypted=False
        )
    except DecoderFailure:
        raise
    except Exception as exc:
        raise DecoderFailure(
            ContractErrorCode.DECODE_FAILED,
            f"plain image decoding failed: {exc.__class__.__name__}",
        ) from exc


def _decode_xor(payload: bytes, key: bytes | None) -> DecodedImage:
    if not payload:
        raise DecoderFailure(ContractErrorCode.DECODE_FAILED, "empty media payload")

    if key is not None:
        if not key:
            raise DecoderFailure(ContractErrorCode.DECODE_FAILED, "empty key material")
        decrypted = bytes(
            value ^ key[index % len(key)] for index, value in enumerate(payload)
        )
        for magic, _fmt, _label in IMAGE_MAGICS:
            if decrypted.startswith(magic):
                try:
                    return DecodedImage(
                        image=_load_verified(decrypted), decoder_id=_label, was_encrypted=True
                    )
                except DecoderFailure as exc:
                    raise DecoderFailure(
                        ContractErrorCode.DECODE_FAILED,
                        "keyed XOR decryption did not yield a valid image",
                    ) from exc
        raise DecoderFailure(
            ContractErrorCode.DECODE_FAILED,
            "supplied key did not produce a recognized image format",
        )

    for magic, _fmt, _label in IMAGE_MAGICS:
        if len(payload) < len(magic):
            continue
        candidate = payload[0] ^ magic[0]
        if bytes(value ^ candidate for value in payload[: len(magic)]) == magic:
            decrypted = bytes(value ^ candidate for value in payload)
            try:
                return DecodedImage(
                    image=_load_verified(decrypted), decoder_id=_label, was_encrypted=True
                )
            except DecoderFailure as exc:
                raise DecoderFailure(
                    ContractErrorCode.DECODE_FAILED,
                    "XOR-obfuscated payload did not yield a valid image",
                ) from exc

    raise DecoderFailure(
        ContractErrorCode.DECODER_UNAVAILABLE,
        "payload matches no known container; an explicit key may be required",
    )


def plain_image_adapter(payload: bytes) -> DecodedImage:
    """Decode an unencrypted image payload."""
    return _decode_plain(payload)


def xor_dat_adapter(payload: bytes, key: bytes | None) -> DecodedImage:
    """Decode an XOR-obfuscated image payload with optional in-memory key."""
    return _decode_xor(payload, key)
