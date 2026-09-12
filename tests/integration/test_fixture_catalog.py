"""Integration-adjacent checks for Phase 1 fixture infrastructure only."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

FIXTURE_DIRECTORY = Path(__file__).parents[1] / "fixtures_synthetic"


def _load_builder_module():
    module_path = FIXTURE_DIRECTORY / "build_fixture.py"
    specification = importlib.util.spec_from_file_location("synthetic_fixture_builder", module_path)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def test_synthetic_fixture_builder_has_stable_regular_file_inventory(tmp_path: Path) -> None:
    builder = _load_builder_module()
    report = builder.build_fixture(tmp_path / "xwechat_files")
    manifest = json.loads((FIXTURE_DIRECTORY / "fixture-manifest.json").read_text("utf-8"))

    assert report.regular_file_count == manifest["baseline_regular_file_count"]
    assert report.regular_file_bytes == manifest["baseline_regular_file_bytes"]
    rec_directory = (
        tmp_path
        / "xwechat_files"
        / "wxid_synthetic_alpha_d729"
        / "msg"
        / "attach"
        / "8a8b0c0d0e0f10111213141516171819"
        / "2026-02"
        / "Rec"
        / "record-alpha-0001"
    )
    assert {path.name for path in rec_directory.iterdir()} == {"0", "0_t", "1", "1_t"}


def test_private_baseline_template_has_no_real_path_or_identity() -> None:
    template_path = (
        Path(__file__).parents[1]
        / "fixtures_private.local.example"
        / "private-baseline.local.example.json"
    )
    template = json.loads(template_path.read_text("utf-8"))
    assert template["classification"] == "private-local-only"
    assert template["source_root"].startswith("REPLACE_WITH_")
    assert template["forwarded_record_acceptance_fixture"]["source_path"].startswith(
        "REPLACE_WITH_"
    )


def test_symlink_probe_is_explicit_and_non_blocking(tmp_path: Path) -> None:
    builder = _load_builder_module()
    fixture_root = tmp_path / "xwechat_files"
    report = builder.build_fixture(fixture_root, with_symlink_probe=True)

    assert report.symlink_probe_status in {"created", "unavailable"}
    if report.symlink_probe_status == "created":
        assert (fixture_root / "reparse-probes" / "escape-link").is_symlink()
