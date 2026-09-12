"""Phase 3 integration gates over synthetic data and public boundaries only."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from wechat_cleaner.application import ApplicationFacade, CancellationToken, WorkflowCancelled
from wechat_cleaner.domain.contracts import (
    FilterSpec,
    MappingConfidence,
    MediaType,
    SelectionScope,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures_synthetic"


def _fixture_builder():
    path = FIXTURE_DIR / "build_phase3_fixture.py"
    spec = importlib.util.spec_from_file_location("phase3_integration_fixture_builder", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load Phase 3 fixture builder")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_application_facade_composes_public_phase3_boundaries(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    _fixture_builder().build_phase3_fixture(fixture, seed=20260909)
    with ApplicationFacade(tmp_path / "index.sqlite") as facade:
        registration = facade.scan(fixture / "accounts")
        assert len(registration.scans) == 2
        facade.import_database_copy(
            fixture / "databases" / "synthetic-v1.sqlite",
            account_id="wxid_synthetic_alpha_p3",
        )
        result = facade.query(
            FilterSpec(
                account_ids=("wxid_synthetic_alpha_p3",),
                media_types=(MediaType.IMAGE, MediaType.THUMBNAIL),
                mapping_confidences=(MappingConfidence.EXACT,),
                page_size=20,
            )
        )
        selection = facade.select(result, SelectionScope.CURRENT_RESULT)
        plan_result = facade.build_plan(selection)
        assert plan_result.plan is not None
        assert facade.preflight(plan_result.plan).can_execute
        receipt = facade.dry_run(plan_result.plan)
        assert receipt.status.value == "dry_run"


def test_phase3_cancellation_and_private_manifest_are_fail_closed(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    _fixture_builder().build_phase3_fixture(fixture, seed=20260910)
    token = CancellationToken()
    token.cancel()
    with ApplicationFacade(tmp_path / "index.sqlite") as facade:
        with pytest.raises(WorkflowCancelled):
            facade.scan(fixture / "accounts", cancel=token)
        assert facade.query_index_page().total_count == 0

    manifest = json.loads((fixture / "phase3-manifest.json").read_text(encoding="utf-8"))
    assert manifest["privacy"]["contains_absolute_paths"] is False
    assert manifest["privacy"]["contains_keys"] is False
    assert manifest["privacy"]["contains_message_bodies"] is False
    serialized = json.dumps(manifest, ensure_ascii=False).lower()
    assert "secret" not in serialized
    assert "password" not in serialized


def test_gui_source_stays_behind_facade_and_has_no_file_mutation_imports() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            Path("src/wechat_cleaner/gui/services.py"),
            Path("src/wechat_cleaner/gui/window.py"),
        )
    )
    assert "ApplicationFacadePort" in source
    for forbidden in (
        "wechat_cleaner.executor",
        "os.remove",
        "Path.unlink",
        "shutil.move",
        "sqlite3.connect",
    ):
        assert forbidden not in source


def test_phase3_fixture_builder_and_contract_outputs_are_path_free(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    _fixture_builder().build_phase3_fixture(fixture, seed=20260911)
    for filename in (
        "phase3-manifest.json",
        "database-schema-matrix.json",
        "media-records.json",
    ):
        payload = (fixture / filename).read_text(encoding="utf-8")
        assert not any(marker in payload for marker in ("C:\\\\", "D:\\\\", "/Users/"))
