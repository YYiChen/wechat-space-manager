"""Album-style thumbnail grid for the read-only window.

The table view could not express what a user actually wants from a photo
manager: *see everything at once, without clicking*.  This module provides the
pieces of that experience:

``MediaGridModel``
    a ``QAbstractListModel`` over the filtered records.  Qt's own item views
    are virtualized, so a model with 200k rows costs nothing until a row is
    scrolled into view - no artificial display cap is needed any more.

``MediaCellDelegate``
    paints one tile: a *fixed* square frame with the picture letterboxed inside
    it, plus a two-line caption that can never be overlapped.  Leaving this to
    ``QListView``'s default delegate made the tile geometry follow each image's
    aspect ratio - a square photo filled the whole cell over its own caption,
    while a 24px icon stayed 24px.

``MediaGalleryView``
    a ``QListView`` in ``IconMode``: uniform cells, multi-select, and a
    *lazy* thumbnail pipeline.  Decoding is requested only for the indices the
    user can actually see (plus a bounded buffer), so scrolling an enormous
    account decodes tens of images, not hundreds of thousands.

Threading contract
------------------
Decoding happens on the shared warm thread pool owned by the window; the pool
calls ``apply_thumbnail`` back on a worker thread, which stores the pixmap and
emits ``thumbnails_ready``.  The window connects that signal to a UI-thread
slot.  No Qt widget is touched off the main thread.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractListModel,
    QFileInfo,
    QModelIndex,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFileIconProvider,
    QListView,
    QStyle,
    QStyledItemDelegate,
)

from wechat_cleaner.domain.contracts import MediaRecord, MediaType

# Cell geometry.  Every cell is exactly this size and the picture is drawn
# inside a fixed square frame, so photos of any aspect ratio line up in a
# uniform sheet.  The caption strip is reserved space: a picture is never
# allowed to paint into it.  The frame edge is a *view* property (Ctrl+wheel
# changes it), so the numbers here are the defaults and the helpers below
# derive the rest.
_THUMB_BOX = 132
_CAPTION_HEIGHT = 34
_CELL_PADDING = 8
_CELL_SIZE = QSize(
    _THUMB_BOX + 2 * _CELL_PADDING, _CELL_PADDING + _THUMB_BOX + _CAPTION_HEIGHT
)

# Ctrl+wheel walks this ladder instead of using a continuous factor: discrete
# steps keep a row's worth of columns stable and make the decode resolution
# (which is bucketed, see ``decode_edge_for``) predictable.
_ZOOM_STEPS = (88, 112, 132, 160, 192, 232, 280, 336)
_ZOOM_MIN = _ZOOM_STEPS[0]
_ZOOM_MAX = _ZOOM_STEPS[-1]

# Decode resolution for grid thumbnails.  Slightly larger than the painted
# size so a device-pixel-ratio > 1 screen still looks crisp; the cache is keyed
# by this edge so grid and detail previews do not evict each other.
# Public because the window's decode workers read it off the UI thread.
THUMB_DECODE_EDGE = 256

# How many rows beyond the visible range are queued for decoding.  Two "screens"
# of slack makes fast scrolling feel already-warm without decoding the world.
_PREFETCH_BUFFER = 120

# Roles the delegate reads.  ``THUMB_STATE_ROLE`` lets a cell tell "still
# loading" apart from "has no preview", which the placeholder text reflects.
MEDIA_TYPE_ROLE = Qt.UserRole + 1
THUMB_STATE_ROLE = Qt.UserRole + 2
COVER_LABEL_ROLE = Qt.UserRole + 3
COVER_EXT_ROLE = Qt.UserRole + 4
THUMB_READY = "ready"
THUMB_PENDING = "pending"
THUMB_FAILED = "failed"
THUMB_NONE = "none"

_TYPE_GLYPHS = {
    MediaType.IMAGE.value: "",
    MediaType.THUMBNAIL.value: "",
    MediaType.VIDEO.value: "▶",
    MediaType.FILE.value: "▤",
    MediaType.VOICE.value: "♪",
}

# Per-file covers for records that never get pixels.  Pictures decode into real
# thumbnails; everything else gets a distinct text cover derived from its type
# and filename extension, so a ZIP, a PDF and a voice note no longer look like
# the same grey tile.  Labels are plain ASCII on purpose: they render in any
# system font, unlike emoji folder/archive pictographs.
_ARCHIVE_EXTS = frozenset(
    {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".cab", ".iso"}
)
_DOCUMENT_EXTS = frozenset({".doc", ".docx", ".wps", ".odt", ".rtf"})
_SPREADSHEET_EXTS = frozenset({".xls", ".xlsx", ".csv", ".numbers"})
_PRESENTATION_EXTS = frozenset({".ppt", ".pptx", ".key"})
_TEXT_EXTS = frozenset({".txt", ".md", ".log", ".json", ".xml", ".html", ".htm"})
_AUDIO_EXTS = frozenset(
    {".mp3", ".wav", ".amr", ".m4a", ".aac", ".ogg", ".flac", ".silk"}
)
_VIDEO_EXTS = frozenset(
    {".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".m4v", ".3gp"}
)


def _filename_of(record: MediaRecord) -> str:
    """The bare filename of a record, independent of path separators."""
    path = record.file.relative_path
    return path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def file_kind(record: MediaRecord) -> str:
    """Stable kind key for the tile cover of one record.

    Pictures and voice notes keep their media-type kind.  ``file``/``other``
    records are classified by filename extension; an extension-less ``other``
    record is most likely a directory entry or an unknown container and gets
    the ``folder`` cover instead of pretending to be a file.
    """
    media_type = record.media_type
    if media_type is MediaType.IMAGE:
        return "image"
    if media_type is MediaType.THUMBNAIL:
        return "thumbnail"
    if media_type is MediaType.VIDEO:
        return "video"
    if media_type is MediaType.VOICE:
        return "voice"
    name = _filename_of(record)
    suffix = f".{name.rpartition('.')[2].casefold()}" if "." in name else ""
    if not suffix:
        return "folder" if media_type is MediaType.OTHER else "file"
    if suffix in _ARCHIVE_EXTS:
        return "archive"
    if suffix == ".pdf":
        return "pdf"
    if suffix in _DOCUMENT_EXTS:
        return "document"
    if suffix in _SPREADSHEET_EXTS:
        return "spreadsheet"
    if suffix in _PRESENTATION_EXTS:
        return "presentation"
    if suffix in _TEXT_EXTS:
        return "text"
    if suffix in _AUDIO_EXTS:
        return "voice"
    if suffix in _VIDEO_EXTS:
        return "video"
    return "file"


def cover_label_for(record: MediaRecord) -> str:
    """The big centred cover text for a record that shows no picture.

    Pictures return ``""`` (they show decoded pixels instead).  Archives and
    office documents show their family (``ZIP``/``PDF``/``DOC``/…), audio and
    video show their container (``MP3``/``MP4``/…), and extension-less ``other``
    records say ``文件夹``.  Plain files with an unrecognised extension fall
    back to the upper-cased extension itself (``EPUB``), or ``文件`` when there
    is none.
    """
    kind = file_kind(record)
    if kind in ("image", "thumbnail"):
        return ""
    if kind == "folder":
        return "文件夹"
    if kind == "archive":
        return _extension_tag(record, default="ZIP")
    if kind == "pdf":
        return "PDF"
    if kind == "document":
        return "DOC"
    if kind == "spreadsheet":
        return "XLS"
    if kind == "presentation":
        return "PPT"
    if kind == "text":
        return "TXT"
    if kind in ("video", "voice"):
        return _extension_tag(record, default="▶" if kind == "video" else "♪")
    return _extension_tag(record, default="文件")


def _extension_tag(record: MediaRecord, *, default: str) -> str:
    """Upper-cased filename extension without the dot, or ``default``."""
    name = _filename_of(record)
    if "." not in name:
        return default
    tag = name.rpartition(".")[2].strip().upper()
    if not tag or len(tag) > 5:
        return default
    return tag


def cover_extension(record: MediaRecord) -> str:
    """Lower-cased filename extension with the dot (``".pdf"``), or ``""``."""
    name = _filename_of(record)
    if "." not in name:
        return ""
    ext = f".{name.rpartition('.')[2].strip().casefold()}"
    return ext if len(ext) <= 6 else ""


# Fallback extensions when a kind somehow has no filename suffix: the icon is
# still drawn from the right family instead of a generic blank page.
_KIND_FALLBACK_EXT = {
    "archive": ".zip",
    "pdf": ".pdf",
    "document": ".docx",
    "spreadsheet": ".xlsx",
    "presentation": ".pptx",
    "text": ".txt",
    "video": ".mp4",
    "voice": ".mp3",
    "file": ".bin",
}

_icon_provider: QFileIconProvider | None = None
_cover_icon_cache: dict[str, QIcon] = {}


def cover_icon(media_type_value: str | None, ext: str) -> QIcon | None:
    """The OS file-type icon for a non-picture record (folder/PDF/Word/…).

    This is the same icon Explorer shows: a real folder, the Adobe PDF mark,
    the Word "W", and so on — resolved from the filename extension through
    ``QFileIconProvider``, not drawn by hand.  Pictures return ``None`` (they
    show decoded pixels instead).  Returns ``None`` when no application
    instance exists yet or the platform supplies no icon; callers fall back
    to the text cover label in that case.
    """
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        return None
    if media_type_value in (MediaType.IMAGE.value, MediaType.THUMBNAIL.value):
        return None
    kind_hint = (ext or "").casefold()
    if kind_hint:
        key = kind_hint
    elif media_type_value == MediaType.OTHER.value:
        key = "folder"
    elif media_type_value == MediaType.VIDEO.value:
        key = _KIND_FALLBACK_EXT["video"]
    elif media_type_value == MediaType.VOICE.value:
        key = _KIND_FALLBACK_EXT["voice"]
    else:
        key = "file"
    cached = _cover_icon_cache.get(key)
    if cached is not None:
        return cached
    global _icon_provider
    if _icon_provider is None:
        _icon_provider = QFileIconProvider()
    if key == "folder":
        icon = _icon_provider.icon(QFileIconProvider.IconType.Folder)
    else:
        icon = _icon_provider.icon(QFileInfo(f"cover{key}"))
    if icon.isNull():
        # No shell integration on this platform (e.g. offscreen tests): the
        # caller falls back to the text cover label.  Not cached, so a later
        # call on a capable platform still resolves.
        return None
    _cover_icon_cache[key] = icon
    return icon

_PLACEHOLDER_TEXT = {
    THUMB_PENDING: "载入中…",
    THUMB_FAILED: "无法预览",
    THUMB_NONE: "无预览",
}

# What the album can actually put pixels on: photos decode from their
# containers, videos get a first-frame cover via the bundled ffmpeg.  Files and
# voice notes have no picture at all — they say so instead of spinning a
# "载入中…" that would never end.
_DECODABLE_TYPES = frozenset(
    {
        MediaType.IMAGE.value,
        MediaType.THUMBNAIL.value,
        MediaType.VIDEO.value,
    }
)


def decode_supported(media_type: str | None) -> bool:
    """True when this record type can ever get pixels on its tile."""
    return media_type in _DECODABLE_TYPES


def format_caption_parts(record: MediaRecord) -> tuple[str, str]:
    """``(title, subtitle)`` for one cell: what the item is, then size and date.

    The date carries the year: a library spanning several years is exactly the
    one where "03-14" is useless, and the whole point of the grid is deciding
    which old photos to clear out.  It also comes *first* on the line, so the
    right-elided version on a small tile still shows the whole date and drops
    the size instead.  Same time base as the filters and the tooltip (the
    record's UTC observation time).
    """
    glyph = _TYPE_GLYPHS.get(record.media_type.value, "")
    size = _human_bytes(record.file.byte_size)
    date = record.observed_at.strftime("%Y-%m-%d")
    name = record.file.relative_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = name.split(".")[0][:14]
    prefix = f"{glyph} " if glyph else ""
    return f"{prefix}{stem}", f"{date}｜{size}"


def format_caption(record: MediaRecord) -> str:
    """The two-line caption string exposed as the model's ``DisplayRole``."""
    title, subtitle = format_caption_parts(record)
    return f"{title}\n{subtitle}"


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024


def cell_size_for(box: int) -> QSize:
    """The cell that holds a ``box``-wide picture plus its caption strip."""
    return QSize(box + 2 * _CELL_PADDING, _CELL_PADDING + box + _CAPTION_HEIGHT)


def decode_edge_for(box: int) -> int:
    """Decode resolution for a display box, bucketed so the cache stays useful.

    Buckets instead of "one edge per zoom step": a continuous edge would write
    a new cache file for every nudge of the wheel, while three buckets keep the
    grid warm across the whole ladder.  The breakpoints assume the picture is
    downscaled into the box (a 256px decode stays sharp in a 160px box even at
    a modest device pixel ratio), so a small zoom never triggers a re-decode.
    """
    if box <= 160:
        return THUMB_DECODE_EDGE
    if box <= 240:
        return 384
    return 512


def nearest_zoom_step(box: int) -> int:
    """The ladder entry closest to ``box`` (used as the zoom anchor)."""
    return min(_ZOOM_STEPS, key=lambda step: abs(step - box))


def thumbnail_target_rect(box: QRect, size: QSize) -> QRect:
    """Letterbox ``size`` inside ``box``: centred, aspect kept, never outside.

    The picture is scaled until it touches one pair of the frame's edges, so a
    wide panorama and a tall document both land inside the same square and the
    grid reads as one uniform album sheet.  Small pictures are scaled *up* for
    the same reason: a 24px sticker should not leave a hole in the sheet.
    """
    if size.width() <= 0 or size.height() <= 0:
        return QRect(box)
    scaled = size.scaled(box.size(), Qt.AspectRatioMode.KeepAspectRatio)
    return QRect(
        box.x() + (box.width() - scaled.width()) // 2,
        box.y() + (box.height() - scaled.height()) // 2,
        scaled.width(),
        scaled.height(),
    )


def _caption_fonts(base: QFont) -> tuple[QFont, QFont]:
    """Title/subtitle fonts, with the subtitle one step smaller and legible."""
    title = QFont(base)
    subtitle = QFont(base)
    points = base.pointSizeF()
    if points > 0:
        subtitle.setPointSizeF(max(6.5, points - 1.0))
    else:
        pixels = base.pixelSize()
        if pixels > 0:
            subtitle.setPixelSize(max(9, pixels - 2))
    return title, subtitle


def _small_font(base: QFont) -> QFont:
    """One step smaller than the caption font, for placeholder text."""
    font = QFont(base)
    points = base.pointSizeF()
    if points > 0:
        font.setPointSizeF(max(6.5, points - 1.0))
    else:
        pixels = base.pixelSize()
        if pixels > 0:
            font.setPixelSize(max(9, pixels - 2))
    return font


class MediaCellDelegate(QStyledItemDelegate):
    """Draws one album tile: a fixed frame plus a caption strip.

    Owning the paint step is what makes the grid uniform - the frame is a
    constant square, the picture is letterboxed inside it, and the caption has
    its own reserved strip underneath.  The frame edge is per-instance so the
    view can resize every tile at once (Ctrl+wheel).
    """

    def __init__(self, parent=None, *, box: int = _THUMB_BOX) -> None:
        super().__init__(parent)
        self._box = box

    def box_edge(self) -> int:
        return self._box

    def set_box_edge(self, edge: int) -> None:
        self._box = edge

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt naming
        return cell_size_for(self._box)

    def paint(self, painter, option, index) -> None:  # noqa: N802 - Qt naming
        painter.save()
        try:
            self._paint_cell(painter, option, index)
        finally:
            painter.restore()

    def _paint_cell(self, painter, option, index) -> None:
        palette = option.palette
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(palette.color(QPalette.ColorRole.Highlight))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 6, 6)

        box = QRect(
            rect.x() + _CELL_PADDING,
            rect.y() + _CELL_PADDING,
            self._box,
            self._box,
        )
        # A neutral frame behind the picture so letterboxed edges read as part
        # of the tile instead of as a hole in the sheet.
        painter.setPen(
            QPen(palette.color(QPalette.ColorRole.Highlight), 2)
            if hovered and not selected
            else QPen(palette.color(QPalette.ColorRole.Mid), 1)
        )
        painter.setBrush(palette.color(QPalette.ColorRole.AlternateBase))
        painter.drawRoundedRect(box.adjusted(0, 0, -1, -1), 4, 4)

        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            target = thumbnail_target_rect(box, pixmap.size())
            # Clip as a guard: a picture must never bleed over the caption.
            painter.setClipRect(box)
            painter.drawPixmap(target, pixmap)
            painter.setClipping(False)
        else:
            self._paint_placeholder(
                painter,
                box,
                palette,
                index.data(THUMB_STATE_ROLE),
                index.data(MEDIA_TYPE_ROLE),
                index.data(COVER_LABEL_ROLE),
                index.data(COVER_EXT_ROLE),
            )

        self._paint_badge(painter, box, palette, index.data(MEDIA_TYPE_ROLE))
        self._paint_caption(painter, option, box, selected, index)

    def _paint_placeholder(
        self,
        painter,
        box: QRect,
        palette,
        state,
        media_type: str | None = None,
        cover_label: str | None = None,
        ext: str | None = None,
    ) -> None:
        """Show why a cell has no picture yet.

        Three different truths, three different drawings: "载入中…" (a decode
        is actually running), "无法预览" (a decodable type whose decode
        failed), and "无预览" (files and voice notes never get pixels — a
        static statement, not a spinner).  A failed video additionally gets a
        large centred play mark so the type stays obvious without a thumbnail.
        Other placeholders show the OS file-type icon (folder, PDF, Word, …)
        with the family name underneath.
        """
        if state == THUMB_NONE or (
            state == THUMB_FAILED and media_type == MediaType.VIDEO.value
        ):
            self._paint_type_mark(painter, box, palette, media_type, cover_label, ext)
            text = _PLACEHOLDER_TEXT.get(state or THUMB_NONE, "")
            painter.setFont(_small_font(painter.font()))
            painter.setPen(QPen(palette.color(QPalette.ColorRole.PlaceholderText)))
            painter.drawText(
                QRect(box.x(), box.y() + box.height() * 2 // 3, box.width(), 18),
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                text,
            )
            return

        text = _PLACEHOLDER_TEXT.get(state or THUMB_PENDING)
        if not text:
            return
        painter.setFont(_small_font(painter.font()))
        painter.setPen(QPen(palette.color(QPalette.ColorRole.PlaceholderText)))
        painter.drawText(
            box,
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
            text,
        )

    def _paint_type_mark(
        self,
        painter,
        box: QRect,
        palette,
        media_type: str | None,
        cover_label: str | None = None,
        ext: str | None = None,
    ) -> None:
        """A large centred mark for cells that will never show pixels.

        First choice is the OS file-type icon (the same folder / PDF / Word /
        archive mark Explorer shows), painted large and centred with the
        family name underneath.  When the platform supplies no icon, the
        media-type glyph (▶/▤/♪) plus the family name (``ZIP``/``PDF``/…) is
        drawn instead, so a cell never ends up blank.
        """
        # Videos never use the OS shell icon here: on some machines it is a
        # per-file thumbnail, on others a generic mark — mixing either with a
        # "无法预览" caption reads as broken UI.  A large centred play mark is
        # unambiguous in every case.
        use_icon = media_type != MediaType.VIDEO.value
        icon = cover_icon(media_type_value=media_type, ext=ext or "") if use_icon else None
        label = (cover_label or "").strip()
        mark_color = palette.color(QPalette.ColorRole.PlaceholderText)
        mark_color.setAlpha(170)
        painter.setPen(QPen(mark_color))
        if icon is not None and not icon.isNull():
            edge = max(40, min(box.width(), box.height()) * 2 // 5)
            raw = icon.pixmap(QSize(edge, edge))
            # Belt and braces: the pixmap is explicitly scaled and everything
            # below is clipped to the frame, so no icon can ever bleed into a
            # neighbour cell or the caption strip again.
            pixmap = raw.scaled(
                QSize(edge, edge),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            if not pixmap.isNull():
                target = QRect(
                    box.x() + (box.width() - pixmap.width()) // 2,
                    box.y() + box.height() // 10,
                    pixmap.width(),
                    pixmap.height(),
                )
                painter.setClipRect(box)
                try:
                    painter.drawPixmap(target, pixmap)
                finally:
                    painter.setClipping(False)
                rest = box.bottom() - target.bottom()
                if label and rest >= 14:
                    label_font = QFont(painter.font())
                    points = label_font.pointSizeF()
                    if points > 0:
                        label_font.setPointSizeF(max(9.0, points * 1.1))
                    label_font.setBold(True)
                    painter.setFont(label_font)
                    painter.drawText(
                        QRect(box.x(), target.bottom() + 2, box.width(), rest - 2),
                        int(
                            Qt.AlignmentFlag.AlignHCenter
                            | Qt.AlignmentFlag.AlignTop
                        ),
                        label,
                    )
                return
        glyph = _TYPE_GLYPHS.get(media_type or "", "")
        if not glyph and not label:
            return
        # The fallback text is clipped like everything else: a wide glyph must
        # never reach into the neighbour cell.
        painter.setClipRect(box)
        try:
            if glyph and label:
                glyph_font = QFont(painter.font())
                points = glyph_font.pointSizeF()
                if points > 0:
                    glyph_font.setPointSizeF(max(13.0, points * 1.8))
                painter.setFont(glyph_font)
                painter.drawText(
                    QRect(box.x(), box.y() + box.height() // 8, box.width(), box.height() // 3),
                    int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                    glyph,
                )
                label_font = QFont(painter.font())
                label_points = label_font.pointSizeF()
                if label_points > 0:
                    label_font.setPointSizeF(max(11.0, label_points * 1.4))
                label_font.setBold(True)
                painter.setFont(label_font)
                painter.drawText(
                    QRect(
                        box.x(),
                        box.y() + box.height() // 2,
                        box.width(),
                        box.height() // 4,
                    ),
                    int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                    label,
                )
                return
            font = QFont(painter.font())
            points = font.pointSizeF()
            if points > 0:
                font.setPointSizeF(max(14.0, points * 2.2))
            if label and not glyph:
                font.setBold(True)
            painter.setFont(font)
            painter.drawText(
                QRect(box.x(), box.y() + box.height() // 6, box.width(), box.height() // 2),
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                label or glyph,
            )
        finally:
            painter.setClipping(False)

    def _paint_badge(self, painter, box: QRect, palette, media_type) -> None:
        """A type glyph so videos/files/voice notes read at a glance.

        Videos get a large play mark dead-centre over the thumbnail — a phone
        album convention that reads at any tile size — while files and voice
        notes keep the small corner badge (their centred icon already says
        what they are).
        """
        glyph = _TYPE_GLYPHS.get(media_type or "", "")
        if not glyph:
            return
        if media_type == MediaType.VIDEO.value:
            edge = max(40, min(box.width(), box.height()) // 3)
            badge = QRect(
                box.x() + (box.width() - edge) // 2,
                box.y() + (box.height() - edge) // 2,
                edge,
                edge,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 160))
            painter.drawRoundedRect(badge, edge // 4, edge // 4)
            badge_font = QFont(painter.font())
            points = badge_font.pointSizeF()
            if points > 0:
                badge_font.setPointSizeF(max(16.0, points * 2.0))
            painter.setFont(badge_font)
            painter.setPen(QPen(QColor(255, 255, 255, 240)))
            painter.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), glyph)
            _ = palette  # badge colours are deliberately theme-independent
            return
        badge = QRect(box.x() + 4, box.y() + 4, 22, 18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.drawRoundedRect(badge, 3, 3)
        painter.setPen(QPen(QColor(255, 255, 255, 230)))
        painter.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), glyph)
        _ = palette  # badge colours are deliberately theme-independent

    def _paint_caption(self, painter, option, box: QRect, selected: bool, index) -> None:
        """Two elided, centred lines below the frame - never over it."""
        caption = QRect(
            option.rect.x() + 4,
            box.bottom() + 3,
            option.rect.width() - 8,
            option.rect.bottom() - box.bottom() - 4,
        )
        if caption.height() <= 0 or caption.width() <= 0:
            return
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        title, _, subtitle = str(text).partition("\n")
        palette = option.palette
        title_font, subtitle_font = _caption_fonts(option.font)
        if selected:
            title_color = palette.color(QPalette.ColorRole.HighlightedText)
            subtitle_color = QColor(title_color)
            subtitle_color.setAlpha(190)
        else:
            title_color = palette.color(QPalette.ColorRole.Text)
            subtitle_color = palette.color(QPalette.ColorRole.PlaceholderText)

        title_metrics = QFontMetrics(title_font)
        title_rect = QRect(
            caption.x(), caption.y(), caption.width(), title_metrics.height()
        )
        painter.setFont(title_font)
        painter.setPen(QPen(title_color))
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
            title_metrics.elidedText(title, Qt.TextElideMode.ElideMiddle, caption.width()),
        )

        subtitle_metrics = QFontMetrics(subtitle_font)
        subtitle_rect = QRect(
            caption.x(),
            title_rect.bottom() + 1,
            caption.width(),
            min(subtitle_metrics.height(), caption.bottom() - title_rect.bottom()),
        )
        if subtitle_rect.height() <= 0:
            return
        painter.setFont(subtitle_font)
        painter.setPen(QPen(subtitle_color))
        painter.drawText(
            subtitle_rect,
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
            subtitle_metrics.elidedText(
                subtitle, Qt.TextElideMode.ElideRight, caption.width()
            ),
        )


class MediaGridModel(QAbstractListModel):
    """Virtualized list of media records shown as grid cells.

    Qt only asks for data for the indices it paints, so this model holds the
    whole filtered set regardless of size.  Thumbnails arrive asynchronously:
    until one is decoded the cell paints a neutral placeholder, which is the
    same thing a phone album shows while a picture loads.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._records: tuple[MediaRecord, ...] = ()
        self._thumbs: dict[str, QPixmap] = {}
        self._failed: set[str] = set()
        # media id -> row, so a decode result does not scan 100k records.
        self._row_by_id: dict[str, int] = {}

    # -- data -----------------------------------------------------------
    def set_records(self, records) -> None:
        records = tuple(records)
        if records is self._records:
            # Re-applying the same filters must not reset the view: it would
            # throw away the scroll position and force a full re-decode pass.
            return
        self.beginResetModel()
        self._records = records
        self._row_by_id = {
            str(record.media_id): row for row, record in enumerate(records)
        }
        # Keep decoded thumbnails across a re-filter: they are keyed by media
        # id and the same photo is often still on screen after narrowing.
        self.endResetModel()

    def records(self) -> tuple[MediaRecord, ...]:
        return self._records

    def record_at(self, row: int) -> MediaRecord | None:
        if 0 <= row < len(self._records):
            return self._records[row]
        return None

    def row_of(self, media_id: str) -> int:
        return self._row_by_id.get(media_id, -1)

    def index_of_record(self, record: MediaRecord) -> int:
        return self.row_of(str(record.media_id))

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802, B008 - Qt naming/signature
        if parent.isValid():
            return 0
        return len(self._records)

    def data(self, index, role=Qt.DisplayRole):  # noqa: N802 - Qt naming
        if not index.isValid():
            return None
        row = index.row()
        if not 0 <= row < len(self._records):
            return None
        record = self._records[row]
        media_id = str(record.media_id)

        if role == Qt.DisplayRole:
            return format_caption(record)
        if role == Qt.DecorationRole:
            return self._thumbs.get(media_id)
        if role == Qt.UserRole:
            return media_id
        if role == MEDIA_TYPE_ROLE:
            return record.media_type.value
        if role == COVER_LABEL_ROLE:
            return cover_label_for(record)
        if role == COVER_EXT_ROLE:
            return cover_extension(record)
        if role == THUMB_STATE_ROLE:
            if media_id in self._thumbs:
                return THUMB_READY
            if media_id in self._failed:
                return THUMB_FAILED
            if not decode_supported(record.media_type.value):
                return THUMB_NONE
            return THUMB_PENDING
        if role == Qt.ToolTipRole:
            return (
                f"{record.file.relative_path}\n"
                f"大小：{_human_bytes(record.file.byte_size)}\n"
                f"时间：{record.observed_at.strftime('%Y-%m-%d %H:%M')}"
            )
        if role == Qt.SizeHintRole:
            return _CELL_SIZE
        return None

    # -- thumbnails ------------------------------------------------------
    def thumbnail(self, media_id: str) -> QPixmap | None:
        return self._thumbs.get(media_id)

    def has_thumbnail(self, media_id: str) -> bool:
        return media_id in self._thumbs

    def has_failed(self, media_id: str) -> bool:
        return media_id in self._failed

    def apply_thumbnail(self, media_id: str, pixmap: QPixmap | None) -> None:
        """Record a decoded thumbnail and notify the view.

        Called on a worker thread by the warm pool: it only writes plain
        Python state and emits a queued signal, so no widget is touched from
        off the main thread.  ``pixmap=None`` records a decode failure so the
        cell stops showing a "loading" placeholder.
        """
        if pixmap is None or pixmap.isNull():
            self._failed.add(media_id)
        else:
            self._thumbs[media_id] = pixmap
            self._failed.discard(media_id)
        row = self.row_of(media_id)
        if row < 0:
            return
        index = self.index(row, 0)
        self.dataChanged.emit(index, index, [Qt.DecorationRole, THUMB_STATE_ROLE])

    def clear_thumbnails(self) -> None:
        self._thumbs.clear()
        self._failed.clear()

    def clear_unsupported_failures(self) -> None:
        """Drop failure marks on records that can never decode.

        Files and voice notes have no picture to fail; a stale mark (left by
        a warmer that queued them anyway) would replace their type icon with
        a bare "无法预览".  Emits one repaint when anything was healed.
        """
        healed = {
            str(record.media_id)
            for record in self._records
            if not decode_supported(record.media_type.value)
        } & self._failed
        if not healed:
            return
        self._failed -= healed
        if self._records:
            first = self.index(0, 0)
            last = self.index(len(self._records) - 1, 0)
            self.dataChanged.emit(first, last, [THUMB_STATE_ROLE])


class MediaGalleryView(QListView):
    """Icon-mode grid with lazy thumbnail loading for visible cells only.

    Ctrl+wheel resizes every tile, the way a Windows folder does.  Deliberately
    *no* list/details modes: a photo album has one useful arrangement, and the
    only thing a user wants to change is how big the pictures are.
    """

    visible_range_changed = Signal(int, int)
    thumb_box_changed = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._thumb_box = _THUMB_BOX
        self.setViewMode(QListView.IconMode)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setUniformItemSizes(True)
        self.setGridSize(cell_size_for(self._thumb_box))
        self.setIconSize(QSize(self._thumb_box, self._thumb_box))
        # The delegate owns the tile layout, so cells are identical squares
        # regardless of what each picture's aspect ratio is.
        self._delegate = MediaCellDelegate(self, box=self._thumb_box)
        self.setItemDelegate(self._delegate)
        self.setWordWrap(False)
        self.setSpacing(4)
        self.setSelectionMode(QListView.ExtendedSelection)
        self.setEditTriggers(QListView.NoEditTriggers)
        self.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._visible: tuple[int, int] = (0, -1)

    # -- zoom -----------------------------------------------------------
    def thumb_box(self) -> int:
        """Current picture frame edge, in pixels."""
        return self._thumb_box

    def decode_edge(self) -> int:
        """Decode resolution that matches the current frame size."""
        return decode_edge_for(self._thumb_box)

    def first_visible_row(self) -> int:
        """Row at the top-left of the viewport, or ``-1`` when empty."""
        index = self.indexAt(self.viewport().rect().topLeft())
        return index.row() if index.isValid() else -1

    def set_thumb_box(self, edge: int) -> bool:
        """Resize every tile; returns whether anything changed.

        The picture the user is looking at is pinned to the top of the viewport
        across the resize, so zooming does not throw away their place in the
        album - the same behaviour as Explorer's thumbnail size slider.
        """
        edge = max(_ZOOM_MIN, min(_ZOOM_MAX, int(edge)))
        if edge == self._thumb_box:
            return False
        anchor = self.first_visible_row()
        self._thumb_box = edge
        self._delegate.set_box_edge(edge)
        self.setGridSize(cell_size_for(edge))
        self.setIconSize(QSize(edge, edge))
        self._visible = (-1, -1)
        # Lay out now so the anchor can be restored against the new geometry.
        self.doItemsLayout()
        model = self.model()
        if anchor >= 0 and model is not None and anchor < model.rowCount():
            self.scrollTo(
                model.index(anchor, 0), QListView.ScrollHint.PositionAtTop
            )
        self._on_visible_changed()
        self.thumb_box_changed.emit(edge)
        return True

    def zoom_by(self, steps: int) -> bool:
        """Walk the zoom ladder by ``steps`` (``+1`` bigger, ``-1`` smaller)."""
        index = _ZOOM_STEPS.index(nearest_zoom_step(self._thumb_box))
        index = max(0, min(len(_ZOOM_STEPS) - 1, index + steps))
        return self.set_thumb_box(_ZOOM_STEPS[index])

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                self.zoom_by(1 if delta > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    def visible_range(self) -> tuple[int, int]:
        """First/last visible row expanded by the prefetch buffer, clamped."""
        model = self.model()
        if model is None or model.rowCount() == 0:
            return (0, -1)
        rect = self.viewport().rect()
        first_index = self.indexAt(rect.topLeft())
        last_index = self.indexAt(rect.bottomRight())
        first = first_index.row() if first_index.isValid() else 0
        last = last_index.row() if last_index.isValid() else first
        count = model.rowCount()
        first = max(0, min(first, count - 1))
        last = max(first, min(last, count - 1))
        return (max(0, first - _PREFETCH_BUFFER), min(count - 1, last + _PREFETCH_BUFFER))

    def rowCount(self) -> int:  # noqa: N802 - mirrors the list model's naming
        """Number of cells currently in the grid (no display cap)."""
        model = self.model()
        return model.rowCount() if model is not None else 0

    def invalidate_visible_range(self) -> None:
        """Forget the cached range so the next check reports it again.

        A model reset does not move the scroll position, so the cached range
        stays stale exactly when it matters most: right after a filter.  The
        window calls this once the new records are in place.
        """
        self._visible = (-1, -1)
        self._on_visible_changed()

    def _on_visible_changed(self) -> None:
        try:
            first, last = self.visible_range()
        except RuntimeError:
            # The C++ view is already destroyed (a deferred re-check can fire
            # during teardown); there is nothing left to report.
            return
        if (first, last) == self._visible:
            return
        self._visible = (first, last)
        self.visible_range_changed.emit(first, last)

    def _on_model_reset(self) -> None:
        """Re-report the range once the view has re-laid out.

        Right after a model reset the viewport still describes the *pre-reset*
        layout, so a range computed now can be badly wrong: filtering a long
        list down while scrolled to the bottom reported the top of the new list
        while the user was looking at its end, and the wrong cells got decoded
        first.  The deferred call runs after Qt's delayed layout settles.
        """
        self._visible = (-1, -1)
        QTimer.singleShot(0, self._on_visible_changed)

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802 - Qt naming
        super().scrollContentsBy(dx, dy)
        self._on_visible_changed()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._on_visible_changed()

    def setModel(self, model) -> None:  # noqa: N802 - Qt naming
        previous = self.model()
        if previous is not None:
            try:
                previous.modelReset.disconnect(self._on_model_reset)
            except (RuntimeError, TypeError):
                pass
        super().setModel(model)
        if model is not None:
            model.modelReset.connect(self._on_model_reset)
        # A fresh model invalidates the cached range; re-emit on next paint.
        self._visible = (-1, -1)
        self._on_visible_changed()
