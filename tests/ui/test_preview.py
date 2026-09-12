"""Preview orchestration: decoder failures surface as outcomes, not raises."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

from wechat_cleaner.domain.contracts import ContractErrorCode, DecodeVariant
from wechat_cleaner.ui import request_preview
from wechat_cleaner.ui.preview import artifact_path, open_artifact

PREVIEW_STEM = "msg/attach/8a8b0c0d0e0f10111213141516171819/2026-02/Image/preview"


def _record_for(stager, png_factory, record_factory, relative=PREVIEW_STEM):
    identity = stager(f"{relative}.png", png_factory())
    return record_factory(
        relative_path=identity.relative_path,
        byte_size=identity.byte_size,
        modified_time_ns=identity.modified_time_ns,
    )


def test_preview_success_returns_artifact(stager, png_factory, record_factory, cache_root):
    record = _record_for(stager, png_factory, record_factory)
    outcome = request_preview(record, copy_root=str(stager.root), cache_root=cache_root)

    assert outcome.error is None
    assert outcome.artifact is not None
    assert outcome.artifact.output_format == "PNG"
    assert PureWindowsPath(outcome.artifact.output_file.relative_path).parts[0] == "sessions"


def test_preview_thumbnail_request_sets_edge(stager, png_factory, record_factory, cache_root):
    record = _record_for(stager, png_factory, record_factory)
    outcome = request_preview(
        record,
        copy_root=str(stager.root),
        cache_root=cache_root,
        variant=DecodeVariant.THUMBNAIL,
        max_edge_px=16,
    )

    assert outcome.artifact is not None
    output = artifact_path(outcome.artifact, cache_root=cache_root)
    from PIL import Image

    with Image.open(output) as thumb:
        thumb.load()
        assert max(thumb.size) <= 16


def test_preview_failure_becomes_contract_error(stager, png_factory, record_factory, cache_root):
    record = _record_for(stager, png_factory, record_factory)
    (stager.root / record.file.relative_path).unlink()  # approved copy disappears

    outcome = request_preview(record, copy_root=str(stager.root), cache_root=cache_root)

    assert outcome.artifact is None
    assert outcome.error is not None
    assert outcome.error.code is ContractErrorCode.FILE_IDENTITY_MISMATCH


def test_artifact_path_resolves_inside_cache_root(stager, png_factory, record_factory, cache_root):
    record = _record_for(stager, png_factory, record_factory)
    outcome = request_preview(record, copy_root=str(stager.root), cache_root=cache_root)
    artifact = outcome.artifact
    assert artifact is not None

    resolved = artifact_path(artifact, cache_root=cache_root)
    assert resolved.is_file()
    assert Path(cache_root).resolve() in resolved.resolve().parents


def test_open_artifact_uses_injected_opener(stager, png_factory, record_factory, cache_root):
    record = _record_for(stager, png_factory, record_factory)
    outcome = request_preview(record, copy_root=str(stager.root), cache_root=cache_root)
    artifact = outcome.artifact
    assert artifact is not None

    opened: list[str] = []
    opened_path = open_artifact(
        artifact, cache_root=cache_root, opener=lambda path: opened.append(path)
    )
    assert opened == [str(opened_path)]
    assert opened_path.is_file()


def test_open_artifact_missing_file_raises(stager, png_factory, record_factory, cache_root):
    record = _record_for(stager, png_factory, record_factory)
    outcome = request_preview(record, copy_root=str(stager.root), cache_root=cache_root)
    artifact = outcome.artifact
    assert artifact is not None

    resolved = artifact_path(artifact, cache_root=cache_root)
    resolved.unlink()
    with pytest.raises(FileNotFoundError):
        open_artifact(artifact, cache_root=cache_root, opener=lambda path: None)
