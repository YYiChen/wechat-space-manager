"""Real read-only session tests: orchestration over synthetic account trees.

The scanner runs for real (read-only) against a temporary synthetic account
directory; the database adapter and media backend are injected fakes so no real
WeChat data, key or upstream package is required.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from wechat_cleaner.application import SESSION_VERSION, RealReadOnlySession
from wechat_cleaner.decoder.formats import DecoderFailure
from wechat_cleaner.domain.contracts import (
    ContractErrorCode,
    DecodeArtifact,
    DecodeVariant,
    FileIdentity,
    MappingConfidence,
)

ACCOUNT = "wxid_synthetic_account_d729"
OTHER = "wxid_synthetic_other_1111"


class FakeAdapter:
    def __init__(
        self, *, database_count: int = 3, database_bytes: int = 900, keyed: int = 2
    ) -> None:
        self.closed = False
        self._summary = {
            "adapter_version": "fake",
            "database_count": database_count,
            "database_bytes": database_bytes,
            "keyed_count": keyed,
            "unkeyed_count": database_count - keyed,
        }

    def summary(self) -> dict:
        return dict(self._summary)

    def close(self) -> None:
        self.closed = True


class FakeBackend:
    def __init__(self, *, cache_root: str) -> None:
        self.cache_root = Path(cache_root)
        self.closed = False
        self.calls: list[str] = []

    def decode_image(self, media, *, variant, max_edge_px, cache_session_id) -> DecodeArtifact:
        self.calls.append(str(media.media_id))
        preview_dir = self.cache_root / "sessions" / str(cache_session_id)
        preview_dir.mkdir(parents=True, exist_ok=True)
        target = preview_dir / f"{media.media_id}.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        return DecodeArtifact(
            request_id=uuid4(),
            media_id=media.media_id,
            created_at=datetime.now(UTC),
            cache_session_id=cache_session_id,
            variant=variant,
            output_file=FileIdentity(
                relative_path=f"sessions/{cache_session_id}/{target.name}",
                byte_size=target.stat().st_size,
                modified_time_ns=target.stat().st_mtime_ns,
            ),
            output_format="PNG",
            decoder_id="fake-backend",
        )

    def close(self) -> None:
        self.closed = True


def _build_db_root(tmp_path: Path) -> Path:
    root = tmp_path / "xwechat_files"
    for account, size in ((ACCOUNT, 4096), (OTHER, 256)):
        db_storage = root / account / "db_storage" / "message"
        db_storage.mkdir(parents=True)
        (db_storage / "message_0.db").write_bytes(b"\x00" * size)
    return root


def _build_account_tree(root: Path) -> None:
    account = root / ACCOUNT
    (account / "msg" / "attach" / "8a8b0c0d" / "2026-02" / "Img").mkdir(parents=True)
    image_file = account / "msg" / "attach" / "8a8b0c0d" / "2026-02" / "Img" / "a1_t.dat"
    image_file.write_bytes(b"x" * 64)
    video = account / "msg" / "video" / "2026-03"
    video.mkdir(parents=True)
    (video / "clip.mp4").write_bytes(b"y" * 128)


@pytest.fixture
def env(tmp_path: Path):
    db_root = _build_db_root(tmp_path)
    _build_account_tree(db_root)
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    return db_root, str(cache_root)


def _session(env, **kwargs) -> RealReadOnlySession:
    db_root, cache_root = env
    return RealReadOnlySession.start(
        db_root=str(db_root),
        cache_root=cache_root,
        session_id="test-session",
        adapter_factory=lambda **_: FakeAdapter(),
        backend_factory=lambda **kw: FakeBackend(cache_root=kw["cache_root"]),
        **kwargs,
    )


def test_start_selects_largest_account_and_exposes_summary(env):
    session = _session(env)

    summary = session.database_summary()
    assert summary["database_count"] == 3
    assert summary["keyed_count"] == 2
    assert ACCOUNT not in json.dumps(summary)


def test_records_are_unmapped_and_skip_protected(env):
    session = _session(env)

    records = session.records()

    assert records, "the synthetic account tree has media files"
    assert all(record.mapping_confidence is MappingConfidence.UNMAPPED for record in records)
    assert all(record.contact is None for record in records)
    relative_paths = [record.file.relative_path for record in records]
    assert all("db_storage" not in path for path in relative_paths)
    assert any(path.endswith("a1_t.dat") for path in relative_paths)


def test_records_limit_is_honoured(env):
    session = _session(env)

    assert len(session.records(limit=1)) == 1


def test_records_progress_callback_reports_running_file_count(env, monkeypatch):
    from wechat_cleaner.scanner import scan_account
    from wechat_cleaner.scanner import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "_PROGRESS_EVERY", 1)
    db_root, _ = env
    session = _session(env)

    counts: list[int] = []
    session.records(progress=counts.append)

    total = scan_account(db_root / ACCOUNT).manifest.file_count
    assert counts == list(range(1, total + 1)), "every file must be reported exactly once"


def test_session_mapping_degrades_without_upstream(env):
    session = _session(env)

    mapping = session.session_mapping()

    assert set(mapping) == {"8a8b0c0d"}, "the synthetic tree has one attach dir"
    assert mapping["8a8b0c0d"]["name"] == ""


def test_session_mapping_resolves_names_via_upstream(env):
    class FakeUpstream:
        def list_message_chats(self):
            return [
                {
                    "md5": "8a8b0c0d",
                    "username": "known_friend",
                    "name": "已知好友",
                    "message_count": 42,
                }
            ]

    db_root, cache_root = env
    session = RealReadOnlySession(
        session_id="s",
        db_root=str(db_root),
        cache_root=cache_root,
        account_root=str(db_root / ACCOUNT),
        adapter=FakeAdapter(),
        backend=FakeBackend(cache_root=cache_root),
        upstream=FakeUpstream(),
    )

    mapping = session.session_mapping()

    assert mapping["8a8b0c0d"] == {
        "name": "已知好友",
        "username": "known_friend",
        "is_chatroom": False,
        "message_count": 42,
    }


def test_session_mapping_swallows_upstream_failure(env):
    class BoomUpstream:
        def list_message_chats(self):
            raise RuntimeError("keyed session unavailable")

    db_root, cache_root = env
    session = RealReadOnlySession(
        session_id="s",
        db_root=str(db_root),
        cache_root=cache_root,
        account_root=str(db_root / ACCOUNT),
        adapter=FakeAdapter(),
        backend=FakeBackend(cache_root=cache_root),
        upstream=BoomUpstream(),
    )

    mapping = session.session_mapping()

    assert mapping["8a8b0c0d"]["name"] == ""


def test_preview_delegates_to_backend_and_counts(env):
    session = _session(env)
    record = session.records()[0]

    artifact = session.preview(record, variant=DecodeVariant.THUMBNAIL, max_edge_px=64)

    assert artifact.decoder_id == "fake-backend"
    assert session.manifest().preview_count == 1


def test_manifest_is_anonymous_and_lists_relative_paths(env):
    db_root, cache_root = env
    session = _session(env)
    session.preview(session.records()[0])

    manifest = session.manifest()
    payload = manifest.to_anonymous_dict()

    assert payload["session_version"] == SESSION_VERSION
    assert payload["preview_count"] == 1
    assert payload["database_count"] == 3
    assert ACCOUNT not in json.dumps(payload)
    assert cache_root not in json.dumps(payload)
    assert all(
        not path.startswith("/") and ":" not in path
        for path in manifest.decoded_cache_files
    )
    assert len(manifest.decoded_cache_files) == 1


def test_clear_decoded_cache_only_touches_previews(env):
    db_root, cache_root = env
    session = _session(env)
    session.preview(session.records()[0])
    key_cache = Path(cache_root) / "db-cache"
    key_cache.mkdir(parents=True, exist_ok=True)
    (key_cache / "keys.json").write_text("{}", encoding="utf-8")

    removed = session.clear_decoded_cache()

    assert removed == 1
    assert not (Path(cache_root) / "sessions").exists()
    assert (key_cache / "keys.json").exists()


def test_close_releases_both_components(env):
    db_root, cache_root = env
    adapter = FakeAdapter()
    backend = FakeBackend(cache_root=cache_root)
    session = RealReadOnlySession.start(
        db_root=str(db_root),
        cache_root=cache_root,
        adapter_factory=lambda **_: adapter,
        backend_factory=lambda **_: backend,
    )

    session.close()

    assert adapter.closed is True
    assert backend.closed is True


def test_unknown_account_is_rejected(tmp_path):
    db_root = _build_db_root(tmp_path)

    with pytest.raises(DecoderFailure) as exc_info:
        RealReadOnlySession.start(
            db_root=str(db_root),
            cache_root=str(tmp_path / "cache"),
            account="wxid_absent",
            adapter_factory=lambda **_: FakeAdapter(),
            backend_factory=lambda **kw: FakeBackend(cache_root=kw["cache_root"]),
        )
    assert exc_info.value.code is ContractErrorCode.DECODER_UNAVAILABLE


def test_no_accounts_is_rejected(tmp_path):
    empty_root = tmp_path / "empty"
    empty_root.mkdir()

    with pytest.raises(DecoderFailure) as exc_info:
        RealReadOnlySession.start(db_root=str(empty_root), cache_root=str(tmp_path / "cache"))
    assert exc_info.value.code is ContractErrorCode.DECODER_UNAVAILABLE


def test_refine_media_type_covers_real_variant_families():
    from wechat_cleaner.application.real_workflow import _refine_media_type
    from wechat_cleaner.domain.contracts import MediaType

    base = "msg\\attach\\8a8b0c0d\\2026-02\\Img\\"
    assert _refine_media_type(base + "abc_t.dat", MediaType.OTHER) is MediaType.THUMBNAIL
    assert _refine_media_type(base + "abc_t_W.dat", MediaType.OTHER) is MediaType.THUMBNAIL
    assert _refine_media_type(base + "abc_h.dat", MediaType.OTHER) is MediaType.IMAGE
    assert _refine_media_type(base + "abc_W.dat", MediaType.OTHER) is MediaType.IMAGE
    assert _refine_media_type(base + "abc.dat", MediaType.OTHER) is MediaType.IMAGE
    assert (
        _refine_media_type("msg\\attach\\8a8b\\2026-03\\Video\\abc_W.dat", MediaType.OTHER)
        is MediaType.VIDEO
    )


def test_rec_zone_attachments_classified_by_extension_not_directory():
    """``Rec`` holds merged-forward chat-record attachments of ARBITRARY types.

    The probed real account stores a 109 MB design PDF under
    ``Rec\\<hash>\\F\\3``; the scanner's Rec rule called it IMAGE and preview
    failed with a decode error.  A clear extension must outrank the directory.
    Every assertion runs the real scanner->refine pipeline.
    """
    from pathlib import Path

    from wechat_cleaner.application.real_workflow import _refine_media_type
    from wechat_cleaner.domain.contracts import MediaType
    from wechat_cleaner.scanner.scanner import _classify_media

    def pipeline(relative: str) -> MediaType:
        return _refine_media_type(relative, _classify_media(tuple(Path(relative).parts)))

    rec = "msg\\attach\\6c49b54c\\2025-07\\Rec\\77b8511b\\F\\3\\"
    assert pipeline(rec + "设计方案.pdf") is MediaType.FILE
    assert pipeline("msg\\attach\\8a8b\\2025-05\\Rec\\abc\\F\\0\\clip.mp4") is MediaType.VIDEO
    assert pipeline("msg\\attach\\8a8b\\2025-05\\Rec\\abc\\F\\0\\memo.amr") is MediaType.VOICE
    # Image-family names and extension-less/_t names keep the scanner verdict.
    assert pipeline(rec + "photo.jpg") is MediaType.IMAGE
    assert pipeline(rec + "abc.dat") is MediaType.IMAGE
    assert pipeline(rec + "abc_t") is MediaType.THUMBNAIL
    # Outside Rec nothing changes even when the extension is document-like.
    assert pipeline("msg\\attach\\8a8b\\2025-05\\Img\\abc.pdf") is MediaType.IMAGE
