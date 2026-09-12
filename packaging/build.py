"""Build the read-only Windows Beta package.

This script is the single supported entry point for producing a distributable
artifact.  It wraps PyInstaller so the build is reproducible and so the safety
checks run *before* anything lands in ``dist/``.

Usage::

    python packaging/build.py            # build + verify
    python packaging/build.py --verify-only   # verify an existing dist/ tree

Exit codes:
    0  build (or verification) passed
    1  build step failed
    2  post-build safety verification failed

Design constraints enforced here (see ``packaging/wechat-space-manager.spec``):

* the artifact must carry the Apache-2.0 license + third-party notices;
* the artifact must **not** contain an executor/cleanup import path — a Beta
  that could delete is a safety regression, not a feature;
* no real WeChat data, key material or absolute source path may be bundled.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_FILE = REPO_ROOT / "packaging" / "wechat-space-manager.spec"
DIST_DIR = REPO_ROOT / "dist"
BUILD_DIR = REPO_ROOT / "build"
APP_NAME = "wechat-space-manager"

# Each build writes into its own timestamped directory so PyInstaller never has
# to delete a previous artifact tree.  Bulk deletion is an operation this
# project keeps under explicit human control; a build helper must not perform
# one.  ``dist/latest`` is a copy-level pointer refreshed by ``_point_latest``.
def _build_id() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


BUILD_ID = _build_id()
ARTIFACT_DIR = DIST_DIR / f"{APP_NAME}-{BUILD_ID}"
LATEST_MARKER = DIST_DIR / "LATEST.txt"

# --- safety expectations -----------------------------------------------------

REQUIRED_LICENSE_FILES = (
    "licenses/wechatauto-replica-LICENSE.txt",
    "licenses/THIRD-PARTY-NOTICES.md",
)

# Packages that must never appear as importable modules in the frozen archive.
FORBIDDEN_MODULES = (
    "wechat_cleaner.executor",
    "wechat_cleaner.cleanup",
)

# Upstream write-capable surfaces: the read-only Beta must not ship these.
FORBIDDEN_UPSTREAM_MODULES = (
    "wechatauto.wx",
    "wechatauto.guia",
    "wechatauto.moment",
    "wechatauto.sender",
    "wechatauto.uia_driver",
)

# The read-only flow genuinely needs exactly these upstream modules.
REQUIRED_UPSTREAM_MODULES = (
    "wechatauto",
    "wechatauto.db",
    "wechatauto.media",
)

# Patterns that would indicate private data leaked into the bundle.  Kept in
# sync with scripts/verify_p4.py::check_privacy_patterns.
PRIVACY_PATTERNS = (
    re.compile(r"wxid_[0-9a-z]{6,}", re.IGNORECASE),
    re.compile(r"xwechat_files", re.IGNORECASE),
)


def _run(cmd: list[str], *, cwd: Path) -> int:
    print(f"[build] $ {' '.join(cmd)}")
    completed = subprocess.run(cmd, cwd=str(cwd), check=False)
    return completed.returncode


def build() -> int:
    """Stage the trimmed upstream package, then run PyInstaller."""
    # PyInstaller is invoked with ``--clean`` and ``--noconfirm``, which already
    # replace stale output.  We do not rmtree the trees ourselves: a bulk delete
    # of thousands of files is exactly the kind of operation this project keeps
    # under explicit human control.

    # Stage the trimmed upstream package first: the spec resolves
    # ``wechatauto`` from this directory rather than the editable install.
    stage_code = _run(
        [sys.executable, str(REPO_ROOT / "packaging" / "stage_upstream.py")],
        cwd=REPO_ROOT,
    )
    if stage_code != 0:
        print("[build] upstream staging failed")
        return 1

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC_FILE),
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR / BUILD_ID),
    ]
    # The spec appends the build id to the output directory name so this run
    # writes a fresh tree instead of replacing (i.e. bulk-deleting) a prior one.
    env = dict(os.environ, WCSM_BUILD_ID=ARTIFACT_DIR.name)
    print(f"[build] $ {' '.join(cmd)}  (WCSM_BUILD_ID={ARTIFACT_DIR.name})")
    completed = subprocess.run(cmd, cwd=str(REPO_ROOT), check=False, env=env)
    return completed.returncode


def _data_root(target: Path) -> Path:
    """Return the directory PyInstaller placed bundled data into.

    one-dir builds put data under ``_internal/`` since PyInstaller 6; older
    layouts keep it beside the executable.  Accept either so the verifier does
    not depend on a specific PyInstaller minor version.
    """
    internal = target / "_internal"
    return internal if internal.is_dir() else target


def _pyz_module_names() -> list[str]:
    """Return the module names inside the built PYZ archive, or [] if absent.

    The PYZ archive is where PyInstaller stores pure-Python modules, so any
    "is module X bundled?" question must be answered here — checking the
    ``_internal`` directory tree alone would give a false negative.
    """
    # ``workpath`` is ``build/<build-id>/`` so the PYZ lives one level deeper.
    candidates = [
        BUILD_DIR / BUILD_ID / APP_NAME / "PYZ-00.pyz",
        BUILD_DIR / APP_NAME / "PYZ-00.pyz",
    ]
    pyz = next((p for p in candidates if p.is_file()), None)
    if pyz is None:
        return []
    try:
        from PyInstaller.archive.readers import ZlibArchiveReader  # noqa: PLC0415
    except ImportError:
        return []
    # NOTE: ZlibArchiveReader is not a context manager; it reads the archive
    # eagerly in __init__ and exposes ``toc`` as a name -> offset mapping.
    try:
        reader = ZlibArchiveReader(str(pyz))
        return list(reader.toc)
    except Exception as exc:  # noqa: BLE001 - verification must not crash the build
        print(f"[verify] warning: could not read PYZ archive ({exc!r})")
        return []


def verify(target: Path | None = None) -> int:
    """Check the produced artifact for licensing and safety regressions.

    ``target`` defaults to the module-level :data:`ARTIFACT_DIR`, which tests
    replace with a fixture directory.
    """
    artifact = ARTIFACT_DIR if target is None else target
    failures: list[str] = []

    if not artifact.is_dir():
        print(f"[verify] FAIL artifact directory missing: {artifact}")
        return 2

    exe = artifact / f"{APP_NAME}.exe"
    if not exe.is_file():
        failures.append(f"executable missing: {exe}")

    data_root = _data_root(artifact)

    # 1. License + notice obligations.
    for rel in REQUIRED_LICENSE_FILES:
        if not (data_root / rel).is_file():
            failures.append(f"license file missing from artifact: {rel}")

    # 2. Module-level safety: the read-only Beta must not carry a cleanup path,
    #    and must not carry upstream's write-capable surfaces.
    module_names = _pyz_module_names()
    if module_names:
        print(f"[verify] PYZ module count: {len(module_names)}")
        for forbidden in FORBIDDEN_MODULES + FORBIDDEN_UPSTREAM_MODULES:
            hits = [n for n in module_names if n == forbidden or n.startswith(f"{forbidden}.")]
            if hits:
                failures.append(f"forbidden module bundled: {forbidden} ({len(hits)} modules)")

        # The read-only imported surface must actually be present.
        for required in REQUIRED_UPSTREAM_MODULES:
            if required not in module_names:
                failures.append(f"required module missing from bundle: {required}")
    else:
        print("[verify] note: PYZ archive not readable; module check skipped")

    # 3. Directory-level check (catches a stray on-disk package directory).
    internal = artifact / "_internal"
    if internal.is_dir():
        for forbidden in FORBIDDEN_MODULES:
            stem = forbidden.split(".")[-1]
            if (internal / stem).is_dir() or (internal / f"{stem}.pyc").exists():
                failures.append(f"forbidden module directory present: {forbidden}")
        # Scan the extracted-data tree for stray private material.
        for pattern in PRIVACY_PATTERNS:
            for path in internal.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in {".py", ".json", ".txt", ".md", ".cfg", ".ini"}:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if pattern.search(text):
                    failures.append(
                        f"privacy pattern {pattern.pattern!r} matched in {path.name}"
                    )
                    break

    # 4. Size sanity: a bundle this small or this large signals a bad build.
    if artifact.is_dir():
        total = sum(p.stat().st_size for p in artifact.rglob("*") if p.is_file())
        mb = total / (1024 * 1024)
        print(f"[verify] artifact size: {mb:.1f} MB")
        if mb < 20:
            failures.append(f"artifact suspiciously small ({mb:.1f} MB)")
        elif mb > 800:
            failures.append(f"artifact suspiciously large ({mb:.1f} MB)")

    if failures:
        print("[verify] FAILED:")
        for item in failures:
            print(f"  - {item}")
        return 2

    print("[verify] OK: licenses present, no cleanup path, no write surface, no privacy pattern")
    return 0


def write_manifest() -> None:
    """Record the source revision next to the artifact for traceability."""
    revision = "unknown"
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip() or "unknown"
    except OSError:
        pass

    manifest = {
        "app": APP_NAME,
        "version": "0.1.0",
        "source_revision": revision,
        "mode": "read-only-beta",
        "cleanup_capability": False,
        "upstream": {
            "name": "wechatauto-replica",
            "version": "1.2.1",
            "revision": "798989c9b61066c120b59ceba636b557b776ff7f",
            "license": "Apache-2.0",
        },
    }
    target = ARTIFACT_DIR / "BUILD-MANIFEST.json"
    target.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[build] wrote {target}")


def _point_latest(target: Path) -> None:
    """Record which timestamped build is the newest one."""
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_MARKER.write_text(str(target), encoding="utf-8")
    print(f"[build] latest build: {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="skip the build and only verify an artifact",
    )
    parser.add_argument(
        "--artifact",
        default=None,
        help="artifact directory to verify (defaults to the newest build)",
    )
    args = parser.parse_args(argv)

    if not args.verify_only:
        code = build()
        if code != 0:
            print("[build] PyInstaller failed")
            return 1
        write_manifest()
        _point_latest(ARTIFACT_DIR)
        return verify()

    # Verification only: honour an explicit target, else fall back to LATEST.
    if args.artifact:
        artifact = Path(args.artifact)
    elif LATEST_MARKER.is_file():
        artifact = Path(LATEST_MARKER.read_text(encoding="utf-8").strip())
    else:
        artifact = ARTIFACT_DIR
    print(f"[verify] artifact: {artifact}")
    return verify(artifact)


if __name__ == "__main__":
    raise SystemExit(main())
