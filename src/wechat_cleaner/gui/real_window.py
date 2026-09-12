"""Read-only desktop window for real WeChat data (Phase 4 Beta).

Design stance:

* there is **no** cleanup, plan or delete entry point — the window cannot reach
  the executor, and the only removals it offers act on the application's own
  cache root;
* a preview is a decoded product of a source file: clearing it never touches the
  WeChat source, and the window says so in plain language;
* every failure is shown as a readable reason (error code + hint) instead of a
  silent empty cell.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import (
    QDate,
    QEventLoop,
    QItemSelectionModel,
    QProcess,
    QRunnable,
    QSize,
    Qt,
    QThread,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from wechat_cleaner.domain.contracts import MediaRecord, MediaType
from wechat_cleaner.real_db import is_merged_forward, session_dir_of

from .data_root import find_data_root
from .gallery import (
    _THUMB_BOX,
    THUMB_DECODE_EDGE,
    UNATTRIBUTED_LABEL,
    MediaGalleryView,
    MediaGridModel,
    decode_supported,
    is_attributable,
)
from .real_session import (
    READ_ONLY_NOTICE,
    CacheStatus,
    ExportOutcome,
    FacadeError,
    PreviewOutcome,
    RealReadOnlyPort,
    RecycleOutcome,
    VideoClipOutcome,
)

# Combo data value for the pseudo-session "unattributable records".  Real
# attach dirs are 32-hex hashes, so this can never collide with one.
UNATTRIBUTED_SESSION = "__unattributed__"

_TYPE_LABELS = {
    "all": "全部类型",
    "original": "原图（可导出）",
    MediaType.IMAGE.value: "图片",
    MediaType.THUMBNAIL.value: "缩略图",
    MediaType.VIDEO.value: "视频",
    MediaType.FILE.value: "文件",
    MediaType.VOICE.value: "语音",
}
_AVAILABILITY_LABELS = {
    "all": "全部状态",
    "original_available": "原图可用",
    "thumbnail_only": "仅有缩略图",
    "missing": "源文件缺失",
    "undecodable": "不可解码",
    "unknown": "未知",
}
_TIME_PRESETS = (
    ("all", "全部时间"),
    ("d7", "最近 7 天"),
    ("d30", "最近 30 天"),
    ("d90", "最近 90 天"),
    ("y1", "最近一年"),
    # The deletion workflow is "clear out the old stuff first", so the same
    # spans are offered as an *older-than* cutoff rather than only a recent one.
    ("old7", "7 天前（更早）"),
    ("old30", "30 天前（更早）"),
    ("old90", "90 天前（更早）"),
    ("old180", "半年以前"),
    ("old365", "一年以前"),
    ("before", "自定义日期之前…"),
)

# Grid ordering.  A photo manager is browsed by date by default, but "which of
# these is eating my disk" needs size, and "where is that one file" needs name.
_SORT_ORDERS = (
    ("time_desc", "时间：新 → 旧"),
    ("time_asc", "时间：旧 → 新"),
    ("size_desc", "大小：大 → 小"),
    ("size_asc", "大小：小 → 大"),
    ("name_asc", "名称：A → Z"),
    ("name_desc", "名称：Z → A"),
)
_AUTO_PREVIEW_DELAY_MS = 300

# Why a record cannot be exported as an original, keyed by availability.  Only
# records that really hold a decodable original count - "只有缩略图" must not
# pretend an export could produce anything.
_UNAVAILABLE_EXPORT_REASONS = {
    "thumbnail_only": "仅有缩略图，无原图可导出",
    "missing": "源文件已不在磁盘上，无原图可导出",
    "undecodable": "源文件无法解码，无原图可导出",
    "unknown": "尚未确认源文件状态，无法导出",
}

# How many availability checks run between UI progress updates.  Each check is
# one stat call; pumping events per batch keeps the window responsive while the
# status bar shows "checking N/M" instead of appearing frozen.
_AVAILABILITY_PROGRESS_BATCH = 1000

# --- background thumbnail decoding -----------------------------------------
# Browsing should feel like a phone album: the pictures you can see are already
# decoded, and scrolling keeps them coming.  A thread pool decodes the visible
# grid range (plus a buffer) into the preview cache ahead of any click.  There
# is no display cap: the grid is virtualized, so an enormous account decodes
# only what is actually looked at.
_WARM_POOL_SIZE = 4        # parallel decode threads (PIL/AES release the GIL)
_WARM_BATCH = 120          # grid cells queued after every render, in view order
_WARM_NEIGHBOURHOOD = 24   # cells queued ahead of/behind the selection
_WARM_YIELD_TIMEOUT = 30.0  # seconds a warmer waits for a foreground step
# Sort key for cells the current filter does not contain: they travel behind
# everything the user can actually see.
_WARM_FAR_DISTANCE = 1 << 30
_WORKER_STOP_TIMEOUT_MS = 15000  # join budget for the shared facade thread
_WARM_POOL_DRAIN_TIMEOUT_MS = 10000  # join budget for in-flight thumbnail decodes


class _ThumbRunnable(QRunnable):
    """Decode one record's grid thumbnail on a worker thread.

    Qt only allows ``QPixmap`` on the GUI thread, so this worker does the
    expensive half - decrypt and decode the picture into the app cache - and
    hands just the *cache path* back.  The window loads the pixmap and gives it
    to the model on the UI thread, which is what keeps a real account from
    crashing on the cross-thread pixmap that an offscreen test platform
    happens to tolerate.

    Every window attribute touched here is plain Python state, and the
    completion bookkeeping is thread-safe by construction (GIL-atomic ops,
    no Qt widget access).
    """

    def __init__(self, window: RealReadOnlyWindow, record: MediaRecord) -> None:
        super().__init__()
        self._window = window
        self._record = record
        # Auto-delete is left *off* deliberately.  A QRunnable that Qt deletes
        # itself owns its C++ half while Python still holds the wrapper (and
        # this class holds a window reference); the two lifetimes can disagree
        # and the pool then frees the object underneath a running ``run()``.
        # Keeping the Python object alive and letting the pool release it costs
        # a few bytes per cell and removes the race entirely.
        self.setAutoDelete(False)

    def run(self) -> None:  # noqa: RETR504 - worker body, no return value
        window = self._window
        if window._warm_stopped:
            return
        # Yield to foreground work: while a blocking step (or the user's own
        # preview) is decoding, warming would just steal CPU and disk.
        deadline = time.monotonic() + _WARM_YIELD_TIMEOUT
        while window._busy_loop is not None and not window._warm_stopped:
            if time.monotonic() >= deadline:
                window._warm_finish(self._record, ok=False)
                return
            time.sleep(0.05)
        if window._warm_stopped:
            return
        # Read the decode resolution from the module constant rather than the
        # view: this runs on a worker thread and must not touch a widget.
        path = ""
        try:
            outcome = window._facade.preview(
                self._record,
                variant=window._warm_variant,
                max_edge_px=window._warm_decode_edge,
            )
            if outcome.ok:
                path = outcome.output_path
        except Exception:  # noqa: BLE001 - warming is best-effort
            path = ""
        if not path:
            window._warm_finish(self._record, ok=False)
            return
        # Settle the bookkeeping *before* emitting.  The slot runs on the UI
        # thread and repaints the grid, which calls back into
        # ``_warm_visible_cells``; were this cell still unmarked, it would be
        # re-queued and the same signal emitted re-entrantly (which PySide6
        # rejects with "only accepts 0 argument(s)").
        window._warm_finish(self._record, ok=True)
        if window._warm_stopped:
            return
        try:
            window.thumbnail_ready.emit(str(self._record.media_id), path)
        except TypeError:
            # A re-entrant emission was refused; the cell is already marked
            # warm, so dropping this duplicate loses nothing.
            pass
        except RuntimeError:
            # The window is gone (a decode that outlived shutdown).  Nothing to
            # paint, and this must not surface as an error at exit.
            pass

# Pillow-reported image format -> file extension for exported originals.
_FORMAT_EXTENSIONS = {
    "jpeg": ".jpg",
    "jpg": ".jpg",
    "png": ".png",
    "gif": ".gif",
    "bmp": ".bmp",
    "webp": ".webp",
}

# Container-family markers stripped from the hash stem when suggesting an
# export filename (``<hash>_W.dat`` exports as ``<hash>.jpg``).
_EXPORT_CONTAINER_SUFFIXES = ("_t_w", "_t", "_w", "_h")


def _export_suggested_name(relative_path: str) -> str:
    stem = Path(relative_path).stem
    for suffix in _EXPORT_CONTAINER_SUFFIXES:
        if stem.casefold().endswith(suffix) and len(stem) > len(suffix):
            stem = stem[: -len(suffix)]
            break
    # Most originals decode to JPEG; the worker corrects the extension if the
    # payload turns out to be PNG/other, and QFileDialog confirms overwrites.
    return f"{stem}.jpg"


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024


def _app_icon_file() -> Path | None:
    """The bundled application icon, in source checkouts and frozen builds.

    The frozen layout carries ``icons/app.png`` as bundle data (see the spec);
    a source checkout reads it from the packaging tree.  ``None`` when neither
    exists — the window then keeps the platform default.
    """
    candidates: list[Path] = []
    frozen_root = getattr(sys, "_MEIPASS", "")
    if frozen_root:
        candidates.append(Path(str(frozen_root)) / "icons" / "app.png")
    candidates.append(
        Path(__file__).resolve().parent.parent.parent.parent
        / "packaging"
        / "icons"
        / "app.png"
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _open_local_path(path: str) -> bool:
    """Open a local file with the OS default application (local-only).

    Module-level (rather than a window method) so tests can monkeypatch it
    without constructing a window.  Returns whether the OS accepted the open.
    """
    try:
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
    except Exception:  # noqa: BLE001 - an open failure is reported by the caller
        return False


def _reveal_local_path(path: str) -> bool:
    """Show a local file's containing folder, highlighting the file.

    On Windows this is ``explorer /select,``; elsewhere the parent folder is
    opened.  Local-only like :func:`_open_local_path`.
    """
    try:
        if sys.platform == "win32":
            return QProcess.startDetached("explorer", ["/select,", os.path.normpath(path)])
        return bool(
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
        )
    except Exception:  # noqa: BLE001 - reported by the caller
        return False


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """Aggregate result of a multi-select management action.

    A batch is many single-file operations run back to back.  Each file keeps
    its own outcome - one damaged original must not hide the nineteen that
    exported fine - so the report carries per-file detail plus the totals the
    status bar shows.
    """

    action: str
    requested: int
    succeeded: tuple[tuple[str, str], ...] = ()   # (relative_path, note)
    failed: tuple[tuple[str, str], ...] = ()      # (relative_path, reason)
    skipped: tuple[tuple[str, str], ...] = ()     # (relative_path, reason)
    export_dir: str = ""
    # Media ids whose source file left the account; the view drops them.
    recycled_media_ids: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def succeeded_count(self) -> int:
        return len(self.succeeded)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    def summary(self) -> str:
        parts = [f"成功 {self.succeeded_count} 项"]
        if self.skipped:
            parts.append(f"跳过 {len(self.skipped)} 项")
        if self.failed:
            parts.append(f"失败 {self.failed_count} 项")
        return f"{self.action}：" + "，".join(parts)


class _FacadeWorker(QThread):
    """Run blocking facade calls on one long-lived worker thread.

    The real-data pipeline does its heaviest work synchronously (upstream key
    extraction, database decryption, a full 100k-file account scan).  Running
    it on the UI thread freezes the window, so it runs here instead.

    A single thread is reused for the whole session *deliberately*.  The
    upstream ``WeChatDB`` session is built on whichever thread calls
    ``facade.connect`` and keeps thread-affine native state afterwards; if
    that thread exits, later use or teardown of the session faults the
    process with a bare access violation.  So the thread that builds the
    session must outlive it.  ``BaseException`` is reported, not just
    ``Exception``, so a guard-triggered ``SystemExit`` can never strand the
    waiting event loop.  ``progress`` carries coarse step counters from the
    worker thread to the UI thread.
    """

    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._job: Callable[[], object] | None = None
        self._job_ready = threading.Condition()
        self._stopping = False

    def submit(self, func: Callable[[], object]) -> None:
        """Hand the worker its next job (call on the UI thread, after start)."""
        with self._job_ready:
            self._job = func
            self._job_ready.notify_all()

    def stop(self) -> None:
        """Ask the loop to finish; the thread ends after the current job."""
        with self._job_ready:
            self._stopping = True
            self._job_ready.notify_all()

    def run(self) -> None:  # noqa: RETR504 - signal delivery is the result path
        while True:
            with self._job_ready:
                while self._job is None and not self._stopping:
                    self._job_ready.wait()
                if self._stopping:
                    return
                func = self._job
                self._job = None
            try:
                self.succeeded.emit(func())
            except BaseException as exc:  # noqa: BLE001 - the loop must always quit
                self.failed.emit(f"{type(exc).__name__}: {exc}")


class _ImagePane(QLabel):
    """Preview pane that never lets its picture resize the window layout.

    A plain ``QLabel`` reports the pixmap's size as its size hint, and a box
    layout allocates from hints, so the preview pane (which shares a row with
    the grid) grew the moment a picture appeared: the grid lost ~100px and a
    small window silently dropped from four tiles per row to three.  Neither a
    size *policy* nor scaling the pixmap beforehand helps - the hint itself is
    what the layout reads.

    So the hint is pinned here and the picture is scaled into whatever space
    the layout grants.  The pane's share of the row now depends on the window
    size only.
    """

    _FALLBACK_HINT = QSize(380, 320)

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Ignored in both directions: the pane takes what the layout gives
        # (stretch plus the minimum size below), the picture adapts to it.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._source: QPixmap | None = None

    def source(self) -> QPixmap | None:
        return self._source

    def show_message(self, text: str) -> None:
        """Drop the picture and show a line of text instead."""
        self._source = None
        super().setPixmap(QPixmap())
        super().setText(text)

    def show_pixmap(self, pixmap: QPixmap) -> None:  # noqa: N802 - Qt naming
        """Show ``pixmap`` scaled to the pane; the pane's size never changes."""
        self._source = QPixmap(pixmap) if pixmap is not None else None
        super().setText("")
        super().setPixmap(self._scaled())

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        """Constant, picture-independent preferred size."""
        return self.minimumSizeHint()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        """Constant, picture-independent minimum size."""
        floor = self.minimumSize()
        if floor.width() > 0 and floor.height() > 0:
            return floor
        return QSize(self._FALLBACK_HINT)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        if self._source is not None:
            super().setPixmap(self._scaled())

    def _scaled(self) -> QPixmap:
        if self._source is None or self._source.isNull():
            return QPixmap()
        size = self.size()
        if size.width() <= 0 or size.height() <= 0:
            return self._source
        return self._source.scaled(
            size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )


class _StableHintLabel(QLabel):
    """A word-wrapped label whose preferred width does not follow its text.

    Status lines change with every action ("已选择 12 项，共 1.2 GB"), and a
    word-wrapped ``QLabel`` reports the *full* text width as its size hint.  In
    a column that shares a row with the grid, a longer status line therefore
    widened the column and took a grid column with it (measured: the preview
    column demanded 100px more, and the grid below 700px dropped one column).

    Two things are needed to stop that:

    * the preferred width is pinned to a constant, and
    * height-for-width is switched off and *kept* off - ``QLabel`` re-applies
      its size policy (with ``setHeightForWidth(wordWrap)``) on every
      ``setText``, which puts the enclosing box layout back into HFW mode,
      where it recomputes a preferred width from the text and ignores the
      pinned hint.

    The text still wraps visually; only its opinion about the layout changes.
    """

    def __init__(self, text: str = "", *, hint_width: int = 300) -> None:
        super().__init__(text)
        self._hint_width = hint_width
        self.setWordWrap(True)
        self._pin_policy()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        super().setText(text)
        self._pin_policy()

    def _pin_policy(self) -> None:
        policy = self.sizePolicy()
        policy.setHeightForWidth(False)
        self.setSizePolicy(policy)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        wrapped = self.heightForWidth(self._hint_width)
        height = max(super().sizeHint().height(), wrapped)
        return QSize(self._hint_width, height)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return self.sizeHint()


class RealReadOnlyWindow(QMainWindow):
    """Browse and preview real WeChat media without any mutation capability."""

    # Worker thread -> UI thread: (media id, cache path) of a decoded thumbnail.
    # The pixmap itself is built on the GUI thread, where Qt requires it.
    thumbnail_ready = Signal(str, str)

    # (data root, account id) of a session that connected successfully.  The
    # launcher uses it to remember the choice, so the next launch opens
    # pre-filled instead of asking for the path again.
    session_connected = Signal(str, str)

    # New tile size after a Ctrl+wheel zoom; the launcher persists it so the
    # album reopens at the size the user picked.
    thumbnail_size_changed = Signal(int)

    def __init__(self, facade: RealReadOnlyPort) -> None:
        super().__init__()
        self._facade = facade
        self._all_records: tuple[MediaRecord, ...] = ()
        self._records: tuple[MediaRecord, ...] = ()
        self._record_by_id: dict[str, MediaRecord] = {}
        self._availability: dict[uuid.UUID, str] = {}
        self._summary: dict = {}
        self._busy_loop: QEventLoop | None = None
        self._active_worker: _FacadeWorker | None = None
        # The shared facade worker thread.  Created lazily on the first
        # blocking step and kept for the window's lifetime so the upstream
        # session it builds stays on the thread that created it.
        self._worker: _FacadeWorker | None = None
        self._last_previewed_id: str | None = None
        self._session_mapping: dict[str, dict] = {}
        self._busy_message: str = ""
        self._busy_started = 0.0
        self._busy_progress: str = ""
        # Worker-thread bridge for progress callbacks: while a step runs, the
        # blocking call reports counts via ``_emit_progress`` which forwards
        # them as a queued Qt signal; ``None`` outside a busy step.
        self._progress_emit = None
        self._busy_clock = QTimer(self)
        self._busy_clock.setInterval(1000)
        self._busy_clock.timeout.connect(self._tick_busy_clock)
        self._last_scan_seconds: float | None = None
        self._scan_seconds_note: str = ""
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(_AUTO_PREVIEW_DELAY_MS)
        self._preview_timer.timeout.connect(self._auto_preview)
        # In-app video clip playback: sampled PNG frames flipped by a timer
        # (no audio, no seeking — a lightweight stand-in, not a media player).
        # Pixmaps live here (GUI thread only); the bytes arrive from the worker.
        self._clip_frames: list = []
        self._clip_index = 0
        self._clip_playing = False
        self._last_clip_id: str | None = None
        self._clip_timer = QTimer(self)
        self._clip_timer.timeout.connect(self._advance_clip)
        self.last_preview: PreviewOutcome | None = None
        self._last_export_dir: str = ""
        self.last_batch: BatchOutcome | None = None
        # Background thumbnail decoding: a thread pool decodes the visible grid
        # cells into the model before they are ever clicked.
        #
        # The pool is deliberately *not* parented to the window.  A pool owned
        # by the widget is destroyed as part of the widget's C++ teardown, which
        # can happen while worker threads are still inside ``run()`` and
        # touching this window's state: a real Windows account faulted the
        # process with a bare access violation at the first paint (``show()``)
        # for exactly that reason.  Unparented, it is shut down explicitly in
        # ``closeEvent`` (stop, then wait) so no thread outlives the window.
        self._warm_pool = QThreadPool()
        self._warm_pool.setMaxThreadCount(_WARM_POOL_SIZE)
        # Decode completions arrive on worker threads and pop from the queue;
        # the UI thread reorders it while scrolling.  Every queue mutation
        # takes this lock so a fast scroll cannot tear a rebuild in half.
        self._warm_lock = threading.Lock()
        self._warm_queue: deque[MediaRecord] = deque()
        self._warm_queued: set[str] = set()
        self._warmed: set[str] = set()
        self._warm_failed: set[str] = set()
        self._warm_inflight = 0
        self._warm_stopped = False
        self._warm_variant = "thumbnail"
        # Decode resolution for the grid, kept in step with the tile size the
        # user zoomed to (workers read this attribute, never the widget).
        self._warm_decode_edge = THUMB_DECODE_EDGE
        # Previous tile size, so a Ctrl+wheel zoom knows whether the pictures
        # already decoded are still sharp enough to keep.
        self._last_thumb_box = _THUMB_BOX
        # Runnables handed to the pool, keyed by media id.  The pool does not
        # auto-delete them, so this is what keeps them alive until their decode
        # reports back and ``_warm_finish`` releases the entry.
        self._warm_live: dict[str, _ThumbRunnable] = {}
        # Decoded thumbnails arrive as paths from worker threads; the pixmap is
        # loaded here, on the GUI thread, where Qt requires it.
        self.thumbnail_ready.connect(self._apply_thumbnail_path)
        # Join the shared facade worker on application shutdown as well as on
        # window close: a script or a frozen build that exits without an
        # explicit close() would otherwise destroy a running QThread, which Qt
        # reports as "Destroyed while thread is still running" and which can
        # fault natively.
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown_worker)
        self.setWindowTitle("微信空间管理器（只读相册）")
        icon_file = _app_icon_file()
        if icon_file is not None:
            self.setWindowIcon(QIcon(str(icon_file)))
        self.resize(1280, 840)
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)

        layout.addWidget(self._wizard_group())
        layout.addWidget(self._filter_group())
        layout.addLayout(self._middle_row())
        layout.addWidget(self._cache_group())

        self.read_only_label = QLabel(READ_ONLY_NOTICE)
        self.read_only_label.setWordWrap(True)
        self.read_only_label.setStyleSheet("color: #555; padding: 4px;")
        layout.addWidget(self.read_only_label)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        # Indeterminate-capable progress bar parked on the right of the status
        # bar.  Pulse (range 0,0) for steps with no observable completion
        # ratio; real percent/counts where the pipeline provides them.
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedWidth(180)
        self.progress_bar.hide()
        self.statusBar().addPermanentWidget(self.progress_bar)
        self._set_status("选择微信数据目录后点击「自动检测」")

    def _wizard_group(self) -> QGroupBox:
        group = QGroupBox("① 连接真实微信数据（只读）")
        grid = QGridLayout(group)

        self.data_root_edit = QLineEdit()
        self.data_root_edit.setPlaceholderText("例如 C:\\...\\xwechat_files")
        self.detect_button = QPushButton("自动检测")
        self.detect_button.clicked.connect(self.detect_accounts)

        self.account_combo = QComboBox()
        self.account_combo.setMinimumWidth(320)
        self.connect_button = QPushButton("连接")
        self.connect_button.clicked.connect(self.connect_session)

        self.summary_label = QLabel("未连接")
        self.summary_label.setWordWrap(True)

        grid.addWidget(QLabel("数据目录"), 0, 0)
        grid.addWidget(self.data_root_edit, 0, 1, 1, 3)
        grid.addWidget(self.detect_button, 0, 4)
        grid.addWidget(QLabel("账号"), 1, 0)
        grid.addWidget(self.account_combo, 1, 1, 1, 2)
        grid.addWidget(self.connect_button, 1, 3)
        grid.addWidget(self.summary_label, 2, 1, 1, 4)
        return group

    def _filter_group(self) -> QGroupBox:
        group = QGroupBox("② 筛选（文件系统维度）")
        outer = QVBoxLayout(group)
        row = QHBoxLayout()

        self.type_combo = QComboBox()
        for key, label in _TYPE_LABELS.items():
            self.type_combo.addItem(label, key)
        self.availability_combo = QComboBox()
        for key, label in _AVAILABILITY_LABELS.items():
            self.availability_combo.addItem(label, key)

        self.time_combo = QComboBox()
        for key, label in _TIME_PRESETS:
            self.time_combo.addItem(label, key)
        self.before_date = QDateEdit(QDate.currentDate())
        self.before_date.setCalendarPopup(True)
        self.before_date.setDisplayFormat("yyyy-MM-dd")
        self.before_date.setEnabled(False)
        self.time_combo.currentIndexChanged.connect(self._on_time_mode_changed)

        self.min_mb_spin = QDoubleSpinBox()
        self.min_mb_spin.setRange(0.0, 100_000.0)
        # Two decimals so a "50 KB" style cutoff (0.05 MB) is expressible; the
        # step stays at 0.1 MB so the arrows remain useful for large sweeps.
        self.min_mb_spin.setDecimals(2)
        self.min_mb_spin.setSingleStep(0.1)
        self.min_mb_spin.setSuffix(" MB")
        self.min_mb_spin.setValue(0.0)

        self.sort_combo = QComboBox()
        for key, label in _SORT_ORDERS:
            self.sort_combo.addItem(label, key)
        self.sort_combo.setToolTip(
            "相册网格的排列顺序。切换后立即重排（回到列表顶部）。"
        )
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)

        self.load_button = QPushButton("重新加载数据库")
        self.load_button.clicked.connect(self.load_records)
        self.apply_button = QPushButton("应用筛选")
        self.apply_button.clicked.connect(self.apply_filters)

        # Session filter (second row): "the chat with X / the group Y".
        # Editable so typing a nickname filters the long dropdown to matches.
        self.session_combo = QComboBox()
        self.session_combo.setEditable(True)
        self.session_combo.setInsertPolicy(QComboBox.NoInsert)
        self.session_combo.addItem("全部会话", "")
        self.session_combo.setToolTip(
            "按聊天对象筛选图片：选择某账号/某群聊后，只显示你们会话目录下的媒体。\n"
            "可输入昵称关键字缩小下拉范围。会话归属来自 msg/attach 的目录结构，"
            "文件/视频/语音按月份存放、无法归属会话，故会话筛选时它们不参与。"
        )
        completer = self.session_combo.completer()
        if completer is not None:
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            completer.setFilterMode(Qt.MatchContains)

        self.merged_check = QCheckBox("仅合并转发记录")
        self.merged_check.setToolTip(
            "只看来自合并转发聊天记录的附件（msg/attach/…/Rec/ 布局）。"
        )

        row.addWidget(QLabel("类型"))
        row.addWidget(self.type_combo)
        row.addWidget(QLabel("最小"))
        row.addWidget(self.min_mb_spin)
        row.addWidget(QLabel("可用性"))
        row.addWidget(self.availability_combo)
        row.addWidget(QLabel("时间"))
        row.addWidget(self.time_combo)
        row.addWidget(self.before_date)
        row.addStretch(1)
        row.addWidget(self.load_button)
        row.addWidget(self.apply_button)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("会话"))
        row2.addWidget(self.session_combo, 1)
        row2.addWidget(QLabel("排序"))
        row2.addWidget(self.sort_combo)
        row2.addWidget(self.merged_check)
        outer.addLayout(row)
        outer.addLayout(row2)
        # Live aggregate of the current filter (excluding the session choice
        # itself): total items/bytes plus how much each conversation holds, so
        # the size impact of a cleanup decision is visible before selecting.
        self.filter_summary_label = QLabel("当前筛选：未加载记录")
        self.filter_summary_label.setWordWrap(True)
        self.filter_summary_label.setStyleSheet("color: #555;")
        outer.addWidget(self.filter_summary_label)
        return group

    def _on_time_mode_changed(self) -> None:
        self.before_date.setEnabled(self.time_combo.currentData() == "before")

    def _middle_row(self) -> QHBoxLayout:
        row = QHBoxLayout()

        # The album grid replaces the old table: every matching photo is tiled
        # on screen with its thumbnail decoded lazily, and multi-select drives
        # the batch actions.  ``records_table`` used to be a QTableWidget; the
        # name is kept as the grid so callers and tests keep one handle for
        # "the thing showing records".
        self._grid_model = MediaGridModel(self)
        self.records_table = MediaGalleryView()
        self.records_table.setModel(self._grid_model)
        # Right-click menu (preview / open / reveal / export / recycle) and
        # double-click to open: pictures preview their decrypted content,
        # plain files open directly with the OS default application.
        self.records_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.records_table.customContextMenuRequested.connect(
            self._show_grid_context_menu
        )
        self.records_table.doubleClicked.connect(self._on_grid_double_clicked)
        self.records_table.selectionModel().selectionChanged.connect(
            self._on_selection_changed
        )
        self.records_table.visible_range_changed.connect(self._on_visible_range_changed)
        self.records_table.thumb_box_changed.connect(self._on_thumb_box_changed)
        row.addWidget(self.records_table, 3)

        preview_group = QGroupBox("③ 预览与管理")
        preview_layout = QVBoxLayout(preview_group)

        self.selection_label = _StableHintLabel("未选择照片")
        preview_layout.addWidget(self.selection_label)

        # The pane can show either decode product, so the button no longer
        # promises a thumbnail; the combo picks which one is shown.
        self.preview_button = QPushButton("预览图片")
        self.preview_button.clicked.connect(self.request_preview)
        self.preview_button.setEnabled(False)
        self.preview_variant_combo = QComboBox()
        self.preview_variant_combo.addItem("智能（小图用原图）", "auto")
        self.preview_variant_combo.addItem("缩略图（最快）", "thumbnail")
        self.preview_variant_combo.addItem("原图（大图较慢）", "original")
        self.preview_variant_combo.setToolTip(
            "智能：文件较小时（≤1MB）直接解原图显示，更大的图仍用缩略图，"
            "兼顾清晰与速度。\n缩略图/原图则固定使用对应版本。"
        )
        self.preview_label = _ImagePane("未选择照片")
        self.preview_label.setMinimumSize(380, 320)
        self.preview_label.setStyleSheet("border: 1px solid #bbb; background: #fafafa;")
        self.preview_status = _StableHintLabel("")

        preview_layout.addWidget(self.preview_button)
        preview_layout.addWidget(self.preview_variant_combo)
        preview_layout.addWidget(self.preview_label, 1)
        preview_layout.addWidget(self.preview_status)
        # Video clip transport: visible only while a video preview is loaded.
        # (A QPushButton rather than a menu entry, so keyboard users get it too;
        # both labels are whitelisted in packaging/verify_artifact.py.)
        self.clip_play_button = QPushButton("播放")
        self.clip_play_button.setToolTip("播放 / 暂停视频抽样预览（无声）")
        self.clip_play_button.clicked.connect(self.toggle_clip_playback)
        self.clip_play_button.hide()
        preview_layout.addWidget(self.clip_play_button)

        self.recycle_button = QPushButton("移到回收站…")
        self.recycle_button.setEnabled(False)
        self.recycle_button.clicked.connect(self.recycle_selected)
        preview_layout.addWidget(self.recycle_button)
        self.export_button = QPushButton("导出原图…")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_selected)
        preview_layout.addWidget(self.export_button)
        self.export_recycle_button = QPushButton("导出后移入回收站…")
        self.export_recycle_button.setEnabled(False)
        self.export_recycle_button.clicked.connect(self.export_and_recycle_selected)
        preview_layout.addWidget(self.export_recycle_button)

        batch_hint = QLabel("批量（作用于当前多选）：")
        batch_hint.setStyleSheet("color: #555; padding-top: 6px;")
        preview_layout.addWidget(batch_hint)
        self.batch_export_button = QPushButton("批量导出原图…")
        self.batch_export_button.setEnabled(False)
        self.batch_export_button.clicked.connect(self.export_selected_batch)
        preview_layout.addWidget(self.batch_export_button)
        self.batch_recycle_button = QPushButton("批量移到回收站…")
        self.batch_recycle_button.setEnabled(False)
        self.batch_recycle_button.clicked.connect(self.recycle_selected_batch)
        preview_layout.addWidget(self.batch_recycle_button)
        self.batch_export_recycle_button = QPushButton("批量导出后移入回收站…")
        self.batch_export_recycle_button.setEnabled(False)
        self.batch_export_recycle_button.clicked.connect(
            self.export_and_recycle_selected_batch
        )
        preview_layout.addWidget(self.batch_export_recycle_button)

        row.addWidget(preview_group, 2)
        return row

    def _cache_group(self) -> QGroupBox:
        group = QGroupBox("④ 应用缓存（不会触碰微信源文件）")
        layout = QFormLayout(group)

        self.cache_label = QLabel("未连接")
        self.clear_decoded_button = QPushButton("清除解码缓存（预览产物）")
        self.clear_decoded_button.clicked.connect(self.clear_decoded_cache)
        self.clear_all_button = QPushButton("清除全部本地缓存（含密钥缓存）")
        self.clear_all_button.clicked.connect(self.clear_all_cache)
        self.refresh_cache_button = QPushButton("刷新缓存占用")
        self.refresh_cache_button.clicked.connect(self.refresh_cache_status)

        buttons = QHBoxLayout()
        buttons.addWidget(self.refresh_cache_button)
        buttons.addWidget(self.clear_decoded_button)
        buttons.addWidget(self.clear_all_button)
        buttons.addStretch(1)

        layout.addRow("占用", self.cache_label)
        layout.addRow("", buttons)
        return group

    # ------------------------------------------------------------------
    # behaviour
    # ------------------------------------------------------------------
    def _set_status(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def _progress_indeterminate(self) -> None:
        """Show the bar in pulse mode for steps with no observable ratio."""
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()

    def _progress_value(self, percent: int) -> None:
        """Show the bar at a real completion percentage (0..100)."""
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(percent)
        self.progress_bar.show()

    def _progress_hide(self) -> None:
        self.progress_bar.hide()

    def _emit_progress(self, count: int) -> None:
        """Bridge a worker-thread progress callback into a queued signal.

        Called on the worker thread by the blocking call (e.g. the scanner);
        the actual UI update happens in ``progress_slot`` on the UI thread.
        Outside a busy step the bridge is closed and the call is a no-op.
        """
        emit = self._progress_emit
        if emit is not None:
            emit(int(count))

    def set_data_root(self, path: str) -> None:
        self.data_root_edit.setText(path)

    def prefer_account(self, account_id: str) -> bool:
        """Select ``account_id`` if it was detected; report whether it was."""
        if not account_id:
            return False
        index = self.account_combo.findData(account_id)
        if index < 0:
            return False
        self.account_combo.setCurrentIndex(index)
        return True

    def detect_accounts(self) -> tuple[str, ...]:
        root = self.data_root_edit.text().strip()
        if not root:
            # "自动检测" should mean exactly that: with an empty field, look for
            # the conventional locations and a shallow drive sweep before
            # reporting failure and making the user type a path.
            root = find_data_root() or ""
            if root:
                self.data_root_edit.setText(root)
        try:
            options = self._facade.discover(root)
        except FacadeError as exc:
            self.account_combo.clear()
            self._set_status(str(exc))
            return ()
        self.account_combo.clear()
        for index, option in enumerate(options):
            size = _human_bytes(option.database_bytes)
            label = f"{option.account_id}（{option.database_count} 库，{size}"
            label += "，最大）" if option.is_largest else "）"
            self.account_combo.addItem(label, option.account_id)
            if option.is_largest:
                # Default to the largest account: it is the one most likely to be
                # the signed-in primary, and it matches the headless session rule.
                self.account_combo.setCurrentIndex(index)
        self._set_status(f"检测到 {len(options)} 个账号，请选择后点击「连接」")
        return tuple(option.account_id for option in options)

    def connect_session(self) -> bool:
        account = self.account_combo.currentData()
        outcome = self._run_blocking(
            lambda: self._facade.connect(
                self.data_root_edit.text().strip(),
                self._cache_root(),
                account,
            ),
            "正在连接（提取数据库密钥并解密数据库，10 万级账号可能需要几十秒）…",
        )
        if not outcome.get("ok"):
            return False
        self._summary = outcome["value"]
        self.summary_label.setText(
            "已连接：{databases} 个库 / {size}；密钥 {keyed} 个，未取到 {unkeyed} 个".format(
                databases=self._summary.get("database_count", "?"),
                size=_human_bytes(int(self._summary.get("database_bytes", 0))),
                keyed=self._summary.get("keyed_count", "?"),
                unkeyed=self._summary.get("unkeyed_count", "?"),
            )
        )
        if not self._summary.get("keyed_count"):
            self.summary_label.setText(
                self.summary_label.text()
                + "　⚠ 未取得任何数据库密钥：请确认微信已登录并保持运行，"
                "然后重新连接；该账号的媒体将无法预览。"
            )
        self.refresh_cache_status()
        self.load_records()
        # Announce the root that worked so the launcher can remember it; the
        # next launch then opens pre-filled instead of asking for the path.
        self.session_connected.emit(self.data_root_edit.text().strip(), account or "")
        return True

    def load_records(self) -> int:
        hint = (
            f"（上次用时 {self._last_scan_seconds:.0f} 秒）"
            if self._last_scan_seconds is not None
            else ""
        )
        started = time.monotonic()
        outcome = self._run_blocking(
            lambda: (
                self._facade.load_records(progress=self._emit_progress),
                self._facade.session_mapping(),
            ),
            f"正在扫描媒体文件（大账号约 10 万条，需要十几秒）{hint}",
            progress_slot=self._on_scan_progress,
        )
        if not outcome.get("ok"):
            return 0
        self._last_scan_seconds = time.monotonic() - started
        self._scan_seconds_note = f"｜扫描用时 {self._last_scan_seconds:.1f} 秒"
        self._all_records, self._session_mapping = outcome["value"]
        # Availability is resolved lazily (only for displayed rows / active filter)
        # so a 100k-file account does not trigger 100k stat calls up front.
        self._availability = {}
        self.apply_filters()
        return len(self._all_records)

    def _session_display(self, dir_name: str) -> tuple[str, str]:
        """``(display name, kind label)`` for an attach directory.

        Display names come from the keyed session mapping and degrade to the
        hash prefix when a conversation cannot be resolved.
        """
        info = self._session_mapping.get(dir_name) or {}
        name = str(info.get("name") or "")
        if not name:
            name = f"未知会话 {dir_name[:8]}"
        kind = "群聊" if info.get("is_chatroom") else "联系人"
        return name, kind

    def _refresh_session_filter(
        self, records=None
    ) -> dict[str, tuple[int, int]]:
        """Fill the session dropdown from ``records`` plus the chat mapping.

        Only conversations that actually have media on disk are listed, **most
        bytes first under the current filter** (not by all-time totals), and
        every entry carries its share — ``小张［联系人］（123 项，共 1.2 GB)`` —
        so the size impact of a decision is visible while choosing.  Records
        outside the per-chat layout get one unified, selectable entry —
        ``未归属会话`` — right after the total, so they are searchable (the
        combo completes on typing) and manageable like any conversation.  The
        caller's session choice is preserved when it still has matching
        records; otherwise the dropdown falls back to all sessions.

        Returns ``{attach dir: (count, bytes)}`` for the status/summary lines.
        The unattributable share is ``total - sum(stats)`` by construction, so
        the dropdown can never show a total the rows do not add up to.
        """
        source = self._all_records if records is None else records
        stats: dict[str, list] = {}
        unattributed_count = 0
        unattributed_bytes = 0
        for record in source:
            dir_name = session_dir_of(record.file.relative_path)
            if not dir_name:
                unattributed_count += 1
                unattributed_bytes += record.file.byte_size
                continue
            entry = stats.get(dir_name)
            if entry is None:
                stats[dir_name] = [1, record.file.byte_size]
            else:
                entry[0] += 1
                entry[1] += record.file.byte_size
        total_count = len(source)
        total_bytes = sum(record.file.byte_size for record in source)
        previous = ""
        try:
            previous = self.session_combo.currentData() or ""
        except RuntimeError:
            previous = ""
        combo = self.session_combo
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem(f"全部会话（{total_count} 项，共 {_human_bytes(total_bytes)}）", "")
            if unattributed_count:
                combo.addItem(
                    f"{UNATTRIBUTED_LABEL}（{unattributed_count} 项，"
                    f"共 {_human_bytes(unattributed_bytes)}）",
                    UNATTRIBUTED_SESSION,
                )
            for dir_name, (count, byte_size) in sorted(
                stats.items(), key=lambda kv: (-kv[1][1], -kv[1][0], kv[0])
            ):
                name, kind = self._session_display(dir_name)
                combo.addItem(
                    f"{name}［{kind}］（{count} 项，共 {_human_bytes(byte_size)}）",
                    dir_name,
                )
            if previous:
                restored = combo.findData(previous)
                combo.setCurrentIndex(restored if restored >= 0 else 0)
        finally:
            combo.blockSignals(False)
        return {key: (value[0], value[1]) for key, value in stats.items()}

    def _facade_worker(self) -> _FacadeWorker:
        """The window's single long-lived facade worker, started on demand.

        Reused rather than created per call: the upstream session is built on
        this thread and keeps thread-affine native state, so the thread must
        outlive the session (see ``_FacadeWorker``).
        """
        worker = self._worker
        if worker is None:
            worker = self._worker = _FacadeWorker(self)
            worker.start()
        return worker

    def _run_blocking(self, func, busy_message: str, *, progress_slot=None) -> dict:
        """Run ``func`` on the shared worker thread, staying synchronous here.

        The heavy real-data steps (upstream session, full account scan) block
        for seconds to minutes.  Waiting inside a local ``QEventLoop`` keeps
        the caller's control flow synchronous (tests and callers unchanged)
        while Qt keeps painting and delivering input; all buttons are disabled
        so no re-entrant action can start mid-flight, and closing the window
        is refused until the step finishes.  While the step runs the status
        bar shows a pulse progress bar; ``progress_slot`` (optional) receives
        real step counters reported by the worker via ``_emit_progress``.
        """
        loop = QEventLoop()
        outcome: dict = {}

        def _done(value) -> None:
            outcome["ok"] = True
            outcome["value"] = value
            loop.quit()

        def _fail(message: str) -> None:
            outcome["ok"] = False
            outcome["message"] = message
            loop.quit()

        worker = self._facade_worker()
        # Connections are per-call so a stale slot from a previous step cannot
        # fire for a later one; they are torn down again after the job ends.
        worker.succeeded.connect(_done)
        worker.failed.connect(_fail)
        if progress_slot is not None:
            worker.progress.connect(progress_slot)
        self._set_busy(True)
        self._busy_loop = loop
        self._active_worker = worker
        self._busy_message = busy_message
        self._busy_started = time.monotonic()
        self._busy_progress = ""
        self._progress_emit = worker.progress.emit
        self._progress_indeterminate()
        self._set_status(busy_message)
        self._busy_clock.start()
        worker.submit(func)
        try:
            loop.exec()
        finally:
            worker.succeeded.disconnect(_done)
            worker.failed.disconnect(_fail)
            if progress_slot is not None:
                worker.progress.disconnect(progress_slot)
        self._busy_clock.stop()
        self._busy_loop = None
        self._active_worker = None
        self._progress_emit = None
        self._busy_progress = ""
        self._progress_hide()
        self._set_busy(False)
        if not outcome.get("ok"):
            self._set_status(f"操作失败：{outcome.get('message', '未知错误')}")
        # The user may have moved the selection while this step was running;
        # re-fire the auto-preview timer so the visible row ends up shown.
        current = self.selected_record
        if current is not None and self._last_previewed_id != str(current.media_id):
            self._preview_timer.start()
        return outcome

    def _base_candidates(self) -> tuple[list[MediaRecord], str]:
        """Type/size/time/merged-forward narrowing, without session or availability.

        Returns ``(candidates, availability)``.  Session narrowing runs *after*
        availability so the per-conversation byte shares in the dropdown always
        describe the full current filter — otherwise every unselected
        conversation would report zero.  The availability narrowing itself still
        runs before the session cut, so one stat pass serves both the shares
        and the grid.
        """
        media_type = self.type_combo.currentData()
        availability = self.availability_combo.currentData()
        if media_type == "original":
            # 「原图」= 图片族里真正解得开、能导出原图的记录。即使"可用性"
            # 还停在"全部"，也按"原图可用"收窄——这正是这个选项的意义，
            # 与导出按钮的可用性门控用同一个判定。
            if availability == "all":
                availability = "original_available"
        minimum = self.min_mb_spin.value() * 1024 * 1024
        cutoff_floor, cutoff_ceiling = self._time_cutoffs()
        merged_only = self.merged_check.isChecked()

        candidates = []
        for record in self._all_records:
            if media_type == "original":
                if record.media_type is not MediaType.IMAGE:
                    continue
            elif media_type != "all" and record.media_type.value != media_type:
                continue
            if record.file.byte_size < minimum:
                continue
            if cutoff_floor is not None and record.observed_at < cutoff_floor:
                continue
            if cutoff_ceiling is not None and record.observed_at > cutoff_ceiling:
                continue
            if merged_only and not is_merged_forward(record.file.relative_path):
                continue
            candidates.append(record)
        return candidates, availability

    def apply_filters(self, *, preserve_position: bool = False) -> int:
        """Re-filter the grid; recycle/export passes preserve the viewport.

        A user-driven filter change goes back to the top (newest first); a
        re-render after files left the account keeps the scroll anchor so the
        user stays where the action happened.
        """
        candidates, availability = self._base_candidates()
        resolved = self._resolve_availability(candidates, availability)
        # Per-conversation shares under the current filter (session choice
        # excluded): the dropdown lists every conversation with its filtered
        # item count and bytes, most bytes first.
        stats = self._refresh_session_filter(resolved)
        session_dir = ""
        try:
            session_dir = self.session_combo.currentData() or ""
        except RuntimeError:
            session_dir = ""
        if session_dir == UNATTRIBUTED_SESSION:
            selected = [
                record for record in resolved if not is_attributable(record)
            ]
        elif session_dir:
            selected = [
                record
                for record in resolved
                if session_dir_of(record.file.relative_path) == session_dir
            ]
        else:
            selected = resolved
        self._sort_records(selected)
        total = len(selected)
        total_bytes = sum(record.file.byte_size for record in selected)
        # The grid is virtualized, so the whole filtered set is handed to the
        # model; nothing is decoded until a cell scrolls into view.
        saved_scroll: int | None = None
        if preserve_position:
            # Pixel scroll value, captured before the model reset: restoring it
            # afterwards keeps the exact viewport (unlike scrollTo, whose item
            # anchoring drifts on this wrapped grid).  Restored after the
            # summary line below, whose height change is the last reflow.
            saved_scroll = self.records_table.verticalScrollBar().value()
        self._records = tuple(selected)
        self._render_grid(scroll_to_top=not preserve_position)
        self._update_filter_summary(resolved, stats)
        notes = ""
        if session_dir == UNATTRIBUTED_SESSION:
            notes += (
                f"｜{UNATTRIBUTED_LABEL}：按月存放的文件/视频/语音，"
                "无法归属到具体联系人，但可照常预览/导出/回收"
            )
        elif session_dir:
            notes += "｜会话筛选覆盖图片/缩略图（文件、视频、语音按月存放，无法归属会话）"
        if self.merged_check.isChecked():
            notes += "｜仅显示合并转发聊天记录的附件"
        self._set_status(
            f"匹配 {total} 条，共 {_human_bytes(total_bytes)}，已平铺展示"
            "（缩略图随滚动加载）。单击选中、Ctrl/Shift 多选后可批量管理。"
            f"{notes}{self._scan_seconds_note}"
        )
        if saved_scroll is not None and self._grid_model.rowCount():
            bar = self.records_table.verticalScrollBar()
            self.records_table.doItemsLayout()
            bar.setValue(min(saved_scroll, bar.maximum()))
        return len(self._records)

    def _update_filter_summary(
        self, resolved: list[MediaRecord], stats: dict[str, tuple[int, int]]
    ) -> None:
        """Aggregate line under the filters: totals plus the top conversations.

        ``resolved`` is the current filter *before* the session cut, so the
        totals describe all conversations and the top entries are ordered by
        their filtered bytes — the same order as the session dropdown.
        """
        label = getattr(self, "filter_summary_label", None)
        if label is None:
            return
        total_count = len(resolved)
        total_bytes = sum(record.file.byte_size for record in resolved)
        unattributed = total_count - sum(count for count, _size in stats.values())
        unattributed_bytes = total_bytes - sum(size for _count, size in stats.values())
        text = f"当前筛选：{total_count} 条，共 {_human_bytes(total_bytes)}"
        ranked = sorted(stats.items(), key=lambda kv: (-kv[1][1], -kv[1][0], kv[0]))[:3]
        if ranked:
            parts = []
            for dir_name, (count, byte_size) in ranked:
                name, _kind = self._session_display(dir_name)
                parts.append(f"{name}（{count} 项，共 {_human_bytes(byte_size)}）")
            text += "｜占用最多：" + "、".join(parts)
        if unattributed:
            text += (
                f"｜另有 {unattributed} 条{UNATTRIBUTED_LABEL}"
                f"（共 {_human_bytes(unattributed_bytes)}，多为按月存放的文件/视频/语音，"
                "可在会话下拉中单独查看）"
            )
        label.setText(text)

    def _time_cutoffs(self) -> tuple[datetime | None, datetime | None]:
        """Preset date ranges as ``(keep_newer_than, keep_older_than)`` (UTC).

        The ``old*`` presets invert the recent ones: they keep media *older*
        than the span, which is the direction a cleanup pass works in ("show me
        the 90-day-old stuff so I can decide about it").
        """
        key = self.time_combo.currentData()
        now = datetime.now(UTC)
        recent = {
            "d7": timedelta(days=7),
            "d30": timedelta(days=30),
            "d90": timedelta(days=90),
            "y1": timedelta(days=365),
        }
        if key in recent:
            return now - recent[key], None
        older = {
            "old7": timedelta(days=7),
            "old30": timedelta(days=30),
            "old90": timedelta(days=90),
            "old180": timedelta(days=182),
            "old365": timedelta(days=365),
        }
        if key in older:
            return None, now - older[key]
        if key == "before":
            picked = self.before_date.date()
            return None, datetime(
                picked.year(), picked.month(), picked.day(), 23, 59, 59, tzinfo=UTC
            )
        return None, None

    def _sort_records(self, records: list[MediaRecord]) -> None:
        """Order the filtered set according to the sort control (in place)."""
        key = self.sort_combo.currentData()
        if key == "time_asc":
            records.sort(key=lambda record: record.observed_at)
        elif key == "size_desc":
            records.sort(key=lambda record: record.file.byte_size, reverse=True)
        elif key == "size_asc":
            records.sort(key=lambda record: record.file.byte_size)
        elif key == "name_asc":
            records.sort(key=lambda record: self._sort_name(record))
        elif key == "name_desc":
            records.sort(key=lambda record: self._sort_name(record), reverse=True)
        else:
            # Default: newest first, so the visible window shows recent photos.
            records.sort(key=lambda record: record.observed_at, reverse=True)

    @staticmethod
    def _sort_name(record: MediaRecord) -> str:
        """Case-insensitive file name for the name orderings."""
        path = record.file.relative_path
        name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        return name.casefold()

    def _on_sort_changed(self) -> None:
        """Re-order the grid as soon as the sort control changes."""
        if not self._all_records:
            return
        self.apply_filters()

    def _resolve_availability(
        self, candidates: list[MediaRecord], availability: str
    ) -> list[MediaRecord]:
        """Apply the availability filter with progress feedback.

        Every check costs filesystem stats; on a 100k-record account the whole
        pass takes seconds to minutes depending on cache warmth.  We pump the
        event loop between batches so the status bar keeps showing progress,
        and disable the buttons so no re-entrant click can start a second pass.
        """
        if availability == "all":
            return list(candidates)
        if availability in ("original_available", "thumbnail_only"):
            # "原图/缩略图"是图片专属语义。文件、语音、视频的源文件同样
            # "在磁盘上"，但混进这个筛选只会让用户选中后撞上"不是图片"
            # 的报错；在这里按类型收窄，还顺带省掉它们的 stat 调用。
            candidates = [
                record
                for record in candidates
                if record.media_type in (MediaType.IMAGE, MediaType.THUMBNAIL)
            ]
        self._set_busy(True)
        interrupted = False
        selected: list[MediaRecord] = []
        # The pass knows its own denominator, so this is a real percent bar.
        self._progress_value(0)
        try:
            for index, record in enumerate(candidates, start=1):
                try:
                    if self._availability_of(record) == availability:
                        selected.append(record)
                except FacadeError:
                    # The session was closed while we were working; stop cleanly.
                    interrupted = True
                    break
                if index % _AVAILABILITY_PROGRESS_BATCH == 0:
                    percent = index * 100 // len(candidates) if candidates else 100
                    self._progress_value(percent)
                    self._set_status(
                        f"正在检查源文件可用性 {index}/{len(candidates)}（{percent}%）…"
                    )
                    QApplication.processEvents()
        finally:
            self._progress_hide()
            self._set_busy(False)
        if interrupted:
            self._set_status("可用性检查已中止：连接已被关闭")
        return selected

    def _on_batch_progress(self, done: int, total: int, action: str) -> None:
        """Batch progress: 第X张/共Y张 plus a real percent bar (UI-thread slot)."""
        self._busy_progress = f"{action}第 {done}/{total} 张"
        self._progress_value(done * 100 // total if total else 100)
        if self._busy_loop is not None:
            elapsed = int(time.monotonic() - self._busy_started)
            self._set_status(
                f"{self._busy_message}｜{self._busy_progress}（已 {elapsed} 秒）"
            )

    def _on_scan_progress(self, count: int) -> None:
        """Real scan progress: files discovered so far (UI-thread slot)."""
        self._busy_progress = f"已扫描 {count:,} 个文件"
        if self._busy_loop is not None:
            elapsed = int(time.monotonic() - self._busy_started)
            self._set_status(
                f"{self._busy_message}｜{self._busy_progress}（已 {elapsed} 秒）"
            )

    def _tick_busy_clock(self) -> None:
        """Live elapsed-seconds readout while a worker step is in flight.

        A silent multi-second wait feels frozen even when it is not; a ticking
        counter reframes it as honest progress.  A live progress fragment from
        the worker (e.g. the scan's file count) is kept visible between ticks.
        """
        if self._busy_loop is None:
            self._busy_clock.stop()
            return
        elapsed = int(time.monotonic() - self._busy_started)
        fragment = f"｜{self._busy_progress}" if self._busy_progress else ""
        self._set_status(f"{self._busy_message}{fragment}（已 {elapsed} 秒）")

    def _set_busy(self, busy: bool) -> None:
        for button in (
            self.detect_button,
            self.connect_button,
            self.load_button,
            self.apply_button,
            self.preview_button,
            self.clip_play_button,
            self.recycle_button,
            self.export_button,
            self.export_recycle_button,
            self.batch_export_button,
            self.batch_recycle_button,
            self.batch_export_recycle_button,
            self.refresh_cache_button,
            self.clear_decoded_button,
            self.clear_all_button,
        ):
            button.setEnabled(not busy)
        if not busy:
            self._sync_action_buttons()

    def _sync_action_buttons(self) -> None:
        """Light the single/batch actions according to the current selection.

        Single-file actions need exactly one selected cell; batch actions light
        up as soon as anything is selected.  Keeping every enable decision in
        one place means a finished step, a re-filter and a selection change all
        leave the toolbox in the same, correct state.
        """
        if self._busy_loop is not None:
            return
        selected = self.selected_records
        self.selection_label.setText(self._selection_summary(selected))
        single = selected[0] if len(selected) == 1 else None
        exportable = [record for record in selected if self._may_export_original(record)]
        single_export_ok = single is not None and self._can_export_original(single)
        single_video = single is not None and single.media_type is MediaType.VIDEO
        self.preview_button.setEnabled(
            (single is not None and self._is_previewable(single)) or single_video
        )
        # The button names its subject so a video selection does not promise
        # a picture; the frozen self-test only ever sees the default text.
        self.preview_button.setText("预览视频" if single_video else "预览图片")
        self.recycle_button.setEnabled(single is not None)
        self.export_button.setEnabled(single_export_ok)
        self.export_recycle_button.setEnabled(single_export_ok)
        # A greyed-out button must say why: "只有缩略图" is the case the user
        # asked about - clicking export there could never produce an original.
        self._set_export_hint(single, single_export_ok)
        self.batch_export_button.setEnabled(bool(exportable))
        self.batch_recycle_button.setEnabled(bool(selected))
        self.batch_export_recycle_button.setEnabled(bool(exportable))

    def _set_export_hint(self, single: MediaRecord | None, enabled: bool) -> None:
        """Explain a disabled export on the buttons themselves."""
        hint = ""
        if single is not None and not enabled:
            hint = self._export_skip_reason(single) or "该记录类型不支持导出原图"
        if not hint:
            hint = "把这张照片解密后导出到你选择的位置（不修改微信源文件）"
        for button in (self.export_button, self.export_recycle_button):
            button.setToolTip(hint)

    def _can_export_original(self, record: MediaRecord) -> bool:
        """True when this record really has an original that can be exported.

        A record whose availability is ``thumbnail_only`` has no original on
        disk - an export click there could only ever fail, so the buttons stay
        disabled and say why instead.  The availability lookup is cached per
        record, so this costs one stat at most.
        """
        if not self._is_exportable(record):
            return False
        return self._availability_of(record) == "original_available"

    def _may_export_original(self, record: MediaRecord) -> bool:
        """Cheap batch-side check: type plus *cached* availability only.

        A large multi-select must not trigger a stat per file on the UI thread
        just to light a button, so unknown availability counts as "maybe"; the
        worker resolves it and reports those files as skipped with a reason.
        """
        if not self._is_exportable(record):
            return False
        cached = self._availability.get(record.media_id)
        return cached is None or cached == "original_available"

    def _export_skip_reason(self, record: MediaRecord) -> str:
        """Why this record cannot be exported as an original ('' when it can).

        Like :meth:`_may_export_original`, this reads only cached state so the
        click stays instant; the worker resolves whatever is still unknown.
        """
        if record.media_type is MediaType.THUMBNAIL:
            return "仅有缩略图，无原图可导出"
        if not self._is_exportable(record):
            return "非图片，无原图可导出"
        cached = self._availability.get(record.media_id)
        if cached is None:
            return ""
        return _UNAVAILABLE_EXPORT_REASONS.get(cached, "")

    def _selection_summary(self, selected: list[MediaRecord]) -> str:
        if not selected:
            return "未选择照片（单击选中；Ctrl/Shift 可多选）"
        if len(selected) == 1:
            record = selected[0]
            state = _AVAILABILITY_LABELS.get(
                self._availability.get(record.media_id, "unknown"), "未知"
            )
            stem = record.file.relative_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            return f"已选择 1 项（{state}）：{stem}\n{self._conversation_of(record)}"
        total = sum(record.file.byte_size for record in selected)
        return f"已选择 {len(selected)} 项，共 {_human_bytes(total)}"

    def _conversation_of(self, record: MediaRecord) -> str:
        """Which chat this record came from, as a user-facing phrase.

        The attach directory name is ``md5(username)`` and the keyed mapping
        resolves it back to a display name, so this is exact, not a guess.
        Types stored by month (files, videos, voice notes) carry no
        conversation attribute at all and say so instead of pretending.
        """
        dir_name = session_dir_of(record.file.relative_path)
        if not dir_name:
            return (
                f"「{UNATTRIBUTED_LABEL}」：该类型按月份存放，"
                "无法归属到具体会话，可在会话筛选中统一查看"
            )
        chat = self._session_mapping.get(dir_name) or {}
        name = str(chat.get("name") or "")
        username = str(chat.get("username") or "")
        if not name and not username:
            return f"未知会话（目录 {dir_name[:8]}…）"
        kind = "群聊" if chat.get("is_chatroom") else "单聊"
        if is_merged_forward(record.file.relative_path):
            return f"来自「{name or username}」（{kind}）的合并转发记录"
        return f"来自「{name or username}」（{kind}）"

    @staticmethod
    def _is_exportable(record: MediaRecord) -> bool:
        """True for records that can produce an original at all.

        Only true images qualify.  A "缩略图" record *is* the small file — its
        availability is ``thumbnail_only`` by definition — so it must never
        light an export button, and a selection made entirely of thumbnails
        must leave every batch export greyed out instead of promising N skips.
        """
        return record.media_type is MediaType.IMAGE

    @staticmethod
    def _is_previewable(record: MediaRecord) -> bool:
        return record.media_type in (MediaType.IMAGE, MediaType.THUMBNAIL)

    def _availability_of(self, record: MediaRecord) -> str:
        """Resolve availability lazily and cache it; never re-stat the same record."""
        cached = self._availability.get(record.media_id)
        if cached is None:
            cached = self._availability[record.media_id] = self._facade.availability(record)
        return cached

    def _render_grid(self, *, scroll_to_top: bool = True) -> None:
        """Hand the filtered records to the grid model and warm what is visible.

        The old table filled one widget row per record and had to cap the count
        at 2000 to stay responsive.  A virtualized model has no such cost: the
        full set goes in, and only the cells the user can see get decoded.

        A fresh filter jumps back to the top (the list is newest-first); after
        a recycle/export the caller restores the saved pixel scroll value
        (see ``apply_filters``), so the viewport stays where the action
        happened instead of jumping.
        """
        self._record_by_id = {str(record.media_id): record for record in self._records}
        # Grid cells always show the thumbnail variant; the "original" preview
        # choice only affects the detail pane.
        self._warm_variant = "thumbnail"
        self._warm_decode_edge = self.records_table.decode_edge()
        self._grid_model.set_records(self._records)
        # Records that can never decode (files, voice notes) must never carry
        # a failure mark: one heals stale marks left by the neighbourhood
        # warmer before it learned to skip them (see ``_queue_warm``).
        self._grid_model.clear_unsupported_failures()
        # A filter changes which items exist under the old scroll offset.  Left
        # alone, the offset clamps to the end of the new (shorter) list, so the
        # user lands on the oldest media; the list is newest-first, so go back
        # to the top and the visible window is well defined.
        if scroll_to_top:
            self.records_table.scrollToTop()
        # Drop pre-filter work first: those decodes belong to cells that may no
        # longer exist, and they would hold pool slots the visible cells need.
        self._reset_warm_queue()
        # Then re-derive what is visible.  This must happen *after* the scroll
        # reset: right after a model reset the viewport still describes the
        # pre-reset layout, which is how the wrong end of the list used to be
        # queued while the user looked at the other one.
        self.records_table.invalidate_visible_range()
        self._warm_visible_cells()
        self._sync_action_buttons()

    @property
    def displayed_media_ids(self) -> tuple[str, ...]:
        return tuple(str(record.media_id) for record in self._records)

    @property
    def selected_records(self) -> list[MediaRecord]:
        """Every record currently selected in the grid, in display order."""
        indexes = self.records_table.selectionModel().selectedIndexes()
        records = []
        for index in sorted(indexes, key=lambda item: item.row()):
            record = self._grid_model.record_at(index.row())
            if record is not None:
                records.append(record)
        return records

    @property
    def selected_record(self) -> MediaRecord | None:
        """The single selected record, or ``None`` when the selection is not one."""
        selected = self.selected_records
        return selected[0] if len(selected) == 1 else None

    def select_row(self, row: int) -> None:
        """Select one grid cell by position (also used by the tests)."""
        index = self._grid_model.index(row, 0)
        if not index.isValid():
            return
        self.records_table.setCurrentIndex(index)

    def select_rows(self, rows) -> None:
        """Select several grid cells at once (the Ctrl-click equivalent)."""
        selection = self.records_table.selectionModel()
        selection.clearSelection()
        valid = [row for row in rows if self._grid_model.index(row, 0).isValid()]
        if not valid:
            return
        for row in valid:
            index = self._grid_model.index(row, 0)
            selection.select(index, QItemSelectionModel.Select)
        # Setting the current index last keeps Qt's notion of "current" in sync
        # without collapsing the multi-selection that was just built.
        selection.setCurrentIndex(
            self._grid_model.index(valid[-1], 0), QItemSelectionModel.NoUpdate
        )

    def select_all_cells(self) -> None:
        """Select every cell in the grid (the Ctrl+A equivalent)."""
        selection = self.records_table.selectionModel()
        count = self._grid_model.rowCount()
        if count == 0:
            return
        selection.clearSelection()
        for row in range(count):
            selection.select(self._grid_model.index(row, 0), QItemSelectionModel.Select)

    def _on_selection_changed(self, *args) -> None:
        self._sync_action_buttons()
        selected = self.selected_records
        record = selected[0] if len(selected) == 1 else None
        # A clip belongs to exactly one video: leaving it stops playback and,
        # unless the new selection is that same video, drops its frames.
        if self._last_clip_id is not None and (
            record is None or str(record.media_id) != self._last_clip_id
        ):
            self._stop_clip()
        if record is not None and (
            self._is_previewable(record) or record.media_type is MediaType.VIDEO
        ):
            # Pictures auto-decode; videos auto-load only their cheap cover
            # (the ffmpeg sampling pass stays an explicit click).
            self._preview_timer.start()
        else:
            self._preview_timer.stop()
        if record is not None and self._is_previewable(record):
            pass  # the pane updates when the decoded picture lands
        elif (
            record is not None
            and record.media_type is MediaType.VIDEO
            and self._clip_frames
            and self._last_clip_id == str(record.media_id)
        ):
            pass  # the running clip stays on screen untouched
        else:
            if not selected:
                self.preview_label.show_message("未选择照片")
                self.preview_status.setText("未选择照片")
            elif len(selected) > 1:
                # Never keep the previous single picture on screen behind a
                # multi-selection: the pane shows what is selected, nothing else.
                total = sum(item.file.byte_size for item in selected)
                self.preview_label.show_message(
                    f"已选择 {len(selected)} 项，共 {_human_bytes(total)}\n"
                    "右侧大图一次只显示一张，请单击其中一张再预览"
                )
                self.preview_status.setText(
                    f"已选择 {len(selected)} 项，共 {_human_bytes(total)}"
                )
            elif record is not None and record.media_type is MediaType.VIDEO:
                self.preview_label.show_message("视频：封面加载中…也可直接点击「播放视频」")
                self.preview_status.setText("视频可预览：封面加载中，播放为无声抽样预览")
                self.clip_play_button.setText("播放视频")
                self.clip_play_button.show()
            else:
                self.preview_label.show_message("该类型暂不支持预览")
                self.preview_status.setText(
                    "该记录不是图片（文件 / 语音暂不支持预览，可右键直接打开）"
                )
        selected = self.selected_records
        if selected:
            # Warm the cells around the selection so stepping through the album
            # lands on already-decoded pictures.
            row = self._grid_model.index_of_record(selected[0])
            if row >= 0:
                self._queue_neighbourhood_warm(row)
        self._warm_visible_cells()

    # ------------------------------------------------------------------
    # grid context menu + double-click: preview for pictures, direct open
    # for plain files (their source directory holds them unencrypted, so no
    # decoding step is needed).  Removal here is the same audited
    # move-to-Recycle-Bin behind an explicit confirmation — never a delete.
    # ------------------------------------------------------------------
    def _show_grid_context_menu(self, position) -> None:
        """Right-click menu over the album grid (position in viewport coords)."""
        selected = self.selected_records
        if not selected:
            return
        menu = QMenu(self)
        single = selected[0] if len(selected) == 1 else None

        if single is not None and self._is_previewable(single):
            preview_action = QAction("预览图片", menu)
            preview_action.triggered.connect(self.request_preview)
            menu.addAction(preview_action)
        elif single is not None and single.media_type is MediaType.VIDEO:
            video_action = QAction("预览视频", menu)
            video_action.triggered.connect(self.request_preview)
            menu.addAction(video_action)

        open_action = QAction(
            "打开" if single is not None else f"打开这 {len(selected)} 项",
            menu,
        )
        open_action.setToolTip(
            "图片打开其解密预览；压缩包/文档/视频等直接用系统默认应用打开源文件"
        )
        open_action.triggered.connect(self.open_selected_sources)
        menu.addAction(open_action)

        if single is not None:
            reveal_action = QAction("打开所在文件夹", menu)
            reveal_action.triggered.connect(self.reveal_selected_in_folder)
            menu.addAction(reveal_action)

        menu.addSeparator()
        if single is not None and self._can_export_original(single):
            export_action = QAction("导出原图…", menu)
            export_action.triggered.connect(self.export_selected)
            menu.addAction(export_action)
        elif len(selected) > 1 and any(
            self._may_export_original(record) for record in selected
        ):
            batch_export_action = QAction("批量导出原图…", menu)
            batch_export_action.triggered.connect(self.export_selected_batch)
            menu.addAction(batch_export_action)

        if len(selected) == 1:
            recycle_action = QAction("移到回收站…", menu)
            recycle_action.triggered.connect(self.recycle_selected)
            menu.addAction(recycle_action)
        else:
            batch_recycle_action = QAction(
                f"批量移到回收站…（{len(selected)} 项）", menu
            )
            batch_recycle_action.triggered.connect(self.recycle_selected_batch)
            menu.addAction(batch_recycle_action)

        viewport = self.records_table.viewport()
        menu.exec(viewport.mapToGlobal(position))

    def _on_grid_double_clicked(self, index) -> None:
        """Double-click: pictures and videos preview in-app, the rest opens directly."""
        if not index.isValid():
            return
        record = self._grid_model.record_at(index.row())
        if record is None:
            return
        if self._is_previewable(record) or record.media_type is MediaType.VIDEO:
            self.select_row(index.row())
            self.request_preview()
        else:
            self.open_source_file(record)

    def open_source_file(self, record: MediaRecord | None = None) -> bool:
        """Open one record's content with the OS default application.

        Pictures open their *decrypted preview* (the ``.dat`` source is
        encrypted and meaningless to other programs); plain files — archives,
        documents, videos, voice notes — live unencrypted under the account
        directory and open directly.  Status lines only ever name the relative
        path.  Returns whether an open was launched.
        """
        target = record if record is not None else self.selected_record
        if target is None:
            self._set_status("请先在相册中选中一条记录")
            return False
        if self._is_previewable(target):
            result = self._run_blocking(
                lambda: self._facade.preview(target, variant="original", max_edge_px=512),
                "正在解密原图并用系统应用打开…",
            )
            if not result.get("ok"):
                self._set_status(f"打开失败：{result.get('message', '未知错误')}")
                return False
            outcome: PreviewOutcome = result["value"]
            if not outcome.ok:
                self.preview_status.setText(
                    f"失败原因：{outcome.error_code}｜{outcome.error_message}"
                )
                return False
            if _open_local_path(outcome.output_path):
                self._set_status(f"已用系统应用打开：{target.file.relative_path}")
                return True
            self._set_status("系统应用打开失败：未找到可打开该预览的应用")
            return False
        try:
            source = self._facade.source_path(target)
        except FacadeError as exc:
            self._set_status(f"打开失败：{exc}")
            return False
        if _open_local_path(source):
            self._set_status(f"已用系统应用打开：{target.file.relative_path}")
            return True
        self._set_status("系统应用打开失败：未找到可打开该文件的应用")
        return False

    def open_selected_sources(self) -> bool:
        """Open every selected record (batch ``打开``); failures are reported."""
        selected = self.selected_records
        if not selected:
            self._set_status("请先在相册中选中至少一条记录")
            return False
        ok = True
        for record in selected:
            if self._is_previewable(record):
                # Pictures each need their own blocking decode; reuse the
                # single path so progress and failure reasons stay consistent.
                if not self.open_source_file(record):
                    ok = False
            else:
                try:
                    source = self._facade.source_path(record)
                except FacadeError as exc:
                    self._set_status(f"打开失败：{exc}")
                    ok = False
                    continue
                if not _open_local_path(source):
                    ok = False
        if ok:
            self._set_status(f"已打开 {len(selected)} 项")
        else:
            self._set_status("部分文件未能打开（已打开其余项）")
        return ok

    def reveal_selected_in_folder(self) -> bool:
        """Open the source's containing folder with the file highlighted."""
        record = self.selected_record
        if record is None:
            self._set_status("请先在相册中选中一条记录")
            return False
        try:
            source = self._facade.source_path(record)
        except FacadeError as exc:
            self._set_status(f"打开所在文件夹失败：{exc}")
            return False
        if _reveal_local_path(source):
            self._set_status(f"已打开所在文件夹：{record.file.relative_path}")
            return True
        self._set_status("打开所在文件夹失败")
        return False

    # ------------------------------------------------------------------
    # lazy thumbnail loading: decode what is on screen, keep scrolling warm
    # ------------------------------------------------------------------
    def _on_visible_range_changed(self, first: int, last: int) -> None:
        """Queue the cells that just scrolled into view."""
        self._warm_visible_cells()

    def set_thumb_size(self, edge: int) -> bool:
        """Set the album tile size (public entry point, used at startup)."""
        return self.records_table.set_thumb_box(edge)

    def thumb_size(self) -> int:
        """Current album tile size, in pixels."""
        return self.records_table.thumb_box()

    def _on_thumb_box_changed(self, edge: int) -> None:
        """Follow a Ctrl+wheel zoom: re-decode at the new resolution if needed.

        Zooming *out* needs nothing - a larger decode shrinks losslessly.
        Zooming *in* past what was decoded re-queues the visible cells at the
        higher resolution, but **keeps every picture already on screen**: the
        slightly soft image stays until the sharper decode replaces it.  The
        first cut dropped all pixmaps here, which read as "every photo
        vanished while zooming".
        """
        self._warm_decode_edge = self.records_table.decode_edge()
        if edge > self._last_thumb_box:
            with self._warm_lock:
                self._warmed.clear()
                self._warm_failed.clear()
            self._reset_warm_queue()
        self._last_thumb_box = edge
        self._warm_visible_cells()
        self.thumbnail_size_changed.emit(edge)
        self._set_status(f"缩略图大小：{edge} 像素（Ctrl+滚轮调节）")

    def _warm_visible_cells(self) -> None:
        first, last = self.records_table.visible_range()
        if last < first:
            return
        window = [
            record
            for record in (
                self._grid_model.record_at(row) for row in range(first, last + 1)
            )
            if record is not None
            # Files and voice notes can never get pixels; queueing them only
            # burns decode attempts that fail into the "无预览" placeholder.
            if decode_supported(record.media_type.value)
        ]
        self._queue_warm(window)
        # A fast scroll can queue far more than one screen; keep the cells the
        # user is looking at at the front of the queue.
        self._order_warm_queue_by_visibility()

    def _order_warm_queue_by_visibility(self) -> None:
        """Reorder pending decodes so the nearest-to-visible cells come first.

        A fast scroll (or a filter) can queue hundreds of cells at once;
        decoding them in arrival order would finish the far end last, which is
        exactly where the user is not looking.  Cells are sorted by their
        distance from the visible window, so the ones on screen are always the
        next to be decoded no matter how long the backlog is.

        The rebuild happens under the queue lock, so a decode finishing on a
        worker thread cannot mutate the deque mid-reorder.
        """
        first, last = self.records_table.visible_range()
        if last < first:
            return
        with self._warm_lock:
            if len(self._warm_queue) < 2:
                return
            ordered = sorted(self._warm_queue, key=lambda record: self._row_distance(
                record, first, last
            ))
            self._warm_queue.clear()
            self._warm_queue.extend(ordered)
        self._pump_warm_queue()

    def _row_distance(self, record: MediaRecord, first: int, last: int) -> int:
        """Rows from ``record`` to the visible window (``0`` when inside it).

        A record that is not part of the current filter sorts behind everything
        on screen: it may belong to a stale queue entry, and decoding it now
        would only delay a cell the user can actually see.
        """
        row = self._grid_model.index_of_record(record)
        if row < 0:
            return _WARM_FAR_DISTANCE
        if first <= row <= last:
            return 0
        return min(abs(row - first), abs(row - last))

    def _window_alive(self) -> bool:
        """False once Qt has destroyed the underlying C++ window object.

        A pending single-shot timer can fire after teardown (close-then-fire
        race in tests or fast closes); calling any Qt method on a destroyed
        window raises ``RuntimeError``, which we treat as "gone".
        """
        try:
            _visible = self.isVisible()
        except RuntimeError:
            return False
        return True

    def _auto_preview(self) -> None:
        """Preview-on-selection: no extra click needed.

        Artifacts are cached per media id inside the app cache, so revisiting
        a row is instant; the user explicitly opted into a generous cache.
        A row already previewed this session is skipped, and while a worker
        step is running the busy-end hook re-fires for the current selection.
        """
        if not self._window_alive():
            return
        if self._busy_loop is not None:
            return
        # The detail pane previews one photo; with a multi-selection there is
        # no single subject, so auto-preview stays out of the way.
        if len(self.selected_records) != 1:
            return
        record = self.selected_record
        if record is None or not self.preview_button.isEnabled():
            return
        if record.media_type is MediaType.VIDEO:
            # Videos auto-load only their cheap cached cover on selection;
            # the ffmpeg sampling pass behind「预览视频」stays an explicit click.
            if self._last_previewed_id == str(record.media_id):
                return
            self._request_video_cover(record)
            return
        if not self._is_previewable(record):
            return
        if self._last_previewed_id == str(record.media_id):
            return
        self.request_preview()

    # ------------------------------------------------------------------
    # background preview warming
    # ------------------------------------------------------------------
    def _reset_warm_queue(self) -> None:
        """Drop queued-but-unstarted warming for a new grid render.

        In-flight decodes finish on their own; re-queuing the same cell later
        is harmless (an instant cache hit).
        """
        with self._warm_lock:
            self._warm_queue.clear()
            self._warm_queued.clear()
        self._pump_warm_queue()

    def _queue_warm(self, records, *, front: bool = False) -> None:
        """Enqueue records for background thumbnail decoding (dedup by id).

        Records that can never decode (files, voice notes) are skipped here —
        the single choke point for every queueing path.  Queueing them would
        fail each one into a "无法预览" mark and bury its type icon, which is
        exactly what clicking a file used to do to all of its neighbours.
        """
        if self._warm_stopped:
            return
        with self._warm_lock:
            for record in records:
                if not decode_supported(record.media_type.value):
                    continue
                media_id = str(record.media_id)
                if (
                    media_id in self._warm_queued
                    or media_id in self._warmed
                    or media_id in self._warm_failed
                    or media_id == self._last_previewed_id
                    or self._grid_model.has_thumbnail(media_id)
                ):
                    continue
                self._warm_queued.add(media_id)
                if front:
                    self._warm_queue.appendleft(record)
                else:
                    self._warm_queue.append(record)
        self._pump_warm_queue()

    def _pump_warm_queue(self) -> None:
        """Keep the pool fed; safe to call from any thread (no Qt UI access).

        Starting a runnable happens outside the lock so a decode that finishes
        immediately cannot re-enter ``_warm_finish`` while this thread still
        holds the queue lock.
        """
        if self._warm_stopped:
            return
        while True:
            with self._warm_lock:
                if self._warm_stopped:
                    return
                if not self._warm_queue or self._warm_inflight >= _WARM_POOL_SIZE:
                    return
                record = self._warm_queue.popleft()
                self._warm_queued.discard(str(record.media_id))
                self._warm_inflight += 1
            runnable = _ThumbRunnable(self, record)
            # Auto-delete is off, so the pool will not free this; keep it alive
            # until its decode reports back, otherwise Python collects the
            # wrapper while the worker is still using it.
            with self._warm_lock:
                self._warm_live[str(record.media_id)] = runnable
            try:
                self._warm_pool.start(runnable)
            except RuntimeError:
                # The window (and with it the pool) is already destroyed.
                with self._warm_lock:
                    self._warm_inflight -= 1
                    self._warm_live.pop(str(record.media_id), None)
                self._warm_stopped = True
                return

    def _warm_finish(self, record: MediaRecord, *, ok: bool) -> None:
        """Settle one decode's bookkeeping; callable from any thread.

        Must run *before* the result is handed to the UI thread, so the cell
        already counts as warm when a repaint re-examines the visible range.
        """
        media_id = str(record.media_id)
        with self._warm_lock:
            self._warm_inflight -= 1
            # Release the runnable now that its decode is done; the pool no
            # longer needs it and the worker has left ``run()``.
            self._warm_live.pop(media_id, None)
            # The id leaves the "queued" set either way, so the UI's reorder
            # pass never resurrects a decode that has already finished.
            self._warm_queued.discard(media_id)
            if ok:
                self._warmed.add(media_id)
            else:
                # A cell that failed to decode must not be retried on every
                # scroll; it keeps its placeholder and is never queued again.
                self._warm_failed.add(media_id)
                # Tell the model too, or the cell keeps spinning "载入中…"
                # forever (videos the backend cannot cover, for example).
                # Plain state + queued signal, safe from any thread.
                self._grid_model.apply_thumbnail(media_id, None)
        try:
            self._pump_warm_queue()
        except RuntimeError:
            pass


    def _apply_thumbnail_path(self, media_id: str, path: str) -> None:
        """UI-thread slot: load a decoded thumbnail and show it in its cell.

        Runs on the GUI thread (queued signal from a decode worker), which is
        the only place a ``QPixmap`` may be constructed.
        """
        if self._warm_stopped:
            return
        pixmap = QPixmap(path)
        self._grid_model.apply_thumbnail(media_id, None if pixmap.isNull() else pixmap)

    def _queue_neighbourhood_warm(self, row: int) -> None:
        """Warm the cells around the selection first (album-browsing feel)."""
        count = self._grid_model.rowCount()
        neighbours = []
        for delta in range(1, _WARM_NEIGHBOURHOOD + 1):
            for candidate in (row + delta, row - delta):
                if not 0 <= candidate < count:
                    continue
                record = self._grid_model.record_at(candidate)
                if record is not None:
                    neighbours.append(record)
        self._queue_warm(neighbours, front=True)

    def recycle_selected(self) -> RecycleOutcome | None:
        """Move the selected record's source file to the Recycle Bin.

        Exactly one file per invocation, behind an explicit confirmation; the
        facade re-verifies the on-disk identity and journalizes a receipt.
        """
        record = self.selected_record
        if record is None or self._busy_loop is not None:
            return None
        answer = QMessageBox.question(
            self,
            "移到回收站",
            "将把该文件移入系统回收站（不会直接删除，可随时在回收站还原）：\n\n"
            f"{record.file.relative_path}\n"
            f"大小：{_human_bytes(record.file.byte_size)}\n\n确认执行？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            self._set_status("已取消：文件未做任何改动")
            return None
        result = self._run_blocking(
            lambda: self._facade.recycle_record(record),
            "正在移入回收站…",
        )
        if not result.get("ok"):
            message = str(result.get("message", ""))[:200]
            self._set_status(f"移入回收站失败：{message}")
            return RecycleOutcome(
                ok=False,
                error_code="RECYCLE_FAILED",
                error_message=message,
                relative_path=record.file.relative_path,
                byte_size=record.file.byte_size,
            )
        outcome: RecycleOutcome = result["value"]
        if not outcome.ok:
            self.preview_status.setText(f"移入回收站失败：{outcome.error_message}")
            self._set_status(f"移入回收站失败：{outcome.error_code}｜{outcome.error_message}")
            return outcome
        self._all_records = tuple(
            item for item in self._all_records if item.media_id != record.media_id
        )
        self._availability.pop(record.media_id, None)
        self._last_previewed_id = None
        self.preview_label.show_message("已移入回收站")
        self.preview_status.setText(f"已移入回收站：{outcome.relative_path}")
        self.apply_filters(preserve_position=True)
        self._set_status(
            f"已移入回收站：{outcome.relative_path}"
            f"（{_human_bytes(outcome.byte_size)}，可在系统回收站还原）"
        )
        return outcome

    # ------------------------------------------------------------------
    # original export (user-chosen destination; source only touched by the
    # explicit recycle step which follows an explicit confirmation)
    # ------------------------------------------------------------------
    def _image_export_gate(self, record: MediaRecord | None) -> bool:
        if record is None or self._busy_loop is not None:
            return False
        if record.media_type not in (MediaType.IMAGE, MediaType.THUMBNAIL):
            self.preview_status.setText("该记录不是图片，无法导出原图")
            self._set_status("导出取消：视频 / 文件 / 语音没有可导出的原图")
            return False
        # Second line of defence behind the disabled button: a record without
        # an original can only fail, so it is refused with a readable reason.
        state = self._availability_of(record)
        if state != "original_available":
            reason = _UNAVAILABLE_EXPORT_REASONS.get(
                state, f"源文件状态异常（{state}），无原图可导出"
            )
            self.preview_status.setText(f"导出取消：{reason}")
            self._set_status(f"导出取消：{reason}")
            return False
        return True

    def _choose_export_target(self, record: MediaRecord) -> Path | None:
        """Ask for the export destination; remember the folder for next time."""
        start_dir = self._last_export_dir or str(Path.home() / "Pictures")
        if not Path(start_dir).is_dir():
            start_dir = str(Path.home())
        suggested = _export_suggested_name(record.file.relative_path)
        path, _selected = QFileDialog.getSaveFileName(
            self,
            "选择原图导出位置",
            str(Path(start_dir) / suggested),
            "图片 (*.jpg *.jpeg *.png *.gif *.bmp *.webp);;所有文件 (*.*)",
        )
        if not path:
            return None
        self._last_export_dir = str(Path(path).parent)
        return Path(path)

    def _export_to_path(self, record: MediaRecord, target: Path) -> ExportOutcome:
        """Worker-side: decrypt, then write to ``target`` with a fitting suffix.

        The suggested dialog name is ``.jpg`` because most originals are JPEG;
        when the payload is actually PNG/other the written file gets the real
        extension so the file opens correctly everywhere.
        """
        outcome = self._facade.export_original(record)
        if not outcome.ok:
            return outcome
        suffix = _FORMAT_EXTENSIONS.get(outcome.image_format.casefold(), ".jpg")
        final = target if target.suffix.casefold() == suffix else target.with_suffix(suffix)
        try:
            final.write_bytes(outcome.payload)
        except OSError as exc:
            return ExportOutcome(
                ok=False,
                error_code="EXPORT_WRITE_FAILED",
                error_message=f"写入导出文件失败：{type(exc).__name__}",
                relative_path=record.file.relative_path,
            )
        return replace(outcome, written_path=str(final))

    def _export_fidelity_note(self, outcome: ExportOutcome) -> str:
        return "" if outcome.is_original else "（源文件主体不完整，导出的是其内嵌缩略图）"

    def export_selected(self) -> ExportOutcome | None:
        record = self.selected_record
        if not self._image_export_gate(record):
            return None
        assert record is not None  # narrow for the type checker
        target = self._choose_export_target(record)
        if target is None:
            self._set_status("已取消导出，未写入任何文件")
            return None
        result = self._run_blocking(
            lambda: self._export_to_path(record, target),
            "正在解密原图并写入导出文件（大图约需一至五秒）…",
        )
        if not result.get("ok"):
            message = str(result.get("message", ""))[:200]
            self._set_status(f"导出失败：{message}")
            return ExportOutcome(
                ok=False, error_code="EXPORT_FAILED", error_message=message
            )
        outcome: ExportOutcome = result["value"]
        if not outcome.ok:
            self.preview_status.setText(f"导出失败：{outcome.error_message}")
            self._set_status(f"导出失败：{outcome.error_code}｜{outcome.error_message}")
            return outcome
        note = self._export_fidelity_note(outcome)
        size = _human_bytes(outcome.byte_size)
        self.preview_status.setText(
            f"已导出：{outcome.written_path}"
            f"（{outcome.image_format}，{outcome.width}×{outcome.height}，{size}）{note}"
        )
        self._set_status(f"已导出原图：{outcome.written_path}（{size}）{note}")
        return outcome

    def export_and_recycle_selected(self) -> RecycleOutcome | None:
        """Export the original first, then move the source to the Recycle Bin.

        Ordering matters: the export must be safely on disk before the source
        leaves the account directory, and a recycle failure never undoes the
        export - the user keeps the copy and the failure is reported honestly.
        """
        record = self.selected_record
        if not self._image_export_gate(record):
            return None
        assert record is not None  # narrow for the type checker
        answer = QMessageBox.question(
            self,
            "导出后移入回收站",
            "将按顺序执行两步：\n"
            "1) 解密原图并导出到你选择的位置；\n"
            "2) 导出成功后，把微信源文件移入系统回收站"
            "（不会直接删除，可在回收站还原）：\n\n"
            f"{record.file.relative_path}\n"
            f"大小：{_human_bytes(record.file.byte_size)}\n\n确认执行？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            self._set_status("已取消：文件未做任何改动")
            return None
        target = self._choose_export_target(record)
        if target is None:
            self._set_status("已取消：未选择导出位置，文件未做任何改动")
            return None

        def _export_then_recycle() -> tuple[ExportOutcome, RecycleOutcome | None]:
            export = self._export_to_path(record, target)
            if not export.ok:
                return export, None
            return export, self._facade.recycle_record(record)

        result = self._run_blocking(
            _export_then_recycle,
            "正在解密原图并导出，随后移入回收站…",
        )
        if not result.get("ok"):
            message = str(result.get("message", ""))[:200]
            self._set_status(f"操作失败：{message}")
            return RecycleOutcome(
                ok=False, error_code="EXPORT_FAILED", error_message=message
            )
        export, recycle = result["value"]
        if not export.ok:
            self.preview_status.setText(f"导出失败：{export.error_message}")
            self._set_status(f"导出失败：{export.error_code}｜{export.error_message}")
            return None
        note = self._export_fidelity_note(export)
        if recycle is None or not recycle.ok:
            reason = recycle.error_message if recycle is not None else "回收站步骤未返回结果"
            self.preview_status.setText(
                f"已导出：{export.written_path}{note}；但移入回收站失败：{reason}"
            )
            self._set_status(f"导出成功，但移入回收站失败：{reason}")
            return RecycleOutcome(
                ok=False,
                error_code=recycle.error_code if recycle is not None else "RECYCLE_FAILED",
                error_message=reason,
                relative_path=record.file.relative_path,
                byte_size=record.file.byte_size,
            )
        self._all_records = tuple(
            item for item in self._all_records if item.media_id != record.media_id
        )
        self._availability.pop(record.media_id, None)
        self._last_previewed_id = None
        self.preview_label.show_message("已导出并移入回收站")
        self.preview_status.setText(
            f"已导出：{export.written_path}（{_human_bytes(export.byte_size)}）{note}；"
            f"源文件已移入回收站：{recycle.relative_path}"
        )
        self.apply_filters(preserve_position=True)
        self._set_status(
            f"已导出原图并移入回收站：{export.written_path}"
            f"（源文件可在系统回收站还原）"
        )
        return recycle

    # ------------------------------------------------------------------
    # batch management: every action runs many single-file operations, each
    # behind one up-front confirmation, each audited individually
    # ------------------------------------------------------------------
    def _batch_selection_gate(self, records, *, need_image: bool) -> list[MediaRecord]:
        """Validate a batch selection; splits out non-image records.

        Videos, voice notes and documents have no exportable original, so an
        export batch skips them with an explicit reason instead of failing
        silently or aborting the whole run.
        """
        if not records:
            self._set_status("请先在相册中选中至少一张照片（Ctrl/Shift 可多选）")
            return []
        if not need_image:
            return list(records)
        usable = [record for record in records if self._is_exportable(record)]
        if not usable:
            self._set_status("选中的都不是图片：视频 / 文件 / 语音没有可导出的原图")
        return usable

    def _confirm_batch(self, action: str, records, extra: str = "") -> bool:
        total = sum(record.file.byte_size for record in records)
        answer = QMessageBox.question(
            self,
            action,
            f"将对选中的 {len(records)} 个文件执行「{action}」：\n"
            f"合计 {_human_bytes(total)}\n{extra}\n"
            "不会直接删除，移入回收站的文件可随时在系统回收站还原。\n\n确认执行？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def export_selected_batch(self) -> BatchOutcome | None:
        """Export every selected image's original into one chosen folder.

        Records that cannot produce an original are *reported*, never silently
        dropped: the summary carries how many were skipped and why, so a batch
        over mixed content answers "how many of these only had a thumbnail".
        """
        candidates = self._batch_selection_gate(self.selected_records, need_image=True)
        if not candidates:
            return None
        pairs = [
            (record, self._export_skip_reason(record))
            for record in self.selected_records
        ]
        skipped = tuple(
            (record.file.relative_path, reason) for record, reason in pairs if reason
        )
        # Unknown availability stays in: the worker resolves it (one stat per
        # still-unknown file) and reports thumbnail-only records as skipped.
        selected = [record for record, reason in pairs if not reason]
        if not selected:
            self._set_status(
                f"选中的 {len(candidates)} 项都没有可导出的原图"
                f"（{skipped[0][1] if skipped else '无可导出项'}）"
            )
            return None
        if not self._confirm_batch("批量导出原图", selected):
            self._set_status("已取消：未写入任何文件")
            return None
        directory = QFileDialog.getExistingDirectory(
            self,
            "选择导出文件夹（所有原图写入此目录）",
            self._last_export_dir or str(Path.home() / "Pictures"),
        )
        if not directory:
            self._set_status("已取消导出，未写入任何文件")
            return None
        self._last_export_dir = directory
        total = len(selected)
        result = self._run_blocking(
            lambda: self._export_batch(selected, Path(directory), skipped),
            f"正在解密并导出 {total} 张原图…",
            progress_slot=lambda done: self._on_batch_progress(done, total, "导出原图"),
        )
        return self._finish_batch("批量导出原图", total, result, skipped)

    def _export_batch(
        self, records, directory: Path, skipped=()
    ) -> BatchOutcome:
        """Worker-side: decrypt and write each record; keep per-file outcomes.

        Availability that the UI thread had not resolved yet is checked here,
        where a stat per file is free: a thumbnail-only record is skipped with
        its reason instead of turning into a confusing decode failure.
        """
        succeeded: list[tuple[str, str]] = []
        failed: list[tuple[str, str]] = []
        skipped = list(skipped)
        used: set[Path] = set()
        for position, record in enumerate(records, start=1):
            state = self._availability_of(record)
            if state != "original_available":
                # Availability here is a plain stat (no decryption), so this
                # stays cheap even for a large selection.
                reason = _UNAVAILABLE_EXPORT_REASONS.get(
                    state, f"源文件状态异常（{state}），无原图可导出"
                )
                skipped.append((record.file.relative_path, reason))
                self._emit_progress(position)
                continue
            target = directory / _export_suggested_name(record.file.relative_path)
            # Two same-named originals in one session must not overwrite each
            # other, so a numeric suffix is added (still inside the folder the
            # user picked).
            counter = 1
            while target in used or target.exists():
                target = directory / f"{target.stem}（{counter}）{target.suffix}"
                counter += 1
            outcome = self._export_to_path(record, target)
            if outcome.ok:
                used.add(Path(outcome.written_path))
                note = f"{outcome.width}×{outcome.height}"
                if not outcome.is_original:
                    note += "｜源文件主体不完整，导出的是内嵌缩略图"
                succeeded.append((outcome.written_path, note))
            else:
                failed.append(
                    (record.file.relative_path, f"{outcome.error_code}｜{outcome.error_message}")
                )
            self._emit_progress(position)
        return BatchOutcome(
            action="批量导出原图",
            requested=len(records),
            succeeded=tuple(succeeded),
            failed=tuple(failed),
            skipped=tuple(skipped),
            export_dir=str(directory),
        )

    def recycle_selected_batch(self) -> BatchOutcome | None:
        """Move every selected file's source to the Recycle Bin."""
        selected = self._batch_selection_gate(self.selected_records, need_image=False)
        if not selected:
            return None
        if not self._confirm_batch("批量移到回收站", selected):
            self._set_status("已取消：文件未做任何改动")
            return None
        total = len(selected)
        result = self._run_blocking(
            lambda: self._recycle_batch(selected),
            f"正在把 {total} 个文件移入回收站…",
            progress_slot=lambda done: self._on_batch_progress(done, total, "移入回收站"),
        )
        return self._finish_batch("批量移到回收站", total, result)

    def _recycle_batch(self, records) -> BatchOutcome:
        """Worker-side: recycle one file at a time, collecting every receipt."""
        succeeded: list[tuple[str, str]] = []
        failed: list[tuple[str, str]] = []
        recycled_ids: list[str] = []
        for position, record in enumerate(records, start=1):
            outcome = self._facade.recycle_record(record)
            if outcome.ok:
                succeeded.append(
                    (outcome.relative_path, _human_bytes(outcome.byte_size))
                )
                recycled_ids.append(str(record.media_id))
            else:
                failed.append(
                    (record.file.relative_path, f"{outcome.error_code}｜{outcome.error_message}")
                )
            self._emit_progress(position)
        return BatchOutcome(
            action="批量移到回收站",
            requested=len(records),
            succeeded=tuple(succeeded),
            failed=tuple(failed),
            recycled_media_ids=tuple(recycled_ids),
        )

    def export_and_recycle_selected_batch(self) -> BatchOutcome | None:
        """Export every selected image, then recycle only the ones exported.

        The order is per file and matters: a source file is only recycled after
        its decrypted copy is safely on disk.  A record that fails to export is
        reported and *left untouched* rather than recycled without a backup.
        """
        candidates = self._batch_selection_gate(self.selected_records, need_image=True)
        if not candidates:
            return None
        pairs = [
            (record, self._export_skip_reason(record))
            for record in self.selected_records
        ]
        skipped = tuple(
            (record.file.relative_path, reason) for record, reason in pairs if reason
        )
        # 未知可用性留给 worker 判定：缩略图记录会被跳过，且**绝不回收**——
        # 没有备份的源文件一个都不动。
        selected = [record for record, reason in pairs if not reason]
        if not selected:
            self._set_status(
                f"选中的 {len(candidates)} 项都没有可导出的原图"
                f"（{skipped[0][1] if skipped else '无可导出项'}），未做任何改动"
            )
            return None
        if not self._confirm_batch(
            "批量导出后移入回收站",
            selected,
            extra="顺序：先导出全部原图，再把成功导出的源文件移入回收站。",
        ):
            self._set_status("已取消：文件未做任何改动")
            return None
        directory = QFileDialog.getExistingDirectory(
            self,
            "选择导出文件夹（导出的原图写入此目录）",
            self._last_export_dir or str(Path.home() / "Pictures"),
        )
        if not directory:
            self._set_status("已取消：未选择导出位置，文件未做任何改动")
            return None
        self._last_export_dir = directory
        total = len(selected)
        result = self._run_blocking(
            lambda: self._export_then_recycle_batch(selected, Path(directory), skipped),
            f"正在导出 {total} 张原图并移入回收站…",
            progress_slot=lambda done: self._on_batch_progress(done, total, "导出并回收"),
        )
        return self._finish_batch("批量导出后移入回收站", total, result, skipped)

    def _export_then_recycle_batch(self, records, directory: Path, skipped=()) -> BatchOutcome:
        """Worker-side: export each record, recycle it only when the export stuck."""
        succeeded: list[tuple[str, str]] = []
        failed: list[tuple[str, str]] = []
        skipped = list(skipped)
        recycled_ids: list[str] = []
        used: set[Path] = set()
        for position, record in enumerate(records, start=1):
            state = self._availability_of(record)
            if state != "original_available":
                # 无原图可导出的记录既不导出也不回收：没有备份的源文件保持不动。
                reason = _UNAVAILABLE_EXPORT_REASONS.get(
                    state, f"源文件状态异常（{state}），无原图可导出"
                )
                skipped.append((record.file.relative_path, f"{reason}，未回收"))
                self._emit_progress(position)
                continue
            target = directory / _export_suggested_name(record.file.relative_path)
            counter = 1
            while target in used or target.exists():
                target = directory / f"{target.stem}（{counter}）{target.suffix}"
                counter += 1
            outcome = self._export_to_path(record, target)
            if not outcome.ok:
                failed.append(
                    (
                        record.file.relative_path,
                        f"导出失败未回收：{outcome.error_code}｜{outcome.error_message}",
                    )
                )
                self._emit_progress(position)
                continue
            used.add(Path(outcome.written_path))
            note = f"{outcome.width}×{outcome.height}"
            if not outcome.is_original:
                note += "｜源文件主体不完整，导出的是内嵌缩略图"
            recycle = self._facade.recycle_record(record)
            if recycle.ok:
                succeeded.append((outcome.written_path, f"{note}｜源文件已移入回收站"))
                recycled_ids.append(str(record.media_id))
            else:
                # The export is on disk and stays there; only the recycle step
                # failed, and the user is told exactly that.
                failed.append(
                    (
                        record.file.relative_path,
                        f"导出成功但移入回收站失败：{recycle.error_code}｜{recycle.error_message}",
                    )
                )
            self._emit_progress(position)
        return BatchOutcome(
            action="批量导出后移入回收站",
            requested=len(records),
            succeeded=tuple(succeeded),
            failed=tuple(failed),
            skipped=tuple(skipped),
            export_dir=str(directory),
            recycled_media_ids=tuple(recycled_ids),
        )

    def _finish_batch(
        self, action: str, requested: int, result: dict, skipped=()
    ) -> BatchOutcome | None:
        """Report a finished batch and refresh the view.

        A failure of the worker step itself (not of an individual file) is
        reported as one failed entry per requested file, because none of them
        ran.
        """
        if not result.get("ok"):
            message = str(result.get("message", ""))[:200]
            outcome = BatchOutcome(
                action=action,
                requested=requested,
                failed=tuple(
                    (str(index), message) for index in range(1, requested + 1)
                ),
                skipped=tuple(skipped),
            )
            self.last_batch = outcome
            self._set_status(f"{action}失败：{message}")
            return outcome
        outcome: BatchOutcome = result["value"]
        if skipped and not outcome.skipped:
            outcome = replace(outcome, skipped=tuple(skipped))
        self.last_batch = outcome
        # When the source left the account (recycle involved), those records
        # must disappear from the album; re-applying the filters rebuilds it.
        if outcome.recycled_media_ids:
            gone = set(outcome.recycled_media_ids)
            self._all_records = tuple(
                record for record in self._all_records if str(record.media_id) not in gone
            )
            for media_id in gone:
                self._availability.pop(uuid.UUID(media_id), None)
            self._last_previewed_id = None
        message = outcome.summary()
        detail = ""
        if outcome.failed:
            first_path, first_reason = outcome.failed[0]
            detail = f"｜首个失败：{first_path}（{first_reason}）"
        self.preview_status.setText(message + detail)
        if outcome.recycled_media_ids:
            # Rebuild the album without the recycled records, then restore the
            # batch report: the user asked for a management action, so its
            # result is what the status bar should be showing.  The viewport
            # stays where the batch ran instead of jumping to the top.
            self.apply_filters(preserve_position=True)
        self._set_status(message + detail)
        return outcome

    def request_preview(self) -> PreviewOutcome:
        record = self.selected_record
        if record is None:
            moved = len(self.selected_records) > 1
            outcome = PreviewOutcome(
                ok=False,
                error_code="NO_SELECTION",
                error_message=(
                    "已选择多张照片：右侧大图一次只显示一张，请单击其中一张再预览"
                    if moved
                    else "请先选择一条记录"
                ),
            )
            self.preview_status.setText(outcome.error_message)
            return outcome

        if record.media_type is MediaType.VIDEO:
            return self._request_video_preview(record)

        if record.media_type not in (MediaType.IMAGE, MediaType.THUMBNAIL):
            # 文件 / 语音不是图片，预先给出可读原因而不是后端英文报错。
            outcome = PreviewOutcome(
                ok=False,
                error_code="DECODER_UNAVAILABLE",
                error_message="该记录不是图片（文件 / 语音暂不支持预览，可右键直接打开）",
            )
            self.last_preview = outcome
            self.preview_label.show_message("预览不可用")
            self.preview_status.setText(
                f"失败原因：{outcome.error_code}｜{outcome.error_message}"
            )
            return outcome

        if self._busy_loop is not None:
            # 上一条预览还在解码；busy 结束后会按当前选中自动补一次。
            return self.last_preview or PreviewOutcome(
                ok=False, error_code="BUSY", error_message="正在处理上一条预览"
            )

        variant = self.preview_variant_combo.currentData()
        # Decryption + PNG re-encode can take seconds on multi-megapixel
        # originals; run it on the worker thread so selection stays responsive.
        result = self._run_blocking(
            lambda: self._facade.preview(record, variant=variant, max_edge_px=256),
            "正在解码预览（大图约需一至三秒）…",
        )
        if not result.get("ok"):
            outcome = PreviewOutcome(
                ok=False,
                error_code="PREVIEW_FAILED",
                error_message=str(result.get("message", ""))[:200],
            )
        else:
            outcome = result["value"]
        self.last_preview = outcome
        if outcome.ok:
            self._last_previewed_id = str(record.media_id)
            self._warmed.add(str(record.media_id))
            pixmap = QPixmap(outcome.output_path)
            if pixmap.isNull():
                self.preview_label.show_message("预览文件无法显示")
                self.preview_status.setText("已生成缓存文件，但无法加载图像")
            else:
                self.preview_label.show_pixmap(pixmap)
                shown = "原图" if outcome.variant == "original" else "缩略图"
                # The size that means anything to the user is the *source*
                # file's, the same number the album tile shows - the cached
                # preview artifact is a small re-encode and used to be shown
                # here, which read as "原图 4 KB".
                self.preview_status.setText(
                    f"预览成功（{shown}，源文件 {_human_bytes(record.file.byte_size)}）"
                    "；原图不会被修改"
                )
        else:
            self.preview_label.show_message("预览不可用")
            self.preview_status.setText(
                f"失败原因：{outcome.error_code}｜{outcome.error_message}"
            )
        return outcome

    # ------------------------------------------------------------------
    # video clip preview: sampled frames + play/pause (no audio, no seek)
    # ------------------------------------------------------------------
    def _request_video_cover(self, record: MediaRecord) -> PreviewOutcome | None:
        """Show a video's cached cover frame on selection (cheap, no sampling).

        The cover comes from the same bounded cache as grid thumbnails, so
        revisiting a video is instant.  Dynamic playback stays behind the
        explicit「预览视频」button.  The preview id is claimed even on failure
        so a missing cover does not retry on every selection event.
        """
        if self._busy_loop is not None:
            return None
        self._last_previewed_id = str(record.media_id)
        result = self._run_blocking(
            lambda: self._facade.preview(record, variant="thumbnail", max_edge_px=256),
            "正在加载视频封面…",
        )
        if not result.get("ok"):
            message = str(result.get("message", ""))[:200]
            return self._video_cover_failed(record, "PREVIEW_FAILED", message)
        outcome: PreviewOutcome = result["value"]
        if not outcome.ok:
            return self._video_cover_failed(record, outcome.error_code, outcome.error_message)
        pixmap = QPixmap(outcome.output_path)
        if pixmap.isNull():
            return self._video_cover_failed(record, "DECODE_FAILED", "视频封面文件无法显示")
        self.last_preview = outcome
        self.preview_label.show_pixmap(pixmap)
        self.preview_status.setText(
            f"视频封面（源文件 {_human_bytes(record.file.byte_size)}）。"
            "点击「预览视频」播放抽样预览（无声）"
        )
        self.clip_play_button.setText("播放视频")
        self.clip_play_button.show()
        return outcome

    def _video_cover_failed(self, record: MediaRecord, code: str, message: str) -> PreviewOutcome:
        outcome = PreviewOutcome(
            ok=False, error_code=code or "PREVIEW_FAILED", error_message=message[:200]
        )
        self.last_preview = outcome
        self.preview_label.show_message("该视频暂无封面，可点击「预览视频」尝试动态解码")
        self.preview_status.setText(
            f"视频封面失败：{outcome.error_code}｜{outcome.error_message}"
        )
        return outcome

    def _request_video_preview(self, record: MediaRecord) -> PreviewOutcome:
        """Load a video's sampled frames and start playing them in the pane."""
        if self._busy_loop is not None:
            return self.last_preview or PreviewOutcome(
                ok=False, error_code="BUSY", error_message="正在处理上一条预览"
            )
        self._stop_clip()
        result = self._run_blocking(
            lambda: self._facade.video_clip(record),
            "正在提取视频预览帧（大视频约需几秒）…",
        )
        if not result.get("ok"):
            return self._video_preview_failed(
                record, "PREVIEW_FAILED", str(result.get("message", ""))[:200]
            )
        clip: VideoClipOutcome = result["value"]
        if not clip.ok:
            return self._video_preview_failed(record, clip.error_code, clip.error_message)
        frames = []
        for raw in clip.frames:
            pixmap = QPixmap()
            if pixmap.loadFromData(raw):
                frames.append(pixmap)
        if not frames:
            return self._video_preview_failed(
                record, "DECODE_FAILED", "未能从该视频解出可显示的帧"
            )
        self._clip_frames = frames
        self._clip_index = 0
        self._last_clip_id = str(record.media_id)
        self._last_previewed_id = str(record.media_id)
        self.last_preview = PreviewOutcome(
            ok=True, variant="thumbnail", decoder_id="video-clip",
            output_bytes=record.file.byte_size,
        )
        self._show_clip_frame()
        self.clip_play_button.show()
        self._set_clip_playing(True, fps=clip.fps)
        self._set_status(
            f"视频预览：{len(frames)} 帧抽样（无声），可播放 / 暂停"
            f"（源文件 {_human_bytes(record.file.byte_size)}，原文件不会被修改）"
        )
        return self.last_preview

    def _video_preview_failed(
        self, record: MediaRecord, code: str, message: str
    ) -> PreviewOutcome:
        outcome = PreviewOutcome(
            ok=False, error_code=code or "PREVIEW_FAILED", error_message=message[:200]
        )
        self.last_preview = outcome
        self.preview_label.show_message("预览不可用")
        self.preview_status.setText(
            f"失败原因：{outcome.error_code}｜{outcome.error_message}"
        )
        return outcome

    def _show_clip_frame(self) -> None:
        """Paint the current clip frame and its position (UI thread only)."""
        if not self._clip_frames:
            return
        self.preview_label.show_pixmap(self._clip_frames[self._clip_index])
        self.preview_status.setText(
            f"视频预览 {self._clip_index + 1}/{len(self._clip_frames)}"
            "（无声抽样，可播放 / 暂停）"
        )

    def _set_clip_playing(self, playing: bool, *, fps: float = 10.0) -> None:
        """Start or pause the clip timer; the button always names the next action."""
        self._clip_playing = playing and bool(self._clip_frames)
        if self._clip_playing:
            # Cap the pace: a 60 fps phone video previews fine at 12 fps.
            interval = int(1000 / max(1.0, min(float(fps or 10.0), 12.0)))
            self._clip_timer.start(max(80, min(500, interval)))
            self.clip_play_button.setText("暂停")
        else:
            self._clip_timer.stop()
            self.clip_play_button.setText("播放")

    def toggle_clip_playback(self) -> None:
        """Transport button: sample-and-play first, then pause/resume."""
        if not self._clip_frames:
            record = self.selected_record
            if record is not None and record.media_type is MediaType.VIDEO:
                self._request_video_preview(record)
            return
        self._set_clip_playing(not self._clip_playing)

    def _advance_clip(self) -> None:
        """Next sampled frame; wraps around for a looping preview."""
        if not self._clip_frames or not self._window_alive():
            return
        self._clip_index = (self._clip_index + 1) % len(self._clip_frames)
        self._show_clip_frame()

    def _stop_clip(self) -> None:
        """Halt playback, drop the frames and hide the transport button."""
        self._clip_timer.stop()
        self._clip_playing = False
        self._clip_frames = []
        self._clip_index = 0
        self._last_clip_id = None
        self.clip_play_button.setText("播放")
        self.clip_play_button.hide()

    def refresh_cache_status(self) -> CacheStatus:
        """Measure the cache and show it — off the UI thread.

        ``cache_status`` walks every cached file (thousands once previews
        accumulate), so running it inline froze the window for seconds.
        ``_run_blocking`` already provides the busy state, the pulse progress
        bar and the per-second clock that the user sees while it works.
        """
        result = self._run_blocking(self._facade.cache_status, "正在统计缓存占用…")
        if not result.get("ok"):
            message = str(result.get("message", ""))[:160]
            self._set_status(f"统计缓存失败：{message}")
            raise FacadeError(message)
        status: CacheStatus = result["value"]
        self.cache_label.setText(
            f"共 {_human_bytes(status.total_bytes)}，{status.file_count} 个文件；"
            f"其中解码预览 {status.decoded_count} 个，密钥缓存 {status.key_cache_count} 个"
        )
        return status

    def clear_decoded_cache(self) -> int:
        result = self._run_blocking(
            self._facade.clear_decoded_cache, "正在清除解码缓存…"
        )
        if not result.get("ok"):
            message = str(result.get("message", ""))[:160]
            self._set_status(f"清除解码缓存失败：{message}")
            raise FacadeError(message)
        removed: int = result["value"]
        self._refresh_grid_after_cache_clear()
        self.refresh_cache_status()
        self._set_status(f"已清除解码缓存 {removed} 个文件（微信源文件未受影响）")
        return removed

    def clear_all_cache(self) -> int:
        result = self._run_blocking(
            self._facade.clear_all_local_cache, "正在清除全部本地缓存…"
        )
        if not result.get("ok"):
            message = str(result.get("message", ""))[:160]
            self._set_status(f"清除全部本地缓存失败：{message}")
            raise FacadeError(message)
        removed: int = result["value"]
        self._refresh_grid_after_cache_clear()
        self.refresh_cache_status()
        self._set_status(f"已清除全部本地缓存 {removed} 个文件（微信源文件未受影响）")
        return removed

    def _refresh_grid_after_cache_clear(self) -> None:
        """Drop the in-memory previews so the album reflects the emptied cache.

        Without this the grid kept showing the pixmaps that were just deleted
        on disk, which reads as "the button did nothing".  Re-warming only
        makes sense with a connected session.
        """
        if not self._all_records:
            return
        self._grid_model.clear_thumbnails()
        with self._warm_lock:
            self._warmed.clear()
            self._warm_failed.clear()
        self._reset_warm_queue()
        self._warm_visible_cells()

    def _cache_root(self) -> str:
        root = getattr(self._facade, "cache_root", "") or ""
        if not root:
            root = str(Path.home() / "AppData" / "Local" / "WeChatSpaceManager" / "cache")
        return root

    def _shutdown_worker(self) -> None:
        """Stop and join the shared facade worker; safe to call twice.

        The upstream session is built on this thread and holds thread-affine
        native state, so the thread must be joined before Qt destroys it or the
        process exits.
        """
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        try:
            worker.stop()
            worker.wait(_WORKER_STOP_TIMEOUT_MS)
        except RuntimeError:
            # The C++ object is already gone; nothing left to join.
            pass

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._busy_loop is not None:
            # A blocking pipeline step is in flight; closing now would yank the
            # facade out from under the worker thread.
            event.ignore()
            self._set_status("正在处理中，请等待当前步骤完成后再关闭窗口")
            return
        # Pending auto-preview timers must not fire against a destroyed window.
        self._preview_timer.stop()
        self._clip_timer.stop()
        # Stop background warming: the flag makes in-flight decodes bail out
        # instead of touching dead state, and the queue is emptied under the
        # lock so a finishing decode cannot refill it.
        self._warm_stopped = True
        with self._warm_lock:
            self._warm_queue.clear()
            self._warm_queued.clear()
        # clear() drops what has not started; waitForDone() joins what has, so
        # no decode is still running (or about to touch this window) once the
        # window and its facade go away.
        self._warm_pool.clear()
        self._warm_pool.waitForDone(_WARM_POOL_DRAIN_TIMEOUT_MS)
        with self._warm_lock:
            self._warm_live.clear()
        # Stop the shared facade worker before the facade goes away: the
        # session was built on that thread, so it must be closed and the
        # thread joined *before* the window (and the worker's parent) is
        # destroyed, or Qt tears the thread down under live native state.
        self._shutdown_worker()
        try:
            self._facade.close()
        finally:
            super().closeEvent(event)
