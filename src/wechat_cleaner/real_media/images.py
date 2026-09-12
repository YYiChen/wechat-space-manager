"""Real media image decoding: encrypted ``.dat`` to verified pixels.

Decryption is delegated to the upstream adapter (already validated against real
WeChat 4.1.13.12 payloads); this module owns format detection, pixel
verification and the thumbnail-only distinction so the rest of the application
never touches raw upstream APIs.
"""

from __future__ import annotations

import contextlib
import io
import struct
from enum import StrEnum

from wechat_cleaner.domain.contracts import ContractErrorCode

from ..decoder.formats import DecoderFailure

__all__ = [
    "MediaFormat",
    "DecodedPixels",
    "detect_format",
    "decode_image_file",
    "decode_image_bytes",
    "self_decode_file",
    "verify_pixels",
]

V1_MAGIC = b"\x07\x08\x05\x56\x02\x05"
V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"
V1_HEADER_SIZE = 22
V2_PROBE_SLICE = slice(15, 31)

# Probed against real WeChat 4.1.13.12 data (scripts/probe_original_container.py)
# and confirmed against the upstream adapter's own ``_decrypt_v2``: a V2
# container is THREE segments behind the 15-byte header
# (magic 6B + aes_size LE32 + xor_size LE32 + 1B reserved):
#
#   [AES-ECB(imageKey) of ``aes_size`` bytes, aligned up to 16]  -- JPEG head
#   [plaintext body]                                             -- image body
#   [``xor_size`` bytes XORed with xorKey]                       -- tail
#
# Large files keep the AES segment small (e.g. 1 KiB) while megabytes of body
# sit in the clear and the last megabyte is XOR-only - which is why naive
# whole-file ECB produced "headers decode, body is garbage".
V2_HEADER_SIZE = 15
_XOR_SCAN_LIMIT = 64
# Body segments are passed through verbatim (no per-byte crypto), so reading
# whole large files is cheap; the cap only guards against absurd inputs.
_SELF_DECODE_MAX_BYTES = 256 * 1024 * 1024
IMAGE_MAGICS = (b"\xff\xd8\xff", b"\x89PNG", b"GIF87a", b"GIF89a", b"BM")


class MediaFormat(StrEnum):
    V1 = "v1"
    V2 = "v2"
    UNKNOWN = "unknown"


class DecodedPixels:
    """Verified pixel payload plus its container facts."""

    __slots__ = ("payload", "image_format", "width", "height", "container")

    def __init__(
        self,
        payload: bytes,
        image_format: str,
        width: int,
        height: int,
        container: MediaFormat,
    ) -> None:
        self.payload = payload
        self.image_format = image_format
        self.width = width
        self.height = height
        self.container = container

    def __repr__(self) -> str:
        return (
            f"DecodedPixels(format={self.image_format!r}, size={self.width}x{self.height}, "
            f"container={self.container.value!r}, bytes={len(self.payload)})"
        )


def detect_format(header: bytes) -> MediaFormat:
    if header.startswith(V2_MAGIC):
        return MediaFormat.V2
    if header.startswith(V1_MAGIC):
        return MediaFormat.V1
    return MediaFormat.UNKNOWN


def probe_ciphertext(header: bytes, container: MediaFormat) -> bytes:
    """Extract the AES ciphertext probe block used to validate a candidate key."""
    if container is MediaFormat.V2:
        return header[V2_PROBE_SLICE]
    if container is MediaFormat.V1:
        return header[V1_HEADER_SIZE : V1_HEADER_SIZE + 16]
    return b""


def decode_image_file(
    dat_path: str,
    *,
    downloader,
    aes_key: str | None = None,
    xor_key: int | None = None,
) -> tuple[bytes, MediaFormat]:
    """Decrypt one encrypted media file into raw image bytes.

    The upstream adapter is authoritative for signed containers (V1/V2, which
    includes originals, ``_h.dat`` and ``_t.dat``); unsigned XOR-only families
    (``_t_W.dat``, ``_W.dat``) and any upstream failure fall through to the
    probed self-decode paths.
    """
    payload, container, _is_original = decode_image_bytes(
        dat_path, downloader=downloader, aes_key=aes_key, xor_key=xor_key
    )
    return payload, container


def decode_image_bytes(
    dat_path: str,
    *,
    downloader,
    aes_key: str | None = None,
    xor_key: int | None = None,
) -> tuple[bytes, MediaFormat, bool]:
    """Like :func:`decode_image_file` but also reports payload fidelity.

    The third element is ``True`` when the payload is the decrypted ORIGINAL
    image itself (full-body decode succeeded).  ``False`` means the body was
    damaged/truncated and the payload degraded to the EXIF-embedded fallback —
    honest reporting matters for "export original": the user must know when a
    preview-quality stand-in is written instead of the true original.
    """
    container = MediaFormat.UNKNOWN
    with open(dat_path, "rb") as handle:
        container = detect_format(handle.read(6))
    upstream_eligible = container is not MediaFormat.UNKNOWN
    payload = None
    upstream_failure: DecoderFailure | None = None
    if upstream_eligible:
        sink = io.StringIO()
        try:
            with contextlib.redirect_stdout(sink):
                payload = downloader.decrypt_image(dat_path, aes_key=aes_key, xor_key=xor_key)
        except DecoderFailure as exc:
            upstream_failure = exc
        except Exception:  # noqa: BLE001 - same fallthrough as before
            payload = None

    if payload:
        # Uniform usability gate for upstream AND self-decoded payloads: a
        # perfect header over a damaged body must degrade to the EXIF thumb.
        usable, is_original = _usable_image_or_embedded(payload)
        return usable, container, is_original

    self_payload = self_decode_file(dat_path, aes_key=aes_key, xor_key=xor_key)
    if self_payload is not None:
        usable, is_original = _usable_image_or_embedded(self_payload[0])
        return usable, self_payload[1], is_original
    if upstream_failure is not None:
        raise upstream_failure
    raise DecoderFailure(
        ContractErrorCode.DECODE_FAILED,
        "no decodable container matched this media file",
    )


def self_decode_file(
    dat_path: str,
    *,
    aes_key: str | None = None,
    xor_key: int | None = None,
) -> tuple[bytes, MediaFormat] | None:
    """Best-effort decode for container families the upstream adapter misses.

    Returns ``(payload, container)`` on success or ``None`` when neither the
    XOR-only nor the V2 three-segment path matches.  The V2 path decrypts only
    the small AES header segment (KB-scale) - the body is plaintext and the
    XOR tail is a byte-wise translate, so even 50 MB files decode fast.
    """
    try:
        with open(dat_path, "rb") as handle:
            data = handle.read(_SELF_DECODE_MAX_BYTES)
    except OSError as exc:
        raise DecoderFailure(
            ContractErrorCode.DECODE_FAILED, f"source media could not be read: {type(exc).__name__}"
        ) from exc
    if len(data) < 16:
        return None
    container = detect_format(data[:6])
    if aes_key and container is not MediaFormat.UNKNOWN:
        payload = _self_decode_v2(data, str(aes_key), int(xor_key) if xor_key is not None else None)
        if payload is not None:
            return payload, container
    if xor_key is not None:
        payload = _self_decode_xor(data, int(xor_key))
        if payload is not None:
            return payload, MediaFormat.UNKNOWN
    if aes_key and container is MediaFormat.UNKNOWN:
        # V2-shaped header with a scrambled magic still gets one structured try.
        payload = _self_decode_v2(data, str(aes_key), int(xor_key) if xor_key is not None else None)
        if payload is not None:
            return payload, container
    return None


def _xor_bytes(data: bytes, xor_key: int) -> bytes:
    return data.translate(bytes(byte ^ xor_key for byte in range(256)))


def _self_decode_xor(data: bytes, xor_key: int) -> bytes | None:
    """XOR-only family: scan a few header offsets for a magic after XOR."""
    table = bytes(byte ^ xor_key for byte in range(256))
    scan_end = min(_XOR_SCAN_LIMIT, len(data) - 4)
    for off in range(scan_end + 1):
        window = data[off : off + 4].translate(table)
        if any(window.startswith(magic) for magic in IMAGE_MAGICS):
            return data[off:].translate(table)
    return None


def _self_decode_v2(data: bytes, aes_key: str, xor_key: int | None) -> bytes | None:
    """Three-segment V2: AES head + plaintext body + XOR tail.

    Segment sizes come from the header itself (``aes_size``/``xor_size``,
    little-endian uint32 at offsets 6 and 10), matching the upstream
    adapter's ``_decrypt_v2``.  PKCS#7 padding is stripped only from the AES
    segment; the body and XOR tail are passed through verbatim.
    """
    if len(data) < V2_HEADER_SIZE:
        return None
    aes_size, xor_size = struct.unpack_from("<LL", data, 6)
    aes_blk = (aes_size + 15) // 16 * 16
    aes_ct = data[V2_HEADER_SIZE : V2_HEADER_SIZE + aes_blk]
    if len(aes_ct) < 16:
        return None
    try:
        plain_head = _aes_ecb_decrypt(aes_key, aes_ct)
    except Exception:  # noqa: BLE001 - wrong key length etc. means "not this path"
        return None
    plain_head = _strip_pkcs7(plain_head)
    rest = data[V2_HEADER_SIZE + len(aes_ct) :]
    if xor_size and len(rest) > xor_size:
        body = rest[:-xor_size]
        tail = _xor_bytes(rest[-xor_size:], xor_key) if xor_key is not None else rest[-xor_size:]
    else:
        body = rest
        tail = b""
    return plain_head + body + tail


def _usable_image_or_embedded(plain: bytes) -> tuple[bytes, bool]:
    """Return ``(payload, is_original)``; Pillow must FULLY decode ``plain``.

    ``verify()`` alone is not enough: large originals can carry a perfect
    header while the entropy body is truncated or damaged mid-stream (WeChat
    streams downloads; files may be incomplete), which surfaces as an OSError
    at ``load()`` time.  Decoding is therefore the usability test, and the
    EXIF-embedded JPEG - which lives in the always-intact header segment - is
    the honest fallback preview.  ``is_original`` is ``False`` exactly when the
    returned bytes are that fallback (or, as a last resort, the raw undecodable
    payload which the caller's pixel gate will reject).
    """
    try:
        from PIL import Image  # noqa: PLC0415

        _relax_pillow_bomb_guard()
        with Image.open(io.BytesIO(plain)) as probe:
            probe.load()
        return plain, True
    except Exception:  # noqa: BLE001 - fall through to the embedded image
        embedded = _embedded_image(plain)
        if embedded is not None:
            return embedded, False
        return plain, False


def _embedded_image(plain: bytes) -> bytes | None:
    """Second-chance decode: search for an EXIF-embedded JPEG in ``plain``."""
    search_from = 2
    while True:
        second = plain.find(b"\xff\xd8\xff", search_from)
        if second < 0:
            return None
        candidate = plain[second:]
        try:
            from PIL import Image  # noqa: PLC0415

            with Image.open(io.BytesIO(candidate)) as probe:  # noqa: SIM117
                probe.verify()
            with Image.open(io.BytesIO(candidate)) as image:
                image.load()
            return candidate
        except Exception:  # noqa: BLE001 - try the next SOI occurrence
            search_from = second + 3


def _strip_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data.endswith(bytes([pad]) * pad):
        return data[:-pad]
    return data


def _relax_pillow_bomb_guard() -> None:
    """Decode local media without Pillow's decompression-bomb pixel limit.

    ``MAX_IMAGE_PIXELS`` protects against hostile downloads; this pipeline's
    inputs are on-disk WeChat media files already capped by
    ``_SELF_DECODE_MAX_BYTES`` (256 MiB), and real accounts legitimately hold
    panorama/stitched-screenshot images beyond the 179-megapixel hard limit
    (probed: a healthy 11225x16006 PNG, 99 MB, was rejected at ``Image.open``
    and surfaced as "decrypted payload is not a valid image").  Preview memory
    is bounded by the source-size cap instead of the pixel count.
    """
    from PIL import Image  # noqa: PLC0415

    Image.MAX_IMAGE_PIXELS = None


def _aes_ecb_decrypt(aes_key: str, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: PLC0415

    decryptor = Cipher(algorithms.AES(aes_key.encode()), modes.ECB()).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def verify_pixels(payload: bytes, *, container: MediaFormat = MediaFormat.UNKNOWN) -> DecodedPixels:
    """Open the decrypted payload with Pillow and confirm real pixel data.

    Truncated-image loading is enabled because prefix-decoded payloads (the
    4 MiB self-decode cap) intentionally omit the tail; Pillow then renders
    the available rows instead of failing the whole preview.
    """
    try:
        from PIL import Image, ImageFile  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise DecoderFailure(
            ContractErrorCode.DECODER_UNAVAILABLE, "Pillow is not installed"
        ) from exc

    _relax_pillow_bomb_guard()
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        with Image.open(io.BytesIO(payload)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            width, height = image.size
            image_format = (image.format or "UNKNOWN").upper()
    except DecoderFailure:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DecoderFailure(
            ContractErrorCode.DECODE_FAILED,
            f"decrypted payload is not a valid image: {type(exc).__name__}",
        ) from exc

    if width <= 0 or height <= 0:
        raise DecoderFailure(ContractErrorCode.DECODE_FAILED, "decoded image has no pixels")
    return DecodedPixels(
        payload=payload,
        image_format=image_format,
        width=width,
        height=height,
        container=container,
    )
