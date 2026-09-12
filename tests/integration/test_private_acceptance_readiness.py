"""Tests for the AI-to-human private acceptance boundary."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _checker():
    path = Path(__file__).resolve().parents[2] / "scripts" / "check_private_acceptance_readiness.py"
    spec = importlib.util.spec_from_file_location("private_acceptance_readiness", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load private acceptance readiness checker")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_repo(tmp_path: Path, *, integration_status: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "coordination").mkdir(parents=True)
    (repo / "plan").mkdir()
    template = repo / "tests" / "fixtures_private.local.example"
    template.mkdir(parents=True)
    (repo / "coordination" / "targets.yaml").write_text(
        "\n".join(
            (
                "targets:",
                "  - id: P3-GUI-FLOW-01",
                "    status: done",
                "  - id: P3-ENTRY-01",
                "    status: done",
                "  - id: P3-INTEGRATION-01",
                f"    status: {integration_status}",
            )
        ),
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("tests/fixtures_private.local/\n", encoding="utf-8")
    (repo / "plan" / "P3-PRIVATE-ACCEPT-01-private-acceptance.md").write_text(
        "# human only\n", encoding="utf-8"
    )
    (template / "README.md").write_text("# template\n", encoding="utf-8")
    (template / "private-baseline.local.example.json").write_text("{}\n", encoding="utf-8")
    return repo


def test_blocked_is_a_path_free_ai_terminal_state(tmp_path: Path) -> None:
    report = _checker().build_report(
        _make_repo(tmp_path, integration_status="verification"),
        tracked_files=(),
    )
    assert report["state"] == "blocked"
    assert report["ai_private_execution_allowed"] is False
    assert report["private_data_accessed"] is False
    assert "P3-INTEGRATION-01 status is verification" in " ".join(report["blockers"])
    assert str(tmp_path) not in str(report)


def test_ready_state_hands_control_to_the_human(tmp_path: Path) -> None:
    report = _checker().build_report(
        _make_repo(tmp_path, integration_status="done"),
        tracked_files=(),
    )
    assert report["state"] == "ready_for_human"
    assert report["blockers"] == []
    assert report["actor"] == "human_operator"
    assert report["next_action"].startswith("AI stops")


def test_tracked_private_path_blocks_handoff(tmp_path: Path) -> None:
    report = _checker().build_report(
        _make_repo(tmp_path, integration_status="done"),
        tracked_files=("tests/fixtures_private.local/private-baseline.local.json",),
    )
    assert report["state"] == "blocked"
    assert "private fixture paths are tracked" in " ".join(report["blockers"])
