"""Read-only duplicate detection: normalized-name grouping + content MD5.

Public surface (consumed by the GUI facade, see plan/ITER-20261010-
duplicate-detection.md section 2):

- ``same_name_groups(records)`` — S1/S2 grouping, pure memory;
- ``file_md5(record, path)`` / ``verify_group(group, resolve)`` — S3
  content verdict over the encrypted container bytes, streaming, with an
  identity re-check before any read.

Strictly read-only; no key involvement; nothing here feeds a CleanupPlan.
"""

from .content import file_md5, verify_group
from .groups import same_name_groups
from .model import (
    LEVEL_S1,
    LEVEL_S2,
    DuplicateError,
    DuplicateGroup,
    DuplicateIndex,
    GroupVerification,
)
from .names import duplicate_key, normalize_filename

__all__ = [
    "LEVEL_S1",
    "LEVEL_S2",
    "DuplicateError",
    "DuplicateGroup",
    "DuplicateIndex",
    "GroupVerification",
    "duplicate_key",
    "file_md5",
    "normalize_filename",
    "same_name_groups",
    "verify_group",
]
