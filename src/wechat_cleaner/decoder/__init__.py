"""Decoder module: approved copies in, verified pixels out, keys never on disk."""

from .key_material import KeyMaterial, KeyProvider
from .pipeline import DecodeError, DecoderLimits, decode, new_cache_session

__all__ = [
    "DecodeError",
    "DecoderLimits",
    "KeyMaterial",
    "KeyProvider",
    "decode",
    "new_cache_session",
]
