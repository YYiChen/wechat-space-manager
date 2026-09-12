"""Real database adapter tests: discovery, inventory and session orchestration.

All fixtures build a synthetic ``db_storage`` tree; no real WeChat database,
key, account identifier or message content is involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wechat_cleaner.decoder.formats import DecoderFailure
from wechat_cleaner.domain.contracts import ContractErrorCode
from wechat_cleaner.real_db import (
    ADAPTER_VERSION,
    KeyState,
    RealDatabaseAdapter,
    build_inventory,
    category_of,
    discover_accounts,
    discovery_summary,
    summarize_discovery,
)

ACCOUNT_A = "wxid_synthetic_alpha_d729"
ACCOUNT_B = "wxid_synthetic_beta_1111"
DB_FILES = {
    "message/message_0.db": 4096,
    "message/message_1.db": 2048,
    "media/media_0.db": 1024,
    "session/session.db": 512,
    "biz/biz.db": 256,
}


def _build_account(root: Path, name: str, files: dict[str, int]) -> Path:
    db_storage = root / name / "db_storage"
    for relative, size in files.items():
        target = db_storage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00" * size)
    return db_storage


class FakeSession:
    """Stand-in for the upstream WeChatDB holding a private key map."""

    def __init__(self, *, keys: dict[str, bytes] | None = None, cfg_dword: int | None = 7) -> None:
        self._keys = keys or {}
        self.cfg_dword = cfg_dword
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def db_root(tmp_path: Path) -> Path:
    root = tmp_path / "xwechat_files"
    root.mkdir()
    _build_account(root, ACCOUNT_A, DB_FILES)
    _build_account(root, ACCOUNT_B, {"message/message_0.db": 128})
    # Non-account directories must be ignored.
    (root / "all_users").mkdir()
    (root / "Backup").mkdir()
    (root / "not_an_account").mkdir()
    return root


def test_category_of_normalizes_separators_and_suffix():
    assert category_of("message\\message_0.db") == "message/message_0"
    assert category_of("session/session.db") == "session/session"
    assert category_of("biz.db") == "biz"


def test_discover_accounts_ignores_non_account_dirs(db_root):
    accounts = discover_accounts(db_root)

    assert {account.account_id for account in accounts} == {ACCOUNT_A, ACCOUNT_B}
    alpha = next(account for account in accounts if account.account_id == ACCOUNT_A)
    assert alpha.database_count == len(DB_FILES)
    assert alpha.database_bytes == sum(DB_FILES.values())


def test_discover_rejects_missing_root(tmp_path):
    with pytest.raises(DecoderFailure) as exc_info:
        discover_accounts(tmp_path / "nope")
    assert exc_info.value.code is ContractErrorCode.INVALID_PATH


def test_discovery_summary_is_anonymous(db_root):
    summary = discovery_summary(str(db_root))

    assert summary["account_count"] == 2
    assert summary["database_count"] == len(DB_FILES) + 1
    assert summary["database_bytes"] == sum(DB_FILES.values()) + 128
    assert ACCOUNT_A not in str(summary)


def test_summarize_discovery_of_empty_tuple():
    assert summarize_discovery(()) == {
        "account_count": 0,
        "database_count": 0,
        "database_bytes": 0,
    }


def test_build_inventory_marks_key_states(db_root):
    db_storage = db_root / ACCOUNT_A / "db_storage"
    keyed = {"message\\message_0.db", "session\\session.db"}

    inventory = build_inventory(db_storage, keyed)

    assert len(inventory.items) == len(DB_FILES)
    assert inventory.keyed_count == 2
    assert inventory.unkeyed_count == len(DB_FILES) - 2
    states = {item.category: item.key_state for item in inventory.items}
    assert states["message/message_0"] is KeyState.KEYED
    assert states["biz/biz"] is KeyState.UNKEYED
    assert inventory.total_bytes == sum(DB_FILES.values())


def test_build_inventory_without_key_info_reports_all_unkeyed(db_root):
    inventory = build_inventory(db_root / ACCOUNT_A / "db_storage", None)

    assert inventory.keyed_count == 0
    assert inventory.unkeyed_count == len(DB_FILES)


def test_adapter_open_selects_largest_account_and_summarizes(db_root, tmp_path):
    cache_root = tmp_path / "cache"

    adapter = RealDatabaseAdapter.open(
        db_root=str(db_root),
        cache_root=str(cache_root),
        session_factory=lambda **kwargs: FakeSession(keys={"message\\message_0.db": b"k"}),
    )

    summary = adapter.summary()
    assert summary["adapter_version"] == ADAPTER_VERSION
    assert summary["database_count"] == len(DB_FILES)
    assert summary["keyed_count"] == 1
    assert summary["unkeyed_count"] == len(DB_FILES) - 1
    assert "biz/biz" in summary["unkeyed_categories"]
    assert summary["cfg_dword_present"] is True
    assert ACCOUNT_A not in str(summary)
    assert str(db_root) not in str(summary)


def test_adapter_open_honours_explicit_account(db_root, tmp_path):
    adapter = RealDatabaseAdapter.open(
        db_root=str(db_root),
        cache_root=str(tmp_path / "cache"),
        account=ACCOUNT_B,
        session_factory=lambda **kwargs: FakeSession(keys={}),
    )

    assert adapter.account.account_id == ACCOUNT_B
    assert adapter.summary()["database_count"] == 1


def test_adapter_open_rejects_unknown_account(db_root, tmp_path):
    with pytest.raises(DecoderFailure) as exc_info:
        RealDatabaseAdapter.open(
            db_root=str(db_root),
            cache_root=str(tmp_path / "cache"),
            account="wxid_not_here",
            session_factory=lambda **kwargs: FakeSession(),
        )
    assert exc_info.value.code is ContractErrorCode.DECODER_UNAVAILABLE


def test_adapter_close_releases_session(db_root, tmp_path):
    session = FakeSession()
    adapter = RealDatabaseAdapter.open(
        db_root=str(db_root),
        cache_root=str(tmp_path / "cache"),
        session_factory=lambda **kwargs: session,
    )

    adapter.close()
    assert session.closed is True


def test_adapter_tolerates_session_without_private_keys(db_root, tmp_path):
    class Bare:
        pass

    adapter = RealDatabaseAdapter.open(
        db_root=str(db_root),
        cache_root=str(tmp_path / "cache"),
        session_factory=lambda **kwargs: Bare(),
    )

    summary = adapter.summary()
    assert summary["keyed_count"] == 0
    assert summary["cfg_dword_present"] is False
    adapter.close()  # must not raise for a session without close()
