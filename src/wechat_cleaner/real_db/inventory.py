"""Anonymous inventory of the databases inside one account.

Only relative category names (e.g. ``message/message_0``), sizes and key states
are reported: these are safe to persist as metadata and to show in a UI.  Raw
paths, account identifiers and message content never appear here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

__all__ = [
    "KeyState",
    "DatabaseInventoryItem",
    "DatabaseInventory",
    "build_inventory",
    "category_of",
]


class KeyState(StrEnum):
    KEYED = "keyed"
    UNKEYED = "unkeyed"


@dataclass(frozen=True, slots=True)
class DatabaseInventoryItem:
    category: str
    byte_size: int
    key_state: KeyState


@dataclass(frozen=True, slots=True)
class DatabaseInventory:
    items: tuple[DatabaseInventoryItem, ...]
    total_bytes: int
    keyed_count: int
    unkeyed_count: int


def category_of(relative_path: str) -> str:
    """Normalize a database path into an anonymous category label.

    ``message\\message_0.db`` -> ``message/message_0``; the ``.db`` suffix and
    platform separators are dropped so the label is stable across machines.
    """
    normalized = relative_path.replace("\\", "/").lstrip("/")
    if normalized.endswith(".db"):
        normalized = normalized[: -len(".db")]
    return normalized


def _relative_databases(db_storage: Path) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for dirpath, _dirnames, filenames in os.walk(db_storage):
        for name in filenames:
            if not name.endswith(".db"):
                continue
            full = Path(dirpath) / name
            try:
                size = full.stat().st_size
            except OSError:
                continue
            found.append((os.path.relpath(full, db_storage), size))
    return sorted(found)


def build_inventory(
    db_storage: str | Path, keyed_relatives: set[str] | None = None
) -> DatabaseInventory:
    """Build the inventory; ``keyed_relatives`` lists relatives that got a key.

    Callers that cannot report key state pass ``None`` and every item is marked
    ``UNKEYED`` (honest default: absence of evidence is not evidence of a key).
    """
    root = Path(db_storage)
    keyed = {
        category_of(relative) for relative in (keyed_relatives or set())
    }
    items = []
    for relative, size in _relative_databases(root):
        category = category_of(relative)
        items.append(
            DatabaseInventoryItem(
                category=category,
                byte_size=size,
                key_state=KeyState.KEYED if category in keyed else KeyState.UNKEYED,
            )
        )
    return DatabaseInventory(
        items=tuple(items),
        total_bytes=sum(item.byte_size for item in items),
        keyed_count=sum(1 for item in items if item.key_state is KeyState.KEYED),
        unkeyed_count=sum(1 for item in items if item.key_state is KeyState.UNKEYED),
    )
