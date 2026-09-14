"""Verify a built read-only Beta artifact end-to-end.

This is the acceptance gate for ``P4-REAL-PACKAGE-01``.  Unlike
``packaging/build.py::verify()`` (which inspects files), this script actually
**launches the frozen executable** and inspects the window it produces, so the
assertions hold for the shipped artifact rather than for the source tree.

Usage::

    python packaging/verify_artifact.py                 # newest build
    python packaging/verify_artifact.py --artifact DIR  # explicit build
    python packaging/verify_artifact.py --json          # machine-readable

Checks performed
----------------

1. the executable exists and carries the license files;
2. the frozen app starts and produces the read-only window (no import crash);
3. **no delete / cleanup / execute entry point appears in the UI** — this is the
   structural guarantee the Beta is sold on, asserted on the real artifact;
4. cache controls are present, and every other mutating affordance is either an
   app-cache action or a user-confirmed, recoverable (export / recycle-bin)
   operation on a single file or a multi-selection batch.

Exit codes: ``0`` all checks passed, ``1`` a check failed, ``2`` no artifact.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist"
LATEST_MARKER = DIST_DIR / "LATEST.txt"
APP_NAME = "wechat-space-manager"

# Labels that would indicate a write/delete capability reached the UI.
# NOTE: "回收站" alone is NOT forbidden — since F-20260911-011 the app offers a
# per-file, user-confirmed "move to recycle bin" (Shell FOF_ALLOWUNDO, audited
# receipt).  Permanent deletion stays forbidden in every wording.
FORBIDDEN_LABELS = (
    "删除",
    "清理微信",
    "清理源文件",
    "移动到",
    "执行清理",
    "生成清理计划",
    "确认删除",
    "永久删除",
    "Dry Run",
)

REQUIRED_CACHE_LABELS = (
    "清除解码缓存",
)

# Anything containing these allows a mutating control (all app-cache scoped).
ALLOWED_MUTATING_PREFIXES = (
    "清除",
    "刷新",
)

# Sanctioned per-file actions: explicit user confirmation each time, audited.
# Export writes decrypted bytes the user chose to keep; recycle moves go to the
# OS recycle bin (recoverable).  The "批量…" variants apply the same per-file
# operations to a multi-selection behind one up-front confirmation, and each
# file still gets its own audited outcome.
# 「仅合并转发记录」is a read-only filter checkbox, not a mutating control.
SANCTIONED_ACTION_LABELS = (
    "预览图片",
    "预览视频",
    "播放",
    "暂停",
    "移到回收站…",
    "导出原图…",
    "导出后移入回收站…",
    "批量导出原图…",
    "批量移到回收站…",
    "批量导出后移入回收站…",
    "仅合并转发记录",
)


def resolve_artifact(explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    if LATEST_MARKER.is_file():
        candidate = Path(LATEST_MARKER.read_text(encoding="utf-8").strip())
        if candidate.is_dir():
            return candidate
    # Fall back to the newest timestamped directory.
    candidates = sorted(p for p in DIST_DIR.glob(f"{APP_NAME}-*") if p.is_dir())
    return candidates[-1] if candidates else None


def run_self_test(artifact: Path) -> tuple[int, dict]:
    """Launch the frozen binary in self-test mode and parse its JSON report."""
    exe = artifact / f"{APP_NAME}.exe"
    # Force UTF-8 on the child: a Windows console may default to a legacy code
    # page, and the self-test report contains Chinese labels.
    env = dict(
        os.environ,
        WCSM_SELF_TEST="1",
        QT_QPA_PLATFORM="offscreen",
        PYTHONIOENCODING="utf-8",
        PYTHONUTF8="1",
    )
    completed = subprocess.run(
        [str(exe)],
        cwd=str(artifact),
        env=env,
        capture_output=True,
        check=False,
        timeout=90,
    )
    stdout = (completed.stdout or b"").decode("utf-8", errors="replace").strip()
    report: dict = {}
    # The report is the last JSON object on stdout.
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                report = json.loads(line)
            except json.JSONDecodeError:
                continue
            break
    return completed.returncode, report


def verify(artifact: Path) -> tuple[bool, dict]:
    failures: list[str] = []
    details: dict = {"artifact": str(artifact)}

    exe = artifact / f"{APP_NAME}.exe"
    if not exe.is_file():
        return False, {"artifact": str(artifact), "failures": ["executable missing"]}
    details["exe_bytes"] = exe.stat().st_size

    data_root = artifact / "_internal"
    if not data_root.is_dir():
        data_root = artifact
    for rel in ("licenses/wechatauto-replica-LICENSE.txt", "licenses/THIRD-PARTY-NOTICES.md"):
        path = data_root / rel
        details[rel] = path.is_file()
        if not path.is_file():
            failures.append(f"license file missing: {rel}")

    # Launch the frozen artifact and read its real interactive surface.
    code, report = run_self_test(artifact)
    details["self_test_exit"] = code
    details["report"] = report

    if not report.get("ok"):
        failures.append("frozen app did not produce a readable self-test report")
        return (not failures), {"artifact": str(artifact), "failures": failures, "details": details}

    details["title"] = report.get("title", "")
    buttons = report.get("buttons", [])
    details["buttons"] = buttons
    details["minimum_size"] = report.get("minimum_size")
    details["resized_to"] = report.get("resized_to")
    details["clipped_buttons"] = report.get("clipped_buttons")

    # Layout contract: the frozen window must be shrinkable and must not hide
    # its own controls.  Checked explicitly here as well as in the self-test so
    # a regression names the failing fact instead of a bare "ok: false".
    minimum_size = report.get("minimum_size") or [0, 0]
    if minimum_size[1] > 700:
        failures.append(
            f"window minimum height is {minimum_size[1]}px: vertical resize is blocked"
        )
    resized_to = report.get("resized_to") or [0, 0]
    if resized_to[1] != 640:
        failures.append(
            f"resize to 640px was clamped to {resized_to[1]}px: vertical resize is blocked"
        )
    clipped = report.get("clipped_buttons") or []
    if clipped:
        failures.append(f"controls unreachable at a small window size: {clipped}")

    for label in buttons:
        for forbidden in FORBIDDEN_LABELS:
            if forbidden in label:
                failures.append(f"forbidden UI entry point present: {label!r}")

    for required in REQUIRED_CACHE_LABELS:
        if not any(required in b for b in buttons):
            failures.append(f"expected cache control missing: {required}")

    # Any button that is not navigation/sanctioned action must be app-cache scoped.
    for label in buttons:
        if not label:
            continue
        if any(label.startswith(p) for p in ALLOWED_MUTATING_PREFIXES):
            continue
        if label in {"自动检测", "连接", "重新加载数据库", "应用筛选"}:
            continue
        if label in SANCTIONED_ACTION_LABELS:
            continue
        if "缓存" in label:
            continue
        failures.append(f"unexpected interactive control: {label!r}")

    result = {"artifact": str(artifact), "failures": failures, "details": details}
    return (not failures), result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", default=None, help="artifact directory to verify")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)

    artifact = resolve_artifact(args.artifact)
    if artifact is None:
        print("no build artifact found; run `python packaging/build.py` first")
        return 2

    ok, result = verify(artifact)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"[package-verify] artifact: {artifact}")
        details = result.get("details", {})
        print(f"[package-verify] exe bytes: {details.get('exe_bytes', 'n/a')}")
        print(f"[package-verify] window title: {details.get('title', 'n/a')}")
        print(f"[package-verify] window minimum: {details.get('minimum_size', 'n/a')}")
        print(f"[package-verify] resized to 1024x640 -> {details.get('resized_to', 'n/a')}")
        print(f"[package-verify] unreachable controls: {details.get('clipped_buttons', 'n/a')}")
        print(f"[package-verify] buttons: {details.get('buttons', [])}")
        if result["failures"]:
            print("[package-verify] FAILED:")
            for item in result["failures"]:
                print(f"  - {item}")
        else:
            print("[package-verify] PASSED: frozen read-only Beta has no cleanup entry point")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
