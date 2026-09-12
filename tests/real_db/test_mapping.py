"""Session-mapping tests: attach dir -> chat facts (synthetic trees only)."""

from __future__ import annotations

from pathlib import Path

from wechat_cleaner.real_db import (
    build_session_mapping,
    is_merged_forward,
    session_dir_of,
)


def _tree(tmp_path: Path) -> Path:
    account = tmp_path / "wxid_synthetic_account_d729"
    for rel in ("msg/attach/aaa111", "msg/attach/bbb222", "msg/file"):
        (account / rel).mkdir(parents=True)
    return account


def test_session_dir_of_extracts_attach_dir():
    assert session_dir_of(r"msg\attach\abc123\2026-02\Img\x.dat") == "abc123"
    assert session_dir_of(r"msg\attach\abc123\2026-01\Rec\77aa\F\3\x.pdf") == "abc123"
    assert session_dir_of(r"msg\file\2026-02\File\y.pdf") == ""
    assert session_dir_of("unrelated") == ""


def test_is_merged_forward_detects_rec_segment():
    assert is_merged_forward(r"msg\attach\abc123\2026-01\Rec\77b8\F\3\x.pdf")
    assert is_merged_forward(r"msg\attach\abc123\2026-01\rec\77b8\Img\1_t")  # case-insensitive
    assert not is_merged_forward(r"msg\attach\abc123\2026-01\Img\x.dat")
    # "Rec" is WeChat's dedicated merged-forward directory name; any Rec
    # segment anywhere in the relative path marks a merged-forward payload,
    # so the check deliberately does not constrain the position.
    assert is_merged_forward(r"msg\file\2026-01\Rec\x.pdf")


def test_build_session_mapping_without_upstream_degrades_to_hash(tmp_path):
    mapping = build_session_mapping(_tree(tmp_path), None)

    assert set(mapping) == {"aaa111", "bbb222"}
    entry = mapping["aaa111"]
    assert entry["name"] == ""
    assert entry["username"] == ""
    assert entry["is_chatroom"] is False
    assert entry["message_count"] == 0


def test_build_session_mapping_resolves_names_and_chatrooms(tmp_path):
    class Upstream:
        def list_message_chats(self):
            return [
                {"md5": "aaa111", "username": "friend1", "name": "小张", "message_count": 10},
                {
                    "md5": "bbb222",
                    "username": "room9@chatroom",
                    "name": "九群",
                    "message_count": 99,
                },
            ]

    mapping = build_session_mapping(_tree(tmp_path), Upstream())

    assert mapping["aaa111"] == {
        "name": "小张",
        "username": "friend1",
        "is_chatroom": False,
        "message_count": 10,
    }
    assert mapping["bbb222"]["is_chatroom"] is True
    assert mapping["bbb222"]["name"] == "九群"


def test_build_session_mapping_swallows_upstream_failure(tmp_path):
    class Boom:
        def list_message_chats(self):
            raise RuntimeError("keyed session unavailable")

    mapping = build_session_mapping(_tree(tmp_path), Boom())

    assert set(mapping) == {"aaa111", "bbb222"}
    assert mapping["aaa111"]["name"] == ""


def test_build_session_mapping_ignores_unknown_dirs_but_keeps_them(tmp_path):
    class Upstream:
        def list_message_chats(self):
            # only aaa111 is known; bbb222 stays unresolved
            return [{"md5": "aaa111", "username": "friend1", "name": "小张", "message_count": 3}]

    mapping = build_session_mapping(_tree(tmp_path), Upstream())

    assert mapping["aaa111"]["name"] == "小张"
    assert mapping["bbb222"]["name"] == ""


def test_build_session_mapping_with_missing_attach_root(tmp_path):
    assert build_session_mapping(tmp_path / "does-not-exist", None) == {}
