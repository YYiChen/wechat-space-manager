"""Real WeChat database adapter: discovery, read-only key extraction, inventory.

This is the real-data counterpart of ``db_adapter`` (which only understands
synthetic copies).  Everything upstream-specific is isolated here:

* the source ``db_storage`` is never written — keys and decrypted copies land in
  the application cache root (upstream ``workdir``);
* the adapter reports only anonymous aggregates: library categories, sizes, key
  states and counts.  No message body, contact, account identifier or absolute
  path leaves this module toward logs or artifacts;
* a missing optional upstream package degrades to ``DECODER_UNAVAILABLE``.

Version note: ``keyed_*`` helpers read the upstream session's private ``_keys``
mapping.  That coupling is deliberate and confined to this adapter, which is why
the upstream revision is pinned in the Phase 4 plan.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from wechat_cleaner.domain.contracts import ContractErrorCode

from ..decoder.formats import DecoderFailure
from ..real_media.keys import open_source_session
from .discovery import DiscoveredAccount, discover_accounts, summarize_discovery
from .inventory import DatabaseInventory, build_inventory

__all__ = ["ADAPTER_VERSION", "RealDatabaseAdapter"]

ADAPTER_VERSION = "real-db-1"
_SESSION_CACHE_DIRNAME = "db-cache"


class RealDatabaseAdapter:
    """Read-only adapter over one real account's encrypted databases."""

    def __init__(
        self,
        *,
        account: DiscoveredAccount,
        cache_root: str,
        session: Any,
        adapter_version: str = ADAPTER_VERSION,
    ) -> None:
        self.account = account
        self.cache_root = str(Path(cache_root).resolve())
        self.adapter_version = adapter_version
        self._session = session

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def open(
        cls,
        *,
        db_root: str,
        cache_root: str,
        account: str | None = None,
        session_factory: Callable[..., Any] | None = None,
    ) -> RealDatabaseAdapter:
        """Discover the account, then open a read-only upstream session on it.

        ``session_factory`` is injectable for tests; production uses the pinned
        upstream ``WeChatDB`` through ``open_source_session``.
        """
        accounts = discover_accounts(db_root)
        if not accounts:
            raise DecoderFailure(
                ContractErrorCode.DECODER_UNAVAILABLE, "no account with db_storage was found"
            )
        target = _select_account(accounts, account)

        workdir = str(Path(cache_root) / _SESSION_CACHE_DIRNAME)
        if session_factory is None:
            session = open_source_session(
                db_root=db_root, workdir=workdir, account=target.account_id
            )
        else:
            session = session_factory(db_root=db_root, workdir=workdir, account=target.account_id)

        return cls(account=target, cache_root=cache_root, session=session)

    # ------------------------------------------------------------------
    # read-only inspection
    # ------------------------------------------------------------------
    def keyed_relatives(self) -> set[str]:
        """Relative database paths that hold an extracted key (upstream private map)."""
        keys = getattr(self._session, "_keys", None)
        if not isinstance(keys, dict):
            return set()
        return {str(key) for key in keys}

    def inventory(self) -> DatabaseInventory:
        """Anonymous inventory of this account's databases and key states."""
        return build_inventory(self.account.db_storage, self.keyed_relatives())

    def summary(self) -> dict:
        """Anonymous aggregate: counts, bytes, key coverage, unkeyed categories."""
        inventory = self.inventory()
        return {
            "adapter_version": self.adapter_version,
            "account_count": 1,
            "database_count": len(inventory.items),
            "database_bytes": inventory.total_bytes,
            "keyed_count": inventory.keyed_count,
            "unkeyed_count": inventory.unkeyed_count,
            "unkeyed_categories": sorted(
                item.category for item in inventory.items if item.key_state.value == "unkeyed"
            ),
            "cfg_dword_present": bool(getattr(self._session, "cfg_dword", None)),
        }

    def close(self) -> None:
        closer = getattr(self._session, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - best-effort release
                pass


def _select_account(
    accounts: tuple[DiscoveredAccount, ...], requested: str | None
) -> DiscoveredAccount:
    if requested is None:
        # Largest db_storage wins; ties fall back to the first discovery order.
        return max(accounts, key=lambda item: item.database_bytes)
    for account in accounts:
        if account.account_id == requested:
            return account
    raise DecoderFailure(
        ContractErrorCode.DECODER_UNAVAILABLE, "the requested account was not found"
    )


def discovery_summary(db_root: str) -> dict:
    """Anonymous aggregate for a data root, without opening any database."""
    return summarize_discovery(discover_accounts(db_root))
