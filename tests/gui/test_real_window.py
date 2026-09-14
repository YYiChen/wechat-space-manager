"""Offscreen tests for the real read-only window.

The window is driven through its public methods with a fake facade; no real
WeChat data, key or upstream package is involved.  One test asserts the window
exposes no cleanup or delete entry point at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QPushButton

from wechat_cleaner.domain.contracts import (
    FileIdentity,
    MappingConfidence,
    MediaRecord,
    MediaType,
)
from wechat_cleaner.gui import real_window as real_window_module
from wechat_cleaner.gui.gallery import THUMB_READY, THUMB_STATE_ROLE, decode_supported
from wechat_cleaner.gui.real_session import FakeRealSessionFacade
from wechat_cleaner.gui.real_window import RealReadOnlyWindow


def _record(
    media_type: MediaType, size: int, name: str, attach_dir: str = "8a8b", day: int = 1
) -> MediaRecord:
    return MediaRecord(
        account_id="wxid_demo_alpha",
        file=FileIdentity(
            relative_path=f"msg/attach/{attach_dir}/2026-02/Img/{name}", byte_size=size,
            modified_time_ns=1,
        ),
        media_type=media_type,
        observed_at=datetime(2026, 2, day, tzinfo=UTC),
        mapping_confidence=MappingConfidence.UNMAPPED,
        mapping_reason="filesystem scan only",
    )


def _merged_forward_record(name: str, attach_dir: str = "8a8b") -> MediaRecord:
    """A merged-forward record: attachments live under a Rec segment."""
    return MediaRecord(
        account_id="wxid_demo_alpha",
        file=FileIdentity(
            relative_path=f"msg/attach/{attach_dir}/2026-02/Rec/77aa/F/3/{name}",
            byte_size=4096,
            modified_time_ns=1,
        ),
        media_type=MediaType.FILE,
        observed_at=datetime(2026, 2, 1, tzinfo=UTC),
        mapping_confidence=MappingConfidence.UNMAPPED,
        mapping_reason="filesystem scan only",
    )


@pytest.fixture
def records() -> tuple[MediaRecord, ...]:
    return (
        _record(MediaType.IMAGE, 11 * 1024 * 1024, f"{uuid4().hex}.dat"),
        _record(MediaType.THUMBNAIL, 40 * 1024, f"{uuid4().hex}_t.dat"),
        _record(MediaType.VIDEO, 3 * 1024 * 1024, f"{uuid4().hex}.mp4"),
    )


def _window(qtbot, records, tmp_path=None) -> RealReadOnlyWindow:
    preview_path = ""
    if tmp_path is not None:
        from PySide6.QtGui import QPixmap

        preview_file = tmp_path / "preview.png"
        pixmap = QPixmap(48, 32)
        pixmap.fill()
        pixmap.save(str(preview_file), "PNG")
        preview_path = str(preview_file)
    facade = FakeRealSessionFacade(records=records, preview_image_path=preview_path)
    facade.cache_root = "C:\\synthetic-cache"
    window = RealReadOnlyWindow(facade)
    qtbot.addWidget(window)
    return window


def test_wizard_detects_accounts_and_connects(qtbot, records):
    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")

    detected = window.detect_accounts()

    assert len(detected) == 2
    assert window.account_combo.count() == 2
    # The largest account is preselected so the primary account is the default.
    assert window.account_combo.currentData() == "wxid_demo_alpha"
    assert "最大" in window.account_combo.currentText()
    assert window.connect_session() is True
    assert "35" in window.summary_label.text()


def test_records_render_and_filters_apply(qtbot, records):
    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()

    assert window.records_table.rowCount() == 3

    window.type_combo.setCurrentIndex(window.type_combo.findData("thumbnail"))
    assert window.apply_filters() == 1
    assert window.records_table.rowCount() == 1

    window.type_combo.setCurrentIndex(window.type_combo.findData("all"))
    window.min_mb_spin.setValue(5.0)
    assert window.apply_filters() == 1  # only the 11 MB image


def test_preview_success_shows_image_and_variant(qtbot, records, tmp_path):
    window = _window(qtbot, records, tmp_path)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    window.select_row(1)  # thumbnail record

    outcome = window.request_preview()

    assert outcome.ok is True
    assert outcome.variant == "thumbnail"
    assert "预览成功" in window.preview_status.text()
    assert "原图不会被修改" in window.preview_status.text()


def test_preview_failure_reports_reason(qtbot, records):
    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    voice = _record(MediaType.VOICE, 24 * 1024, "note.silk")
    window._grid_model.set_records((voice,))
    window.select_row(0)

    outcome = window.request_preview()

    assert outcome.ok is False
    assert outcome.error_code == "DECODER_UNAVAILABLE"
    assert "失败原因" in window.preview_status.text()


def test_preview_without_selection_is_rejected(qtbot, records):
    window = _window(qtbot, records)

    outcome = window.request_preview()

    assert outcome.ok is False
    assert outcome.error_code == "NO_SELECTION"


def test_cache_status_and_clearing(qtbot, records):
    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()

    status = window.refresh_cache_status()

    assert status.decoded_count == 3
    assert "解码预览 3 个" in window.cache_label.text()
    assert window.clear_decoded_cache() == 3
    assert window.clear_all_cache() == 43
    assert "微信源文件未受影响" in window.statusBar().currentMessage()


def test_window_has_no_cleanup_or_delete_entry_point(qtbot, records):
    window = _window(qtbot, records)

    labels = [button.text() for button in window.findChildren(QPushButton)]

    assert labels, "the window exposes buttons"
    # The ONLY removal capability is moving files to the Recycle Bin behind an
    # explicit confirmation (one file, or a multi-selection batch); permanent
    # deletion must not exist anywhere.
    forbidden = ("永久删除", "彻底删除", "直接删除", "清理", "执行计划")
    for label in labels:
        assert not any(word in label for word in forbidden), label
    recycle = sorted(label for label in labels if "回收站" in label)
    assert recycle == [
        "导出后移入回收站…",
        "批量导出后移入回收站…",
        "批量移到回收站…",
        "移到回收站…",
    ]
    assert "不会删除、移动或修改微信源文件" in window.read_only_label.text()


def test_recycle_moves_record_after_confirmation(qtbot, records, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    window.select_row(0)
    target = window.selected_record
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))

    outcome = window.recycle_selected()

    assert outcome is not None and outcome.ok is True
    assert target.file.relative_path in window._facade._recycled
    assert all(r.media_id != target.media_id for r in window._all_records)
    assert "已移入回收站" in window.statusBar().currentMessage()


def test_recycle_declined_leaves_everything_alone(qtbot, records, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    window.select_row(0)
    before = window._all_records
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))

    outcome = window.recycle_selected()

    assert outcome is None
    assert window._all_records == before
    assert window._facade._recycled == []
    assert "已取消" in window.statusBar().currentMessage()


def test_zero_keyed_account_warns_in_summary(qtbot, records):
    facade = FakeRealSessionFacade(records=records, keyed_count=0)
    facade.cache_root = "C:\\synthetic-cache"
    window = RealReadOnlyWindow(facade)
    qtbot.addWidget(window)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()

    assert window.connect_session() is True

    assert "未取得任何数据库密钥" in window.summary_label.text()


def test_facade_errors_surface_in_status_bar(qtbot, records, monkeypatch):
    # An empty field makes the window auto-detect a root first; pin the probe
    # to "nothing found" so the test exercises the error path, not the local
    # machine's WeChat install.
    monkeypatch.setattr(real_window_module, "find_data_root", lambda override=None: None)
    window = _window(qtbot, records)
    window.set_data_root("")  # fake facade rejects an empty root

    detected = window.detect_accounts()

    assert detected == ()
    assert "请先选择微信数据目录" in window.statusBar().currentMessage()


def test_detect_accounts_fills_an_empty_root_by_itself(qtbot, records, monkeypatch):
    # "自动检测" must not require the user to type a path first.
    monkeypatch.setattr(
        real_window_module, "find_data_root", lambda override=None: r"C:\found\xwechat_files"
    )
    window = _window(qtbot, records)

    detected = window.detect_accounts()

    assert window.data_root_edit.text() == r"C:\found\xwechat_files"
    assert len(detected) == 2


def test_connect_announces_the_root_so_it_can_be_remembered(qtbot, records):
    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    seen: list = []
    window.session_connected.connect(lambda root, account: seen.append((root, account)))

    assert window.connect_session() is True

    assert seen == [("C:\\synthetic\\xwechat_files", "wxid_demo_alpha")]


def test_grid_tiles_records_newest_first(qtbot, records):
    window = _window(qtbot, records)
    window.set_data_root("C://synthetic//xwechat_files")
    window.detect_accounts()
    window.connect_session()

    # An album shows recent photos first; the grid tiles every match at once.
    assert window.records_table.rowCount() == 3
    rows = window.records_table.model().records()
    stamps = [record.observed_at for record in rows]
    assert stamps == sorted(stamps, reverse=True)


def test_selection_maps_to_record_by_grid_position(qtbot, records):
    window = _window(qtbot, records)
    window.set_data_root("C://synthetic//xwechat_files")
    window.detect_accounts()
    window.connect_session()

    window.select_row(1)
    record = window.selected_record
    assert record is not None
    assert record is window.records_table.model().record_at(1)
    assert window._record_by_id[str(record.media_id)] is record


def test_date_filter_before_cutoff(qtbot, records):
    from PySide6.QtCore import QDate

    window = _window(qtbot, records)
    window.set_data_root("C://synthetic//xwechat_files")
    window.detect_accounts()
    window.connect_session()

    window.time_combo.setCurrentIndex(window.time_combo.findData("before"))
    assert window.before_date.isEnabled()
    window.before_date.setDate(QDate(2026, 1, 31))
    assert window.apply_filters() == 0
    window.before_date.setDate(QDate(2026, 2, 1))
    assert window.apply_filters() == 3
    window.time_combo.setCurrentIndex(window.time_combo.findData("all"))
    assert window.apply_filters() == 3


def test_preview_autoloads_on_selection(qtbot, records, tmp_path):
    window = _window(qtbot, records, tmp_path)
    window.set_data_root("C://synthetic//xwechat_files")
    window.detect_accounts()
    window.connect_session()

    window.select_row(1)  # thumbnail record
    qtbot.waitUntil(lambda: window.last_preview is not None, timeout=3000)

    assert window.last_preview.ok is True
    assert "预览成功" in window.preview_status.text()


def test_original_availability_filter_excludes_non_images(qtbot, records):
    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()

    window.availability_combo.setCurrentIndex(
        window.availability_combo.findData("original_available")
    )

    # image = original_available; thumbnail reports thumbnail_only; the video
    # is not an image at all and must not sneak into an image-only filter.
    assert window.apply_filters() == 1
    assert all(
        window._record_by_id[media_id].media_type
        in (MediaType.IMAGE, MediaType.THUMBNAIL)
        for media_id in window.displayed_media_ids
    )


def test_preview_non_image_reports_readable_reason(qtbot, records):
    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    voice = _record(MediaType.VOICE, 24 * 1024, "note.silk")
    window._grid_model.set_records((voice,))
    window.select_row(0)

    outcome = window.request_preview()

    assert outcome.ok is False
    assert "不是图片" in outcome.error_message
    assert "不是图片" in window.preview_status.text()


def test_export_buttons_follow_selection(qtbot, records):
    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()

    assert window.export_button.isEnabled() is False
    assert window.export_recycle_button.isEnabled() is False
    window.select_row(0)
    assert window.export_button.isEnabled() is True
    assert window.export_recycle_button.isEnabled() is True


def test_export_selected_writes_original_file(qtbot, records, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    facade_payload = b"original-jpeg-bytes"
    window = _window(qtbot, records)
    window._facade.export_payload = facade_payload
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    window.select_row(0)
    target = tmp_path / "orig.jpg"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: (str(target), "Images")),
    )

    outcome = window.export_selected()

    assert outcome is not None and outcome.ok is True
    assert target.read_bytes() == facade_payload
    assert outcome.written_path == str(target)
    assert "已导出" in window.statusBar().currentMessage()


def test_export_corrects_extension_for_png_payload(qtbot, records, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    window = _window(qtbot, records)
    window._facade.export_payload = b"png-bytes"
    window._facade.export_format = "PNG"
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    window.select_row(0)
    target = tmp_path / "orig.jpg"  # suggested JPEG name, actual PNG payload
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: (str(target), "Images")),
    )

    outcome = window.export_selected()

    assert outcome is not None and outcome.ok is True
    assert outcome.written_path.endswith("orig.png")
    assert (tmp_path / "orig.png").read_bytes() == b"png-bytes"


def test_export_cancelled_writes_nothing(qtbot, records, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    window.select_row(0)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))

    outcome = window.export_selected()

    assert outcome is None
    assert list(tmp_path.iterdir()) == []
    assert "已取消" in window.statusBar().currentMessage()


def test_export_non_image_is_rejected(qtbot, records):
    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    window.select_row(2)  # video record

    outcome = window.export_selected()

    assert outcome is None
    assert "不是图片" in window.preview_status.text()


def test_export_and_recycle_full_flow(qtbot, records, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    window = _window(qtbot, records)
    window._facade.export_payload = b"keep-me"
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    window.select_row(0)
    target_record = window.selected_record
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: (str(tmp_path / "out.jpg"), "Images")),
    )

    outcome = window.export_and_recycle_selected()

    assert outcome is not None and outcome.ok is True
    assert (tmp_path / "out.jpg").read_bytes() == b"keep-me"
    assert target_record.file.relative_path in window._facade._recycled
    assert all(r.media_id != target_record.media_id for r in window._all_records)
    assert "移入回收站" in window.statusBar().currentMessage()


def test_export_and_recycle_declined_leaves_everything(qtbot, records, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    window.select_row(0)
    before = window._all_records
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))

    outcome = window.export_and_recycle_selected()

    assert outcome is None
    assert window._all_records == before
    assert window._facade._recycled == []
    assert "已取消" in window.statusBar().currentMessage()


def test_export_and_recycle_keeps_export_when_recycle_fails(
    qtbot, records, tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from wechat_cleaner.gui.real_session import RecycleOutcome

    window = _window(qtbot, records)
    window._facade.export_payload = b"safe-copy"
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    window.select_row(0)
    target_record = window.selected_record
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: (str(tmp_path / "out.jpg"), "Images")),
    )

    def _failing_recycle(record):
        return RecycleOutcome(
            ok=False, error_code="FILE_IDENTITY_MISMATCH", error_message="身份不符",
            relative_path=record.file.relative_path, byte_size=record.file.byte_size,
        )

    monkeypatch.setattr(window._facade, "recycle_record", _failing_recycle)

    outcome = window.export_and_recycle_selected()

    assert outcome is not None and outcome.ok is False
    assert (tmp_path / "out.jpg").read_bytes() == b"safe-copy"  # export survives
    assert any(r.media_id == target_record.media_id for r in window._all_records)
    assert "失败" in window.statusBar().currentMessage()


# ---------------------------------------------------------------------------
# progress feedback: pulse bar for black-box steps, real counts/percent where
# the pipeline provides them
# ---------------------------------------------------------------------------


def test_busy_step_shows_indeterminate_progress_bar_then_hides_it(qtbot, records):
    window = _window(qtbot, records)
    pulses: list[bool] = []
    original = window._progress_indeterminate

    def _spy() -> None:
        pulses.append(True)
        original()

    window._progress_indeterminate = _spy
    window.set_data_root("C:\\synthetic\\xwechat_files")

    assert not window.progress_bar.isVisible()
    window.detect_accounts()
    window.connect_session()

    assert pulses, "the blocking step must switch the bar into pulse mode"
    assert not window.progress_bar.isVisible(), "the bar hides when the step ends"


def test_emit_progress_is_a_noop_outside_a_busy_step(qtbot, records):
    window = _window(qtbot, records)

    window._emit_progress(7)  # must not raise, must not show the bar

    assert not window.progress_bar.isVisible()
    assert window._busy_progress == ""


def test_scan_progress_reports_running_file_counts(qtbot, records, monkeypatch):
    class ProgressFake(FakeRealSessionFacade):
        """Calls the progress callback like the real scanner would."""

        def load_records(self, limit=None, progress=None):
            if progress is not None:
                progress(1)
                progress(3)
            return super().load_records(limit, None)

    facade = ProgressFake(records=records)
    window = RealReadOnlyWindow(facade)
    qtbot.addWidget(window)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()

    messages: list[str] = []
    original = window._set_status

    def _recording(message: str) -> None:
        messages.append(message)
        original(message)

    window._set_status = _recording
    window.connect_session()

    # The queued worker signals are delivered before the step finishes, so the
    # status bar really showed the live counts.
    assert any("已扫描 1 个文件" in message for message in messages)
    assert any("已扫描 3 个文件" in message for message in messages)
    assert not window.progress_bar.isVisible()


def test_availability_filter_drives_real_percent_bar(qtbot, records):
    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    values: list[int] = []
    original = window._progress_value

    def _spy(percent: int) -> None:
        values.append(percent)
        original(percent)

    window._progress_value = _spy
    window.availability_combo.setCurrentIndex(
        window.availability_combo.findData("original_available")
    )

    window.apply_filters()

    assert values and values[0] == 0, "the bar starts at a real 0%"
    assert not window.progress_bar.isVisible()


# ---------------------------------------------------------------------------
# session filter (the chat with X / the group Y) + merged-forward filter
# ---------------------------------------------------------------------------


def test_session_combo_lists_chats_by_media_count_and_filters(qtbot):
    recs = (
        _record(MediaType.IMAGE, 1024, "a.dat", attach_dir="8a8b"),
        _record(MediaType.IMAGE, 1024, "b.dat", attach_dir="8a8b"),
        _record(MediaType.THUMBNAIL, 512, "c_t.dat", attach_dir="99cc"),
    )
    facade = FakeRealSessionFacade(records=recs)
    facade.session_names = {"8a8b": "小张", "99cc": "九群"}
    facade.session_chatrooms = {"99cc"}
    window = RealReadOnlyWindow(facade)
    qtbot.addWidget(window)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()

    window.connect_session()

    assert window.session_combo.count() == 3, "全部会话 + two chats with media"
    assert window.session_combo.itemData(0) == ""
    # Most media first; display names come from the session mapping.
    assert window.session_combo.itemData(1) == "8a8b"
    assert "小张" in window.session_combo.itemText(1)
    assert window.session_combo.itemData(2) == "99cc"
    assert "九群" in window.session_combo.itemText(2)
    assert "群聊" in window.session_combo.itemText(2)

    window.session_combo.setCurrentIndex(window.session_combo.findData("8a8b"))
    assert window.apply_filters() == 2
    assert window.records_table.rowCount() == 2
    assert "会话筛选" in window.statusBar().currentMessage()

    window.session_combo.setCurrentIndex(window.session_combo.findData("99cc"))
    assert window.apply_filters() == 1

    window.session_combo.setCurrentIndex(0)  # 全部会话
    assert window.apply_filters() == 3


def test_session_filter_degrades_to_hash_label_without_mapping(qtbot, records):
    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()

    # The default fake has no session_names: entries show the hash prefix.
    assert window.session_combo.count() == 2
    assert "未知会话 8a8b" in window.session_combo.itemText(1)


def test_merged_forward_checkbox_filters_rec_records(qtbot):
    normal = _record(MediaType.IMAGE, 1024, "a.dat")
    forwarded = _merged_forward_record("report.pdf")
    other_chat = _merged_forward_record("doc.pdf", attach_dir="99cc")
    facade = FakeRealSessionFacade(records=(normal, forwarded, other_chat))
    window = RealReadOnlyWindow(facade)
    qtbot.addWidget(window)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()

    assert window.records_table.rowCount() == 3
    window.merged_check.setChecked(True)
    assert window.apply_filters() == 2
    assert "合并转发" in window.statusBar().currentMessage()

    # Composes with the session filter (AND semantics).
    window.session_combo.setCurrentIndex(window.session_combo.findData("8a8b"))
    assert window.apply_filters() == 1
    survivor = window.records_table.model().record_at(0)
    assert survivor is not None
    assert survivor.file.relative_path.endswith("report.pdf")

    window.merged_check.setChecked(False)
    window.session_combo.setCurrentIndex(0)
    assert window.apply_filters() == 3


# ---------------------------------------------------------------------------
# background preview warming: rows are decoded into the cache before they
# are selected, like a file manager's thumbnail pass
# ---------------------------------------------------------------------------


def _warm_settled(window: RealReadOnlyWindow) -> bool:
    """Queue drained *and* every completion already delivered to the model.

    ``_warmed`` is updated directly on the worker thread, while the pixmap
    reaches the model via a queued signal — checking only the queue can race
    ahead of the delivery and read a cell as "not decoded".
    """
    if window._warm_queue or window._warm_inflight:
        return False
    return all(
        window.records_table.model().has_thumbnail(str(record.media_id))
        or window.records_table.model().has_failed(str(record.media_id))
        for record in window._grid_model.records()
        if decode_supported(record.media_type.value)
    )


def test_render_decodes_visible_cells_in_background(qtbot, records, tmp_path):
    window = _window(qtbot, records, tmp_path)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()

    window.connect_session()

    # The fake preview returns instantly; the pool must drain the batch.
    # The image and thumbnail decode into the grid model; the video has no
    # cover decoder behind the fake facade, so it is recorded as failed
    # instead of "warmed".
    qtbot.waitUntil(lambda: _warm_settled(window), timeout=5000)
    decodable = {
        str(record.media_id)
        for record in records
        if record.media_type is not MediaType.VIDEO
    }
    assert window._warmed == decodable
    assert str(records[2].media_id) in window._warm_failed
    for media_id in decodable:
        assert window.records_table.model().has_thumbnail(media_id)

    # Already-decoded cells are not queued again on a re-render.
    window.apply_filters()
    assert not window._warm_queue
    assert window._warm_inflight == 0


def test_selection_queues_neighbourhood_cells_first(qtbot, records, tmp_path):
    window = _window(qtbot, records, tmp_path)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    qtbot.waitUntil(lambda: _warm_settled(window), timeout=5000)

    # Pretend every cell is cold again, then select row 0: its neighbourhood
    # must be decoded without waiting for the user to click each one.  (The
    # video in row 2 has no still frame, so only the thumbnail can arrive.)
    window._warmed.clear()
    window._warm_failed.clear()
    window.records_table.model().clear_thumbnails()
    window.select_row(0)

    neighbour_id = str(records[1].media_id)
    qtbot.waitUntil(
        lambda: _warm_settled(window) and neighbour_id in window._warmed, timeout=5000
    )


def test_close_stops_background_warming(qtbot, records, tmp_path):
    window = _window(qtbot, records, tmp_path)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()

    window.close()

    assert window._warm_stopped is True
    assert not window._warm_queue


# ---------------------------------------------------------------------------
# shared facade worker lifecycle
# ---------------------------------------------------------------------------
# The upstream session is built on the worker thread and keeps thread-affine
# native state, so that thread has to outlive the session.  Building a thread
# per call left a live QThread for Qt to destroy at teardown, which faulted
# the real (Windows) process with a bare access violation.


def test_blocking_steps_reuse_one_worker_thread(qtbot, records, tmp_path):
    window = _window(qtbot, records, tmp_path)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()

    first = window._worker
    assert first is not None

    window.load_records()

    assert window._worker is first, "a new thread per call would strand the session"


def test_close_joins_the_worker_thread(qtbot, records, tmp_path):
    window = _window(qtbot, records, tmp_path)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()

    worker = window._worker
    assert worker is not None

    window.close()

    assert window._worker is None
    assert worker.isRunning() is False


def test_shutdown_worker_is_idempotent(qtbot, records, tmp_path):
    window = _window(qtbot, records, tmp_path)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()

    window._shutdown_worker()
    window._shutdown_worker()  # must not raise on a missing/joined worker

    assert window._worker is None


def test_worker_started_only_when_a_blocking_step_runs(qtbot, records, tmp_path):
    window = _window(qtbot, records, tmp_path)
    # Merely constructing the window must not spawn a thread: demo mode and
    # the wizard's first screen need none.
    assert window._worker is None


# ---------------------------------------------------------------------------
# multi-select and batch management
# ---------------------------------------------------------------------------


def test_multi_select_records_and_lights_batch_actions(qtbot, records):
    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()

    # Nothing selected: every management action is dark.
    assert window.batch_export_button.isEnabled() is False
    assert window.batch_recycle_button.isEnabled() is False
    assert window.selected_records == []

    window.select_rows([0, 1])
    assert len(window.selected_records) == 2
    assert "已选择 2 项" in window.selection_label.text()
    assert window.batch_recycle_button.isEnabled() is True
    # Both selected records are images (photo + thumbnail), so export applies.
    assert window.batch_export_button.isEnabled() is True
    # Single-file actions need exactly one selection.
    assert window.export_button.isEnabled() is False
    assert window.recycle_button.isEnabled() is False

    window.select_rows([2])  # a video: not exportable
    assert window.export_button.isEnabled() is False
    assert window.batch_export_button.isEnabled() is False
    assert window.batch_recycle_button.isEnabled() is True


def test_multi_select_does_not_auto_preview(qtbot, records, tmp_path):
    window = _window(qtbot, records, tmp_path)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()

    window.select_rows([0, 1])
    qtbot.wait(500)  # longer than the auto-preview debounce

    # The detail pane shows one photo; with a multi-selection it stays quiet.
    assert window.last_preview is None
    outcome = window.request_preview()
    assert outcome.ok is False
    assert "多张" in outcome.error_message


def test_batch_export_writes_every_selected_image(
    qtbot, records, tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    window = _window(qtbot, records)
    window._facade.export_payload = b"original-jpeg-bytes"
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    window.select_rows([0, 1])  # image + thumbnail
    out_dir = tmp_path / "album-export"
    out_dir.mkdir()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(out_dir))
    )

    outcome = window.export_selected_batch()

    assert outcome is not None
    # 只有缩略图的记录（第 2 行）不参与导出，也不产生文件。
    assert outcome.succeeded_count == 1
    assert outcome.failed == ()
    assert len(outcome.skipped) == 1
    assert "仅有缩略图" in outcome.skipped[0][1]
    assert "跳过 1 项" in outcome.summary()
    written = sorted(path.name for path in out_dir.iterdir())
    assert len(written) == 1
    assert "批量导出原图" in window.statusBar().currentMessage()
    # Export never touches the WeChat source files.
    assert window._facade._recycled == []
    assert len(window._all_records) == 3


def test_batch_export_skips_non_images_with_a_reason(
    qtbot, records, tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    window = _window(qtbot, records)
    window._facade.export_payload = b"original-jpeg-bytes"
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    window.select_rows([0, 1, 2])  # includes a video
    out_dir = tmp_path / "album-export"
    out_dir.mkdir()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(out_dir))
    )

    outcome = window.export_selected_batch()

    assert outcome is not None
    # 视频：非图片跳过；缩略图记录：无原图跳过（worker 侧判定）。
    assert outcome.succeeded_count == 1
    assert len(outcome.skipped) == 2
    reasons = "；".join(reason for _path, reason in outcome.skipped)
    assert "非图片" in reasons
    assert "仅有缩略图" in reasons
    assert "跳过 2 项" in outcome.summary()


def test_batch_recycle_removes_every_selected_record(qtbot, records, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    window.select_rows([0, 1])
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))

    outcome = window.recycle_selected_batch()

    assert outcome is not None
    assert outcome.succeeded_count == 2
    assert len(window._facade._recycled) == 2
    remaining = {str(record.media_id) for record in window._all_records}
    assert remaining == {str(records[2].media_id)}
    assert window.records_table.rowCount() == 1
    assert "批量移到回收站" in window.statusBar().currentMessage()


def test_batch_recycle_declined_leaves_everything_alone(qtbot, records, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window = _window(qtbot, records)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    window.select_rows([0, 1])
    before = window._all_records
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))

    outcome = window.recycle_selected_batch()

    assert outcome is None
    assert window._all_records == before
    assert window._facade._recycled == []
    assert "已取消" in window.statusBar().currentMessage()


def test_batch_export_then_recycle_keeps_unexported_files(
    qtbot, records, tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    window = _window(qtbot, records)
    window._facade.export_payload = b"original-jpeg-bytes"
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    window.select_rows([0, 1])
    out_dir = tmp_path / "album-export"
    out_dir.mkdir()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(out_dir))
    )

    outcome = window.export_and_recycle_selected_batch()

    assert outcome is not None
    # 只有缩略图的记录：不导出、也绝不回收（没有备份的源文件保持不动）。
    assert outcome.succeeded_count == 1
    assert len(window._facade._recycled) == 1
    assert len(outcome.skipped) == 1
    assert "仅有缩略图" in outcome.skipped[0][1]
    assert {str(record.media_id) for record in window._all_records} == {
        str(records[1].media_id),
        str(records[2].media_id),
    }


def test_batch_reports_per_file_failures_without_aborting(
    qtbot, tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    # 两张都有原图的记录：第一张导出失败，第二张必须照常导出。
    window = _window(qtbot, _many_records(2))
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    window.select_rows([0, 1])
    out_dir = tmp_path / "album-export"
    out_dir.mkdir()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(out_dir))
    )

    calls = {"n": 0}
    real_export = window._facade.export_original

    def flaky_export(record):
        calls["n"] += 1
        if calls["n"] == 1:
            from wechat_cleaner.gui.real_session import ExportOutcome

            return ExportOutcome(
                ok=False,
                error_code="DECODE_FAILED",
                error_message="合成失败",
                relative_path=record.file.relative_path,
            )
        return real_export(record)

    monkeypatch.setattr(window._facade, "export_original", flaky_export)

    outcome = window.export_selected_batch()

    assert outcome is not None
    assert outcome.succeeded_count == 1
    assert outcome.failed_count == 1
    assert "DECODE_FAILED" in outcome.failed[0][1]
    assert "首个失败" in window.preview_status.text()


# ----------------------------------------------------------------------
# filtering: decode what is on screen, in the order it is on screen
# ----------------------------------------------------------------------
def _many_records(count: int) -> tuple[MediaRecord, ...]:
    return tuple(
        _record(MediaType.IMAGE, 1024 * (index + 1), f"pic_{index:04d}.dat")
        for index in range(count)
    )


def _shown_window(qtbot, records, preview_path: str = "") -> RealReadOnlyWindow:
    facade = FakeRealSessionFacade(records=records, preview_image_path=preview_path)
    facade.cache_root = "C:\\synthetic-cache"
    window = RealReadOnlyWindow(facade)
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    qtbot.waitExposed(window)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()
    return window


def test_filter_returns_the_grid_to_the_top(qtbot):
    """A narrowing filter must not leave the user at the far end of the list.

    The scroll offset used to survive the model reset, so the offset clamped to
    the end of the shorter list: the grid decoded the *oldest* media while the
    user looked at it, and never the top of the album.
    """
    window = _shown_window(qtbot, _many_records(300))
    bar = window.records_table.verticalScrollBar()
    bar.setValue(bar.maximum())
    assert bar.value() > 0

    window.apply_filters()

    assert bar.value() == 0
    first, last = window.records_table.visible_range()
    assert first == 0
    assert last >= 0


def test_visible_cells_are_queued_before_their_neighbours(qtbot):
    window = _shown_window(qtbot, _many_records(300))
    records = window._grid_model.records()
    first, last = window.records_table.visible_range()
    assert first == 0

    # Seed a deliberately reversed backlog and keep the pool out of the way so
    # the queue can be inspected as the reorder left it.
    with window._warm_lock:
        window._warm_stopped = True
        window._warm_queue.clear()
        window._warm_queue.extend(reversed(records))
    window._order_warm_queue_by_visibility()

    rows = [window._grid_model.index_of_record(record) for record in window._warm_queue]
    distances = [
        0 if first <= row <= last else min(abs(row - first), abs(row - last))
        for row in rows
    ]
    assert distances == sorted(distances), "the queue must be nearest-first"
    assert distances[0] == 0, "a visible cell must be decoded next"
    assert rows[0] in range(first, last + 1)


def test_rows_outside_the_filter_decode_last(qtbot):
    window = _shown_window(qtbot, _many_records(20))
    model = window._grid_model
    visible = model.record_at(0)

    assert window._row_distance(visible, 0, 3) == 0
    assert window._row_distance(model.record_at(9), 0, 3) == 6

    # A record from a previous render is not in the model any more; decoding it
    # would only delay a cell the user can see.
    stale = _record(MediaType.IMAGE, 4096, "gone.dat", attach_dir="ffff")
    assert window._row_distance(stale, 0, 3) >= 1 << 20


# ----------------------------------------------------------------------
# the preview pane must not steal width from the grid
# ----------------------------------------------------------------------
def _columns_per_row(view) -> int:
    """How many tiles the view actually puts on the first row."""
    model = view.model()
    if model is None or model.rowCount() < 2:
        return model.rowCount() if model is not None else 0
    first_y = view.visualRect(model.index(0, 0)).y()
    count = 1
    for row in range(1, min(20, model.rowCount())):
        if view.visualRect(model.index(row, 0)).y() != first_y:
            break
        count += 1
    return count


def test_preview_does_not_resize_the_grid(qtbot, tmp_path):
    """Selecting a photo must not change how many tiles fit per row.

    The preview column shares a row with the grid, and the widgets in it carry
    the selection text and the preview status line.  A word-wrapped QLabel
    reports its whole text as the size hint (and re-enables height-for-width on
    every setText), so the first preview widened the column by ~100px, the grid
    lost a column, and a small window went from four tiles per row to three -
    a layout change caused purely by selection.
    """
    big = tmp_path / "big_preview.png"
    pixmap = QPixmap(1200, 900)
    pixmap.fill()
    pixmap.save(str(big), "PNG")

    window = _shown_window(qtbot, _many_records(40), preview_path=str(big))
    grid = window.records_table
    grid.doItemsLayout()
    columns_before = _columns_per_row(grid)
    width_before = grid.width()

    window.select_row(0)
    outcome = window.request_preview()

    assert outcome.ok is True
    assert window.preview_label.source() is not None, "a picture must be on screen"
    grid.doItemsLayout()
    assert grid.width() == width_before
    assert _columns_per_row(grid) == columns_before
    assert columns_before >= 3, "the fixture must fill a row to be meaningful"


def test_status_line_length_does_not_widen_the_preview_column(qtbot, tmp_path):
    """The pane's demand on the layout must not follow its text."""
    big = tmp_path / "big_preview.png"
    pixmap = QPixmap(1200, 900)
    pixmap.fill()
    pixmap.save(str(big), "PNG")
    window = _shown_window(qtbot, _many_records(20), preview_path=str(big))
    group = window.preview_label.parentWidget()
    before = group.sizeHint().width()

    window.select_all_cells()
    window.request_preview()

    assert group.sizeHint().width() == before
    assert "已选择 20 项" in window.selection_label.text()


def test_preview_pane_shrinks_the_picture_to_fit(qtbot, tmp_path):
    big = tmp_path / "big_preview.png"
    pixmap = QPixmap(2000, 1000)
    pixmap.fill()
    pixmap.save(str(big), "PNG")
    window = _shown_window(qtbot, _many_records(5), preview_path=str(big))
    window.select_row(0)

    window.request_preview()

    pane = window.preview_label
    shown = pane.pixmap()
    assert not shown.isNull()
    assert shown.width() <= pane.width()
    assert shown.height() <= pane.height()


# ----------------------------------------------------------------------
# time presets: recent *and* "older than"
# ----------------------------------------------------------------------
def test_time_presets_can_select_old_media(qtbot):
    # Every fixture record is dated 2026-02, months before "now".
    window = _shown_window(qtbot, _many_records(6))

    window.time_combo.setCurrentIndex(window.time_combo.findData("d30"))
    assert window.apply_filters() == 0, "nothing is from the last 30 days"

    window.time_combo.setCurrentIndex(window.time_combo.findData("old30"))
    assert window.apply_filters() == 6, "everything is older than 30 days"

    window.time_combo.setCurrentIndex(window.time_combo.findData("old365"))
    assert window.apply_filters() == 0, "nothing is older than a year"


# ----------------------------------------------------------------------
# sort orders
# ----------------------------------------------------------------------
def _names(window) -> list[str]:
    return [
        record.file.relative_path.rsplit("\\", 1)[-1]
        for record in window._grid_model.records()
    ]


def test_sort_control_reorders_the_grid(qtbot):
    records = (
        _record(MediaType.IMAGE, 9 * 1024, "a_big_old.dat", day=1),
        _record(MediaType.IMAGE, 1 * 1024, "b_small_new.dat", day=20),
        _record(MediaType.IMAGE, 5 * 1024, "c_mid.dat", day=10),
    )
    window = _shown_window(qtbot, records)

    # Default: newest first.
    assert _names(window) == ["b_small_new.dat", "c_mid.dat", "a_big_old.dat"]

    window.sort_combo.setCurrentIndex(window.sort_combo.findData("size_desc"))
    assert _names(window) == ["a_big_old.dat", "c_mid.dat", "b_small_new.dat"]

    window.sort_combo.setCurrentIndex(window.sort_combo.findData("size_asc"))
    assert _names(window) == ["b_small_new.dat", "c_mid.dat", "a_big_old.dat"]

    window.sort_combo.setCurrentIndex(window.sort_combo.findData("name_asc"))
    assert _names(window) == ["a_big_old.dat", "b_small_new.dat", "c_mid.dat"]

    window.sort_combo.setCurrentIndex(window.sort_combo.findData("name_desc"))
    assert _names(window) == ["c_mid.dat", "b_small_new.dat", "a_big_old.dat"]

    window.sort_combo.setCurrentIndex(window.sort_combo.findData("time_asc"))
    assert _names(window) == ["a_big_old.dat", "c_mid.dat", "b_small_new.dat"]


def test_sort_returns_the_grid_to_the_top(qtbot):
    window = _shown_window(qtbot, _many_records(300))
    bar = window.records_table.verticalScrollBar()
    bar.setValue(bar.maximum())

    window.sort_combo.setCurrentIndex(window.sort_combo.findData("size_asc"))

    assert bar.value() == 0


# ----------------------------------------------------------------------
# Ctrl+wheel zoom, wired to the decode resolution
# ----------------------------------------------------------------------
def test_zoom_never_drops_the_pictures_on_screen(qtbot):
    """放大重解码时，已显示的图必须保留，直到更清晰的版本替换它。"""
    window = _shown_window(qtbot, _many_records(30))
    model = window._grid_model
    media_id = str(model.records()[0].media_id)
    decoded = QPixmap(64, 64)
    decoded.fill()
    model.apply_thumbnail(media_id, decoded)
    sizes: list = []
    window.thumbnail_size_changed.connect(sizes.append)

    assert window.thumb_size() == 132
    assert window._warm_decode_edge == 256

    # Smaller: nothing to re-decode, nothing to lose.
    assert window.set_thumb_size(112) is True
    assert model.has_thumbnail(media_id) is True
    assert window._warm_decode_edge == 256
    assert sizes == [112]

    # Bigger past the decoded resolution: the visible cells are re-queued at
    # the sharper resolution, but the pictures stay on screen throughout.
    assert window.set_thumb_size(232) is True
    assert model.has_thumbnail(media_id) is True
    assert window._grid_model.data(
        model.index(model.row_of(media_id), 0), THUMB_STATE_ROLE
    ) == THUMB_READY
    assert window._warm_decode_edge == 384
    qtbot.waitUntil(lambda: bool(window._warmed), timeout=5000)
    assert sizes == [112, 232]


# ----------------------------------------------------------------------
# preview quality choice: smart / thumbnail / original
# ----------------------------------------------------------------------
def test_smart_preview_is_the_default_and_upgrades_small_pictures(qtbot):
    """「智能」：小图直接解原图，大图仍用缩略图，按钮不再叫"缩略图"。"""
    window = _shown_window(qtbot, _many_records(6))

    assert window.preview_button.text() == "预览图片"
    assert window.preview_variant_combo.currentData() == "auto"
    # _many_records 里的图都很小（几 KB），智能档应当直接用原图。
    window.select_row(0)
    outcome = window.request_preview()

    assert outcome.ok is True
    assert outcome.variant == "original"

    window.preview_variant_combo.setCurrentIndex(
        window.preview_variant_combo.findData("thumbnail")
    )
    assert window.request_preview().variant == "thumbnail"

    window.preview_variant_combo.setCurrentIndex(
        window.preview_variant_combo.findData("auto")
    )
    assert window.request_preview().variant == "original"


# ----------------------------------------------------------------------
# no original -> no export: the buttons must say so
# ----------------------------------------------------------------------
def test_export_is_blocked_when_only_a_thumbnail_exists(qtbot, records):
    """只有缩略图的记录禁用「导出原图」，并说明原因（用户点不出结果来）。"""
    window = _shown_window(qtbot, records)

    window.select_row(1)  # THUMBNAIL 记录：磁盘上没有原图
    assert window.export_button.isEnabled() is False
    assert window.export_recycle_button.isEnabled() is False
    assert "仅有缩略图" in window.export_button.toolTip()

    window.select_row(0)  # IMAGE 记录：有原图
    assert window.export_button.isEnabled() is True
    assert window.export_recycle_button.isEnabled() is True


def test_single_export_refuses_thumbnail_only_records(qtbot, records):
    """按钮被禁用之外，直接调用也必须拒绝并给出可读原因。"""
    window = _shown_window(qtbot, records)
    window.select_row(1)

    outcome = window.export_selected()

    assert outcome is None, "没有原图的记录不允许导出"
    assert "仅有缩略图" in window.preview_status.text()
    assert "仅有缩略图" in window.statusBar().currentMessage()


def test_type_filter_can_narrow_to_exportable_originals(qtbot):
    """「原图（可导出）」= 图片族里真正解得开、能导出的那些。"""
    window = _shown_window(qtbot, _many_records(6) + (records_fixture_thumbnail(),))
    window.select_all_cells  # noqa: B018 - 属性存在性自检（无副作用）
    index = window.type_combo.findData("original")
    assert index >= 0, "类型下拉里必须有「原图（可导出）」"

    window.type_combo.setCurrentIndex(index)

    matched = window.apply_filters()
    types = {record.media_type for record in window._grid_model.records()}
    assert matched == 6
    assert types == {MediaType.IMAGE}


def records_fixture_thumbnail():
    """一条缩略图记录，用来验证「原图」筛选会把它排除。"""
    return _record(MediaType.THUMBNAIL, 40 * 1024, "thumb_only_t.dat")


# ----------------------------------------------------------------------
# which chat did this photo come from
# ----------------------------------------------------------------------
def test_selection_shows_which_chat_the_photo_came_from(qtbot, records):
    """选中单张照片时，预览面板要说明它来自哪个会话。"""
    window = _shown_window(qtbot, records)
    window._session_mapping = {
        "8a8b": {"name": "测试联系人", "username": "wxid_friend", "is_chatroom": False},
    }
    window.select_row(0)

    text = window.selection_label.text()
    assert "来自「测试联系人」（单聊）" in text


def test_selection_shows_chatroom_and_merged_forward_source(qtbot, records):
    window = _shown_window(qtbot, records)
    window._session_mapping = {
        "8a8b": {"name": "家庭群", "username": "123@chatroom", "is_chatroom": True},
    }
    merged = MediaRecord(
        account_id="wxid_demo_alpha",
        file=FileIdentity(
            relative_path="msg\\attach\\8a8b\\2026-02\\Rec\\77aa\\F\\3\\card.png",
            byte_size=4096,
            modified_time_ns=1,
        ),
        media_type=MediaType.FILE,
        observed_at=datetime(2026, 2, 1, tzinfo=UTC),
        mapping_confidence=MappingConfidence.UNMAPPED,
        mapping_reason="probe",
    )
    window._grid_model.set_records((merged,))
    window.select_row(0)

    assert "来自「家庭群」（群聊）的合并转发记录" in window.selection_label.text()


def test_selection_reports_unattributable_types(qtbot):
    """文件/视频/语音按月存放，没有会话属性——如实说明而不是乱猜。"""
    monthly = MediaRecord(
        account_id="wxid_demo_alpha",
        file=FileIdentity(
            relative_path="msg\\file\\2026-02\\abc.dat",
            byte_size=4096,
            modified_time_ns=1,
        ),
        media_type=MediaType.FILE,
        observed_at=datetime(2026, 2, 1, tzinfo=UTC),
        mapping_confidence=MappingConfidence.UNMAPPED,
        mapping_reason="probe",
    )
    window = _shown_window(qtbot, (monthly,))
    window._session_mapping = {}
    window.select_row(0)

    assert "无法归属到具体会话" in window.selection_label.text()


def test_selection_says_unknown_chat_when_mapping_misses(qtbot, records):
    """映射缺失（如未拿到联系人库密钥）时显示目录前缀，不显示空名字。"""
    window = _shown_window(qtbot, records)
    window._session_mapping = {}
    window.select_row(0)

    assert "未知会话（目录 8a8b…" in window.selection_label.text()


# ----------------------------------------------------------------------
# "全部选中项都是缩略图"时，批量导出必须保持禁用
# ----------------------------------------------------------------------
def test_batch_export_stays_disabled_for_thumbnail_only_selections(qtbot, records):
    """THUMBNAIL 记录本身没有原图：全选它们时批量导出不能亮。"""
    window = _shown_window(qtbot, records)
    window.select_rows([1])  # THUMBNAIL 记录

    assert window.batch_export_button.isEnabled() is False
    assert window.batch_export_recycle_button.isEnabled() is False

    # 混选（图片 + 缩略图）时按钮亮，但缩略图部分会被如实计入"跳过"。
    window.select_rows([0, 1])
    assert window.batch_export_button.isEnabled() is True


def _png_frame_bytes(color: str = "red") -> bytes:
    """Tiny in-memory PNG for synthetic clip frames (no disk involved)."""
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QPixmap

    pixmap = QPixmap(48, 32)
    pixmap.fill(color)
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    pixmap.save(buffer, "PNG")
    return bytes(buffer.data())


def _video_window(qtbot, records) -> RealReadOnlyWindow:
    """A connected window whose fake facade serves a 3-frame clip for videos."""
    window = _shown_window(qtbot, records)
    window._facade.clip_frames = [
        _png_frame_bytes("red"),
        _png_frame_bytes("green"),
        _png_frame_bytes("blue"),
    ]
    window._facade.clip_fps = 8.0
    return window


def test_video_selection_arms_the_preview_button(qtbot, records):
    window = _shown_window(qtbot, records)
    window.select_row(2)  # video record

    assert window.preview_button.isEnabled() is True
    assert window.preview_button.text() == "预览视频"
    assert "播放" in window.preview_status.text()
    assert window.clip_play_button.isVisible() is True
    assert window.clip_play_button.text() == "播放视频"


def test_video_cover_loads_automatically_on_selection(qtbot, records, tmp_path):
    from PySide6.QtGui import QPixmap

    preview_file = tmp_path / "cover.png"
    cover = QPixmap(64, 40)
    cover.fill()
    assert cover.save(str(preview_file), "PNG")
    window = _shown_window(qtbot, records)
    window._facade.video_preview_ok = True
    window._facade._preview_image_path = str(preview_file)
    window.select_row(2)  # video record

    qtbot.waitUntil(lambda: window.preview_label.source() is not None, timeout=5000)

    assert "视频封面" in window.preview_status.text()


def test_video_cover_failure_reports_and_claims_the_id(qtbot, records):
    window = _shown_window(qtbot, records)
    window.select_row(2)  # video record; the fake facade rejects video covers

    qtbot.waitUntil(lambda: "视频封面失败" in window.preview_status.text(), timeout=5000)

    selected = window.selected_record
    assert selected is not None
    # Claimed even on failure: a missing cover must not retry on every event.
    assert window._last_previewed_id == str(selected.media_id)


def test_video_preview_loads_clip_and_plays(qtbot, records):
    window = _video_window(qtbot, records)
    window.select_row(2)  # video record

    outcome = window.request_preview()

    assert outcome.ok is True
    assert len(window._clip_frames) == 3
    assert window.clip_play_button.isVisible() is True
    assert window.clip_play_button.text() == "暂停"
    # The running timer keeps rewriting the position line, so only the stable
    # parts are asserted: it is a video preview over 3 frames.
    assert "视频预览" in window.preview_status.text()
    assert "/3" in window.preview_status.text()

    window.toggle_clip_playback()
    assert window.clip_play_button.text() == "播放"
    assert window._clip_playing is False

    window.toggle_clip_playback()
    first = window._clip_index
    window._advance_clip()
    assert window._clip_index == (first + 1) % 3


def test_leaving_the_video_stops_playback_and_hides_transport(qtbot, records):
    window = _video_window(qtbot, records)
    window.select_row(2)
    assert window.request_preview().ok is True
    assert window._clip_playing is True

    window.select_row(0)  # back to a picture

    assert window._clip_playing is False
    assert window._clip_frames == []
    assert window.clip_play_button.isVisible() is False


def test_stale_preview_is_cleared_when_selection_is_not_previewable(qtbot, records, tmp_path):
    """选到不可预览的文件时，上一张图的残留必须换掉，不能一直留着。”"""
    from PySide6.QtGui import QPixmap

    preview_file = tmp_path / "preview.png"
    shown = QPixmap(48, 32)
    shown.fill()
    assert shown.save(str(preview_file), "PNG")
    window = _shown_window(qtbot, records)
    window._facade._preview_image_path = str(preview_file)
    window.select_row(0)
    assert window.request_preview().ok is True
    assert window.preview_label.source() is not None

    voice = _record(MediaType.VOICE, 24 * 1024, "note.silk")
    window._grid_model.set_records((voice,))
    window.select_row(0)

    assert window.preview_label.source() is None
    assert "暂不支持预览" in window.preview_label.text()


def test_multi_selection_clears_the_single_picture_pane(qtbot, records, tmp_path):
    from PySide6.QtGui import QPixmap

    preview_file = tmp_path / "preview.png"
    shown = QPixmap(48, 32)
    shown.fill()
    assert shown.save(str(preview_file), "PNG")
    window = _shown_window(qtbot, records)
    window._facade._preview_image_path = str(preview_file)
    window.select_row(0)
    assert window.request_preview().ok is True

    window.select_rows([0, 1])

    assert window.preview_label.source() is None
    assert "一次只显示一张" in window.preview_label.text()


def test_double_click_on_video_previews_in_app(qtbot, records, monkeypatch):
    window = _shown_window(qtbot, records)
    previews: list = []
    monkeypatch.setattr(window, "request_preview", lambda: previews.append(True))

    window._on_grid_double_clicked(window._grid_model.index(2, 0))  # video

    assert previews, "双击视频应在应用内预览，而不是调外部播放器"


def test_grid_context_menu_offers_preview_and_recycle(qtbot, records, monkeypatch):
    captured: dict = {}
    from PySide6.QtCore import QObject

    class _FakeMenu(QObject):
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.actions: list = []

        def addAction(self, action):
            self.actions.append(action)
            return action

        def addSeparator(self):
            pass

        def exec(self, *args, **kwargs):
            captured["actions"] = [action.text() for action in self.actions]

    monkeypatch.setattr(real_window_module, "QMenu", _FakeMenu)
    window = _shown_window(qtbot, records)
    window.select_row(2)  # video record

    from PySide6.QtCore import QPoint

    window._show_grid_context_menu(QPoint(10, 10))

    labels = captured["actions"]
    assert "预览视频" in labels
    assert "打开" in labels
    assert "打开所在文件夹" in labels
    assert "移到回收站…" in labels
    assert not any("删除" in label for label in labels), "菜单里只能进回收站，不能真删"


def test_selecting_a_file_does_not_poison_neighbour_covers(qtbot):
    """点一个文件，相邻文件的系统图标不许翻成"无法预览"。

    回归：邻域预热曾把不可解码的记录也送去解码，逐个失败打标，
    把整屏文件图标埋成"无法预览"。
    """
    from wechat_cleaner.gui.gallery import THUMB_NONE

    recs = tuple(_record(MediaType.FILE, 1024, f"doc_{index}.pdf") for index in range(6))
    window = _shown_window(qtbot, recs)
    window.select_row(2)
    qtbot.waitUntil(
        lambda: not window._warm_queue and window._warm_inflight == 0, timeout=5000
    )
    qtbot.wait(500)

    assert window._warm_failed == set()
    model = window._grid_model
    for row in range(model.rowCount()):
        assert model.data(model.index(row, 0), THUMB_STATE_ROLE) == THUMB_NONE


def test_refilter_heals_stale_failure_marks_on_files(qtbot):
    from wechat_cleaner.gui.gallery import THUMB_FAILED, THUMB_NONE

    recs = tuple(_record(MediaType.FILE, 1024, f"doc_{index}.pdf") for index in range(3))
    window = _shown_window(qtbot, recs)
    model = window._grid_model
    for record in recs:
        model.apply_thumbnail(str(record.media_id), None)
    assert model.data(model.index(0, 0), THUMB_STATE_ROLE) == THUMB_FAILED

    window.apply_filters()

    assert model.data(model.index(0, 0), THUMB_STATE_ROLE) == THUMB_NONE


def test_recycle_keeps_the_viewport_where_the_action_happened(qtbot, monkeypatch):
    """清理完不回顶部：视口锚定在原首行（被删则顺延同行号）。"""
    from PySide6.QtWidgets import QMessageBox

    window = _shown_window(qtbot, _many_records(300))
    grid = window.records_table
    grid.scrollTo(
        window._grid_model.index(100, 0), grid.ScrollHint.PositionAtTop
    )
    grid.doItemsLayout()
    # Settle the selection first: selecting itself may nudge the viewport
    # (EnsureVisible), which is unrelated to the recycle under test.
    for _ in range(2):
        window.select_row(grid.first_visible_row() + 2)
        grid.doItemsLayout()
    before = grid.first_visible_row()
    assert before > 0
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))

    outcome = window.recycle_selected()

    assert outcome is not None and outcome.ok is True
    grid.doItemsLayout()
    assert grid.first_visible_row() == before


def test_batch_recycle_reports_per_file_progress(qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    recs = (
        _record(MediaType.IMAGE, 1024, "a.dat", attach_dir="8a8b"),
        _record(MediaType.IMAGE, 2048, "b.dat", attach_dir="8a8b"),
        _record(MediaType.IMAGE, 4096, "c.dat", attach_dir="8a8b"),
    )
    window = _shown_window(qtbot, recs)
    window.select_rows([0, 1, 2])
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    seen: list = []
    original = window._progress_value
    window._progress_value = lambda percent: (seen.append(percent), original(percent))

    outcome = window.recycle_selected_batch()

    assert outcome is not None and outcome.ok is True
    assert seen == [33, 66, 100]
    assert "成功 3 项" in window.statusBar().currentMessage()


def test_batch_export_reports_per_file_progress(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    recs = (
        _record(MediaType.IMAGE, 1024, "a.dat", attach_dir="8a8b"),
        _record(MediaType.IMAGE, 2048, "b.dat", attach_dir="8a8b"),
    )
    window = _shown_window(qtbot, recs)
    window._facade.export_payload = b"original-bytes"
    window.select_rows([0, 1])
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory",
        staticmethod(lambda *a, **k: str(tmp_path)),
    )
    seen: list = []
    original = window._progress_value
    window._progress_value = lambda percent: (seen.append(percent), original(percent))

    outcome = window.export_selected_batch()

    assert outcome is not None and outcome.ok is True
    assert seen == [50, 100]


def _monthly_record(name: str = "report.pdf", size: int = 4096) -> MediaRecord:
    """A file stored by month: attributable to no conversation."""
    return MediaRecord(
        account_id="wxid_demo_alpha",
        file=FileIdentity(
            relative_path=f"msg/file/2026-02/{name}", byte_size=size,
            modified_time_ns=1,
        ),
        media_type=MediaType.FILE,
        observed_at=datetime(2026, 2, 1, tzinfo=UTC),
        mapping_confidence=MappingConfidence.UNMAPPED,
        mapping_reason="filesystem scan only",
    )


def test_unattributed_records_get_a_searchable_unified_entry(qtbot):
    from wechat_cleaner.gui.real_window import UNATTRIBUTED_SESSION

    chat = _record(MediaType.IMAGE, 1024, "a.dat", attach_dir="8a8b")
    stray = _monthly_record("big.pdf", size=10 * 1024 * 1024)
    facade = FakeRealSessionFacade(records=(chat, stray))
    facade.session_names = {"8a8b": "小张"}
    window = RealReadOnlyWindow(facade)
    qtbot.addWidget(window)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()

    assert window.session_combo.count() == 3
    assert window.session_combo.itemData(1) == UNATTRIBUTED_SESSION
    assert "未归属会话" in window.session_combo.itemText(1)
    assert "10.0 MB" in window.session_combo.itemText(1)

    # The pseudo-session filters exactly the unattributable records.
    window.session_combo.setCurrentIndex(window.session_combo.findData(UNATTRIBUTED_SESSION))
    assert window.apply_filters() == 1
    assert window.records_table.model().record_at(0) is stray
    assert "未归属会话" in window.statusBar().currentMessage()

    # And the single-selection line carries the same unified label.
    window.select_row(0)
    assert "「未归属会话」" in window.selection_label.text()


def test_filter_totals_equal_sessions_plus_unattributed(qtbot):
    """总量恒等于各会话之和加未归属：下拉数字永远对得上。"""
    from wechat_cleaner.real_db import session_dir_of

    chat = _record(MediaType.IMAGE, 1024, "a.dat", attach_dir="8a8b")
    chat2 = _record(MediaType.IMAGE, 2048, "b.dat", attach_dir="99cc")
    stray = _monthly_record("big.pdf", size=4096)
    window = _shown_window(qtbot, (chat, chat2, stray))

    shown = window._grid_model.records()
    attached = sum(1 for record in shown if session_dir_of(record.file.relative_path))
    unattributed = sum(1 for record in shown if not session_dir_of(record.file.relative_path))

    assert (attached, unattributed, len(shown)) == (2, 1, 3)
    combo = window.session_combo
    assert any(combo.itemData(row) == "__unattributed__" for row in range(combo.count()))
    assert "未归属会话" in window.filter_summary_label.text()


def test_file_records_never_get_queued_for_decoding(qtbot, records):
    """文件/语音不可解码：可见区预热必须跳过它们，不让占位符空转。"""
    from wechat_cleaner.gui.gallery import THUMB_NONE

    window = _shown_window(qtbot, records)
    model = window._grid_model
    file_record = _record(MediaType.FILE, 8 * 1024, "doc.dat", attach_dir="8a8b")
    model.set_records((file_record,))
    window._warmed.clear()
    window._warm_failed.clear()
    window._reset_warm_queue()
    window._warm_visible_cells()

    assert model.data(model.index(0, 0), THUMB_STATE_ROLE) == THUMB_NONE
    assert len(window._warm_queue) == 0, "不可解码的记录不应进入解码队列"


# ----------------------------------------------------------------------
# 筛选统计：当前条件下的总量 + 按会话占用从大到小
# ----------------------------------------------------------------------
def _sized_session_records() -> tuple[MediaRecord, ...]:
    """Five tiny files in one chat vs one big photo in another.

    Count order and byte order disagree on purpose: the dropdown must follow
    bytes (the cleanup-relevant order), not counts.
    """
    small = tuple(
        _record(MediaType.IMAGE, 100, f"tiny_{index}.dat", attach_dir="aa11")
        for index in range(5)
    )
    big = (_record(MediaType.IMAGE, 10 * 1024 * 1024, "big.dat", attach_dir="bb22"),)
    return small + big


def test_session_dropdown_sorts_by_filtered_bytes_and_shows_sizes(qtbot):
    recs = _sized_session_records()
    facade = FakeRealSessionFacade(records=recs)
    facade.session_names = {"aa11": "碎图群", "bb22": "大图联系人"}
    window = RealReadOnlyWindow(facade)
    qtbot.addWidget(window)
    window.set_data_root("C:\\synthetic\\xwechat_files")
    window.detect_accounts()
    window.connect_session()

    assert window.session_combo.count() == 3
    # Bytes first: the single 10 MB photo outranks five 100 B thumbnails.
    assert window.session_combo.itemData(1) == "bb22"
    assert window.session_combo.itemData(2) == "aa11"
    assert "共" in window.session_combo.itemText(1)
    assert "10.0 MB" in window.session_combo.itemText(1)
    assert "大图联系人" in window.session_combo.itemText(1)
    # The "all sessions" entry carries the filter total.
    assert "全部会话" in window.session_combo.itemText(0)
    assert "6 项" in window.session_combo.itemText(0)
    assert "10.0 MB" in window.session_combo.itemText(0)
    # The summary line and the status bar report the same total.
    assert "当前筛选" in window.filter_summary_label.text()
    assert "6 条" in window.filter_summary_label.text()
    assert "10.0 MB" in window.filter_summary_label.text()
    assert "大图联系人" in window.filter_summary_label.text()
    assert "共 10.0 MB" in window.statusBar().currentMessage()


def test_session_choice_survives_refilter_and_falls_back_when_empty(qtbot):
    image = _record(MediaType.IMAGE, 1024, "a.dat", attach_dir="8a8b")
    video = _record(MediaType.VIDEO, 2048, "b.mp4", attach_dir="99cc")
    window = _shown_window(qtbot, (image, video))

    window.session_combo.setCurrentIndex(window.session_combo.findData("8a8b"))
    window.apply_filters()
    assert window.session_combo.currentData() == "8a8b"

    # Narrow to videos: the 8a8b chat has no match and the dropdown falls
    # back to all sessions instead of pointing at an empty choice.
    window.type_combo.setCurrentIndex(window.type_combo.findData("video"))
    assert window.apply_filters() == 1
    assert window.session_combo.currentData() == ""
    assert window.session_combo.count() == 2  # 全部会话 + 99cc


def test_min_size_filter_accepts_fifty_kilobyte_cutoffs(qtbot):
    """最小值支持两位小数 MB：5 万 B（约 0.05 MB）这类阈值可表达。"""
    small = _record(MediaType.IMAGE, 40 * 1024, "small.dat")
    big = _record(MediaType.IMAGE, 60 * 1024, "big.dat")
    window = _shown_window(qtbot, (small, big))

    window.min_mb_spin.setValue(0.05)

    assert window.apply_filters() == 1
    assert window.records_table.model().record_at(0) is not None
    assert window.records_table.model().record_at(0).file.byte_size == 60 * 1024


# ----------------------------------------------------------------------
# 右键/双击：图片预览，普通文件直接打开，删除只进回收站
# ----------------------------------------------------------------------
def test_double_click_previews_pictures_and_opens_plain_files(qtbot, records, monkeypatch):
    window = _shown_window(qtbot, records)
    previews: list = []
    opened: list = []
    monkeypatch.setattr(window, "request_preview", lambda: previews.append(True))
    monkeypatch.setattr(window, "open_source_file", lambda record=None: opened.append(record))

    window._on_grid_double_clicked(window._grid_model.index(0, 0))  # IMAGE

    assert previews and not opened

    previews.clear()
    file_record = _record(MediaType.FILE, 8 * 1024, "doc.pdf", attach_dir="8a8b")
    window._grid_model.set_records((file_record,))
    window._on_grid_double_clicked(window._grid_model.index(0, 0))

    assert opened and not previews
    assert opened[0] is file_record


def test_open_plain_file_uses_the_os_default_app(qtbot, records, monkeypatch):
    window = _shown_window(qtbot, records)
    file_record = _record(MediaType.FILE, 8 * 1024, "doc.pdf", attach_dir="8a8b")
    window._grid_model.set_records((file_record,))
    window.select_row(0)
    launched: list = []
    monkeypatch.setattr(
        real_window_module, "_open_local_path", lambda path: launched.append(path) or True
    )

    assert window.open_source_file() is True

    assert launched == [window._facade.source_path(file_record)]
    # The status line names the relative path only — never the absolute one.
    assert file_record.file.relative_path in window.statusBar().currentMessage()
    assert launched[0] not in window.statusBar().currentMessage()


def test_reveal_in_folder_highlights_the_source_file(qtbot, records, monkeypatch):
    window = _shown_window(qtbot, records)
    window.select_row(0)
    revealed: list = []
    monkeypatch.setattr(
        real_window_module, "_reveal_local_path", lambda path: revealed.append(path) or True
    )

    assert window.reveal_selected_in_folder() is True

    assert revealed == [window._facade.source_path(window.selected_record)]


def test_open_picture_opens_its_decrypted_preview(qtbot, records, monkeypatch):
    window = _shown_window(qtbot, records)
    window.select_row(0)  # IMAGE record
    launched: list = []
    monkeypatch.setattr(
        real_window_module, "_open_local_path", lambda path: launched.append(path) or True
    )

    assert window.open_source_file() is True

    # The encrypted .dat itself is never handed to another program; the
    # decrypted preview product is what opens.
    assert len(launched) == 1
    assert launched[0].endswith(".png")
    assert "已用系统应用打开" in window.statusBar().currentMessage()


# ----------------------------------------------------------------------
# window geometry: it must be resizable and never clip its own controls
# ----------------------------------------------------------------------
def _scroll_ancestor(widget):
    node = widget.parentWidget()
    while node is not None:
        if node.__class__.__name__ == "QScrollArea":
            return node
        node = node.parentWidget()
    return None


def test_window_can_be_made_short_and_small(qtbot, records):
    """竖向必须能缩：窗口最小高度不能再被内容撑到屏幕装不下。

    回归：预览列的按钮栈 + 320px 预览图曾把最小高度顶到 1129px（> 屏幕可用
    1019px），于是竖向根本拉不动，最大化还会把底部按钮切掉。
    """
    window = _window(qtbot, records)
    window.show()
    qtbot.addWidget(window)

    minimum = window.minimumSizeHint().height()
    assert minimum <= 680, f"窗口最小高度仍然过高：{minimum}px"

    for size in ((1366, 768), (1280, 720), (1024, 640)):
        window.resize(*size)
        qtbot.wait(10)
        assert (window.width(), window.height()) == size, (
            f"请求 {size} 却得到 {window.width()}x{window.height()}：竖向被最小高度卡住"
        )


def test_every_action_button_is_reachable_at_small_sizes(qtbot, records):
    """任何窗口尺寸下，导出/回收站这些按钮都不能"找不到"。"""
    window = _window(qtbot, records)
    window.show()
    qtbot.addWidget(window)

    buttons = [
        window.preview_button,
        window.recycle_button,
        window.export_button,
        window.export_recycle_button,
        window.batch_export_button,
        window.batch_recycle_button,
        window.batch_export_recycle_button,
    ]
    for size in ((1366, 768), (1024, 640)):
        window.resize(*size)
        qtbot.wait(10)
        central = window.centralWidget()
        for button in buttons:
            top = button.mapTo(central, button.rect().topLeft())
            bottom = top.y() + button.height()
            if -1 <= top.y() and bottom <= central.height() + 1:
                continue
            area = _scroll_ancestor(button)
            assert area is not None, (
                f"{button.text()!r} 在 {size} 下既不在可见区域、也没有可滚动的父容器"
            )
            assert area.verticalScrollBar().maximum() > 0, (
                f"{button.text()!r} 在 {size} 下被裁掉且无法滚动到"
            )


def test_preview_pane_can_shrink(qtbot, records):
    """预览图面板的最小高度要保持小巧，否则又会把窗口顶高。"""
    window = _window(qtbot, records)

    assert window.preview_label.minimumHeight() <= 200
    assert window.preview_label.minimumWidth() <= 280


def test_preview_column_scrolls_instead_of_clipping(qtbot, records):
    """预览列自带滚动容器：小窗口下按钮靠滚动可达，而不是消失在窗口外。"""
    window = _window(qtbot, records)
    window.show()
    qtbot.addWidget(window)

    area = _scroll_ancestor(window.batch_export_recycle_button)

    assert area is not None, "预览列应放在滚动容器里"
    assert area.widgetResizable() is True
    window.resize(1024, 640)
    qtbot.wait(10)
    assert area.verticalScrollBar().maximum() > 0


def test_window_title_carries_the_release_version(qtbot, records):
    """标题栏要能看出跑的是哪个版本（用户报 bug 时就靠这个对齐）。"""
    from wechat_cleaner.gui.disclaimer import APP_VERSION

    window = _window(qtbot, records)

    assert APP_VERSION.startswith("v")
    assert APP_VERSION in window.windowTitle()
