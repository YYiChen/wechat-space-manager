"""Fixtures for the real-media backend.

Everything here is synthetic: a locally built V2 container, a fake downloader
that decrypts it with the same AES-ECB/PKCS7 contract, and synthetic records.
No real WeChat payload, path or key is involved.

Helpers are exposed as pytest fixtures (flat test directories share the module
namespace, so no cross-directory imports).
"""

from __future__ import annotations

import hashlib
import io
import struct
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wechat_cleaner.domain.contracts import (
    FileIdentity,
    MappingConfidence,
    MediaRecord,
    MediaType,
)

V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"
V1_MAGIC = b"\x07\x08\x05\x56\x02\x05"


def make_image_bytes(width: int = 40, height: int = 30, color=(120, 90, 200)) -> bytes:
    from PIL import Image  # noqa: PLC0415

    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def aes_ecb_encrypt(payload: bytes, aes_key: str) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(payload) + padder.finalize()
    encryptor = Cipher(algorithms.AES(aes_key.encode()), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def aes_ecb_decrypt(ciphertext: bytes, aes_key: str) -> bytes:
    decryptor = Cipher(algorithms.AES(aes_key.encode()), modes.ECB()).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def make_v2_dat(image_bytes: bytes, aes_key: str, xor_key: int = 0x88) -> bytes:
    """Build a synthetic V2 container: header + AES-ECB ciphertext + xor segment."""
    ciphertext = aes_ecb_encrypt(image_bytes, aes_key)
    header = V2_MAGIC + struct.pack("<LL", len(ciphertext), 0) + b"\x00"
    return header + ciphertext


def make_v1_dat(image_bytes: bytes, xor_key: int = 0x88) -> bytes:
    header = V1_MAGIC + bytes([xor_key]) * 16
    return header + bytes(value ^ xor_key for value in image_bytes)


class FakeDownloader:
    """Minimal stand-in for the upstream MediaDownloader used by the adapter."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def decrypt_image(
        self, dat_path: str, aes_key: str | None = None, xor_key: int | None = None
    ) -> bytes:
        self.calls.append(dat_path)
        with open(dat_path, "rb") as handle:
            data = handle.read()
        if data[:6] == V2_MAGIC:
            aes_size, xor_size = struct.unpack_from("<LL", data, 6)
            body = data[15:]
            ciphertext = body[:aes_size]
            plain = aes_ecb_decrypt(ciphertext, aes_key or "")
            if xor_size:
                xor_body = body[aes_size : aes_size + xor_size]
                plain += bytes(value ^ (xor_key or 0) for value in xor_body)
            return plain
        if data[:6] == V1_MAGIC:
            return bytes(value ^ (xor_key or 0x88) for value in data[22:])
        raise ValueError("unrecognized container")


def make_record(
    *,
    relative_path: str,
    media_type: MediaType = MediaType.IMAGE,
    byte_size: int = 0,
    modified_time_ns: int = 0,
    confidence: MappingConfidence = MappingConfidence.HIGH,
    account_id: str = "wxid_synthetic_real",
) -> MediaRecord:
    return MediaRecord(
        account_id=account_id,
        file=FileIdentity(
            relative_path=relative_path,
            byte_size=byte_size,
            modified_time_ns=modified_time_ns,
        ),
        media_type=media_type,
        observed_at=datetime(2026, 2, 1, tzinfo=UTC),
        mapping_confidence=confidence,
        mapping_reason="synthetic real-media fixture",
    )


@pytest.fixture
def image_factory():
    return make_image_bytes


@pytest.fixture
def dat_factory():
    return make_v2_dat


@pytest.fixture
def v1_dat_factory():
    return make_v1_dat


@pytest.fixture
def fake_downloader():
    return FakeDownloader()


@pytest.fixture
def account_root(tmp_path: Path) -> Path:
    root = tmp_path / "account"
    root.mkdir()
    return root


@pytest.fixture
def cache_root(tmp_path: Path) -> Path:
    root = tmp_path / "cache"
    root.mkdir()
    return root


@pytest.fixture
def aes_key() -> str:
    return hashlib.md5(b"synthetic-media-key").hexdigest()[:16]


@pytest.fixture
def stager(account_root: Path):
    def _stage(relative_path: str, payload: bytes) -> FileIdentity:
        target = account_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        stat = target.stat()
        return FileIdentity(
            relative_path=relative_path,
            byte_size=stat.st_size,
            modified_time_ns=stat.st_mtime_ns,
        )

    return _stage


@pytest.fixture
def record_from_identity():
    def _make(identity: FileIdentity, media_type: MediaType = MediaType.IMAGE) -> MediaRecord:
        return make_record(
            relative_path=identity.relative_path,
            media_type=media_type,
            byte_size=identity.byte_size,
            modified_time_ns=identity.modified_time_ns,
        )

    return _make
