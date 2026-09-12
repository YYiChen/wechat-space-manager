"""Tests for the packaging layer (no build required).

These tests guard the *policy* encoded in the packaging scripts rather than the
produced artifact: the spec must be read-only, the verifier must reject a
bundle that carries a cleanup path or private data, and the frozen entry point
must never reach an execution module.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGING_DIR = REPO_ROOT / "packaging"

sys.path.insert(0, str(PACKAGING_DIR))


@pytest.fixture()
def build_module():
    import build  # type: ignore[import-not-found]

    return build


@pytest.fixture()
def stage_module():
    import stage_upstream  # type: ignore[import-not-found]

    return stage_upstream


# --- upstream staging policy -------------------------------------------------


def test_stage_only_copies_read_only_modules(stage_module, tmp_path, monkeypatch) -> None:
    """The staged upstream package must contain only db/media plus a bare init."""
    fake_pkg = tmp_path / "source" / "wechatauto"
    fake_pkg.mkdir(parents=True)
    (fake_pkg / "db.py").write_text("# db", encoding="utf-8")
    (fake_pkg / "media.py").write_text("# media", encoding="utf-8")
    (fake_pkg / "sender.py").write_text("# write path", encoding="utf-8")
    (fake_pkg / "moment.py").write_text("# write path", encoding="utf-8")
    (fake_pkg.parent / "LICENSE").write_text("Apache License", encoding="utf-8")

    monkeypatch.setattr(stage_module, "STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(stage_module, "locate_upstream_package", lambda: fake_pkg)

    root = stage_module.stage()

    staged = sorted(p.name for p in (root / "wechatauto").iterdir() if p.is_file())
    assert staged == ["__init__.py", "db.py", "media.py"]
    assert (root / "wechatauto-replica-LICENSE.txt").is_file()


def test_staged_init_does_not_import_write_surfaces(stage_module) -> None:
    init_source = stage_module.PACKAGE_INIT
    for forbidden in ("sender", "moment", "guia", "uia", "wx"):
        assert f"from .{forbidden}" not in init_source
    assert "from .db import" in init_source
    assert "from .media import" in init_source


def test_stage_module_allowlist_is_minimal(stage_module) -> None:
    assert set(stage_module.ALLOWED_MODULES) == {"db.py", "media.py"}


# --- spec policy -------------------------------------------------------------


def test_spec_excludes_cleanup_and_executor() -> None:
    spec = (PACKAGING_DIR / "wechat-space-manager.spec").read_text(encoding="utf-8")
    assert "wechat_cleaner.executor" in spec
    assert "wechat_cleaner.cleanup" in spec


def test_spec_bundles_license_material() -> None:
    spec = (PACKAGING_DIR / "wechat-space-manager.spec").read_text(encoding="utf-8")
    assert "wechatauto-replica-LICENSE.txt" in spec
    assert "THIRD-PARTY-NOTICES.md" in spec


def test_license_files_exist() -> None:
    licenses = PACKAGING_DIR / "licenses"
    assert (licenses / "wechatauto-replica-LICENSE.txt").is_file()
    notice = licenses / "THIRD-PARTY-NOTICES.md"
    assert notice.is_file()
    text = notice.read_text(encoding="utf-8")
    # Apache-2.0 obligations must be acknowledged explicitly.
    assert "Apache-2.0" in text
    assert "798989c9b61066c120b59ceba636b557b776ff7f" in text


# --- frozen entry point ------------------------------------------------------


def test_frozen_entry_imports_only_the_launcher() -> None:
    source = (PACKAGING_DIR / "frozen_entry.py").read_text(encoding="utf-8")
    assert "from wechat_cleaner.gui.launcher import launch" in source
    # The frozen entry must not import or call any execution/removal primitive.
    for forbidden in (
        "import wechat_cleaner.executor",
        "import wechat_cleaner.cleanup",
        "unlink(",
        "rmtree(",
        "os.remove(",
    ):
        assert forbidden not in source, f"frozen entry must not reference {forbidden!r}"


# --- verifier behaviour ------------------------------------------------------


def _make_artifact(tmp_path, build_module, *, with_licenses: bool = True):
    """Build a fake one-dir artifact matching PyInstaller 6's layout."""
    artifact = tmp_path / "wechat-space-manager"
    artifact.mkdir()
    (artifact / "wechat-space-manager.exe").write_bytes(b"MZ")
    internal = artifact / "_internal"
    internal.mkdir()
    if with_licenses:
        for rel in build_module.REQUIRED_LICENSE_FILES:
            target = internal / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
    # Pad the tree so the size heuristic does not fire first.
    (internal / "pad.bin").write_bytes(b"\0" * (21 * 1024 * 1024))
    return artifact, internal


def test_verify_fails_when_artifact_missing(build_module, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(build_module, "ARTIFACT_DIR", tmp_path / "nope")
    assert build_module.verify() == 2


def test_verify_fails_when_license_missing(build_module, tmp_path, monkeypatch) -> None:
    artifact, _ = _make_artifact(tmp_path, build_module, with_licenses=False)
    monkeypatch.setattr(build_module, "ARTIFACT_DIR", artifact)
    assert build_module.verify() == 2


def test_verify_detects_forbidden_module(build_module, tmp_path, monkeypatch) -> None:
    artifact, internal = _make_artifact(tmp_path, build_module)
    (internal / "executor").mkdir()
    monkeypatch.setattr(build_module, "ARTIFACT_DIR", artifact)
    assert build_module.verify() == 2


def test_verify_detects_privacy_pattern(build_module, tmp_path, monkeypatch) -> None:
    artifact, internal = _make_artifact(tmp_path, build_module)
    (internal / "leak.json").write_text('{"p":"C:/!D/xwechat_files"}', encoding="utf-8")
    monkeypatch.setattr(build_module, "ARTIFACT_DIR", artifact)
    assert build_module.verify() == 2


def test_verify_passes_on_clean_artifact(build_module, tmp_path, monkeypatch) -> None:
    artifact, _ = _make_artifact(tmp_path, build_module)
    monkeypatch.setattr(build_module, "ARTIFACT_DIR", artifact)
    assert build_module.verify() == 0
