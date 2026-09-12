"""Run the strict Phase 3 synthetic integration gate.

The gate is intentionally separate from the older Phase 2 checker.  It emits
machine-readable checks, failures, unresolved governance items and timings,
while never opening a private WeChat path or generating a private fixture.
Use ``--allow-unresolved`` only to inspect the complete report while upstream
verification or optional Qt dependencies are still outstanding.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    elapsed_ms: float
    command: tuple[str, ...]
    failures: tuple[str, ...] = ()
    output_tail: str = ""


_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:[\\/][^\r\n ]*")
_TARGET_ID = re.compile(r"^\s{2}- id: ([A-Za-z0-9_-]+)\s*$")
_STATUS = re.compile(r"^\s{4}status: ([A-Za-z0-9_-]+)\s*$")
_DEPENDS = re.compile(r"^\s{4}depends_on: \[([^]]*)\]\s*$")
_OWNERSHIP_PATH = re.compile(r"^\s{4}- path: (.+?)\s*$")


def _redact(text: str, repo: Path) -> str:
    """Remove local paths from captured child-process output."""

    values = {
        str(repo),
        str(repo).replace("/", "\\"),
        str(repo).replace("\\", "/"),
        str(Path(tempfile.gettempdir())),
    }
    for value in values:
        text = text.replace(value, "<local-path>")
    return _WINDOWS_PATH.sub("<local-path>", text)


def _run(
    repo: Path,
    name: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    skip_reason: str | None = None,
) -> Check:
    if skip_reason is not None:
        return Check(name, "skipped", 0.0, tuple(args), output_tail=skip_reason)
    started = time.perf_counter()
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    completed = subprocess.run(
        [sys.executable, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=child_env,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    output = _redact((completed.stdout + completed.stderr).strip(), repo)
    failures = () if completed.returncode == 0 else (f"returncode={completed.returncode}",)
    return Check(
        name=name,
        status="passed" if completed.returncode == 0 else "failed",
        elapsed_ms=elapsed_ms,
        command=tuple(args),
        failures=failures,
        output_tail=output[-2_000:],
    )


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _parse_targets(path: Path) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    statuses: dict[str, str] = {}
    dependencies: dict[str, tuple[str, ...]] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        target = _TARGET_ID.match(line)
        if target:
            current = target.group(1)
            continue
        if current is None:
            continue
        status = _STATUS.match(line)
        if status:
            statuses[current] = status.group(1)
            continue
        depends = _DEPENDS.match(line)
        if depends:
            values = tuple(item.strip() for item in depends.group(1).split(",") if item.strip())
            dependencies[current] = values
    return statuses, dependencies


def _governance_check(repo: Path, *, allow_unresolved: bool) -> tuple[Check, tuple[str, ...]]:
    statuses, dependencies = _parse_targets(repo / "coordination" / "targets.yaml")
    unresolved: list[str] = []
    visited: set[str] = set()

    def visit(target_id: str) -> None:
        if target_id in visited:
            return
        visited.add(target_id)
        for dependency in dependencies.get(target_id, ()):
            if statuses.get(dependency) != "done":
                unresolved.append(
                    f"{dependency} status is {statuses.get(dependency, 'missing')}, expected done"
                )
            visit(dependency)

    visit("P3-INTEGRATION-01")

    locks = repo.joinpath("coordination", "contract-lock.yaml").read_text(encoding="utf-8")
    active_locks = [
        f"active contract lock at line {number}"
        for number, line in enumerate(locks.splitlines(), start=1)
        if re.search(r"^\s+active:\s+true\s*$", line)
    ]
    unresolved.extend(active_locks)

    ownership_lines = repo.joinpath("coordination", "path-ownership.yaml").read_text(
        encoding="utf-8"
    ).splitlines()
    owned_paths = []
    for line in ownership_lines:
        match = _OWNERSHIP_PATH.match(line)
        if match:
            owned_paths.append(match.group(1))
    duplicates = sorted({path for path in owned_paths if owned_paths.count(path) > 1})
    unresolved.extend(f"duplicate ownership rule: {path}" for path in duplicates)

    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo,
        capture_output=True,
        check=False,
    ).stdout.decode("utf-8", errors="replace").split("\0")
    private_prefixes = (
        "tests/fixtures_private.local/",
        "fixtures/private/",
        "private/",
    )
    tracked_private = [
        item for item in tracked if item and item.replace("\\", "/").startswith(private_prefixes)
    ]
    unresolved.extend(f"private path is tracked: {item}" for item in tracked_private)

    if unresolved and not allow_unresolved:
        status = "failed"
    elif unresolved:
        status = "warning"
    else:
        status = "passed"
    return (
        Check(
            name="governance",
            status=status,
            elapsed_ms=0.0,
            command=("governance", "P3-INTEGRATION-01"),
            failures=tuple(unresolved),
            output_tail=(
                "; ".join(unresolved)
                if unresolved
                else "dependencies, locks and ownership pass"
            ),
        ),
        tuple(unresolved),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-unresolved",
        action="store_true",
        help="report but do not fail on verification dependencies or unavailable optional Qt",
    )
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    checks: list[Check] = []

    governance, unresolved = _governance_check(repo, allow_unresolved=args.allow_unresolved)
    checks.append(governance)
    checks.extend(
        (
            _run(repo, "static", ["-m", "ruff", "check", "src", "tests", "contracts"]),
            _run(
                repo,
                "compileall",
                ["-m", "compileall", "-q", "src", "tests", "contracts"],
            ),
            _run(
                repo,
                "contracts_and_fixtures",
                ["-m", "pytest", "tests/contract", "tests/fixtures_synthetic", "-q"],
            ),
            _run(
                repo,
                "scanner_mapper_decoder",
                ["-m", "pytest", "tests/scanner", "tests/mapper", "tests/decoder", "-q"],
            ),
            _run(
                repo,
                "index_database_adapter_filter",
                [
                    "-m",
                    "pytest",
                    "tests/local_index",
                    "tests/db_adapter",
                    "tests/filtering",
                    "-q",
                ],
            ),
            _run(
                repo,
                "performance_100k",
                [
                    "-m",
                    "pytest",
                    "tests/local_index/test_local_index.py::test_one_hundred_thousand_streaming_records_and_index_query",
                    "-q",
                ],
            ),
            _run(
                repo,
                "cleanup_executor_workflow",
                ["-m", "pytest", "tests/cleanup", "tests/executor", "tests/application", "-q"],
            ),
            _run(repo, "integration_tests", ["-m", "pytest", "tests/integration", "-q"]),
            _run(repo, "entrypoint_tests", ["-m", "pytest", "tests/entrypoint", "-q"]),
            _run(repo, "entrypoint_help", ["-m", "wechat_cleaner", "--help"]),
            _run(repo, "entrypoint_legacy_help", ["-m", "wechat_cleaner", "legacy", "--help"]),
        )
    )

    qt_available = _module_available("PySide6") and _module_available("pytestqt")
    if qt_available:
        checks.append(
            _run(
                repo,
                "gui_offscreen",
                ["-m", "pytest", "tests/gui", "-q"],
                env={"QT_QPA_PLATFORM": "offscreen"},
            )
        )
    else:
        reason = (
            "PySide6 and pytest-qt are not installed; "
            "install -e .[gui,test] to run Qt offscreen"
        )
        checks.append(_run(repo, "gui_offscreen", [], skip_reason=reason))
        unresolved = (*unresolved, "gui_offscreen optional dependencies unavailable")

    checks.append(
        _run(
            repo,
            "full_tests",
            ["-m", "pytest", "-q"],
            env={"QT_QPA_PLATFORM": "offscreen"},
        )
    )
    failures = [
        f"{check.name}: {failure}"
        for check in checks
        if check.status == "failed"
        for failure in (check.failures or ("check failed",))
    ]
    strict_unresolved = tuple(unresolved) if not args.allow_unresolved else ()
    payload = {
        "gate": "P3-INTEGRATION-01",
        "status": "failed"
        if failures or strict_unresolved
        else ("passed_with_warnings" if unresolved else "passed"),
        "python": "<runtime>",
        "checks": [asdict(check) for check in checks],
        "failures": failures,
        "unresolved": list(unresolved),
        "timings_ms": {check.name: check.elapsed_ms for check in checks},
        "allow_unresolved": args.allow_unresolved,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if failures:
        return 1
    if strict_unresolved:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
