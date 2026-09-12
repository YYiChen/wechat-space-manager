"""Run the Phase 4 public/synthetic gate for the real-data backends.

The gate never opens a private WeChat path, never extracts a key, and never
decodes real media.  It verifies that:

* the new optional backends import cleanly **without** the upstream package
  (missing dependency must degrade, not crash);
* the full public suite, lint and byte-compilation still pass;
* no real account identifier, source path or delete capability leaked into the
  new modules.

Machine-readable report on stdout; exit 0 only with ``--require-passed``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
P4_SOURCE_DIRS = (
    "src/wechat_cleaner/real_db",
    "src/wechat_cleaner/real_media",
)
P4_FILES = (
    "src/wechat_cleaner/application/real_workflow.py",
    "scripts/probe_real_media_key.py",
)

_FORBIDDEN_PATTERNS = (
    (re.compile(r"wxid_[0-9a-z]{6,}", re.IGNORECASE), "hard-coded account identifier"),
    (re.compile(r"C:\\!D", re.IGNORECASE), "hard-coded real source path"),
    # Only a *top-level* upstream import is forbidden; the adapters import the
    # optional backend lazily inside functions on purpose.
    (re.compile(r"^from\s+wechatauto", re.MULTILINE), "top-level upstream import"),
    (re.compile(r"^import\s+wechatauto", re.MULTILINE), "top-level upstream import"),
)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    elapsed_ms: float
    detail: str = ""


def _run(command: list[str]) -> tuple[bool, str]:
    completed = subprocess.run(
        command, cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    tail = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode == 0, tail.strip().splitlines()[-1] if tail.strip() else ""


def _venv_python() -> str:
    candidate = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    return str(candidate if candidate.exists() else sys.executable)


def check_p4_suites() -> Check:
    python = _venv_python()
    targets = ["tests/real_db", "tests/real_media", "tests/application"]
    started = time.perf_counter()
    ok, tail = _run([python, "-m", "pytest", *targets, "-q"])
    return Check("p4_module_suites", "passed" if ok else "failed",
                 (time.perf_counter() - started) * 1000, tail)


def check_full_suite() -> Check:
    started = time.perf_counter()
    ok, tail = _run([_venv_python(), "-m", "pytest", "tests", "-q"])
    return Check("full_public_suite", "passed" if ok else "failed",
                 (time.perf_counter() - started) * 1000, tail)


def check_lint() -> Check:
    started = time.perf_counter()
    ok, tail = _run(
        [_venv_python(), "-m", "ruff", "check", "src", "tests", "contracts", "scripts"]
    )
    return Check("lint", "passed" if ok else "failed", (time.perf_counter() - started) * 1000, tail)


def check_compile() -> Check:
    started = time.perf_counter()
    ok, tail = _run(
        [_venv_python(), "-m", "compileall", "-q", "src", "tests", "contracts", "scripts"]
    )
    return Check("byte_compile", "passed" if ok else "failed",
                 (time.perf_counter() - started) * 1000, tail)


def check_import_without_upstream() -> Check:
    """Importing the backends must not require the optional upstream package."""
    started = time.perf_counter()
    code = (
        "import importlib.util, sys;"
        "assert importlib.util.find_spec('wechatauto') is None, 'upstream unexpectedly present';"
        "import wechat_cleaner.real_db as a;"
        "import wechat_cleaner.real_media as b;"
        "from wechat_cleaner.application import RealReadOnlySession;"
        "from wechat_cleaner.domain.contracts import ContractErrorCode;"
        "print('imports ok')"
    )
    ok, tail = _run([_venv_python(), "-c", code])
    return Check("imports_without_upstream", "passed" if ok else "failed",
                 (time.perf_counter() - started) * 1000, tail)


def check_privacy_patterns() -> Check:
    """No hard-coded account id, real source path or top-level upstream import."""
    started = time.perf_counter()
    findings: list[str] = []
    files = [
        path
        for directory in P4_SOURCE_DIRS
        for path in (REPO_ROOT / directory).rglob("*.py")
    ] + [REPO_ROOT / name for name in P4_FILES]
    for path in files:
        if not path.exists():
            findings.append(f"missing: {path.name}")
            continue
        text = path.read_text(encoding="utf-8")
        for pattern, label in _FORBIDDEN_PATTERNS:
            if pattern.search(text):
                findings.append(f"{path.name}: {label}")
    status = "passed" if not findings else "failed"
    return Check("privacy_patterns", status, (time.perf_counter() - started) * 1000,
                 "; ".join(findings) or "no forbidden patterns")


def check_no_executor_coupling() -> Check:
    """The real read-only path must not reach the cleanup executor."""
    started = time.perf_counter()
    findings = []
    paths = [REPO_ROOT / "src/wechat_cleaner/application/real_workflow.py"]
    paths.extend((REPO_ROOT / "src/wechat_cleaner/real_media").rglob("*.py"))
    paths.extend((REPO_ROOT / "src/wechat_cleaner/real_db").rglob("*.py"))
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if "wechat_cleaner.executor" in text or "wechat_cleaner.cleanup" in text:
            findings.append(path.name)
    status = "passed" if not findings else "failed"
    return Check("no_executor_coupling", status, (time.perf_counter() - started) * 1000,
                 "; ".join(findings) or "no executor or cleanup imports")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-passed", action="store_true",
                        help="exit non-zero unless every check passes")
    parser.add_argument("--skip-suite", action="store_true",
                        help="skip the pytest checks (fast static-only run)")
    args = parser.parse_args(argv)

    checks: list[Check] = [check_import_without_upstream(), check_privacy_patterns(),
                           check_no_executor_coupling()]
    if not args.skip_suite:
        checks = [check_p4_suites(), *checks, check_full_suite(), check_lint(), check_compile()]

    failures = [check for check in checks if check.status != "passed"]
    payload = {
        "gate": "P4-REAL-INTEGRATION-01",
        "private_data_accessed": False,
        "state": "passed" if not failures else "blocked",
        "checks": [asdict(check) for check in checks],
        "failures": [check.name for check in failures],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.require_passed and failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
