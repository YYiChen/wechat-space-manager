"""Run the integration gate and report pending modules without hiding failures.

The default mode validates the currently available cross-module boundary.  Once
cleanup/UI tests and upstream decoder fixes are merged, run without
``--allow-pending`` and add ``--full`` for the repository-wide gate.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    returncode: int
    command: tuple[str, ...]
    output_tail: str


def _run(repo: Path, name: str, args: list[str]) -> Check:
    completed = subprocess.run(
        [sys.executable, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (completed.stdout + completed.stderr).strip()
    return Check(
        name=name,
        status="passed" if completed.returncode == 0 else "failed",
        returncode=completed.returncode,
        command=tuple(args),
        output_tail=output[-2_000:],
    )


def _pending(repo: Path) -> list[str]:
    required = (
        ("decoder_source", repo / "src" / "wechat_cleaner" / "decoder"),
        ("decoder_tests", repo / "tests" / "decoder"),
        ("scanner_source", repo / "src" / "wechat_cleaner" / "scanner"),
        ("scanner_tests", repo / "tests" / "scanner"),
        ("mapper_source", repo / "src" / "wechat_cleaner" / "mapper"),
        ("mapper_tests", repo / "tests" / "mapper"),
        ("cleanup_source", repo / "src" / "wechat_cleaner" / "cleanup"),
        ("cleanup_tests", repo / "tests" / "cleanup"),
        ("ui_source", repo / "src" / "wechat_cleaner" / "ui"),
        ("ui_tests", repo / "tests" / "ui"),
    )
    return [name for name, path in required if not path.exists()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-pending", action="store_true")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    checks: list[Check] = []

    test_paths = [
        "tests/integration",
        "tests/contract",
        "tests/scanner",
        "tests/mapper",
        "tests/decoder",
    ]
    for optional_path in ("tests/cleanup", "tests/ui"):
        if (repo / optional_path).exists():
            test_paths.append(optional_path)

    checks.append(
        _run(
            repo,
            "integration_tests",
            ["-m", "pytest", *test_paths, "-q"],
        )
    )
    static_paths = [
        "src/wechat_cleaner/scanner",
        "src/wechat_cleaner/mapper",
        "src/wechat_cleaner/decoder",
        "tests/integration",
        "tests/scanner",
        "tests/mapper",
        "tests/decoder",
        "contracts",
    ]
    optional_static_paths = (
        "src/wechat_cleaner/cleanup",
        "tests/cleanup",
        "src/wechat_cleaner/ui",
        "tests/ui",
    )
    for optional_path in optional_static_paths:
        if (repo / optional_path).exists():
            static_paths.append(optional_path)
    checks.append(
        _run(
            repo,
            "owned_static_checks",
            ["-m", "ruff", "check", *static_paths],
        )
    )
    checks.append(_run(repo, "compileall", ["-m", "compileall", "-q", "src", "tests", "contracts"]))
    if args.full:
        checks.append(
            _run(repo, "full_static_checks", ["-m", "ruff", "check", "src", "tests", "contracts"])
        )
        checks.append(_run(repo, "full_tests", ["-m", "pytest", "-q"]))

    pending = _pending(repo)
    payload = {
        "checks": [asdict(check) for check in checks],
        "pending": pending,
        "allow_pending": args.allow_pending,
        "full": args.full,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if any(check.status == "failed" for check in checks):
        return 1
    if pending and not args.allow_pending:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
