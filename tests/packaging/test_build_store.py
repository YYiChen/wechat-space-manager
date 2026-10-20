"""Build registry, LATEST pointer and retention policy tests.

Everything here runs against temporary synthetic directories: no real build,
no real dist tree and no deletion outside ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGING_DIR = REPO_ROOT / "packaging"
sys.path.insert(0, str(PACKAGING_DIR))

import build_store  # type: ignore[import-not-found]  # noqa: E402


def _stamp(hours: int = 0) -> str:
    moment = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=hours)
    return moment.isoformat()


def _make_success(
    store: build_store.BuildStore,
    build_id: str,
    version: str,
    *,
    hours: int = 0,
    payload: bytes = b"artifact",
) -> Path:
    artifact = store.dist_dir / f"wechat-space-manager-{build_id}"
    artifact.mkdir(parents=True)
    (artifact / "wechat-space-manager.exe").write_bytes(b"MZ" + payload)
    internal = artifact / "_internal"
    internal.mkdir()
    (internal / "data.bin").write_bytes(payload)
    manifest = {
        "app": "wechat-space-manager",
        "version": version,
        "build_id": build_id,
        "built_at": _stamp(hours),
        "verified_at": _stamp(hours),
        "status": "success",
        "source_revision": f"rev-{build_id}",
    }
    (artifact / "BUILD-MANIFEST.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    store.start(build_id, version, work_dir=store.build_dir / build_id)
    store.finish(
        build_id,
        build_store.STATUS_SUCCESS,
        version=version,
        manifest=manifest,
    )
    return artifact


def _make_failed(
    store: build_store.BuildStore,
    build_id: str,
    *,
    status: str = build_store.STATUS_VERIFY_FAILED,
) -> Path:
    artifact = store.dist_dir / f"wechat-space-manager-{build_id}"
    artifact.mkdir(parents=True)
    # A failed attempt can leave a partial tree; it must never become current.
    (artifact / "wechat-space-manager.exe").write_bytes(b"MZ")
    store.start(build_id, "0.0.0", work_dir=store.build_dir / build_id)
    store.finish(build_id, status, error="synthetic failure")
    return artifact


def _rewrite_record(store: build_store.BuildStore, build_id: str, **fields) -> None:
    payload = store.load()
    for record in payload["builds"]:
        if record.get("build_id") == build_id:
            record.update(fields)
    store._save(payload)


# -- LATEST portability -------------------------------------------------------


def test_latest_is_relative_and_survives_move_with_spaces(tmp_path) -> None:
    store = build_store.BuildStore(tmp_path / "dist", tmp_path / "build")
    artifact = _make_success(store, "20260101T000000Z", "0.1.0")

    store.write_latest(artifact)

    pointer = (store.dist_dir / "LATEST.txt").read_text(encoding="utf-8").strip()
    assert pointer == artifact.name
    assert not Path(pointer).is_absolute()

    moved_root = tmp_path / "new root with spaces" / "dist"
    shutil.copytree(store.dist_dir, moved_root)
    moved = build_store.BuildStore(moved_root, tmp_path / "new root with spaces" / "build")
    assert moved.resolve_latest() == moved_root / artifact.name


def test_legacy_absolute_latest_falls_back_when_the_old_root_is_gone(tmp_path) -> None:
    store = build_store.BuildStore(tmp_path / "old" / "dist", tmp_path / "old" / "build")
    artifact = _make_success(store, "20260101T000000Z", "0.1.0")
    (store.dist_dir / "LATEST.txt").write_text(str(artifact), encoding="utf-8")

    moved_root = tmp_path / "moved root" / "dist"
    shutil.copytree(store.dist_dir, moved_root)
    shutil.rmtree(store.dist_dir)  # simulate the old machine/root disappearing

    moved = build_store.BuildStore(moved_root, tmp_path / "moved root" / "build")
    assert moved.resolve_latest() == moved_root / artifact.name


def test_manifest_time_not_directory_name_decides_the_newest(tmp_path) -> None:
    store = build_store.BuildStore(tmp_path / "dist", tmp_path / "build")
    # Lexicographically "zzzz" sorts last, but it is the older build.
    newer = _make_success(store, "aaaa", "0.1.6", hours=10)
    _make_success(store, "zzzz", "0.1.5", hours=0)

    assert store.resolve_latest() == newer


def test_failed_verification_keeps_the_previous_latest(tmp_path) -> None:
    store = build_store.BuildStore(tmp_path / "dist", tmp_path / "build")
    good = _make_success(store, "20260101T000000Z", "0.1.5")
    store.write_latest(good)
    before = (store.dist_dir / "LATEST.txt").read_text(encoding="utf-8")

    failed = _make_failed(store, "20260102T000000Z")
    record = store.record_for("20260102T000000Z")

    assert record is not None and record["status"] == build_store.STATUS_VERIFY_FAILED
    assert (store.dist_dir / "LATEST.txt").read_text(encoding="utf-8") == before
    assert store.resolve_latest() == good
    assert failed.is_dir()  # retained for diagnosis, just not current


def test_abandoned_started_build_is_reviewed_then_cleanable(tmp_path) -> None:
    store = build_store.BuildStore(tmp_path / "dist", tmp_path / "build")
    artifact = store.dist_dir / "wechat-space-manager-20260102T000000Z"
    artifact.mkdir(parents=True)
    (artifact / "wechat-space-manager.exe").write_bytes(b"MZ")
    store.start("20260102T000000Z", "0.1.6", work_dir=store.build_dir / "20260102T000000Z")
    _rewrite_record(store, "20260102T000000Z", started_at=_stamp(24))

    fresh = store.plan_retention(now=datetime(2026, 1, 2, 1, tzinfo=UTC))
    assert any(
        item["kind"] == "abandoned_build" and item["deletable"] is False
        for item in fresh["review"]
    )

    stale = store.plan_retention(now=datetime(2026, 1, 4, tzinfo=UTC))
    candidate = next(
        item for item in stale["candidates"] if item["kind"] == "abandoned_build"
    )
    assert candidate["deletable"] is True


def test_pointer_to_a_failed_artifact_is_ignored(tmp_path) -> None:
    store = build_store.BuildStore(tmp_path / "dist", tmp_path / "build")
    good = _make_success(store, "20260101T000000Z", "0.1.5")
    failed = _make_failed(store, "20260102T000000Z")
    # Simulate a crash that wrote LATEST before registering failure.
    (store.dist_dir / "LATEST.txt").write_text(failed.name, encoding="utf-8")

    assert store.resolve_latest() == good


# -- version alias ------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction")
def test_version_alias_is_a_junction_not_a_second_copy(tmp_path) -> None:
    store = build_store.BuildStore(tmp_path / "dist", tmp_path / "build")
    artifact = _make_success(store, "20260101T000000Z", "0.1.5")
    before = build_store.tree_bytes(artifact)

    result = store.ensure_version_alias("0.1.5", artifact)

    assert result.status == "created"
    assert result.alias.is_junction()
    assert result.alias.resolve() == artifact.resolve()
    assert build_store.tree_bytes(result.alias) == before
    # Idempotent re-run does not repoint an existing release alias.
    again = store.ensure_version_alias("0.1.5", artifact)
    assert again.status == "unchanged"


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction")
def test_version_alias_never_overwrites_a_real_directory(tmp_path) -> None:
    store = build_store.BuildStore(tmp_path / "dist", tmp_path / "build")
    artifact = _make_success(store, "20260101T000000Z", "0.1.6")
    old = store.dist_dir / build_store.BuildStore.version_alias_name("0.1.6")
    old.mkdir()
    (old / "keep.txt").write_text("old release", encoding="utf-8")

    result = store.ensure_version_alias("0.1.6", artifact)

    assert result.status == "conflict"
    assert (old / "keep.txt").read_text(encoding="utf-8") == "old release"


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction")
def test_broken_alias_after_dist_move_is_repaired(tmp_path) -> None:
    store = build_store.BuildStore(tmp_path / "dist", tmp_path / "build")
    artifact = _make_success(store, "20260101T000000Z", "0.1.5")
    created = store.ensure_version_alias("0.1.5", artifact)
    assert created.status == "created"

    moved_dist = tmp_path / "moved root with spaces" / "dist"
    moved_dist.parent.mkdir(parents=True)
    shutil.move(store.dist_dir, moved_dist)
    moved = build_store.BuildStore(moved_dist, tmp_path / "moved root with spaces" / "build")

    plan = moved.plan_retention()
    assert any(item["kind"] == "broken_version_alias" for item in plan["review"])

    results = moved.repair_aliases()
    alias = moved_dist / build_store.BuildStore.version_alias_name("0.1.5")
    assert any(item["status"] == "created" for item in results)
    assert alias.is_junction()
    assert alias.resolve() == (moved_dist / artifact.name).resolve()


# -- retention ----------------------------------------------------------------


def test_retention_keeps_current_and_rollback_and_bounds_history(tmp_path) -> None:
    store = build_store.BuildStore(tmp_path / "dist", tmp_path / "build")
    artifacts = [
        _make_success(store, f"2026010{index}T000000Z", f"0.1.{index}", hours=index)
        for index in range(1, 6)
    ]
    store.write_latest(artifacts[-1])

    plan = store.plan_retention()

    assert plan["current"] == str(artifacts[-1])
    assert plan["rollback"] == str(artifacts[-2])
    kept = {item["path"] for item in plan["kept"] if item["kind"] in {"current", "rollback"}}
    assert kept == {str(artifacts[-1]), str(artifacts[-2])}
    assert any(
        item["kind"] == "retained_success" and item["path"] == str(artifacts[-3])
        for item in plan["kept"]
    )
    superseded = [
        item for item in plan["candidates"] if item["kind"] == "superseded_release"
    ]
    assert {item["path"] for item in superseded} == {str(item) for item in artifacts[:2]}
    assert all(item["deletable"] for item in superseded)

    results = store.apply_retention(plan)

    assert any(item["removed"] for item in results)
    for artifact in artifacts[:2]:
        assert not artifact.exists()
    for artifact in artifacts[2:]:
        assert artifact.is_dir()
    assert store.resolve_latest() == artifacts[-1]


def test_superseded_release_protected_by_version_junction_is_not_deletable(tmp_path) -> None:
    if os.name != "nt":
        pytest.skip("NTFS junction")
    store = build_store.BuildStore(tmp_path / "dist", tmp_path / "build")
    artifacts = [
        _make_success(store, f"2026010{index}T000000Z", f"0.1.{index}", hours=index)
        for index in range(1, 6)
    ]
    store.write_latest(artifacts[-1])
    alias = store.ensure_version_alias("0.1.1", artifacts[0])
    assert alias.status == "created"

    plan = store.plan_retention()
    candidate = next(
        item for item in plan["candidates"] if item["path"] == str(artifacts[0])
    )

    assert candidate["deletable"] is False
    assert "版本联接" in candidate["reason"]
    store.apply_retention(plan)
    assert artifacts[0].is_dir()


def test_failed_builds_are_bounded_by_count_and_age(tmp_path) -> None:
    store = build_store.BuildStore(tmp_path / "dist", tmp_path / "build")
    failed = []
    for index in range(7):
        build_id = f"2026010{index}T000000Z"
        failed.append(_make_failed(store, build_id))
        _rewrite_record(
            store,
            build_id,
            finished_at=(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index)).isoformat(),
        )
    # The oldest record is also older than the day threshold.
    _rewrite_record(
        store,
        "20260100T000000Z",
        finished_at=(datetime(2025, 12, 1, tzinfo=UTC)).isoformat(),
    )
    # A synthetic extra failed record with an old timestamp but no directory is
    # enough to exercise the age rule; the count rule is exercised by the seven.
    plan = store.plan_retention(now=datetime(2026, 1, 10, tzinfo=UTC))
    candidates = [
        item for item in plan["candidates"] if item["kind"] == "failed_build"
    ]

    assert len(candidates) == 7
    assert sum(1 for item in candidates if item["deletable"]) >= 2
    assert all("failed_build" == item["kind"] for item in candidates)


def test_apply_retention_refuses_an_outside_path(tmp_path) -> None:
    store = build_store.BuildStore(tmp_path / "dist", tmp_path / "build")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not ours", encoding="utf-8")
    plan = {
        "current": None,
        "rollback": None,
        "candidates": [
            {
                "path": str(outside),
                "kind": "forged",
                "logical_bytes": 1,
                "deletable": True,
                "reason": "synthetic",
            }
        ],
    }

    results = store.apply_retention(plan)

    assert results[0]["removed"] is False
    assert "越界" in results[0]["reason"]
    assert (outside / "keep.txt").is_file()


def test_work_cache_is_protected_for_current_and_cleanable_when_orphan(tmp_path) -> None:
    store = build_store.BuildStore(tmp_path / "dist", tmp_path / "build")
    current = _make_success(store, "20260101T000000Z", "0.1.5")
    store.write_latest(current)
    for name in ("20260101T000000Z", "upstream-staging", "20990101T000000Z"):
        work = store.build_dir / name
        work.mkdir(parents=True)
        (work / "cache.bin").write_bytes(b"x" * 1024)

    old = datetime(2025, 1, 1, tzinfo=UTC).timestamp()
    for name in ("20260101T000000Z", "upstream-staging", "20990101T000000Z"):
        os.utime(store.build_dir / name, (old, old))
    plan = store.plan_retention(now=datetime(2026, 1, 10, tzinfo=UTC))
    by_path = {item["path"]: item for item in plan["candidates"]}

    # The current release keeps its work cache so `--verify-only` can still
    # inspect the PYZ archive.  Orphan caches and staging have no such role.
    assert by_path[str(store.build_dir / "20260101T000000Z")]["deletable"] is False
    assert by_path[str(store.build_dir / "upstream-staging")]["deletable"] is True
    assert by_path[str(store.build_dir / "20990101T000000Z")]["deletable"] is True
