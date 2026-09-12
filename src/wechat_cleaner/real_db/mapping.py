"""Session mapping: which chat does each ``msg/attach/<hash>`` dir belong to?

WeChat 4.x lays media out per conversation: ``msg/attach/<md5(username)>/
<YYYY-MM>/...``.  The directory hash is therefore a precise, reversible
conversation key — the keyed contact/session databases resolve it back to a
username and display name, so files can be filtered by "the chat with X /
the group Y" without any heuristic matching.

Merged-forward chat records live under a ``Rec`` segment of the same layout
(``.../<YYYY-MM>/Rec/<record-id>/...``); a path containing a ``Rec`` part is
reliably "came from a merged-forward record", which is the second filter the
read-only window offers.

Facts established by ``scripts/probe_session_mapping.py`` on a real account
(2026-09-11): 747 of 752 attach dirs matched a chat; ``md5(username) ==
dir name`` held on 200/200 sampled chats; the 5 unmatched dirs are treated as
"unknown chat" and remain visible under their hash prefix.

Display names live in the UI layer only: this module reports raw data
(``name`` may be empty when the contact database is unavailable), nothing is
persisted, and no chat content is ever read.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

__all__ = [
    "ATTACH_ROOT",
    "MERGED_FORWARD_SEGMENT",
    "session_dir_of",
    "is_merged_forward",
    "build_session_mapping",
]

ATTACH_ROOT = "attach"
# The directory segment WeChat creates for merged-forward record attachments.
MERGED_FORWARD_SEGMENT = "rec"


def session_dir_of(relative_path: str) -> str:
    """The ``msg/attach/<hash>`` directory name of a media relative path.

    Paths outside the attach layout return an empty string.  The contract
    layer normalizes separators to ``\\`` before this is ever called.
    """
    parts = relative_path.split("\\")
    if len(parts) < 3 or parts[0] != "msg" or parts[1] != ATTACH_ROOT:
        return ""
    return parts[2]


def is_merged_forward(relative_path: str) -> bool:
    """True when the file lives inside a merged-forward ``Rec`` zone."""
    parts = relative_path.split("\\")
    return any(part.casefold() == MERGED_FORWARD_SEGMENT for part in parts)


def _mapping_from_chat_list(upstream: Any) -> dict[str, dict]:
    """Full chat resolution via ``list_message_chats()``.

    Public upstream API, but it counts every message table (seconds on a
    100k-message account); used as the fallback when the fast contact index
    path below is unavailable.
    """
    mapping: dict[str, dict] = {}
    for chat in upstream.list_message_chats():
        username = str(chat.get("username") or "")
        mapping[str(chat.get("md5") or "")] = {
            "name": str(chat.get("name") or ""),
            "username": username,
            "is_chatroom": username.endswith("@chatroom"),
            "message_count": int(chat.get("message_count") or 0),
        }
    return mapping


def _mapping_from_contact_index(upstream: Any) -> dict[str, dict]:
    """Fast chat resolution from the contact/session reverse index.

    Uses the upstream md5(username)->username index (contact.db + session.db)
    and the remark/nickname index; no message database is touched.  These are
    upstream-private helpers — acceptable per the established pattern of
    pinning the upstream revision (wechatauto-replica 1.2.1).
    """
    md5_index = upstream._build_md5_index()
    nicks = upstream._nickname_index()
    mapping: dict[str, dict] = {}
    for md5, username in md5_index.items():
        mapping[md5] = {
            "name": str(nicks.get(username) or ""),
            "username": username,
            "is_chatroom": username.endswith("@chatroom"),
            "message_count": 0,
        }
    return mapping


def build_session_mapping(
    account_root: str | os.PathLike[str], upstream: Any | None
) -> dict[str, dict]:
    """Map every ``msg/attach/<hash>`` dir to its conversation facts.

    Returns ``{dir_name: {"name": str, "username": str, "is_chatroom": bool,
    "message_count": int}}``.  When ``upstream`` (a keyed session) is missing
    or fails, directories degrade to ``name=""`` — the UI shows the hash
    prefix and the filter keeps working, because the dir name alone still
    separates conversations.
    """
    attach_root = Path(account_root) / "msg" / ATTACH_ROOT
    dirs: list[str] = []
    if attach_root.is_dir():
        dirs = sorted(entry.name for entry in attach_root.iterdir() if entry.is_dir())

    chats_by_md5: dict[str, dict] = {}
    if upstream is not None and dirs:
        try:
            try:
                chats_by_md5 = _mapping_from_contact_index(upstream)
            except AttributeError:
                chats_by_md5 = _mapping_from_chat_list(upstream)
        except Exception:  # noqa: BLE001 - mapping is advisory; degrade honestly
            chats_by_md5 = {}

    mapping: dict[str, dict] = {}
    for dir_name in dirs:
        chat = chats_by_md5.get(dir_name)
        if chat is None:
            mapping[dir_name] = {
                "name": "",
                "username": "",
                "is_chatroom": False,
                "message_count": 0,
            }
            continue
        mapping[dir_name] = chat
    return mapping
