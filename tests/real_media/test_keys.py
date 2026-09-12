"""Key derivation, validation and memory discipline."""

from __future__ import annotations

import hashlib

import pytest

from wechat_cleaner.decoder.formats import DecoderFailure
from wechat_cleaner.domain.contracts import ContractErrorCode
from wechat_cleaner.real_media import RealImageKeys, derive_image_keys, validate_aes_key

CFG_DWORD = 0x1A2B3C4D
WXID = "wxid_synthetic_alpha"


def test_derivation_matches_documented_formula():
    aes_key, xor_key = derive_image_keys(CFG_DWORD, WXID)
    expected = hashlib.md5(f"{CFG_DWORD}{WXID}".encode()).hexdigest()[:16]
    assert aes_key == expected
    assert xor_key == CFG_DWORD & 0xFF


def test_derivation_requires_cfg_dword():
    with pytest.raises(DecoderFailure) as exc_info:
        derive_image_keys(0, WXID)
    assert exc_info.value.code is ContractErrorCode.DECODER_UNAVAILABLE


def test_validate_aes_key_accepts_correct_key(image_factory, dat_factory, aes_key):
    payload = image_factory()
    container = dat_factory(payload, aes_key)
    probe = container[15:31]
    assert validate_aes_key(aes_key, probe) is True


def test_validate_aes_key_rejects_wrong_key(image_factory, dat_factory):
    container = dat_factory(image_factory(), "0123456789abcdef")
    probe = container[15:31]
    assert validate_aes_key("fedcba9876543210", probe) is False
    assert validate_aes_key("0123456789abcdef", b"") is False


def test_keys_wrap_derivation_and_mask_repr():
    keys = RealImageKeys.from_cfg_dword(CFG_DWORD, WXID)

    assert len(keys.aes_key) == 16
    assert keys.xor_key == CFG_DWORD & 0xFF
    for rendered in (repr(keys), str(keys)):
        assert keys.aes_key not in rendered
        assert "***" in rendered


def test_keys_reject_wrong_length():
    with pytest.raises(ValueError):
        RealImageKeys(aes_key="short", xor_key=1)


def test_keys_clear_zeroizes_state():
    keys = RealImageKeys.from_cfg_dword(CFG_DWORD, WXID)
    keys.clear()

    assert keys.is_cleared is True
    assert keys.aes_key == ""
    assert keys.xor_key == 0
    assert repr(keys) == "RealImageKeys(aes=***, xor=***)"
