"""Key material discipline: memory-only, zeroizable, never printable."""

from __future__ import annotations

import pytest

from wechat_cleaner.decoder.key_material import KeyMaterial, acquire_key

SECRET = b"\xde\xad\xbe\xef" * 8


def test_expose_roundtrip():
    with KeyMaterial(SECRET) as key:
        assert key.expose() == SECRET
        assert key.size == len(SECRET)


def test_clear_zeroizes_buffer():
    key = KeyMaterial(SECRET)
    key.clear()
    assert key.expose() == b""
    assert key.size == 0


def test_repr_and_str_never_leak_secret():
    key = KeyMaterial(SECRET)
    for rendered in (repr(key), str(key), f"{key}"):
        assert SECRET.decode("latin-1") not in rendered
        assert SECRET.hex() not in rendered


def test_rejects_non_bytes_types():
    with pytest.raises(TypeError):
        KeyMaterial("not-bytes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        KeyMaterial(123)  # type: ignore[arg-type]


def test_acquire_key_ownership_rules():
    material, owned = acquire_key(lambda: SECRET)
    assert owned is True
    assert material is not None
    assert material.expose() == SECRET
    material.clear()

    shared = KeyMaterial(SECRET)
    borrowed, owned = acquire_key(lambda: shared)
    assert owned is False
    assert borrowed is shared

    assert acquire_key(None) == (None, False)
    assert acquire_key(lambda: None) == (None, False)

    with pytest.raises(TypeError):
        acquire_key(lambda: 42)  # type: ignore[arg-type,return-value]
