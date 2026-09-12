"""Command-line shell over the read-only UI layers.

Subcommands:
    browse   filter/summarize mapped media records (JSON in, JSON or text out)
    preview  decode one record via the decoder and report the artifact
    cache    inspect decode-cache usage, purge expired sessions

Exit codes: 0 success; 1 operation failed (e.g. decode error, unknown id);
2 invalid input (bad JSON, contract rejection, unknown arguments).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

from wechat_cleaner.domain.contracts import (
    DecodeVariant,
    MappingConfidence,
    MediaRecord,
    MediaType,
)

from .cache_control import cache_status, over_limit, purge_expired
from .filters import (
    by_confidence,
    by_contact,
    by_media_type,
    by_min_bytes,
    by_time_range,
    sort_by,
    summarize,
)
from .preview import artifact_path, open_artifact, request_preview


class CliInputError(ValueError):
    """Invalid user input; maps to exit code 2."""


def load_records(path: str | Path) -> tuple[MediaRecord, ...]:
    """Load a mapper-produced JSON file containing a list of MediaRecords."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise CliInputError(f"cannot read records file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CliInputError(f"records file is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise CliInputError("records file must contain a JSON list")
    try:
        return tuple(MediaRecord.model_validate(item) for item in raw)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError surfaced readably
        raise CliInputError(f"records failed contract validation: {exc}") from exc


def _parse_datetime(value: str, option: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CliInputError(f"{option} must be an ISO-8601 datetime") from exc
    return parsed if parsed.tzinfo else parsed.astimezone()


def _record_brief(record: MediaRecord) -> dict:
    return {
        "media_id": str(record.media_id),
        "media_type": record.media_type.value,
        "confidence": record.mapping_confidence.value,
        "byte_size": record.file.byte_size,
        "relative_path": record.file.relative_path,
        "contact_id": record.contact.contact_id if record.contact else None,
        "observed_at": record.observed_at.isoformat(),
    }


def _apply_filters(records, args) -> tuple:
    if args.media_type is not None:
        try:
            media_type = MediaType(args.media_type)
        except ValueError as exc:
            raise CliInputError(f"unknown media type {args.media_type!r}") from exc
        records = by_media_type(records, media_type)
    if args.confidence:
        try:
            levels = tuple(MappingConfidence(item.strip()) for item in args.confidence.split(","))
        except ValueError as exc:
            raise CliInputError(f"unknown confidence level in {args.confidence!r}") from exc
        records = by_confidence(records, levels)
    if args.contact is not None:
        records = by_contact(records, args.contact)
    if args.min_bytes is not None:
        records = by_min_bytes(records, args.min_bytes)
    if args.after is not None:
        records = by_time_range(records, start=_parse_datetime(args.after, "--after"))
    if args.before is not None:
        records = by_time_range(records, end=_parse_datetime(args.before, "--before"))
    return records


def _cmd_browse(args: argparse.Namespace) -> int:
    records = _apply_filters(load_records(args.records), args)
    if args.sort is not None:
        records = sort_by(records, args.sort, reverse=args.descending)
    summary = summarize(records)
    if args.json:
        payload = {
            "summary": {
                "total_count": summary.total_count,
                "total_bytes": summary.total_bytes,
                "by_media_type": [
                    {"type": name, "count": count, "bytes": size}
                    for name, count, size in summary.by_media_type
                ],
                "by_confidence": [
                    {"confidence": name, "count": count} for name, count in summary.by_confidence
                ],
                "regenerable_cache_count": summary.regenerable_cache_count,
            },
            "records": [_record_brief(record) for record in records],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(f"{summary.total_count} records, {summary.total_bytes} bytes")
        for record in records:
            brief = _record_brief(record)
            print(
                f"{brief['media_id']}  {brief['media_type']:<9}  {brief['confidence']:<8}"
                f"  {brief['byte_size']:>12}  {brief['relative_path']}"
            )
    return 0


def _cmd_preview(args: argparse.Namespace) -> int:
    records = load_records(args.records)
    try:
        media_id = uuid.UUID(args.media_id)
    except ValueError as exc:
        raise CliInputError("--media-id must be a UUID") from exc
    target = next((record for record in records if record.media_id == media_id), None)
    if target is None:
        print(json.dumps({"status": "error", "error": {"code": "UNKNOWN_MEDIA_ID"}}))
        return 1
    try:
        variant = DecodeVariant(args.variant)
    except ValueError as exc:
        raise CliInputError("--variant must be 'thumbnail' or 'original'") from exc
    outcome = request_preview(
        target,
        copy_root=args.copy_root,
        cache_root=args.cache_root,
        variant=variant,
        max_edge_px=args.max_edge,
    )
    if outcome.error is not None:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": {"code": outcome.error.code.value, "message": outcome.error.message},
                }
            )
        )
        return 1
    artifact = outcome.artifact
    assert artifact is not None
    payload = {
        "status": "ok",
        "artifact": {
            "relative_path": artifact.output_file.relative_path,
            "byte_size": artifact.output_file.byte_size,
            "decoder_id": artifact.decoder_id,
        },
    }
    print(json.dumps(payload, indent=2))
    if args.open:
        print(f"opened: {artifact_path(artifact, cache_root=args.cache_root).name}")
        open_artifact(artifact, cache_root=args.cache_root)
    return 0


def _cmd_cache(args: argparse.Namespace) -> int:
    if args.purge_expired:
        removed = purge_expired(args.cache_root)
        print(f"purged expired sessions: {removed}")
    status = cache_status(args.cache_root)
    payload = {
        "total_bytes": status.total_bytes,
        "file_count": status.file_count,
        "session_count": status.session_count,
    }
    if args.max_bytes is not None:
        payload["over_limit"] = over_limit(args.cache_root, args.max_bytes)
        payload["max_bytes"] = args.max_bytes
    print(json.dumps(payload, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wechat-cleaner ui", description="Read-only media review and cache control"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    browse = subparsers.add_parser("browse", help="filter and summarize media records")
    browse.add_argument(
        "--records", required=True, help="JSON file of MediaRecords (mapper output)"
    )
    browse.add_argument("--type", dest="media_type", choices=[item.value for item in MediaType])
    browse.add_argument("--confidence", help="comma-separated levels, e.g. exact,high")
    browse.add_argument("--contact", help="contact_id to keep")
    browse.add_argument("--min-bytes", type=int)
    browse.add_argument("--after", help="ISO-8601 datetime lower bound on observed_at")
    browse.add_argument("--before", help="ISO-8601 datetime upper bound on observed_at")
    browse.add_argument("--sort", choices=["bytes", "time", "confidence"])
    browse.add_argument("--descending", action="store_true")
    browse.add_argument("--json", action="store_true", help="machine-readable output")
    browse.set_defaults(handler=_cmd_browse)

    preview = subparsers.add_parser("preview", help="decode one record and report the artifact")
    preview.add_argument("--records", required=True)
    preview.add_argument("--media-id", required=True)
    preview.add_argument("--copy-root", required=True, help="staging tree of approved copies")
    preview.add_argument("--cache-root", required=True, help="decode cache root")
    preview.add_argument("--variant", default="thumbnail", choices=["thumbnail", "original"])
    preview.add_argument("--max-edge", type=int, default=None, help="thumbnail edge in pixels")
    preview.add_argument("--open", action="store_true", help="open the artifact with the OS shell")
    preview.set_defaults(handler=_cmd_preview)

    cache = subparsers.add_parser("cache", help="decode cache status and expiry purge")
    cache.add_argument("--cache-root", required=True)
    cache.add_argument("--purge-expired", action="store_true")
    cache.add_argument("--max-bytes", type=int, help="report whether usage exceeds this limit")
    cache.set_defaults(handler=_cmd_cache)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except CliInputError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - manual smoke entry
    raise SystemExit(main())
