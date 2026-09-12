"""Report whether P3 private acceptance is ready for a human operator.

This checker intentionally does not read the private fixture directory, a WeChat
directory, a database, or any media.  Its output is a valid terminal result for
an AI evaluator: ``blocked`` means public/synthetic prerequisites remain;
``ready_for_human`` means the AI must stop and hand control to the user.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

_TARGET_ID = re.compile(r"^\s{2}- id: ([A-Za-z0-9_-]+)\s*$")
_STATUS = re.compile(r"^\s{4}status: ([A-Za-z0-9_-]+)\s*$")
_PRIVATE_PREFIXES = (
    "tests/fixtures_private.local/",
    "fixtures/private/",
    "private/",
)


def _target_statuses(path: Path) -> dict[str, str]:
    statuses: dict[str, str] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        target = _TARGET_ID.match(line)
        if target:
            current = target.group(1)
            continue
        status = _STATUS.match(line)
        if current is not None and status:
            statuses[current] = status.group(1)
    return statuses


def _tracked_files(repo: Path) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return ("<git-tracking-check-unavailable>",)
    return tuple(
        item.replace("\\", "/")
        for item in completed.stdout.decode("utf-8", errors="replace").split("\0")
        if item
    )


def build_report(
    repo: Path,
    *,
    tracked_files: tuple[str, ...] | None = None,
) -> dict[str, object]:
    """Build a path-free readiness report without opening private inputs."""

    statuses = _target_statuses(repo / "coordination" / "targets.yaml")
    checks: list[dict[str, object]] = []
    blockers: list[str] = []

    def add_check(
        name: str,
        passed: bool,
        passed_detail: str,
        failure_detail: str,
    ) -> None:
        detail = passed_detail if passed else failure_detail
        checks.append({"name": name, "passed": passed, "detail": detail})
        if not passed:
            blockers.append(failure_detail)

    for target_id in ("P3-GUI-FLOW-01", "P3-ENTRY-01", "P3-INTEGRATION-01"):
        actual = statuses.get(target_id, "missing")
        add_check(
            f"target:{target_id}",
            actual == "done",
            f"{target_id} is done",
            f"{target_id} status is {actual}; expected done",
        )

    required_files = (
        "plan/P3-PRIVATE-ACCEPT-01-private-acceptance.md",
        "tests/fixtures_private.local.example/README.md",
        "tests/fixtures_private.local.example/private-baseline.local.example.json",
    )
    for relative in required_files:
        add_check(
            f"template:{relative}",
            repo.joinpath(*relative.split("/")).is_file(),
            f"required handoff file exists: {relative}",
            f"required handoff file is missing: {relative}",
        )

    ignore_text = (repo / ".gitignore").read_text(encoding="utf-8")
    add_check(
        "private-ignore-rule",
        "tests/fixtures_private.local/" in ignore_text,
        "private local fixture ignore rule is present",
        "private local fixture ignore rule is missing",
    )

    tracked = tracked_files if tracked_files is not None else _tracked_files(repo)
    tracking_available = "<git-tracking-check-unavailable>" not in tracked
    add_check(
        "git-tracking-check",
        tracking_available,
        "git private tracking check completed",
        "git private tracking check is unavailable",
    )
    if tracking_available:
        leaked = tuple(
            item
            for item in tracked
            if any(item.startswith(prefix) for prefix in _PRIVATE_PREFIXES)
        )
        add_check(
            "private-files-untracked",
            not leaked,
            "no private fixture path is tracked by Git",
            "one or more private fixture paths are tracked by Git",
        )

    ready = not blockers
    return {
        "target": "P3-PRIVATE-ACCEPT-01",
        "state": "ready_for_human" if ready else "blocked",
        "actor": "human_operator",
        "ai_private_execution_allowed": False,
        "private_data_accessed": False,
        "manual": "docs/project-layout-and-acceptance-manual.md",
        "checks": checks,
        "blockers": blockers,
        "next_action": (
            "AI stops and hands the manual to the human operator."
            if ready
            else "Resolve public or synthetic blockers; do not inspect private data."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="return exit code 2 when the state is blocked",
    )
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    report = build_report(repo)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.require_ready and report["state"] != "ready_for_human":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
