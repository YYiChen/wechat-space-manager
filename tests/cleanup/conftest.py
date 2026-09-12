from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).parents[2] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from wechat_cleaner.domain.contracts import (  # noqa: E402
    AccountRef,
    FileIdentity,
    MappingConfidence,
    MediaRecord,
    MediaType,
)

NOW = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)


@pytest.fixture
def account_root(tmp_path: Path) -> Path:
    root = tmp_path / "wxid_synthetic_cleanup"
    root.mkdir()
    return root


@pytest.fixture
def account(account_root: Path) -> AccountRef:
    return AccountRef(account_id="wxid_synthetic_cleanup", account_root=str(account_root))


def stage_file(root: Path, relative_path: str, payload: bytes) -> FileIdentity:
    path = root.joinpath(*relative_path.split("\\"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    info = path.stat()
    digest = hashlib.sha256(payload).hexdigest()
    return FileIdentity(
        relative_path=relative_path,
        byte_size=info.st_size,
        modified_time_ns=info.st_mtime_ns,
        sha256=digest,
    )


def make_record(
    account: AccountRef,
    identity: FileIdentity,
    *,
    media_type: MediaType = MediaType.IMAGE,
    confidence: MappingConfidence = MappingConfidence.HIGH,
    cache: bool = False,
    media_id=None,
) -> MediaRecord:
    fields = {
        "account_id": account.account_id,
        "file": identity,
        "media_type": media_type,
        "observed_at": NOW,
        "mapping_confidence": confidence,
        "mapping_reason": "synthetic fixture mapping",
        "is_regenerable_cache": cache,
        "message_local_id": 1 if confidence is MappingConfidence.EXACT else None,
    }
    if media_id is not None:
        fields["media_id"] = media_id
    return MediaRecord(**fields)
