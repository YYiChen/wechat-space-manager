"""Build-artifact registry, pointer resolution and retention planning.

The packager writes one timestamped directory per build.  Without a registry
there is no durable way to tell a successful release from a failed attempt, so
older builds pile up and ``LATEST.txt`` has to trust either an absolute path or
the lexicographic order of mixed timestamp/version directory names.  This
module supplies the missing bookkeeping:

* ``build-registry.json`` records every attempt and its outcome;
* ``LATEST.txt`` is written as a path relative to ``dist/`` (old absolute
  values are still read);
* version-named directories are **junctions** to the immutable timestamped
  artifact, never a second extracted copy;
* retention is a *preview* by default.  Only an explicit ``--apply-retention
  --yes`` removes entries the plan marks deletable, and it never touches the
  current release, the rollback release, a version alias or an unregistered
  directory.

Nothing in this module deletes anything at import time or during a normal
build.  The build flow only calls :meth:`BuildStore.plan_retention` to print a
preview for the operator.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

APP_NAME = "wechat-space-manager"
MANIFEST_NAME = "BUILD-MANIFEST.json"
REGISTRY_NAME = "build-registry.json"
LATEST_NAME = "LATEST.txt"
SCHEMA_VERSION = 1

STATUS_STARTED = "started"
STATUS_BUILD_FAILED = "build_failed"
STATUS_VERIFY_FAILED = "verify_failed"
STATUS_SUCCESS = "success"
FAILED_STATUSES = (STATUS_BUILD_FAILED, STATUS_VERIFY_FAILED)

# Retention defaults.  They are a policy, not a deletion action: the operator
# sees the plan first and passes ``--yes`` to apply it.
POLICY: dict[str, int] = {
    "keep_successful": 3,
    "keep_failed_days": 7,
    "keep_failed_max": 5,
    "keep_work_days": 7,
    "keep_zips": 3,
    "keep_started_hours": 24,
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime | None = None) -> str:
    return (moment or utc_now()).astimezone(UTC).replace(microsecond=0).isoformat()


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` beside ``path`` then replace it in one filesystem step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temp, path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def is_junction(path: Path) -> bool:
    """True for an NTFS junction (``Path.is_junction`` exists on 3.12+)."""
    try:
        checker = getattr(path, "is_junction", None)
        return bool(checker()) if callable(checker) else False
    except OSError:
        return False


def _iter_tree(root: Path):
    """Yield descendant files without following symlinks or junctions."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            yield Path(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue


def tree_bytes(root: Path) -> int:
    """Logical bytes below ``root``; junctions are counted once, not followed."""
    if not root.is_dir():
        return 0
    total = 0
    for file_path in _iter_tree(root):
        try:
            total += file_path.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def _safe_relative(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


def _norm(path: Path | str) -> str:
    """Comparable path key; strips the ``\\\\?\\`` prefix junctions report."""
    try:
        text = os.path.realpath(str(path))
    except OSError:
        text = os.path.abspath(str(path))
    if text.startswith("\\\\?\\"):
        text = text[4:]
    elif text.startswith("//?/"):
        text = text[4:]
    return os.path.normcase(os.path.normpath(text))


@dataclass(frozen=True)
class AliasResult:
    alias: Path
    status: str
    detail: str


class BuildStore:
    """Registry and retention planner for one ``dist/`` + ``build/`` pair."""

    def __init__(self, dist_dir: Path | str, build_dir: Path | str | None = None) -> None:
        self.dist_dir = Path(dist_dir)
        self.build_dir = (
            Path(build_dir) if build_dir is not None else self.dist_dir.parent / "build"
        )
        self.registry_path = self.dist_dir / REGISTRY_NAME
        self.latest_path = self.dist_dir / LATEST_NAME

    # -- registry ---------------------------------------------------------

    def load(self) -> dict[str, Any]:
        payload = read_json(self.registry_path)
        if payload is None:
            return {"schema": SCHEMA_VERSION, "updated_at": None, "builds": []}
        builds = payload.get("builds")
        if not isinstance(builds, list):
            builds = []
        return {
            "schema": payload.get("schema", SCHEMA_VERSION),
            "updated_at": payload.get("updated_at"),
            "builds": [item for item in builds if isinstance(item, dict)],
        }

    def _save(self, payload: dict[str, Any]) -> None:
        payload["schema"] = SCHEMA_VERSION
        payload["updated_at"] = iso()
        atomic_write_json(self.registry_path, payload)

    @staticmethod
    def _find(payload: dict[str, Any], build_id: str) -> dict[str, Any] | None:
        for record in payload["builds"]:
            if record.get("build_id") == build_id:
                return record
        return None

    @staticmethod
    def _upsert(payload: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
        existing = BuildStore._find(payload, str(record["build_id"]))
        if existing is None:
            payload["builds"].append(record)
            return record
        existing.update(record)
        return existing

    def start(
        self,
        build_id: str,
        version: str,
        *,
        work_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        payload = self.load()
        record = {
            "build_id": build_id,
            "artifact": f"{APP_NAME}-{build_id}",
            "version": version,
            "status": STATUS_STARTED,
            "started_at": iso(),
            "finished_at": None,
            "error": None,
            "source_revision": None,
            "built_at": None,
            "verified_at": None,
            "work_dir": str(work_dir) if work_dir is not None else None,
            "alias": None,
        }
        self._upsert(payload, record)
        self._save(payload)
        return record

    def finish(
        self,
        build_id: str,
        status: str,
        *,
        version: str | None = None,
        error: str | None = None,
        manifest: dict[str, Any] | None = None,
        alias: AliasResult | None = None,
    ) -> dict[str, Any]:
        payload = self.load()
        record = self._find(payload, build_id)
        if record is None:
            record = {
                "build_id": build_id,
                "artifact": f"{APP_NAME}-{build_id}",
                "version": version or "unknown",
                "started_at": iso(),
                "work_dir": None,
            }
            payload["builds"].append(record)
        record["status"] = status
        record["finished_at"] = iso()
        if version:
            record["version"] = version
        if error:
            record["error"] = error
        if manifest:
            record["source_revision"] = manifest.get("source_revision")
            record["built_at"] = manifest.get("built_at")
            record["verified_at"] = manifest.get("verified_at")
        if alias is not None:
            record["alias"] = {
                "path": alias.alias.name,
                "status": alias.status,
                "detail": alias.detail,
            }
        self._save(payload)
        return record

    def record_for(self, build_id: str) -> dict[str, Any] | None:
        return self._find(self.load(), build_id)

    def _record_for_artifact(self, artifact_name: str) -> dict[str, Any] | None:
        for record in self.load()["builds"]:
            if record.get("artifact") == artifact_name:
                return record
        return None

    # -- pointer ----------------------------------------------------------

    def _read_pointer(self) -> Path | None:
        try:
            raw = self.latest_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        raw = raw.strip('"')
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.dist_dir / raw
        return candidate

    def _manifest(self, artifact: Path) -> dict[str, Any]:
        return read_json(artifact / MANIFEST_NAME) or {}

    def _manifest_built_at(self, artifact: Path) -> datetime | None:
        manifest = self._manifest(artifact)
        for key in ("built_at", "verified_at", "finished_at"):
            parsed = parse_time(manifest.get(key))
            if parsed is not None:
                return parsed
        return None

    def _is_usable_artifact(self, artifact: Path) -> bool:
        if not artifact.is_dir() or is_junction(artifact):
            return False
        if not (artifact / f"{APP_NAME}.exe").is_file():
            return False
        record = self._record_for_artifact(artifact.name)
        if record is not None and record.get("status") not in (None, STATUS_SUCCESS):
            return False
        return True

    def resolve_latest(self) -> Path | None:
        """Resolve the current release, tolerating old and moved pointers.

        Order: a valid ``LATEST.txt`` value (absolute legacy path or relative
        name) -> the newest registry success -> the artifact with the newest
        manifest ``built_at``.  Directory names are never compared
        lexicographically: mixed ``v0.1.5`` / timestamp names are not a clock.
        """
        pointer = self._read_pointer()
        if pointer is not None and self._is_usable_artifact(pointer):
            return pointer

        payload = self.load()
        registered: list[tuple[datetime, Path]] = []
        for record in payload["builds"]:
            if record.get("status") != STATUS_SUCCESS:
                continue
            artifact = self.dist_dir / str(record.get("artifact", ""))
            if not self._is_usable_artifact(artifact):
                continue
            stamp = (
                self._manifest_built_at(artifact)
                or parse_time(record.get("built_at"))
                or parse_time(record.get("finished_at"))
                or parse_time(record.get("started_at"))
                or datetime.min.replace(tzinfo=UTC)
            )
            registered.append((stamp, artifact))
        if registered:
            return max(registered, key=lambda item: item[0])[1]

        scanned: list[tuple[datetime, Path]] = []
        if self.dist_dir.is_dir():
            for child in self.dist_dir.iterdir():
                if not self._is_usable_artifact(child):
                    continue
                stamp = self._manifest_built_at(child)
                if stamp is None:
                    try:
                        stamp = datetime.fromtimestamp(child.stat().st_mtime, tz=UTC)
                    except OSError:
                        continue
                scanned.append((stamp, child))
        if scanned:
            return max(scanned, key=lambda item: item[0])[1]
        return None

    def write_latest(self, artifact: Path) -> str:
        """Atomically point ``LATEST.txt`` at ``artifact`` (relative when possible)."""
        try:
            value = artifact.resolve().relative_to(self.dist_dir.resolve()).as_posix()
        except (OSError, ValueError):
            value = str(artifact.resolve())
        atomic_write_text(self.latest_path, value + "\n")
        return value

    # -- version alias ----------------------------------------------------

    @staticmethod
    def version_alias_name(version: str) -> str:
        clean = version.strip().lstrip("vV")
        return f"{APP_NAME}-v{clean}-win64-portable"

    def repair_aliases(self) -> list[dict[str, Any]]:
        """Rebuild broken version junctions after moving ``dist/``.

        A junction stores an absolute target, so moving the dist tree breaks
        it while ``LATEST.txt`` keeps working (it is relative).  Only a broken
        junction is removed and recreated; a junction whose target still exists
        and every real directory are left untouched.
        """
        results: list[dict[str, Any]] = []
        for item in self._success_artifacts():
            version = item.get("version")
            artifact = item["artifact"]
            if not version:
                continue
            alias = self.dist_dir / self.version_alias_name(str(version))
            if is_junction(alias):
                try:
                    target = Path(os.readlink(alias))
                except OSError:
                    target = None
                if target is not None and _norm(target) == _norm(artifact):
                    results.append(
                        {
                            "alias": str(alias),
                            "artifact": str(artifact),
                            "status": "unchanged",
                            "detail": "联接已正确",
                        }
                    )
                    continue
                if target is not None and target.exists():
                    results.append(
                        {
                            "alias": str(alias),
                            "artifact": str(artifact),
                            "status": "conflict",
                            "detail": f"联接仍指向存在的 {target}，未覆盖",
                        }
                    )
                    continue
                try:
                    os.rmdir(alias)
                except OSError as exc:
                    results.append(
                        {
                            "alias": str(alias),
                            "artifact": str(artifact),
                            "status": "failed",
                            "detail": f"无法移除失效联接：{exc}",
                        }
                    )
                    continue
            elif alias.exists():
                results.append(
                    {
                        "alias": str(alias),
                        "artifact": str(artifact),
                        "status": "conflict",
                        "detail": "同名实体目录存在，未覆盖",
                    }
                )
                continue
            created = self.ensure_version_alias(str(version), artifact)
            results.append(
                {
                    "alias": str(created.alias),
                    "artifact": str(artifact),
                    "status": created.status,
                    "detail": created.detail,
                }
            )
        return results

    def ensure_version_alias(self, version: str, artifact: Path) -> AliasResult:
        """Create a junction alias; never replace a real directory or another release."""
        alias = self.dist_dir / self.version_alias_name(version)
        if alias.exists() or is_junction(alias):
            if is_junction(alias):
                try:
                    target = Path(os.readlink(alias))
                except OSError as exc:
                    return AliasResult(alias, "conflict", f"无法读取联接目标：{exc}")
                if _norm(target) == _norm(artifact):
                    return AliasResult(alias, "unchanged", f"已指向 {target}")
                return AliasResult(
                    alias,
                    "conflict",
                    f"已有版本联接指向 {target}，未覆盖；请人工确认发布策略",
                )
            return AliasResult(
                alias,
                "conflict",
                "同名实体目录已存在（可能是旧发布副本），未覆盖；交给空间整理核验",
            )
        if os.name != "nt":
            return AliasResult(alias, "unsupported", "非 Windows 平台不创建目录联接")
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(artifact)],
            capture_output=True,
            check=False,
            text=True,
        )
        if completed.returncode != 0 or not alias.exists():
            detail = (completed.stderr or completed.stdout or "").strip()
            return AliasResult(alias, "failed", detail or "mklink 失败")
        return AliasResult(alias, "created", str(artifact))

    # -- retention --------------------------------------------------------

    def _dist_dirs(self) -> list[Path]:
        if not self.dist_dir.is_dir():
            return []
        return sorted(
            (
                child
                for child in self.dist_dir.iterdir()
                if child.is_dir() or is_junction(child)
            ),
            key=lambda child: child.name,
        )

    def _aliases(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for child in self._dist_dirs():
            if not is_junction(child):
                continue
            try:
                target = Path(os.readlink(child))
            except OSError:
                continue
            aliases[_norm(target)] = child.name
        return aliases

    def _success_artifacts(self) -> list[dict[str, Any]]:
        """All successful releases, newest first, with a manifest timestamp."""
        payload = self.load()
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for record in payload["builds"]:
            if record.get("status") != STATUS_SUCCESS:
                continue
            artifact = self.dist_dir / str(record.get("artifact", ""))
            if not self._is_usable_artifact(artifact):
                continue
            stamp = (
                self._manifest_built_at(artifact)
                or parse_time(record.get("built_at"))
                or parse_time(record.get("finished_at"))
                or parse_time(record.get("started_at"))
                or datetime.min.replace(tzinfo=UTC)
            )
            items.append(
                {
                    "artifact": artifact,
                    "build_id": record.get("build_id"),
                    "version": record.get("version"),
                    "stamp": stamp,
                    "source": "registry",
                }
            )
            seen.add(artifact.name)
        for child in self._dist_dirs():
            if child.name in seen or is_junction(child):
                continue
            if not (child / MANIFEST_NAME).is_file() or not self._is_usable_artifact(child):
                continue
            stamp = self._manifest_built_at(child)
            if stamp is None:
                try:
                    stamp = datetime.fromtimestamp(child.stat().st_mtime, tz=UTC)
                except OSError:
                    continue
            manifest = self._manifest(child)
            items.append(
                {
                    "artifact": child,
                    "build_id": manifest.get("build_id"),
                    "version": manifest.get("version"),
                    "stamp": stamp,
                    "source": "manifest",
                }
            )
        items.sort(key=lambda item: item["stamp"], reverse=True)
        return items

    @staticmethod
    def _days_old(stamp: datetime | None, now: datetime) -> float | None:
        if stamp is None:
            return None
        return max(0.0, (now - stamp).total_seconds() / 86400)

    def plan_retention(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Classify every artifact/cache without deleting anything."""
        moment = now or utc_now()
        successes = self._success_artifacts()
        current = self.resolve_latest()
        current_name = current.name if current is not None else None
        keep_count = int(POLICY["keep_successful"])
        kept_success: list[dict[str, Any]] = []
        if current is not None:
            kept_success.append(
                next(
                    (item for item in successes if item["artifact"] == current),
                    {
                        "artifact": current,
                        "build_id": current.name,
                        "version": self._manifest(current).get("version"),
                        "stamp": self._manifest_built_at(current),
                        "source": "pointer",
                    },
                )
            )
        for item in successes:
            if len(kept_success) >= keep_count:
                break
            if all(item["artifact"] != kept["artifact"] for kept in kept_success):
                kept_success.append(item)
        kept_names = {item["artifact"].name for item in kept_success}
        rollback_name = (
            kept_success[1]["artifact"].name if len(kept_success) > 1 else None
        )
        aliases = self._aliases()
        payload = self.load()
        failed_records = [
            record
            for record in payload["builds"]
            if record.get("status") in FAILED_STATUSES
        ]
        failed_records.sort(
            key=lambda record: (
                parse_time(record.get("finished_at") or record.get("started_at"))
                or datetime.min.replace(tzinfo=UTC),
                str(record.get("build_id")),
            ),
            reverse=True,
        )
        failed_rank = {
            str(record.get("build_id")): index
            for index, record in enumerate(failed_records)
        }

        kept: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        review: list[dict[str, Any]] = []

        for child in self._dist_dirs():
            size = tree_bytes(child) if not is_junction(child) else 0
            if is_junction(child):
                try:
                    alias_target = Path(os.readlink(child))
                except OSError:
                    alias_target = None
                if alias_target is None or not alias_target.exists():
                    review.append(
                        {
                            "path": str(child),
                            "kind": "broken_version_alias",
                            "logical_bytes": 0,
                            "deletable": False,
                            "reason": (
                                "版本联接目标不存在（移动 dist 后会断开）；"
                                "可运行 build.py --repair-aliases 重建"
                            ),
                        }
                    )
                else:
                    kept.append(
                        {
                            "path": str(child),
                            "kind": "version_alias",
                            "logical_bytes": 0,
                            "reason": "目录联接，不占第二份内容",
                        }
                    )
                continue
            record = self._record_for_artifact(child.name)
            status = record.get("status") if record else None
            if child.name in kept_names:
                if child.name == current_name:
                    kind, reason = "current", "当前版"
                elif child.name == rollback_name:
                    kind, reason = "rollback", "回滚版"
                else:
                    kind, reason = "retained_success", "近期成功版（保留数内）"
                kept.append(
                    {
                        "path": str(child),
                        "kind": kind,
                        "logical_bytes": size,
                        "reason": reason,
                    }
                )
                continue
            if status in FAILED_STATUSES:
                started = parse_time(record.get("finished_at") or record.get("started_at"))
                age = self._days_old(started, moment)
                failed_index = failed_rank.get(str(record.get("build_id")), 999)
                stale = age is not None and age > POLICY["keep_failed_days"]
                too_many = failed_index >= POLICY["keep_failed_max"]
                candidates.append(
                    {
                        "path": str(child),
                        "kind": "failed_build",
                        "status": status,
                        "logical_bytes": size,
                        "age_days": round(age, 1) if age is not None else None,
                        "deletable": bool(stale or too_many),
                        "reason": (
                            f"{status}，失败构建诊断保留"
                            f"（>{POLICY['keep_failed_days']} 天或超过 "
                            f"{POLICY['keep_failed_max']} 个后可清理）"
                        ),
                    }
                )
                continue
            if status == STATUS_STARTED:
                started = parse_time(record.get("started_at"))
                age_hours = (
                    max(0.0, (moment - started).total_seconds() / 3600)
                    if started is not None
                    else None
                )
                abandoned = (
                    age_hours is not None and age_hours > POLICY["keep_started_hours"]
                )
                entry = {
                    "path": str(child),
                    "kind": "abandoned_build",
                    "status": status,
                    "logical_bytes": size,
                    "age_hours": round(age_hours, 1) if age_hours is not None else None,
                    "deletable": bool(abandoned),
                    "reason": (
                        "构建中断/未收尾"
                        f"（>{POLICY['keep_started_hours']} 小时可清理）"
                        if abandoned
                        else "构建可能仍在进行，暂不清理"
                    ),
                }
                if abandoned:
                    candidates.append(entry)
                else:
                    review.append(entry)
                continue
            if child.name in {item["artifact"].name for item in successes}:
                alias_name = aliases.get(_norm(child))
                reasons = ["超出成功版保留数"]
                if alias_name:
                    reasons.append(f"仍被版本联接 {alias_name} 使用")
                candidates.append(
                    {
                        "path": str(child),
                        "kind": "superseded_release",
                        "logical_bytes": size,
                        "deletable": alias_name is None,
                        "reason": "；".join(reasons),
                    }
                )
                continue
            if child.name.startswith(f"{APP_NAME}-"):
                review.append(
                    {
                        "path": str(child),
                        "kind": "unregistered_artifact",
                        "logical_bytes": size,
                        "deletable": False,
                        "reason": "缺少构建清单/登记，需人工核验来源与可恢复性",
                    }
                )

        # ZIPs are recovery sources: list them, never mark them deletable here.
        zips = sorted(
            (path for path in self.dist_dir.glob("*.zip") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        ) if self.dist_dir.is_dir() else []
        for index, path in enumerate(zips):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if index < POLICY["keep_zips"]:
                kept.append(
                    {
                        "path": str(path),
                        "kind": "release_zip",
                        "logical_bytes": size,
                        "reason": "近期发布 ZIP（恢复来源）",
                    }
                )
            else:
                candidates.append(
                    {
                        "path": str(path),
                        "kind": "superseded_zip",
                        "logical_bytes": size,
                        "deletable": False,
                        "reason": "旧发布 ZIP 是恢复来源，交由空间整理逐文件核验后决定",
                    }
                )

        # Build work directories are disposable caches; keep recent ones only.
        work_dirs = (
            sorted((child for child in self.build_dir.iterdir() if child.is_dir()),
                   key=lambda child: child.name)
            if self.build_dir.is_dir()
            else []
        )
        payload = self.load()
        work_records = {
            record.get("build_id"): record for record in payload["builds"] if record.get("build_id")
        }
        for child in work_dirs:
            try:
                age = self._days_old(
                    datetime.fromtimestamp(child.stat().st_mtime, tz=UTC), moment
                )
            except OSError:
                continue
            size = tree_bytes(child)
            record = work_records.get(child.name)
            if child.name == "upstream-staging":
                candidates.append(
                    {
                        "path": str(child),
                        "kind": "staging_cache",
                        "logical_bytes": size,
                        "deletable": True,
                        "reason": "上游精简副本，构建时按需重建",
                    }
                )
                continue
            if child.name == "wechat-space-manager":
                candidates.append(
                    {
                        "path": str(child),
                        "kind": "legacy_work_cache",
                        "logical_bytes": size,
                        "deletable": True,
                        "reason": "旧 PyInstaller 工作目录，可由下一次构建重建",
                    }
                )
                continue
            if record is not None:
                artifact_name = f"{APP_NAME}-{child.name}"
                protected = artifact_name in {current_name, rollback_name}
                candidates.append(
                    {
                        "path": str(child),
                        "kind": "work_cache",
                        "logical_bytes": size,
                        "age_days": round(age, 1) if age is not None else None,
                        "deletable": bool(
                            age is not None
                            and age > POLICY["keep_work_days"]
                            and not protected
                        ),
                        "reason": (
                            "构建工作缓存，产物已验证；"
                            + (
                                "当前/回滚版验证缓存保留"
                                if protected
                                else f"保留最近 {POLICY['keep_work_days']} 天"
                            )
                        ),
                    }
                )
                continue
            artifact = self.dist_dir / f"{APP_NAME}-{child.name}"
            if artifact.is_dir():
                protected = artifact.name in {current_name, rollback_name}
                candidates.append(
                    {
                        "path": str(child),
                        "kind": "work_cache",
                        "logical_bytes": size,
                        "age_days": round(age, 1) if age is not None else None,
                        "deletable": bool(
                            age is not None
                            and age > POLICY["keep_work_days"]
                            and not protected
                        ),
                        "reason": (
                            "发布产物的构建工作缓存"
                            + ("；当前版工作缓存暂保留" if protected else "")
                        ),
                    }
                )
                continue
            # No dist artifact and no registry record: this is an orphan work
            # directory left by a build whose output is already gone.  It is a
            # regenerable cache, not a release and not a recovery source.
            candidates.append(
                {
                    "path": str(child),
                    "kind": "orphan_work_cache",
                    "logical_bytes": size,
                    "age_days": round(age, 1) if age is not None else None,
                    "deletable": bool(age is not None and age > POLICY["keep_work_days"]),
                    "reason": (
                        "无对应发布产物的孤儿构建缓存（可用源码重新构建），"
                        f"保留最近 {POLICY['keep_work_days']} 天"
                    ),
                }
            )

        candidate_bytes = sum(item["logical_bytes"] for item in candidates)
        deletable_bytes = sum(
            item["logical_bytes"] for item in candidates if item["deletable"]
        )
        return {
            "generated_at": iso(moment),
            "dist": str(self.dist_dir),
            "build": str(self.build_dir),
            "policy": dict(POLICY),
            "latest_pointer": self._latest_raw(),
            "current": str(current) if current is not None else None,
            "rollback": (
                str(self.dist_dir / rollback_name) if rollback_name else None
            ),
            "kept": kept,
            "candidates": candidates,
            "review": review,
            "totals": {
                "candidate_bytes": candidate_bytes,
                "deletable_bytes": deletable_bytes,
                "candidate_human": human_bytes(candidate_bytes),
                "deletable_human": human_bytes(deletable_bytes),
            },
        }

    def _latest_raw(self) -> str | None:
        try:
            return self.latest_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def apply_retention(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        """Delete only explicitly deletable entries, re-checking every boundary."""
        current = Path(plan["current"]).resolve() if plan.get("current") else None
        rollback = Path(plan["rollback"]).resolve() if plan.get("rollback") else None
        aliases = self._aliases()
        results: list[dict[str, Any]] = []
        for item in plan.get("candidates", []):
            if not item.get("deletable"):
                continue
            path = Path(item["path"])
            if is_junction(path):
                results.append({"path": str(path), "removed": False, "reason": "联接不删除"})
                continue
            try:
                resolved = path.resolve()
            except OSError:
                results.append({"path": str(path), "removed": False, "reason": "无法解析路径"})
                continue
            if resolved == current or resolved == rollback:
                results.append({"path": str(path), "removed": False, "reason": "当前/回滚版保护"})
                continue
            if _norm(resolved) in aliases:
                results.append({"path": str(path), "removed": False, "reason": "版本联接保护"})
                continue
            allowed_roots = [self.dist_dir.resolve(), self.build_dir.resolve()]
            if not any(resolved == root or root in resolved.parents for root in allowed_roots):
                results.append({"path": str(path), "removed": False, "reason": "越界路径拒绝"})
                continue
            if resolved in (self.dist_dir.resolve(), self.build_dir.resolve()):
                results.append({"path": str(path), "removed": False, "reason": "拒绝删除根目录"})
                continue
            try:
                _remove_tree(resolved)
            except OSError as exc:
                results.append({"path": str(path), "removed": False, "reason": str(exc)})
                continue
            results.append({"path": str(path), "removed": True, "reason": item.get("kind")})
        return results


def _remove_tree(root: Path) -> None:
    """Remove one tree without following junctions; refuse unexpected reparse points."""
    directories: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        directories.append(current)
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
                    raise OSError(f"unexpected reparse point inside candidate: {entry.path}")
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    Path(entry.path).unlink()
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        directory.rmdir()


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


__all__ = [
    "AliasResult",
    "BuildStore",
    "FAILED_STATUSES",
    "MANIFEST_NAME",
    "POLICY",
    "REGISTRY_NAME",
    "STATUS_BUILD_FAILED",
    "STATUS_STARTED",
    "STATUS_SUCCESS",
    "STATUS_VERIFY_FAILED",
    "atomic_write_json",
    "atomic_write_text",
    "human_bytes",
    "is_junction",
    "parse_time",
    "tree_bytes",
    "utc_now",
]
