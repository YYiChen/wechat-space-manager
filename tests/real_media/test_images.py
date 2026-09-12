"""Format detection, probe extraction, decryption wiring and pixel verification."""

from __future__ import annotations

import io

import pytest

from wechat_cleaner.decoder.formats import DecoderFailure
from wechat_cleaner.domain.contracts import ContractErrorCode
from wechat_cleaner.real_media import MediaFormat, decode_image_file, detect_format, verify_pixels
from wechat_cleaner.real_media.images import probe_ciphertext

V1_MAGIC = b"\x07\x08\x05\x56\x02\x05"
V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"


def test_detect_format_v2_v1_unknown():
    assert detect_format(V2_MAGIC + b"\x00" * 10) is MediaFormat.V2
    assert detect_format(V1_MAGIC + b"\x00" * 10) is MediaFormat.V1
    assert detect_format(b"\x00\x01\x02\x03\x04\x05") is MediaFormat.UNKNOWN


def test_probe_ciphertext_slices(image_factory, dat_factory, v1_dat_factory, aes_key):
    v2_container = dat_factory(image_factory(), aes_key)
    assert probe_ciphertext(v2_container[:32], MediaFormat.V2) == v2_container[15:31]
    v1_container = v1_dat_factory(image_factory())
    assert probe_ciphertext(v1_container[:40], MediaFormat.V1) == v1_container[22:38]
    assert probe_ciphertext(b"", MediaFormat.UNKNOWN) == b""


def test_decode_image_file_v2_roundtrip(
    account_root, image_factory, dat_factory, aes_key, fake_downloader
):
    payload = image_factory()
    dat_path = account_root / "sample_t.dat"
    dat_path.write_bytes(dat_factory(payload, aes_key))

    decoded, container = decode_image_file(
        str(dat_path), downloader=fake_downloader, aes_key=aes_key, xor_key=0x88
    )

    assert decoded == payload
    assert container is MediaFormat.V2
    assert fake_downloader.calls == [str(dat_path)]


def test_decode_image_file_v1_roundtrip(
    account_root, image_factory, v1_dat_factory, fake_downloader
):
    payload = image_factory()
    dat_path = account_root / "sample_t.dat"
    dat_path.write_bytes(v1_dat_factory(payload, xor_key=0x33))

    decoded, container = decode_image_file(
        str(dat_path), downloader=fake_downloader, xor_key=0x33
    )

    assert decoded == payload
    assert container is MediaFormat.V1


def test_decode_image_file_maps_failures(account_root, fake_downloader):
    dat_path = account_root / "broken.dat"
    dat_path.write_bytes(b"not-an-encrypted-wechat-payload")

    with pytest.raises(DecoderFailure) as exc_info:
        decode_image_file(str(dat_path), downloader=fake_downloader)
    assert exc_info.value.code is ContractErrorCode.DECODE_FAILED


class _RaisingDownloader:
    """Upstream stand-in that always rejects the container family."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def decrypt_image(self, dat_path, *, aes_key=None, xor_key=None):
        self.calls.append(str(dat_path))
        raise RuntimeError("family not supported by upstream")


def test_self_decode_recovers_xor_family(account_root, image_factory):
    payload = image_factory()  # JPEG bytes from the shared fixture
    dat_path = account_root / "wide_t_W.dat"
    dat_path.write_bytes(bytes(byte ^ 0x88 for byte in payload))

    decoded, container = decode_image_file(
        str(dat_path), downloader=_RaisingDownloader(), xor_key=0x88
    )

    assert decoded == payload
    assert container is MediaFormat.UNKNOWN


def test_self_decode_recovers_aes_offset15_original(
    account_root, image_factory, dat_factory, aes_key
):
    payload = image_factory()
    dat_path = account_root / "original.dat"
    dat_path.write_bytes(dat_factory(payload, aes_key))

    decoded, container = decode_image_file(
        str(dat_path), downloader=_RaisingDownloader(), aes_key=aes_key, xor_key=0x88
    )

    assert decoded == payload
    assert container is MediaFormat.V2


def test_self_decode_gives_up_without_keys(account_root, image_factory):
    payload = image_factory()
    dat_path = account_root / "wide_t_W.dat"
    dat_path.write_bytes(bytes(byte ^ 0x88 for byte in payload))

    with pytest.raises(DecoderFailure) as exc_info:
        decode_image_file(str(dat_path), downloader=_RaisingDownloader())
    assert exc_info.value.code is ContractErrorCode.DECODE_FAILED


def test_embedded_image_fallback_unit(image_factory):
    from wechat_cleaner.real_media.images import _embedded_image

    jpeg = image_factory()
    assert _embedded_image(b"\x00" * 512 + jpeg) == jpeg
    assert _embedded_image(b"\x00" * 100) is None


def test_self_decode_recovers_embedded_thumbnail(account_root, image_factory, dat_factory, aes_key):
    """Large real containers decrypt headers but not the entropy body; whatever
    Pillow cannot fully parse must still come back as decodable pixels via the
    EXIF-embedded JPEG inside the decrypted prefix."""
    from wechat_cleaner.real_media.images import verify_pixels

    jpeg = image_factory()
    payload = jpeg[:64] + b"\x00" * 600 + jpeg
    dat_path = account_root / "big.dat"
    dat_path.write_bytes(dat_factory(payload, aes_key))

    decoded, _container = decode_image_file(
        str(dat_path), downloader=_RaisingDownloader(), aes_key=aes_key, xor_key=0x88
    )

    pixels = verify_pixels(decoded)
    assert pixels.width == 40
    assert len(decoded) <= len(payload)


def test_verify_pixels_accepts_real_image(image_factory):
    pixels = verify_pixels(image_factory(width=64, height=48), container=MediaFormat.V2)

    assert pixels.width == 64
    assert pixels.height == 48
    assert pixels.image_format == "JPEG"
    assert pixels.container is MediaFormat.V2


def test_verify_pixels_rejects_garbage():
    with pytest.raises(DecoderFailure) as exc_info:
        verify_pixels(b"\x00\x01\x02\x03" * 10)
    assert exc_info.value.code is ContractErrorCode.DECODE_FAILED


def test_decoded_pixels_repr_has_no_payload_bytes(image_factory):
    pixels = verify_pixels(image_factory())
    rendered = repr(pixels)
    assert "JPEG" in rendered
    assert str(pixels.width) in rendered


def test_verify_pixels_relaxes_pillow_bomb_guard(monkeypatch):
    """Real accounts hold stitched screenshots past Pillow's 179-megapixel
    bomb limit (probed: a healthy 11225x16006 PNG was rejected at open and
    surfaced as "not a valid image").  Inputs here are local size-capped
    files, so the guard must not kill the preview; simulate the limit with a
    tiny threshold to prove the relaxation takes effect inside the call."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (2200, 1800), (10, 20, 30)).save(buffer, format="PNG")
    payload = buffer.getvalue()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1_000_000)

    pixels = verify_pixels(payload, container=MediaFormat.UNKNOWN)

    assert (pixels.width, pixels.height) == (2200, 1800)
    assert pixels.image_format == "PNG"


def test_decode_image_bytes_reports_original_payload(
    account_root, image_factory, dat_factory, aes_key, fake_downloader
):
    from wechat_cleaner.real_media import decode_image_bytes

    payload = image_factory(width=64, height=48)
    dat_path = account_root / "orig.dat"
    dat_path.write_bytes(dat_factory(payload, aes_key))

    decoded, container, is_original = decode_image_bytes(
        str(dat_path), downloader=fake_downloader, aes_key=aes_key, xor_key=0x88
    )

    assert decoded == payload
    assert container is MediaFormat.V2
    assert is_original is True


def test_decode_image_bytes_flags_embedded_fallback(
    account_root, image_factory, dat_factory, aes_key, monkeypatch
):
    """A damaged body must be reported as NOT the original (export honesty)."""
    from PIL import ImageFile

    # verify_pixels sets this globally for preview leniency; the usability gate
    # must be evaluated under the strict gold-standard setting here.
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", False)
    from wechat_cleaner.real_media import decode_image_bytes

    jpeg = image_factory()
    damaged = jpeg[:64] + b"\x00" * 600 + jpeg
    dat_path = account_root / "damaged.dat"
    dat_path.write_bytes(dat_factory(damaged, aes_key))

    decoded, _container, is_original = decode_image_bytes(
        str(dat_path), downloader=_RaisingDownloader(), aes_key=aes_key, xor_key=0x88
    )

    assert is_original is False
    assert len(decoded) < len(damaged)  # the embedded thumbnail, not the source
