"""Key derivation, validation and memory discipline."""

from __future__ import annotations

import hashlib
import os

import pytest

from wechat_cleaner.decoder.formats import DecoderFailure
from wechat_cleaner.domain.contracts import ContractErrorCode
from wechat_cleaner.real_media import RealImageKeys, derive_image_keys, validate_aes_key
from wechat_cleaner.real_media import keys as keys_module

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


# --- upstream session construction -------------------------------------------


class _HealingFakeDB:
    """Stand-in for upstream WeChatDB with the 1.3.x self-heal hooks."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.keys_dir_at_construction = os.environ.get("WECHATAUTO_KEYS_DIR")

    def _try_other_accounts(self) -> bool:
        return True

    def _select_account_by_keys(self, candidates) -> bool:
        return True

    def _all_key_candidates(self):
        return {b"candidate"}


class _BareFakeDB:
    """Stand-in for an upstream pin without the self-heal hooks."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _install_fake_upstream(monkeypatch, fake_cls):
    monkeypatch.setattr(
        keys_module, "require_upstream", lambda: (fake_cls, object), raising=True
    )


def test_explicit_account_skips_upstream_account_self_heal(tmp_path, monkeypatch):
    _install_fake_upstream(monkeypatch, _HealingFakeDB)

    session = keys_module.open_source_session(
        db_root=str(tmp_path / "wx"), workdir=str(tmp_path / "cache"), account=WXID
    )

    assert isinstance(session, _HealingFakeDB)
    assert session.kwargs["account"] == WXID
    assert session._try_other_accounts() is False
    assert session._select_account_by_keys({b"x"}) is False
    assert session._all_key_candidates() == set()


def test_account_discovery_keeps_full_self_heal(tmp_path, monkeypatch):
    _install_fake_upstream(monkeypatch, _HealingFakeDB)

    session = keys_module.open_source_session(
        db_root=str(tmp_path / "wx"), workdir=str(tmp_path / "cache"), account=None
    )

    assert session._try_other_accounts() is True
    assert session._all_key_candidates() == {b"candidate"}


def test_pinned_class_degrades_when_upstream_lacks_hooks(tmp_path, monkeypatch):
    _install_fake_upstream(monkeypatch, _BareFakeDB)

    session = keys_module.open_source_session(
        db_root=str(tmp_path / "wx"), workdir=str(tmp_path / "cache"), account=WXID
    )

    assert type(session) is _BareFakeDB


def test_key_mirror_is_confined_to_the_cache_root(tmp_path, monkeypatch):
    _install_fake_upstream(monkeypatch, _HealingFakeDB)
    monkeypatch.delenv("WECHATAUTO_KEYS_DIR", raising=False)

    session = keys_module.open_source_session(
        db_root=str(tmp_path / "wx"), workdir=str(tmp_path / "cache"), account=WXID
    )

    mirrored = session.keys_dir_at_construction
    assert mirrored is not None
    assert mirrored.startswith(str(tmp_path / "cache"))
    # Restored afterwards: the caller's environment must not leak the override.
    assert "WECHATAUTO_KEYS_DIR" not in os.environ


def test_key_mirror_override_restores_previous_value(tmp_path, monkeypatch):
    _install_fake_upstream(monkeypatch, _HealingFakeDB)
    monkeypatch.setenv("WECHATAUTO_KEYS_DIR", "sentinel-dir")

    keys_module.open_source_session(
        db_root=str(tmp_path / "wx"), workdir=str(tmp_path / "cache"), account=WXID
    )

    assert os.environ["WECHATAUTO_KEYS_DIR"] == "sentinel-dir"
