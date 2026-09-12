"""CLI behaviour: JSON in/out, exit codes, error surfaces.

Fixtures come from this directory's conftest; no cross-directory imports.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from wechat_cleaner.domain.contracts import MediaType
from wechat_cleaner.ui.cli import main


def _records_json(tmp_path: Path, records: list) -> str:
    path = tmp_path / "records.json"
    path.write_text(
        json.dumps([record.model_dump(mode="json") for record in records]),
        encoding="utf-8",
    )
    return str(path)


def test_browse_json_output(record_factory, tmp_path, capsys):
    records = [
        record_factory(media_type=MediaType.IMAGE, byte_size=500),
        record_factory(byte_size=2000),
    ]
    source = _records_json(tmp_path, records)
    code = main(["browse", "--records", source, "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["total_count"] == 2
    assert payload["summary"]["total_bytes"] == 2500
    assert len(payload["records"]) == 2
    assert all("relative_path" in record for record in payload["records"])


def test_browse_filter_by_type(record_factory, tmp_path, capsys):
    records = [
        record_factory(media_type=MediaType.IMAGE),
        record_factory(media_type=MediaType.VIDEO),
    ]
    source = _records_json(tmp_path, records)
    code = main(["browse", "--records", source, "--type", "video", "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["total_count"] == 1
    assert payload["records"][0]["media_type"] == "video"


def test_browse_filter_by_contact_and_min_bytes(record_factory, tmp_path, capsys):
    records = [
        record_factory(byte_size=100, contact_id="contact_alpha"),
        record_factory(byte_size=9000, contact_id=None),
    ]
    source = _records_json(tmp_path, records)
    code = main(
        ["browse", "--records", source, "--contact", "contact_alpha", "--min-bytes", "50", "--json"]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["total_count"] == 1
    assert payload["records"][0]["contact_id"] == "contact_alpha"


def test_browse_rejects_bad_json(tmp_path, capsys):
    source = tmp_path / "broken.json"
    source.write_text("{not json", encoding="utf-8")
    code = main(["browse", "--records", str(source), "--json"])

    assert code == 2
    assert "input error" in capsys.readouterr().err


def test_browse_rejects_contract_violation(record_factory, tmp_path, capsys):
    record = record_factory()
    broken = record.model_copy(update={"account_id": "bad id with spaces"})
    source = _records_json(tmp_path, [broken])
    code = main(["browse", "--records", source])

    assert code == 2
    assert "input error" in capsys.readouterr().err


def test_preview_reports_artifact(
    stager, png_factory, record_factory, tmp_path, cache_root, capsys
):
    identity = stager("msg/attach/x/2026-02/Image/a.png", png_factory())
    record = record_factory(
        relative_path=identity.relative_path,
        byte_size=identity.byte_size,
        modified_time_ns=identity.modified_time_ns,
    )
    source = _records_json(tmp_path, [record])

    code = main(
        [
            "preview",
            "--records",
            source,
            "--media-id",
            str(record.media_id),
            "--copy-root",
            str(stager.root),
            "--cache-root",
            cache_root,
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["artifact"]["decoder_id"] == "plain-image"
    assert payload["artifact"]["relative_path"].startswith("sessions")


def test_preview_unknown_media_id(record_factory, tmp_path, cache_root, capsys):
    record = record_factory()
    source = _records_json(tmp_path, [record])
    code = main(
        [
            "preview",
            "--records",
            source,
            "--media-id",
            "00000000-0000-0000-0000-000000000000",
            "--copy-root",
            str(tmp_path),
            "--cache-root",
            cache_root,
        ]
    )

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "UNKNOWN_MEDIA_ID"


def test_preview_decode_failure_exit_one(
    stager, png_factory, record_factory, tmp_path, cache_root, capsys
):
    identity = stager("msg/attach/x/2026-02/Image/a.png", png_factory())
    record = record_factory(relative_path=identity.relative_path, byte_size=identity.byte_size)
    (stager.root / identity.relative_path).unlink()
    source = _records_json(tmp_path, [record])

    code = main(
        [
            "preview",
            "--records",
            source,
            "--media-id",
            str(record.media_id),
            "--copy-root",
            str(stager.root),
            "--cache-root",
            cache_root,
        ]
    )

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "FILE_IDENTITY_MISMATCH"


def test_cache_status_and_purge(cache_root, capsys):
    session = Path(cache_root) / "sessions" / "s1"
    session.mkdir(parents=True)
    (session / "a.png").write_bytes(b"x" * 80)
    (session / "a.png.artifact.json").write_text(
        json.dumps({"expires_at": datetime.now(UTC).isoformat()}), encoding="utf-8"
    )

    code = main(["cache", "--cache-root", cache_root, "--purge-expired", "--max-bytes", "10"])
    assert code == 0
    output = capsys.readouterr().out
    assert "purged expired sessions: 1" in output
    payload = json.loads(output[output.index("{") :])
    assert payload["total_bytes"] == 0
    assert payload["session_count"] == 0
    assert payload["over_limit"] is False
