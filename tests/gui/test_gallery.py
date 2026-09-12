"""Offscreen tests for the album-style thumbnail grid.

The grid is a model/view pair, so these tests run without the window: they
assert the model's data roles, the asynchronous thumbnail hand-off, and the
view's visible-range bookkeeping that drives lazy decoding.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QColor, QPixmap, QWheelEvent
from PySide6.QtWidgets import QStyleOptionViewItem

from wechat_cleaner.domain.contracts import (
    FileIdentity,
    MappingConfidence,
    MediaRecord,
    MediaType,
)
from wechat_cleaner.gui import gallery
from wechat_cleaner.gui.gallery import (
    _CELL_PADDING,
    _CELL_SIZE,
    _THUMB_BOX,
    THUMB_DECODE_EDGE,
    THUMB_FAILED,
    THUMB_NONE,
    THUMB_PENDING,
    THUMB_READY,
    THUMB_STATE_ROLE,
    MediaCellDelegate,
    MediaGalleryView,
    MediaGridModel,
    format_caption,
    thumbnail_target_rect,
)


def _record(
    media_type: MediaType = MediaType.IMAGE,
    size: int = 1024,
    name: str = "photo.dat",
    day: int = 1,
) -> MediaRecord:
    return MediaRecord(
        account_id="wxid_demo_alpha",
        file=FileIdentity(
            relative_path=f"msg/attach/8a8b/2026-02/Img/{name}",
            byte_size=size,
            modified_time_ns=1,
        ),
        media_type=media_type,
        observed_at=datetime(2026, 2, day, tzinfo=UTC),
        mapping_confidence=MappingConfidence.UNMAPPED,
        mapping_reason="filesystem scan only",
    )


@pytest.fixture
def records() -> tuple[MediaRecord, ...]:
    return (
        _record(size=11 * 1024 * 1024, name=f"{uuid4().hex}.dat"),
        _record(MediaType.THUMBNAIL, 40 * 1024, f"{uuid4().hex}_t.dat"),
        _record(MediaType.VIDEO, 3 * 1024 * 1024, f"{uuid4().hex}.mp4"),
    )


def test_model_exposes_records_and_row_count(records):
    model = MediaGridModel()
    model.set_records(records)

    assert model.rowCount() == 3
    assert model.records() == records
    assert model.record_at(1) is records[1]
    assert model.record_at(9) is None


def test_model_reports_media_id_and_caption(records):
    model = MediaGridModel()
    model.set_records(records)

    index = model.index(0, 0)
    assert model.data(index, Qt.UserRole) == str(records[0].media_id)
    caption = model.data(index, Qt.DisplayRole)
    assert isinstance(caption, str) and caption
    assert "11.0 MB" in caption
    # Tooltips carry the full path for the cells that elide their label.
    assert records[0].file.relative_path in model.data(index, Qt.ToolTipRole)


def test_model_ignores_invalid_indexes(records):
    model = MediaGridModel()
    model.set_records(records)

    from PySide6.QtCore import QModelIndex

    assert model.data(QModelIndex(), Qt.DisplayRole) is None
    assert model.data(model.index(99, 0), Qt.DisplayRole) is None
    assert model.rowCount(model.index(0, 0)) == 0


def test_thumbnail_arrives_asynchronously_and_notifies(records, qtbot):
    model = MediaGridModel()
    model.set_records(records)
    media_id = str(records[0].media_id)

    # Until a decode lands, the cell has no decoration (the view paints its
    # own "loading" placeholder).
    assert model.has_thumbnail(media_id) is False
    assert model.data(model.index(0, 0), Qt.DecorationRole) is None

    decoded = QPixmap(32, 32)
    decoded.fill()
    changed: list = []
    model.dataChanged.connect(lambda *args: changed.append(args))

    model.apply_thumbnail(media_id, decoded)

    assert model.has_thumbnail(media_id) is True
    assert isinstance(model.data(model.index(0, 0), Qt.DecorationRole), QPixmap)
    assert changed, "the view must be told to repaint the cell"


def test_failed_decode_is_recorded_not_retried(records):
    model = MediaGridModel()
    model.set_records(records)
    media_id = str(records[2].media_id)

    model.apply_thumbnail(media_id, None)

    assert model.has_failed(media_id) is True
    assert model.has_thumbnail(media_id) is False


def test_row_lookup_by_media_id(records):
    model = MediaGridModel()
    model.set_records(records)

    assert model.row_of(str(records[1].media_id)) == 1
    assert model.index_of_record(records[1]) == 1
    assert model.row_of(str(uuid4())) == -1


def test_caption_labels_non_image_types():
    video = format_caption(_record(MediaType.VIDEO, name="clip.mp4"))
    voice = format_caption(_record(MediaType.VOICE, name="note.silk"))
    image = format_caption(_record(MediaType.IMAGE, name="pic.dat"))

    assert video.startswith("▶")
    assert voice.startswith("♪")
    assert not image.startswith(("▶", "♪"))


def test_caption_always_shows_the_year():
    """A multi-year library is exactly where "03-14" is useless."""
    caption = format_caption(_record(name="old_photo.dat", day=1))

    assert "2026-02-01" in caption
    # The date and the size share one line; both must survive elision tests.
    assert "KB" in caption or "MB" in caption or "B" in caption


def test_view_row_count_tracks_the_model(records, qtbot):
    view = MediaGalleryView()
    qtbot.addWidget(view)
    model = MediaGridModel()
    view.setModel(model)
    assert view.rowCount() == 0

    model.set_records(records)

    assert view.rowCount() == 3


def test_view_is_in_icon_mode_with_multi_selection(qtbot):
    view = MediaGalleryView()
    qtbot.addWidget(view)

    assert view.viewMode() == MediaGalleryView.IconMode
    assert view.selectionMode() == MediaGalleryView.ExtendedSelection
    assert view.decode_edge() > 0


def test_visible_range_is_empty_without_records(qtbot):
    view = MediaGalleryView()
    qtbot.addWidget(view)
    view.setModel(MediaGridModel())

    assert view.visible_range() == (0, -1)


def test_visible_range_prefetches_beyond_the_viewport(records, qtbot):
    view = MediaGalleryView()
    qtbot.addWidget(view)
    model = MediaGridModel()
    view.setModel(model)
    model.set_records(records)
    view.resize(300, 200)
    view.show()
    qtbot.waitExposed(view)

    first, last = view.visible_range()

    # A small viewport still queues the whole (tiny) set because the prefetch
    # buffer reaches past the visible cells.
    assert first == 0
    assert last == len(records) - 1


def test_visible_range_change_emits_signal(records, qtbot):
    view = MediaGalleryView()
    qtbot.addWidget(view)
    model = MediaGridModel()
    view.setModel(model)
    ranges: list = []
    view.visible_range_changed.connect(lambda first, last: ranges.append((first, last)))

    model.set_records(records)
    view.resize(800, 600)
    view.show()
    qtbot.waitExposed(view)
    assert ranges, "painting a populated grid reports its visible range"


# ----------------------------------------------------------------------
# fixed-frame geometry: every picture lands in the same square
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "size",
    [
        QSize(256, 256),  # square
        QSize(1000, 40),  # extreme panorama
        QSize(40, 1000),  # extreme portrait
        QSize(24, 24),  # tiny: must be scaled up, not left as a hole
        QSize(1, 1),  # degenerate but legal
    ],
)
def test_thumbnail_target_rect_letterboxes_inside_the_frame(size: QSize):
    box = QRect(0, 0, _THUMB_BOX, _THUMB_BOX)

    target = thumbnail_target_rect(box, size)

    # Never outside the frame, and it fills one dimension exactly so the sheet
    # looks uniform instead of leaving ragged gaps.
    assert box.contains(target)
    assert target.width() == _THUMB_BOX or target.height() == _THUMB_BOX
    # Centred on both axes.
    assert target.center().x() == box.center().x()
    assert target.center().y() == box.center().y()


def test_thumbnail_target_rect_rejects_an_empty_picture():
    box = QRect(0, 0, _THUMB_BOX, _THUMB_BOX)

    assert thumbnail_target_rect(box, QSize(0, 0)) == box


def test_cell_state_role_separates_pending_ready_and_failed(records):
    model = MediaGridModel()
    model.set_records(records)
    ready_id = str(records[0].media_id)
    failed_id = str(records[2].media_id)

    assert model.data(model.index(0, 0), THUMB_STATE_ROLE) == THUMB_PENDING
    decoded = QPixmap(32, 32)
    decoded.fill()
    model.apply_thumbnail(ready_id, decoded)
    model.apply_thumbnail(failed_id, None)

    assert model.data(model.index(0, 0), THUMB_STATE_ROLE) == THUMB_READY
    assert model.data(model.index(2, 0), THUMB_STATE_ROLE) == THUMB_FAILED
    # A failed cell must also repaint, or it keeps saying "loading".
    assert model.data(model.index(2, 0), Qt.DecorationRole) is None


def test_files_and_voice_notes_say_no_preview_not_loading():
    """文件/语音没有图片可解——它们必须显示静态"无预览"，不是永远的"载入中"。"""
    records = (
        _record(MediaType.FILE, 40 * 1024, f"{uuid4().hex}.pdf"),
        _record(MediaType.VOICE, 24 * 1024, f"{uuid4().hex}.silk"),
    )
    model = MediaGridModel()
    model.set_records(records)

    for row in (0, 1):
        assert model.data(model.index(row, 0), THUMB_STATE_ROLE) == THUMB_NONE
        assert gallery.decode_supported(records[row].media_type.value) is False


def _file_record(name: str, media_type: MediaType = MediaType.FILE) -> MediaRecord:
    """A document-style record under the merged-forward Rec layout."""
    return MediaRecord(
        account_id="wxid_demo_alpha",
        file=FileIdentity(
            relative_path=f"msg/attach/8a8b/2026-02/Rec/77aa/F/3/{name}",
            byte_size=4096,
            modified_time_ns=1,
        ),
        media_type=media_type,
        observed_at=datetime(2026, 2, 1, tzinfo=UTC),
        mapping_confidence=MappingConfidence.UNMAPPED,
        mapping_reason="filesystem scan only",
    )


@pytest.mark.parametrize(
    ("name", "media_type", "kind"),
    [
        ("backup.zip", MediaType.FILE, "archive"),
        ("backup.rar", MediaType.FILE, "archive"),
        ("backup.7z", MediaType.FILE, "archive"),
        ("report.pdf", MediaType.FILE, "pdf"),
        ("notes.docx", MediaType.FILE, "document"),
        ("budget.xlsx", MediaType.FILE, "spreadsheet"),
        ("deck.pptx", MediaType.FILE, "presentation"),
        ("readme.txt", MediaType.FILE, "text"),
        ("song.mp3", MediaType.FILE, "voice"),
        ("clip.mp4", MediaType.FILE, "video"),
        ("mystery", MediaType.FILE, "file"),
        ("mystery", MediaType.OTHER, "folder"),
        ("pic.dat", MediaType.IMAGE, "image"),
        ("pic_t.dat", MediaType.THUMBNAIL, "thumbnail"),
    ],
)
def test_file_kind_classifies_covers_by_type_and_extension(name, media_type, kind):
    assert gallery.file_kind(_file_record(name, media_type)) == kind


@pytest.mark.parametrize(
    ("name", "media_type", "label"),
    [
        ("backup.zip", MediaType.FILE, "ZIP"),
        ("report.pdf", MediaType.FILE, "PDF"),
        ("notes.docx", MediaType.FILE, "DOC"),
        ("budget.xlsx", MediaType.FILE, "XLS"),
        ("deck.pptx", MediaType.FILE, "PPT"),
        ("readme.txt", MediaType.FILE, "TXT"),
        ("clip.mp4", MediaType.FILE, "MP4"),
        ("song.mp3", MediaType.FILE, "MP3"),
        ("mystery", MediaType.OTHER, "文件夹"),
        ("pic.dat", MediaType.IMAGE, ""),
    ],
)
def test_cover_label_names_each_file_family(name, media_type, label):
    assert gallery.cover_label_for(_file_record(name, media_type)) == label


def test_model_serves_cover_labels_to_the_delegate(qtbot):
    model = MediaGridModel()
    zipped = _file_record("backup.zip")
    photo = _record(MediaType.IMAGE)
    model.set_records((zipped, photo))

    assert model.data(model.index(0, 0), gallery.COVER_LABEL_ROLE) == "ZIP"
    assert model.data(model.index(1, 0), gallery.COVER_LABEL_ROLE) == ""


@pytest.mark.parametrize(
    ("name", "media_type", "ext"),
    [
        ("report.PDF", MediaType.FILE, ".pdf"),
        ("notes.docx", MediaType.FILE, ".docx"),
        ("mystery", MediaType.FILE, ""),
        ("mystery", MediaType.OTHER, ""),
        ("clip.mp4", MediaType.VIDEO, ".mp4"),
    ],
)
def test_cover_extension_is_lower_cased_with_dot(name, media_type, ext):
    assert gallery.cover_extension(_file_record(name, media_type)) == ext


def test_clear_unsupported_failures_heals_files_but_keeps_video_failures(qtbot):
    voice = _file_record("note.mp3", MediaType.VOICE)
    video = _record(MediaType.VIDEO, 1024 * 1024, f"{uuid4().hex}.mp4")
    model = MediaGridModel()
    model.set_records((voice, video))
    model.apply_thumbnail(str(voice.media_id), None)
    model.apply_thumbnail(str(video.media_id), None)
    assert model.data(model.index(0, 0), THUMB_STATE_ROLE) == THUMB_FAILED
    assert model.data(model.index(1, 0), THUMB_STATE_ROLE) == THUMB_FAILED

    model.clear_unsupported_failures()

    assert model.data(model.index(0, 0), THUMB_STATE_ROLE) == THUMB_NONE
    assert model.data(model.index(1, 0), THUMB_STATE_ROLE) == THUMB_FAILED


def test_model_serves_cover_extensions_to_the_delegate(qtbot):
    model = MediaGridModel()
    model.set_records((_file_record("report.pdf"),))

    assert model.data(model.index(0, 0), gallery.COVER_EXT_ROLE) == ".pdf"


def test_cover_icon_prefers_os_icons_and_falls_back_safely(qtbot):
    """The folder icon exists on every platform; anything else is either a
    real OS icon or None (offscreen has no shell) — never a crash."""
    from PySide6.QtGui import QIcon

    folder = gallery.cover_icon(MediaType.OTHER.value, "")
    assert isinstance(folder, QIcon) and not folder.isNull()

    assert gallery.cover_icon(MediaType.IMAGE.value, ".dat") is None

    pdf = gallery.cover_icon(MediaType.FILE.value, ".pdf")
    assert pdf is None or not pdf.isNull()


def test_video_cells_count_as_decodable_and_fail_explicitly():
    model = MediaGridModel()
    model.set_records((_record(MediaType.VIDEO, 1024 * 1024, f"{uuid4().hex}.mp4"),))
    media_id = str(model.records()[0].media_id)

    assert gallery.decode_supported(MediaType.VIDEO.value) is True
    assert model.data(model.index(0, 0), THUMB_STATE_ROLE) == THUMB_PENDING
    model.apply_thumbnail(media_id, None)
    assert model.data(model.index(0, 0), THUMB_STATE_ROLE) == THUMB_FAILED


def test_delegate_always_asks_for_the_same_cell_size(records, qtbot):
    view = MediaGalleryView()
    qtbot.addWidget(view)
    model = MediaGridModel()
    view.setModel(model)
    model.set_records(records)

    delegate = view.itemDelegate()
    assert isinstance(delegate, MediaCellDelegate)
    option = QStyleOptionViewItem()
    hints = {
        delegate.sizeHint(option, model.index(row, 0))
        for row in range(model.rowCount())
    }
    assert hints == {_CELL_SIZE}


def test_picture_never_paints_into_the_caption_strip(qtbot):
    """The whole point of the fixed frame: a picture cannot cover its caption."""
    model = MediaGridModel()
    record = _record(name="wide_photo.dat")
    model.set_records((record,))
    picture = QPixmap(300, 30)
    picture.fill(QColor("red"))
    model.apply_thumbnail(str(record.media_id), picture)

    view = MediaGalleryView()
    qtbot.addWidget(view)
    view.setModel(model)
    view.resize(240, 240)
    view.show()
    qtbot.waitExposed(view)

    shot = view.grab()
    ratio = shot.devicePixelRatio() or 1.0
    image = shot.toImage()
    red = QColor("red")
    strip_top = int((_CELL_PADDING + _THUMB_BOX) * ratio)
    offenders = 0
    for y in range(strip_top, image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            if pixel.red() > 180 and pixel.green() < 80 and pixel.blue() < 80:
                offenders += 1
    assert red.isValid()
    assert offenders == 0, "the picture bled into the caption strip"


def test_model_reset_invalidates_the_cached_visible_range(records, qtbot):
    view = MediaGalleryView()
    qtbot.addWidget(view)
    model = MediaGridModel()
    view.setModel(model)
    view.resize(400, 300)
    view.show()
    qtbot.waitExposed(view)

    ranges: list = []
    view.visible_range_changed.connect(lambda first, last: ranges.append((first, last)))
    model.set_records(records)
    ranges.clear()

    # Re-applying identical records must still be able to re-report the range:
    # without the invalidation a filter that keeps the same numbers would look
    # like "nothing changed" and the new cells would never be queued.
    view.invalidate_visible_range()

    assert ranges, "an invalidated range must be re-reported"


# ----------------------------------------------------------------------
# Ctrl+wheel zoom
# ----------------------------------------------------------------------
def test_decode_edge_is_bucketed_by_the_frame_size():
    # Three buckets, so nudging the wheel never writes a new cache file.  The
    # breakpoints leave headroom: a 256px decode is still sharp in a 160px box,
    # so a small zoom must not trigger a re-decode at all.
    assert gallery.decode_edge_for(88) == THUMB_DECODE_EDGE
    assert gallery.decode_edge_for(132) == THUMB_DECODE_EDGE
    assert gallery.decode_edge_for(160) == THUMB_DECODE_EDGE
    assert gallery.decode_edge_for(192) == 384
    assert gallery.decode_edge_for(232) == 384
    assert gallery.decode_edge_for(280) == 512
    assert gallery.decode_edge_for(336) == 512


def test_cell_size_grows_with_the_frame():
    small = gallery.cell_size_for(88)
    large = gallery.cell_size_for(232)

    assert large.width() - small.width() == 232 - 88
    assert large.height() - small.height() == 232 - 88


def test_zoom_steps_are_a_closed_ladder(qtbot, records):
    view = MediaGalleryView()
    qtbot.addWidget(view)
    model = MediaGridModel()
    view.setModel(model)
    model.set_records(records)

    # Walk down past the floor and up past the ceiling: the ladder clamps.
    while view.zoom_by(-1):
        pass
    assert view.thumb_box() == gallery._ZOOM_MIN
    assert view.zoom_by(-1) is False

    while view.zoom_by(1):
        pass
    assert view.thumb_box() == gallery._ZOOM_MAX
    assert view.zoom_by(1) is False


def test_zoom_resizes_grid_and_delegate_together(qtbot, records):
    view = MediaGalleryView()
    qtbot.addWidget(view)
    model = MediaGridModel()
    view.setModel(model)
    model.set_records(records)
    sizes: list = []
    view.thumb_box_changed.connect(sizes.append)

    assert view.set_thumb_box(232) is True

    assert view.thumb_box() == 232
    assert view.gridSize() == gallery.cell_size_for(232)
    assert view.iconSize() == QSize(232, 232)
    assert view.itemDelegate().box_edge() == 232
    # The grid geometry the view reports must match what the delegate paints.
    assert view.visualRect(model.index(0, 0)).size() == gallery.cell_size_for(232)
    assert sizes == [232]
    # Asking for the size it already has is a no-op, not a signal.
    assert view.set_thumb_box(232) is False
    assert sizes == [232]


def test_ctrl_wheel_zooms_and_plain_wheel_does_not(qtbot, records):
    view = MediaGalleryView()
    qtbot.addWidget(view)
    model = MediaGridModel()
    view.setModel(model)
    model.set_records(records)
    view.resize(400, 300)
    view.show()
    qtbot.waitExposed(view)

    def wheel(modifiers, delta):
        event = QWheelEvent(
            QPointF(20, 20),
            QPointF(20, 20),
            QPoint(0, 0),
            QPoint(0, delta),
            Qt.MouseButton.NoButton,
            modifiers,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        view.wheelEvent(event)

    before = view.thumb_box()
    wheel(Qt.KeyboardModifier.ControlModifier, 120)
    assert view.thumb_box() > before

    zoomed = view.thumb_box()
    wheel(Qt.KeyboardModifier.NoModifier, 120)
    assert view.thumb_box() == zoomed, "a plain wheel must scroll, not resize"

    wheel(Qt.KeyboardModifier.ControlModifier, -120)
    assert view.thumb_box() == before


def test_zooming_keeps_the_picture_in_place(qtbot):
    many = tuple(_record(name=f"pic_{index:03d}.dat") for index in range(400))
    view = MediaGalleryView()
    qtbot.addWidget(view)
    model = MediaGridModel()
    view.setModel(model)
    view.resize(700, 400)
    view.show()
    qtbot.waitExposed(view)
    model.set_records(many)
    view.doItemsLayout()
    view.scrollTo(model.index(40, 0), MediaGalleryView.ScrollHint.PositionAtTop)
    view.doItemsLayout()
    anchor = view.first_visible_row()
    assert anchor > 0, "the test needs a scrolled view to mean anything"

    view.set_thumb_box(232)

    landed = view.first_visible_row()
    # Zooming must not throw away the user's place: the row they were looking
    # at stays around the top instead of the view jumping back to the start.
    # (Qt's scrollTo aligns to the item rect, and the column count changes with
    # the tile size, so allow the small residual drift.)
    assert landed > 0
    assert abs(landed - anchor) <= 4
