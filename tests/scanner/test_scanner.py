from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from wechat_cleaner.domain.contracts import ContractErrorCode, MediaType
from wechat_cleaner.scanner import ScannerError, scan, scan_account


def _fixture_builder():
    path = Path(__file__).parents[1] / "fixtures_synthetic" / "build_fixture.py"
    spec = importlib.util.spec_from_file_location("synthetic_fixture_builder_scanner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _build_fixture(tmp_path: Path, *, with_symlink_probe: bool = False):
    builder = _fixture_builder()
    root = tmp_path / "xwechat_files"
    report = builder.build_fixture(root, with_symlink_probe=with_symlink_probe)
    return root, report


def test_discovers_accounts_and_emits_deterministic_manifest(tmp_path: Path) -> None:
    root, _ = _build_fixture(tmp_path)

    results = scan(root)
    assert [result.account.account_id for result in results] == [
        "wxid_synthetic_alpha_d729",
        "wxid_synthetic_beta_d729",
    ]
    alpha, beta = results
    assert alpha.manifest.file_count == 9
    assert alpha.manifest.total_bytes == 949
    assert beta.manifest.file_count == 6
    assert beta.manifest.total_bytes == 732
    assert all(not item.relative_path.startswith("all_users") for item in alpha.files + beta.files)

    second_alpha = scan_account(root / "wxid_synthetic_alpha_d729")
    assert alpha.manifest.manifest_sha256 == second_alpha.manifest.manifest_sha256
    assert alpha.files == second_alpha.files


def test_classifies_rec_pairs_and_protects_database_favourites_sendtemp(tmp_path: Path) -> None:
    root, _ = _build_fixture(tmp_path)
    alpha = scan_account(root / "wxid_synthetic_alpha_d729")

    by_path = {item.relative_path: item for item in alpha.files}
    rec_prefix = r"msg\attach\8a8b0c0d0e0f10111213141516171819\2026-02\Rec\record-alpha-0001" + "\\"
    assert by_path[rec_prefix + "0"].media_type is MediaType.IMAGE
    assert by_path[rec_prefix + "0_t"].media_type is MediaType.THUMBNAIL
    assert by_path[r"db_storage\message.db"].is_protected
    assert by_path[r"business\favorite\favorite_001.dat"].is_protected
    assert by_path[r"SendTemp\uploading_001.tmp"].is_protected
    assert by_path[r"cache\image_preview.bin"].is_regenerable_cache
    assert by_path[r"cache\image_preview.bin"].media_type is MediaType.OTHER


def test_reparse_probe_is_skipped_without_scanning_external_sentinel(tmp_path: Path) -> None:
    root, report = _build_fixture(tmp_path, with_symlink_probe=True)
    results = scan(root)
    all_paths = [item.relative_path for result in results for item in result.files]
    if report.symlink_probe_status == "created":
        assert all("must_not_be_scanned" not in path for path in all_paths)
        assert any(
            error.code is ContractErrorCode.INVALID_PATH
            for result in results
            for error in result.manifest.errors
        )


def test_scan_is_read_only_and_rejects_invalid_roots(tmp_path: Path) -> None:
    root, _ = _build_fixture(tmp_path)
    before = {
        path.relative_to(root): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }
    scan(root)
    after = {
        path.relative_to(root): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }
    assert before == after

    with pytest.raises(ScannerError) as missing:
        scan(tmp_path / "does-not-exist")
    assert missing.value.code is ContractErrorCode.INVALID_PATH

    with pytest.raises(ScannerError) as not_account:
        scan_account(root / "all_users")
    assert not_account.value.code is ContractErrorCode.INVALID_PATH


def test_scan_account_progress_reports_running_count_without_changing_result(
    monkeypatch, tmp_path: Path
) -> None:
    from wechat_cleaner.scanner import scanner as scanner_module

    # A 9-file fixture never reaches the default cadence; shrink it so the
    # callback fires and its cadence semantics become observable.
    monkeypatch.setattr(scanner_module, "_PROGRESS_EVERY", 2)
    root, _ = _build_fixture(tmp_path)
    counts: list[int] = []
    with_progress = scan_account(
        root / "wxid_synthetic_alpha_d729", progress=counts.append
    )

    assert counts, "progress callback should fire at least once"
    assert counts == sorted(counts), "running counts must be non-decreasing"
    # Every reported value is a multiple of the cadence...
    assert all(count % 2 == 0 for count in counts)
    # ...and the last one is the highest cadence multiple reached.
    assert counts[-1] == (len(with_progress.files) // 2) * 2

    plain = scan_account(root / "wxid_synthetic_alpha_d729")
    again: list[int] = []
    with_callback = scan_account(
        root / "wxid_synthetic_alpha_d729", progress=again.append
    )
    assert plain.files == with_progress.files == with_callback.files
    assert plain.manifest.manifest_sha256 == with_callback.manifest.manifest_sha256


# ---------------------------------------------------------------------------
# Folder-focus scans (ITER-20261008): restrict the walk to one subtree while
# keeping every path relative to the account root.
# ---------------------------------------------------------------------------

_FOCUS_PREFIX = r"msg\attach\8a8b0c0d0e0f10111213141516171819"


def test_focus_scan_is_exact_subset_of_full_scan(tmp_path: Path) -> None:
    root, _ = _build_fixture(tmp_path)
    alpha_root = root / "wxid_synthetic_alpha_d729"
    full = scan_account(alpha_root)
    focus = scan_account(alpha_root, subpath=_FOCUS_PREFIX)

    expected = tuple(
        item
        for item in full.files
        if item.relative_path.startswith(_FOCUS_PREFIX + "\\")
    )
    assert expected, "fixture must have files under the focus prefix"
    assert focus.files == expected
    assert focus.manifest.file_count == len(expected)
    assert focus.manifest.total_bytes == sum(item.byte_size for item in expected)
    # The focused manifest still describes the same account.
    assert focus.account == full.account
    assert focus.manifest.errors == ()


def test_focus_scan_accepts_forward_slashes_and_keeps_result_identical(
    tmp_path: Path,
) -> None:
    root, _ = _build_fixture(tmp_path)
    alpha_root = root / "wxid_synthetic_alpha_d729"
    focus = scan_account(alpha_root, subpath=_FOCUS_PREFIX)
    forward = scan_account(
        alpha_root, subpath=_FOCUS_PREFIX.replace("\\", "/")
    )
    padded = scan_account(alpha_root, subpath=f"/{_FOCUS_PREFIX}/")

    assert forward.files == focus.files
    assert padded.files == focus.files


def test_focus_scan_none_and_empty_mean_full_scan(tmp_path: Path) -> None:
    root, _ = _build_fixture(tmp_path)
    alpha_root = root / "wxid_synthetic_alpha_d729"
    default = scan_account(alpha_root)

    for variant in (None, "", "   ", "/", "\\\\"):
        variant_result = scan_account(alpha_root, subpath=variant)
        assert variant_result.files == default.files
        assert (
            variant_result.manifest.manifest_sha256
            == default.manifest.manifest_sha256
        )


def test_focus_scan_rejects_unsafe_or_missing_subpaths(tmp_path: Path) -> None:
    root, _ = _build_fixture(tmp_path)
    alpha_root = root / "wxid_synthetic_alpha_d729"

    unsafe = [
        r"..\wxid_synthetic_beta_d729\msg",
        "msg/../../outside",
        r"msg\attach\..\..\..\..\Windows",
        r"msg\nope",  # missing directory
        r"msg\attach\8a8b0c0d0e0f10111213141516171819\2026-02\Image\alpha_001.dat",  # a file
    ]
    for subpath in unsafe:
        with pytest.raises(ScannerError) as excinfo:
            scan_account(alpha_root, subpath=subpath)
        assert excinfo.value.code is ContractErrorCode.INVALID_PATH


def test_within_focus_folds_case_for_windows_semantics() -> None:
    from wechat_cleaner.scanner.scanner import _within_focus

    focus = ("Msg", "ATTACH", "8a8b")
    # Case-insensitive on every segment (Windows paths are case-insensitive).
    assert _within_focus(("msg",), focus)
    assert _within_focus(("msg", "attach"), focus)
    assert _within_focus(("MSG", "ATTACH", "8A8B", "2026-02"), focus)
    assert _within_focus(("msg", "attach", "8a8b", "deep", "file"), focus)
    # Sibling subtrees are pruned.
    assert not _within_focus(("msg", "video"), focus)
    assert not _within_focus(("business",), focus)
    # No focus: everything is allowed.
    assert _within_focus(("anything",), ())


def test_focus_scan_progress_counts_only_focused_files(
    monkeypatch, tmp_path: Path
) -> None:
    from wechat_cleaner.scanner import scanner as scanner_module

    # The default cadence (512) never fires for a 5-file subtree; shrink it so
    # the callback semantics become observable.
    monkeypatch.setattr(scanner_module, "_PROGRESS_EVERY", 2)
    root, _ = _build_fixture(tmp_path)
    alpha_root = root / "wxid_synthetic_alpha_d729"
    counts: list[int] = []
    focus = scan_account(alpha_root, subpath=_FOCUS_PREFIX, progress=counts.append)

    assert counts, "the callback should fire for a focused walk too"
    assert counts == sorted(counts)
    # Cadence semantics: every multiple of 2 is reported; the trailing file
    # below the cadence is not.  The ceiling is the focused count, not the
    # account total.
    assert counts[-1] == (focus.manifest.file_count // 2) * 2
    assert focus.manifest.file_count == 5
