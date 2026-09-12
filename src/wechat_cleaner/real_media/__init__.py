"""Optional real-data media backend (read-only source, cache-root outputs).

The synthetic ``decoder`` stays the default path.  Importing this package does
not import the optional upstream backend: every upstream call happens inside
``adapter``/``images`` at call time so a missing dependency degrades to
``DECODER_UNAVAILABLE`` instead of an ImportError at import time.
"""

from .adapter import BACKEND_ID, RealMediaBackend
from .images import MediaFormat, decode_image_bytes, decode_image_file, detect_format, verify_pixels
from .keys import RealImageKeys, derive_image_keys, validate_aes_key
from .locate import LocatedMedia, MediaAvailability, availability, locate, resolve_source_path

__all__ = [
    "BACKEND_ID",
    "LocatedMedia",
    "MediaAvailability",
    "MediaFormat",
    "RealImageKeys",
    "RealMediaBackend",
    "availability",
    "decode_image_bytes",
    "decode_image_file",
    "derive_image_keys",
    "detect_format",
    "locate",
    "resolve_source_path",
    "validate_aes_key",
    "verify_pixels",
]
