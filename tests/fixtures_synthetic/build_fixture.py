"""Build a non-private, deterministic xwechat_files-shaped test directory.

This is intentionally filesystem-only. It contains no real WeChat payload,
database, key, contact, or account identifier. Do not point it at a real account.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

FIXTURE_TIME = 1_771_206_400  # 2026-02-21 00:00:00 UTC
ALPHA_ACCOUNT = "wxid_synthetic_alpha_d729"
BETA_ACCOUNT = "wxid_synthetic_beta_d729"
ALPHA_CONVERSATION = "8a8b0c0d0e0f10111213141516171819"
BETA_CONVERSATION = "0a0b0c0d0e0f10111213141516171819"


@dataclass(frozen=True)
class FixtureBuildReport:
    destination: str
    regular_file_count: int
    regular_file_bytes: int
    symlink_probe_status: str


def _payload(label: str, size: int) -> bytes:
    prefix = f"SYNTHETIC_FIXTURE_ONLY::{label}::".encode("ascii")
    return (prefix * ((size // len(prefix)) + 1))[:size]


def _write(root: Path, relative_path: str, size: int) -> None:
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_payload(relative_path, size))
    os.utime(target, (FIXTURE_TIME, FIXTURE_TIME))


def _create_symlink_probe(root: Path) -> str:
    """Create an external target and a directory link only when the OS permits it."""
    outside = root.parent / f"{root.name}-outside-sentinel"
    outside.mkdir(exist_ok=True)
    (outside / "must_not_be_scanned.bin").write_bytes(_payload("outside", 29))
    probe = root / "reparse-probes" / "escape-link"
    probe.parent.mkdir(parents=True, exist_ok=True)
    try:
        probe.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        return "unavailable"
    return "created"


def build_fixture(destination: Path, *, with_symlink_probe: bool = False) -> FixtureBuildReport:
    """Build the fixture at an absent or empty destination and return its inventory."""
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"Refusing non-empty fixture destination: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    alpha_attach = f"{ALPHA_ACCOUNT}/msg/attach/{ALPHA_CONVERSATION}/2026-02"
    beta_message = f"{BETA_ACCOUNT}/msg"
    files: tuple[tuple[str, int], ...] = (
        ("all_users/login/synthetic-login-index.bin", 31),
        (f"{alpha_attach}/Image/alpha_001.dat", 97),
        (f"{alpha_attach}/Rec/record-alpha-0001/0", 211),
        (f"{alpha_attach}/Rec/record-alpha-0001/0_t", 71),
        (f"{alpha_attach}/Rec/record-alpha-0001/1", 193),
        (f"{alpha_attach}/Rec/record-alpha-0001/1_t", 65),
        (f"{ALPHA_ACCOUNT}/cache/image_preview.bin", 43),
        (f"{ALPHA_ACCOUNT}/db_storage/message.db", 149),
        (f"{ALPHA_ACCOUNT}/business/favorite/favorite_001.dat", 83),
        (f"{ALPHA_ACCOUNT}/SendTemp/uploading_001.tmp", 37),
        (f"{beta_message}/video/{BETA_CONVERSATION}/2026-03/clip_001.mp4", 257),
        (f"{beta_message}/file/{BETA_CONVERSATION}/2026-03/report_001.pdf", 173),
        (f"{BETA_ACCOUNT}/cache/thumbnail_index.bin", 46),
        (f"{BETA_ACCOUNT}/db_storage/contact.db", 132),
        (f"{BETA_ACCOUNT}/business/favorite/favorite_002.dat", 84),
        (f"{BETA_ACCOUNT}/SendTemp/uploading_002.tmp", 40),
    )
    for relative_path, size in files:
        _write(destination, relative_path, size)

    # These four regular files model protected descendants but are intentionally
    # excluded from the media/cache accounting in later scanner tests.
    regular_file_count = len(files)
    regular_file_bytes = sum(size for _, size in files)
    symlink_status = "not-requested"
    if with_symlink_probe:
        symlink_status = _create_symlink_probe(destination)
    return FixtureBuildReport(
        destination=str(destination),
        regular_file_count=regular_file_count,
        regular_file_bytes=regular_file_bytes,
        symlink_probe_status=symlink_status,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--with-symlink-probe", action="store_true")
    arguments = parser.parse_args()
    report = build_fixture(arguments.destination, with_symlink_probe=arguments.with_symlink_probe)
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
