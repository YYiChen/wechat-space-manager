"""Preview orchestration: records in, decoder artifacts out, paths contained.

The UI never talks to a live WeChat directory: copies are staged below
``copy_root`` by the orchestration layer, decoded outputs land below
``cache_root``, and opening a preview is restricted to the cache root.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from wechat_cleaner.decoder import DecodeError, KeyProvider, decode
from wechat_cleaner.decoder.cache import ensure_within_cache
from wechat_cleaner.decoder.pipeline import DecoderLimits
from wechat_cleaner.domain.contracts import (
    ContractError,
    DecodeArtifact,
    DecodeRequest,
    DecodeVariant,
    MediaRecord,
)

Opener = Callable[[str], object]


@dataclass(frozen=True, slots=True)
class PreviewOutcome:
    """Either an artifact or a contract error; never raises to the caller."""

    artifact: DecodeArtifact | None = None
    error: ContractError | None = None


def build_request(
    record: MediaRecord,
    *,
    cache_root: str,
    variant: DecodeVariant = DecodeVariant.THUMBNAIL,
    max_edge_px: int | None = None,
    retain_until: datetime | None = None,
) -> DecodeRequest:
    """Build a contract request, letting contract validation reject bad input."""
    if variant is DecodeVariant.THUMBNAIL and max_edge_px is None:
        max_edge_px = 256
    return DecodeRequest(
        media=record,
        variant=variant,
        cache_root=cache_root,
        max_edge_px=max_edge_px,
        retain_until=retain_until,
    )


def request_preview(
    record: MediaRecord,
    *,
    copy_root: str,
    cache_root: str,
    variant: DecodeVariant = DecodeVariant.THUMBNAIL,
    max_edge_px: int | None = None,
    retain_until: datetime | None = None,
    key_provider: KeyProvider | None = None,
    limits: DecoderLimits | None = None,
    now: datetime | None = None,
) -> PreviewOutcome:
    """Decode a preview for one record; decoder failures become ``error``."""
    try:
        request = build_request(
            record,
            cache_root=cache_root,
            variant=variant,
            max_edge_px=max_edge_px,
            retain_until=retain_until,
        )
        artifact = decode(
            request,
            copy_root=copy_root,
            key_provider=key_provider,
            limits=limits,
            now=now,
        )
    except DecodeError as failure:
        return PreviewOutcome(error=failure.error)
    return PreviewOutcome(artifact=artifact)


def artifact_path(artifact: DecodeArtifact, *, cache_root: str | Path) -> Path:
    """Resolve an artifact output inside ``cache_root``; refuse escapes."""
    raw = str(Path(cache_root) / artifact.output_file.relative_path)
    return ensure_within_cache(cache_root, raw)


def open_artifact(
    artifact: DecodeArtifact,
    *,
    cache_root: str | Path,
    opener: Opener | None = None,
) -> Path:
    """Open a decoded preview file; defaults to the OS shell association."""
    path = artifact_path(artifact, cache_root=cache_root)
    if not path.is_file():
        raise FileNotFoundError(f"preview artifact is missing: {path.name}")
    if opener is None:
        opener = os.startfile  # noqa: S606 - Windows shell association, read-only open
    opener(str(path))
    return path
