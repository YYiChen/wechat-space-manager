#!/usr/bin/env python
"""Local real-data harness for the Phase 4 read-only path.

Runs the in-app real read-only session end to end against a real WeChat data
root and prints **anonymous aggregates only**: counts, bytes and error types.
No account identifier, absolute path, contact, message body, key or media
content is printed or written outside the private session root.

This script is meant to be executed on the user's own machine with the optional
upstream backend installed (for example via
``C:\\Projects\\wechat-space-manager-real-readonly-venv-isolated-20260909``).

It never runs cleanup, never writes the WeChat source directory, and never
deletes the session it creates.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_SESSION_BASE = (
    Path.home() / "AppData" / "Local" / "WeChatSpaceManager" / "private-acceptance"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-root", required=True, help="xwechat_files root (read-only)")
    parser.add_argument("--session-base", default=str(DEFAULT_SESSION_BASE))
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--account", default=None, help="account directory name (default: largest)")
    parser.add_argument("--preview-limit", type=int, default=1,
                        help="how many records to decode for the media gate")
    args = parser.parse_args()

    session_id = args.session_id or f"p4-harness-{datetime.now(UTC):%Y%m%d-%H%M%S}"
    session_root = Path(args.session_base) / session_id
    session_root.mkdir(parents=True, exist_ok=True)

    result: dict = {
        "harness": "P4-REAL-INTEGRATION-01",
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": "blocked",
        "preview_limit": args.preview_limit,
        "error_types": [],
    }

    try:
        from wechat_cleaner.application import RealReadOnlySession
        from wechat_cleaner.decoder.formats import DecoderFailure
        from wechat_cleaner.domain.contracts import DecodeVariant

        session = RealReadOnlySession.start(
            db_root=args.db_root,
            cache_root=str(session_root),
            account=args.account,
            session_id=session_id,
        )
        try:
            summary = session.database_summary()
            result["account_count"] = summary.get("account_count", 1)
            result["database_count"] = summary.get("database_count")
            result["database_bytes"] = summary.get("database_bytes")
            result["keyed_count"] = summary.get("keyed_count")
            result["unkeyed_count"] = summary.get("unkeyed_count")

            records = session.records()
            result["record_count"] = len(records)
            by_type: dict[str, int] = {}
            for record in records:
                by_type[record.media_type.value] = by_type.get(record.media_type.value, 0) + 1
            result["records_by_type"] = by_type

            # v1 support is confirmed for the V2 thumbnail container; large
            # originals and the `_h` high-definition variant are attempted after
            # thumbnails so the media gate reflects what actually decodes.
            thumbnails = [r for r in records if r.media_type.value == "thumbnail"]
            images = [r for r in records if r.media_type.value == "image"]
            result["image_candidate_count"] = len(images)
            result["thumbnail_candidate_count"] = len(thumbnails)
            decodable = thumbnails + images
            previews = 0
            openable = 0
            for record in decodable[: max(0, args.preview_limit)]:
                try:
                    artifact = session.preview(
                        record, variant=DecodeVariant.THUMBNAIL, max_edge_px=256
                    )
                    previews += 1
                    result.setdefault("preview_variants", []).append(artifact.variant.value)
                    output = Path(session_root) / artifact.output_file.relative_path
                    if output.is_file() and output.stat().st_size > 0:
                        try:
                            from PIL import Image

                            with Image.open(output) as probe:
                                probe.load()
                                openable += 1
                        except Exception:  # noqa: BLE001
                            result["error_types"].append("PreviewNotOpenable")
                except DecoderFailure as exc:
                    result["error_types"].append(exc.code.value)
                except Exception as exc:  # noqa: BLE001
                    error = getattr(exc, "error", None)
                    code = getattr(error, "code", None)
                    result["error_types"].append(
                        getattr(code, "value", None) or type(exc).__name__
                    )

            result["preview_count"] = previews
            result["preview_openable_count"] = openable

            manifest = session.manifest()
            result["manifest"] = manifest.to_anonymous_dict()
            result["decoded_cache_file_count"] = len(manifest.decoded_cache_files)
            result["key_cache_file_count"] = len(manifest.key_cache_relative_paths)

            media_gate = openable > 0
            result["media_gate"] = "passed" if media_gate else "blocked"
            result["status"] = (
                "passed"
                if media_gate and (result.get("database_count") or 0) > 0
                else "partial"
            )
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        result["error_types"].append(type(exc).__name__)
        result["traceback_tail"] = traceback.format_exc().strip().splitlines()[-1]

    payload_path = session_root / "anonymous-result.json"
    payload_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nsession root (kept for review): {session_id}", file=sys.stderr)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
