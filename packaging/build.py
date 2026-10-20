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
from datetime import UTC, datetime
from pathlib import Path

PACKAGING_DIR = Path(__file__).resolve().parent
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

from build_store import (  # noqa: E402
    STATUS_BUILD_FAILED,
    STATUS_SUCCESS,
    STATUS_VERIFY_FAILED,
    BuildStore,
    human_bytes,
)

REPO_ROOT = PACKAGING_DIR.parent
SPEC_FILE = REPO_ROOT / "packaging" / "wechat-space-manager.spec"
APP_NAME = "wechat-space-manager"
DIST_DIR = REPO_ROOT / "dist"
BUILD_DIR = REPO_ROOT / "build"
BUILD_ID = ""
ARTIFACT_DIR = DIST_DIR / APP_NAME
LATEST_MARKER = DIST_DIR / "LATEST.txt"
STORE = BuildStore(DIST_DIR, BUILD_DIR)


def _build_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def project_version() -> str:
    """Read the release version from ``pyproject.toml`` (single source of truth)."""
    try:
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def configure_paths(
    dist_root: Path | str | None = None,
    build_root: Path | str | None = None,
    *,
    build_id: str | None = None,
) -> BuildStore:
    """Point the module at a dist/build pair (used by isolated build tests)."""
    global DIST_DIR, BUILD_DIR, BUILD_ID, ARTIFACT_DIR, LATEST_MARKER, STORE
    DIST_DIR = Path(dist_root).resolve() if dist_root else REPO_ROOT / "dist"
    BUILD_DIR = Path(build_root).resolve() if build_root else REPO_ROOT / "build"
    BUILD_ID = build_id or _build_id()
    ARTIFACT_DIR = DIST_DIR / f"{APP_NAME}-{BUILD_ID}"
    LATEST_MARKER = DIST_DIR / "LATEST.txt"
    STORE = BuildStore(DIST_DIR, BUILD_DIR)
    return STORE


configure_paths()

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


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> int:
    print(f"[build] $ {' '.join(cmd)}")
    completed = subprocess.run(cmd, cwd=str(cwd), check=False, env=env)
    return completed.returncode


def build() -> int:
    """Stage the trimmed upstream package, then run PyInstaller."""
    # PyInstaller is invoked with ``--clean`` and ``--noconfirm``, which already
    # replace stale output.  We do not rmtree the trees ourselves: a bulk delete
    # of thousands of files is exactly the kind of operation this project keeps
    # under explicit human control.

    # Stage the trimmed upstream package first: the spec resolves
    # ``wechatauto`` from this directory rather than the editable install.
    # The staging root follows BUILD_DIR so an isolated build never writes to
    # the repository's build/ tree (and two builds cannot share staging).
    staging_root = BUILD_DIR / "upstream-staging"
    stage_code = _run(
        [
            sys.executable,
            str(REPO_ROOT / "packaging" / "stage_upstream.py"),
            "--staging-root",
            str(staging_root),
        ],
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
    env = dict(
        os.environ,
        WCSM_BUILD_ID=ARTIFACT_DIR.name,
        WCSM_STAGING_ROOT=str(staging_root),
    )
    print(
        f"[build] $ {' '.join(cmd)}"
        f"  (WCSM_BUILD_ID={ARTIFACT_DIR.name}, WCSM_STAGING_ROOT={staging_root})"
    )
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


def _pyz_module_names(build_id: str | None = None) -> list[str]:
    """Return the module names inside the built PYZ archive, or [] if absent.

    The PYZ archive is where PyInstaller stores pure-Python modules, so any
    "is module X bundled?" question must be answered here — checking the
    ``_internal`` directory tree alone would give a false negative.
    """
    # ``workpath`` is ``build/<build-id>/`` so the PYZ lives one level deeper.
    candidates = [
        BUILD_DIR / (build_id or BUILD_ID) / APP_NAME / "PYZ-00.pyz",
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
    manifest = {}
    manifest_path = artifact / "BUILD-MANIFEST.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
    build_id = manifest.get("build_id")
    if not build_id:
        prefix = f"{APP_NAME}-"
        build_id = artifact.name[len(prefix):] if artifact.name.startswith(prefix) else None
    module_names = _pyz_module_names(build_id)
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


def write_manifest(version: str) -> dict:
    """Record the source revision and build metadata next to the artifact."""
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

    built_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    manifest = {
        "app": APP_NAME,
        "version": version,
        "build_id": BUILD_ID,
        "built_at": built_at,
        "verified_at": built_at,
        "status": STATUS_SUCCESS,
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
    return manifest


def write_release_root_files(version: str) -> None:
    """Copy the human-readable release files next to the executable.

    Beginners never open ``_internal/licenses``: the disclaimer, the license
    and the notice travel at the artifact root so they are seen before the
    first double-click.  The disclaimer text is generated from
    ``docs/disclaimer.md`` so the two cannot drift apart unnoticed.
    """
    source = REPO_ROOT / "docs" / "disclaimer.md"
    try:
        body = source.read_text(encoding="utf-8")
    except OSError:
        print("[build] warning: docs/disclaimer.md missing; skipping release files")
        return
    header = (
        f"微信空间管理器（只读相册版）v{version} —— 使用前必读\n"
        "（以下内容与仓库 docs/disclaimer.md 一致）\n"
        "========================================\n\n"
    )
    try:
        (ARTIFACT_DIR / "免责声明_使用前必读.txt").write_text(
            header + body, encoding="utf-8-sig"
        )
        for name in ("LICENSE", "NOTICE"):
            blob = (REPO_ROOT / name).read_bytes()
            (ARTIFACT_DIR / f"{name}.txt").write_bytes(blob)
    except OSError as exc:
        print(f"[build] warning: could not write release root files ({exc!r})")
        return
    print(f"[build] wrote release root files to {ARTIFACT_DIR}")


def _point_latest(target: Path) -> str:
    """Atomically record the release; called only after verification passed."""
    value = STORE.write_latest(target)
    print(f"[build] latest build: {target} (LATEST.txt: {value})")
    return value


def _print_retention(plan: dict, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return
    policy = plan["policy"]
    print(
        "[retention] policy: "
        f"成功版保留 {policy['keep_successful']} 个｜"
        f"失败构建保留 {policy['keep_failed_max']} 个 / {policy['keep_failed_days']} 天｜"
        f"中断构建保留 {policy['keep_started_hours']} 小时｜"
        f"工作缓存保留 {policy['keep_work_days']} 天｜ZIP 保留 {policy['keep_zips']} 个"
    )
    print(f"[retention] current: {plan.get('current') or '（无，LATEST 无效）'}")
    if plan.get("rollback"):
        print(f"[retention] rollback: {plan['rollback']}")
    for item in plan["kept"]:
        print(
            f"[retention] keep   [{item['kind']}] {item['path']} "
            f"({human_bytes(item['logical_bytes'])}) — {item['reason']}"
        )
    for item in plan["candidates"]:
        flag = "可以清理" if item.get("deletable") else "仅登记"
        print(
            f"[retention] {flag} [{item['kind']}] {item['path']} "
            f"({human_bytes(item['logical_bytes'])}) — {item['reason']}"
        )
    for item in plan["review"]:
        print(
            f"[retention] 待判断 [{item['kind']}] {item['path']} "
            f"({human_bytes(item['logical_bytes'])}) — {item['reason']}"
        )
    totals = plan["totals"]
    print(
        f"[retention] candidate total: {totals['candidate_human']}；"
        f"deletable after review: {totals['deletable_human']}（本次未删除任何文件）"
    )


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
    parser.add_argument(
        "--dist-root",
        default=None,
        help="override dist/ (isolated build validation)",
    )
    parser.add_argument(
        "--build-root",
        default=None,
        help="override build/ (isolated build validation)",
    )
    parser.add_argument(
        "--build-id",
        default=None,
        help="override the timestamp build id (isolated build validation)",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="override the manifest version (defaults to pyproject.toml)",
    )
    parser.add_argument(
        "--retention-plan",
        action="store_true",
        help="classify artifacts/caches and print a cleanup preview (read-only)",
    )
    parser.add_argument(
        "--apply-retention",
        action="store_true",
        help="apply the previewed cleanup; requires --yes",
    )
    parser.add_argument(
        "--repair-aliases",
        action="store_true",
        help="recreate version junctions whose target disappeared after a dist move",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm --apply-retention (never implied)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="machine-readable output for --retention-plan",
    )
    args = parser.parse_args(argv)

    configure_paths(args.dist_root, args.build_root, build_id=args.build_id)
    version = args.version or project_version()

    if args.retention_plan or args.apply_retention:
        plan = STORE.plan_retention()
        _print_retention(plan, as_json=args.json)
        if not args.apply_retention:
            return 0
        if not args.yes:
            print("[retention] refused: pass --yes together with --apply-retention")
            return 2
        results = STORE.apply_retention(plan)
        removed = [item for item in results if item.get("removed")]
        kept = [item for item in results if not item.get("removed")]
        print(
            f"[retention] applied: removed {len(removed)} item(s), "
            f"kept {len(kept)} item(s)"
        )
        for item in kept:
            print(f"[retention] kept {item['path']} — {item.get('reason')}")
        return 0

    if args.repair_aliases:
        results = STORE.repair_aliases()
        for item in results:
            print(
                f"[alias] {item['status']}: {item['alias']} -> {item['artifact']}"
                f" ({item['detail']})"
            )
        conflicts = [item for item in results if item["status"] in {"conflict", "failed"}]
        return 1 if conflicts else 0

    if not args.verify_only:
        # Register the attempt before PyInstaller runs: a failed build must be
        # distinguishable from a successful one even when it leaves a partial
        # directory behind.
        STORE.start(BUILD_ID, version, work_dir=BUILD_DIR / BUILD_ID)
        code = build()
        if code != 0:
            STORE.finish(
                BUILD_ID,
                STATUS_BUILD_FAILED,
                version=version,
                error=f"PyInstaller exited {code}",
            )
            print("[build] PyInstaller failed; LATEST.txt unchanged")
            return 1
        write_release_root_files(version)
        # Verification happens *before* the manifest, version alias and pointer.
        # A failed verification therefore leaves the previous release current.
        code = verify()
        if code != 0:
            STORE.finish(
                BUILD_ID,
                STATUS_VERIFY_FAILED,
                version=version,
                error=f"verify exited {code}",
            )
            print("[build] verification failed; LATEST.txt unchanged")
            return code
        manifest = write_manifest(version)
        alias = STORE.ensure_version_alias(version, ARTIFACT_DIR)
        print(
            f"[build] version alias: {alias.alias.name} -> {alias.status}"
            f" ({alias.detail})"
        )
        STORE.finish(
            BUILD_ID,
            STATUS_SUCCESS,
            version=version,
            manifest=manifest,
            alias=alias,
        )
        _point_latest(ARTIFACT_DIR)
        _print_retention(STORE.plan_retention())
        return 0

    # Verification only: honour an explicit target, else resolve LATEST with
    # legacy absolute paths, the registry, and manifest timestamps.
    artifact = Path(args.artifact) if args.artifact else STORE.resolve_latest()
    if artifact is None:
        print("[verify] no usable artifact found; run `python packaging/build.py` first")
        return 2
    print(f"[verify] artifact: {artifact}")
    return verify(artifact)


if __name__ == "__main__":
    raise SystemExit(main())
