"""Read-only discovery of real WeChat account roots.

Discovery answers "which directories under the data root look like accounts that
hold an encrypted ``db_storage``".  Names are only used internally; every value
that leaves this module for a report is an anonymous aggregate (counts, bytes).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from wechat_cleaner.domain.contracts import ContractErrorCode

from ..decoder.formats import DecoderFailure

__all__ = ["DiscoveredAccount", "discover_accounts", "summarize_discovery"]

DB_STORAGE_DIRNAME = "db_storage"
_SKIP_DIRNAMES = {"all_users", "Backup", "old_backup", "WMPF", "Finderlive"}


@dataclass(frozen=True, slots=True)
class DiscoveredAccount:
    """One account root; ``account_id`` matches the directory name on disk."""

    account_id: str
    root: str
    db_storage: str
    database_count: int
    database_bytes: int


def _account_candidates(db_root: Path) -> tuple[Path, ...]:
    try:
        entries = sorted(db_root.iterdir())
    except OSError as exc:
        raise DecoderFailure(
            ContractErrorCode.INVALID_PATH, "the WeChat data root is not readable"
        ) from exc
    return tuple(
        entry
        for entry in entries
        if entry.is_dir()
        and entry.name not in _SKIP_DIRNAMES
        and (entry / DB_STORAGE_DIRNAME).is_dir()
    )


def _database_stats(db_storage: Path) -> tuple[int, int]:
    count = 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(db_storage):
        for name in filenames:
            if name.endswith(".db"):
                count += 1
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    continue
    return count, total


def discover_accounts(db_root: str | Path) -> tuple[DiscoveredAccount, ...]:
    """Return every account directory that holds a ``db_storage`` tree."""
    root = Path(db_root)
    if not root.is_dir():
        raise DecoderFailure(ContractErrorCode.INVALID_PATH, "the WeChat data root does not exist")
    accounts = []
    for candidate in _account_candidates(root):
        db_storage = candidate / DB_STORAGE_DIRNAME
        count, total = _database_stats(db_storage)
        accounts.append(
            DiscoveredAccount(
                account_id=candidate.name,
                root=str(candidate.resolve()),
                db_storage=str(db_storage.resolve()),
                database_count=count,
                database_bytes=total,
            )
        )
    return tuple(accounts)


def summarize_discovery(accounts: tuple[DiscoveredAccount, ...]) -> dict:
    """Anonymous aggregate over discovered accounts (no identifiers)."""
    return {
        "account_count": len(accounts),
        "database_count": sum(account.database_count for account in accounts),
        "database_bytes": sum(account.database_bytes for account in accounts),
    }
