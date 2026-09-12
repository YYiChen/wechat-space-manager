"""Read-only discovery of WeChat filesystem candidates."""

from .scanner import (
    CACHE_ROOT,
    DEFAULT_SCANNER_VERSION,
    PROTECTED_ROOTS,
    ScannedFile,
    ScannerError,
    ScanResult,
    discover_account_roots,
    scan,
    scan_account,
)

__all__ = [
    "CACHE_ROOT",
    "DEFAULT_SCANNER_VERSION",
    "PROTECTED_ROOTS",
    "ScanResult",
    "ScannedFile",
    "ScannerError",
    "discover_account_roots",
    "scan",
    "scan_account",
]
