"""In-memory key handling for the decoder.

Key material is machine-confidential (see ``docs/data-and-license-boundaries.md``):
it may exist in process memory for the shortest possible time and must never be
written to disk, logs, exceptions, or serialized artifacts.  This module wraps
key bytes in a small container that supports explicit zeroization and refuses to
reveal its content through ``repr``/``str``.
"""

from __future__ import annotations

from collections.abc import Callable


class KeyMaterial:
    """Mutable, zeroizable holder for raw key bytes."""

    __slots__ = ("_buf",)

    def __init__(self, data: bytes | bytearray) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("KeyMaterial accepts bytes or bytearray only")
        self._buf = bytearray(data)

    def expose(self) -> bytes:
        """Return a temporary read-only view for the current decode call only."""
        return bytes(self._buf)

    @property
    def size(self) -> int:
        return len(self._buf)

    def clear(self) -> None:
        """Overwrite the buffer with zeros and release it."""
        for index in range(len(self._buf)):
            self._buf[index] = 0
        self._buf.clear()

    def __enter__(self) -> KeyMaterial:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.clear()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"KeyMaterial(redacted bytes={self.size})"

    __str__ = __repr__


KeyProvider = Callable[[], "KeyMaterial | bytes | bytearray | None"]


def acquire_key(key_provider: KeyProvider | None) -> tuple[KeyMaterial | None, bool]:
    """Normalize a key provider result into a KeyMaterial plus ownership flag.

    The boolean reports whether *this* function created the KeyMaterial wrapper
    and therefore must clear it after use.  A provider returning ``None`` is not
    an error here; callers decide whether their adapter can proceed keyless.
    """
    if key_provider is None:
        return None, False
    produced = key_provider()
    if produced is None:
        return None, False
    if isinstance(produced, KeyMaterial):
        return produced, False
    if isinstance(produced, (bytes, bytearray)):
        return KeyMaterial(produced), True
    raise TypeError("key_provider must return KeyMaterial, bytes, bytearray, or None")
