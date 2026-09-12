"""Real WeChat database adapter (read-only, optional backend).

Importing this package does not import the optional upstream backend: the
session is opened lazily in :meth:`RealDatabaseAdapter.open`, so a missing
dependency degrades to ``DECODER_UNAVAILABLE`` rather than an ImportError.
"""

from .adapter import ADAPTER_VERSION, RealDatabaseAdapter, discovery_summary
from .discovery import DiscoveredAccount, discover_accounts, summarize_discovery
from .inventory import (
    DatabaseInventory,
    DatabaseInventoryItem,
    KeyState,
    build_inventory,
    category_of,
)
from .mapping import build_session_mapping, is_merged_forward, session_dir_of

__all__ = [
    "ADAPTER_VERSION",
    "DatabaseInventory",
    "DatabaseInventoryItem",
    "DiscoveredAccount",
    "KeyState",
    "RealDatabaseAdapter",
    "build_inventory",
    "build_session_mapping",
    "category_of",
    "discover_accounts",
    "discovery_summary",
    "is_merged_forward",
    "session_dir_of",
    "summarize_discovery",
]
