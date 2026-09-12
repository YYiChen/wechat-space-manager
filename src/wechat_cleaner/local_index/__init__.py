"""Private, disposable SQLite metadata index for Phase 3 consumers."""

from .index import (
    CURRENT_SCHEMA_VERSION,
    AggregateBucket,
    ClearReceipt,
    IndexErrorBase,
    IndexMigrationError,
    IndexOpenError,
    IndexPage,
    IndexQueryError,
    IndexStats,
    IndexSummary,
    IndexWriteError,
    LocalIndexError,
    LocalMetadataIndex,
)

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "AggregateBucket",
    "ClearReceipt",
    "IndexErrorBase",
    "IndexMigrationError",
    "IndexOpenError",
    "IndexPage",
    "IndexQueryError",
    "IndexStats",
    "IndexSummary",
    "IndexWriteError",
    "LocalIndexError",
    "LocalMetadataIndex",
]
