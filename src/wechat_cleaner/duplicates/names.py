"""Normalized-filename helpers for duplicate detection (pure functions, R0).

WeChat re-save/copy flows produce ``photo.jpg``, ``photo (1).jpg``,
``photo(2).jpg`` … copies.  To group "the same picture saved several times",
the trailing counter is stripped repeatedly before comparing names.  The
stripping is deliberately aggressive (repeatedly, both spacing styles) because
a false grouping is harmless — the byte-size and MD5 gates refine it — while a
missed grouping is the failure the user asked to avoid.
"""

from __future__ import annotations

import re

from wechat_cleaner.domain.contracts import MediaRecord

__all__ = ["duplicate_key", "normalize_filename"]

_TRAILING_COUNTER = re.compile(r"\s*\((\d{1,6})\)$")


def normalize_filename(filename: str) -> str:
    """Strip trailing `` (N)``/``(N)`` counters from a filename, repeatedly.

    The extension participates unchanged: ``photo.jpg`` and ``photo.png``
    never group together.  A name consisting solely of a counter (``(1).jpg``)
    is left intact — stripping must not invent an empty stem.
    """

    stem, sep, ext = filename.rpartition(".")
    if not sep or not stem:
        # No extension (or a ".jpg"-style dotfile): treat the whole name as
        # the stem so counters still strip, e.g. "archive (3)".
        stem, ext = filename, ""
    while True:
        stripped = _TRAILING_COUNTER.sub("", stem).rstrip()
        if stripped == stem or not stripped:
            break
        stem = stripped
    return f"{stem}.{ext}" if ext else stem


def duplicate_key(record: MediaRecord) -> str:
    """Casefolded normalized filename of a record (the S1 grouping key)."""

    filename = record.file.relative_path.replace("\\", "/").rpartition("/")[2]
    return normalize_filename(filename).casefold()
