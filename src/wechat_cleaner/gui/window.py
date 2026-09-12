"""PySide6 desktop shell and workflow state machine.

Widgets only call :class:`ApplicationFacadePort`; no widget imports a
filesystem scanner, mapper, decoder or executor and no widget performs a file
mutation. The production window uses the application facade adapter by
default; the fake adapter is only selected explicitly by tests or demo mode.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC
from typing import Any
from uuid import UUID

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wechat_cleaner.cleanup import PlanBuildResult, PreflightReport, PreflightStatus
from wechat_cleaner.domain import (
    CleanupPlan,
    CleanupReceipt,
    FilterSortKey,
    FilterSpec,
    SelectionScope,
)
from wechat_cleaner.domain.contracts import CleanupDisposition
from wechat_cleaner.filtering import FilterResult

from .services import (
    ApplicationFacadePort,
    FacadeError,
    create_application_facade_from_args,
    default_filter_spec,
    page_summary,
)


class TaskSignals(QObject):
    started = Signal(str, int)
    progress = Signal(str, int, int, str)
    succeeded = Signal(str, int, object)
    failed = Signal(str, int, str)
    cancelled = Signal(str, int)
    finished = Signal(str, int)


class _Task(QRunnable):
    def __init__(
        self,
        name: str,
        token: int,
        fn: Callable[[threading.Event, Callable[[int, str], None]], Any],
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.name = name
        self.token = token
        self.fn = fn
        self.cancel_event = threading.Event()
        self.signals = TaskSignals()

    def cancel(self) -> None:
        self.cancel_event.set()

    @Slot()
    def run(self) -> None:
        self.signals.started.emit(self.name, self.token)
        try:
            if self.cancel_event.is_set():
                self.signals.cancelled.emit(self.name, self.token)
                return
            value = self.fn(self.cancel_event, self._progress)
            if self.cancel_event.is_set():
                self.signals.cancelled.emit(self.name, self.token)
            else:
                self.signals.succeeded.emit(self.name, self.token, value)
        except Exception as exc:  # noqa: BLE001 - worker boundary becomes a safe UI error
            if self.cancel_event.is_set():
                self.signals.cancelled.emit(self.name, self.token)
            else:
                self.signals.failed.emit(self.name, self.token, _safe_error(exc))
        finally:
            self.signals.finished.emit(self.name, self.token)

    def _progress(self, percent: int, message: str) -> None:
        if not self.cancel_event.is_set():
            self.signals.progress.emit(self.name, self.token, max(0, min(100, percent)), message)


def _safe_error(error: Exception) -> str:
    if isinstance(error, FacadeError):
        return str(error)
    return "任务失败，请检查筛选条件或稍后重试。"


class MainWindow(QMainWindow):
    """Chinese, high-DPI-friendly shell for review and safe workflow steps."""

    page_names = ("总览", "精确筛选", "清理任务", "设置/缓存")

    def __init__(
        self,
        facade: ApplicationFacadePort | None = None,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("微信空间管理器")
        self.resize(1120, 720)
        self.facade: ApplicationFacadePort = facade or create_application_facade_from_args()
        self.pool = QThreadPool.globalInstance()
        self._task_tokens: dict[str, int] = {}
        self._tasks: dict[str, _Task] = {}
        self._task_counter = 0
        self._active_task_name: str | None = None
        self._records = ()
        self._filter_spec = default_filter_spec()
        self._filter_result: FilterResult | None = None
        self._page_cursors: list[str | None] = [None]
        self._page_index = 0
        self._selection = None
        self._plan: CleanupPlan | None = None
        self._preflight: PreflightReport | None = None
        self._receipt: CleanupReceipt | None = None
        self._plan_signature: tuple[object, ...] | None = None
        self._message_box: QMessageBox | None = None
        self._build_ui()
        self._wire_actions()
        self._set_page(0)
        self._start_initial_load()

    # ---------- public test/control surface ----------
    @property
    def active_task_name(self) -> str | None:
        return self._active_task_name

    @property
    def current_page_name(self) -> str:
        return self.navigation.currentItem().text() if self.navigation.currentItem() else ""

    def navigate(self, page: str | int) -> None:
        if isinstance(page, int):
            index = page
        else:
            index = self.page_names.index(page)
        self._set_page(index)

    def add_condition(self, condition: str | None = None) -> None:
        text = (condition if condition is not None else self.condition_input.text()).strip()
        if not text:
            return
        self.conditions_list.addItem(text)
        self.condition_input.clear()
        self._invalidate_plan("条件已修改，请重新生成选择和计划。")

    def remove_condition(self, index: int | None = None) -> None:
        row = self.conditions_list.currentRow() if index is None else index
        if row >= 0:
            self.conditions_list.takeItem(row)
            self._invalidate_plan("条件已修改，请重新生成选择和计划。")

    def set_selection_scope(self, scope: SelectionScope) -> None:
        index = self.scope_combo.findData(scope.value)
        if index >= 0:
            self.scope_combo.setCurrentIndex(index)
        self._invalidate_plan("选择范围已修改，请重新生成计划。")

    def cancel_active_task(self) -> None:
        if self._active_task_name is None:
            return
        task = self._tasks.get(self._active_task_name)
        if task is not None:
            task.cancel()
        self.status_label.setText("正在取消任务……")

    # ---------- UI construction ----------
    def _build_ui(self) -> None:
        root = QWidget(self)
        root_layout = QHBoxLayout(root)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setFixedWidth(150)
        for name in self.page_names:
            self.navigation.addItem(QListWidgetItem(name))
        root_layout.addWidget(self.navigation)

        self.pages = QStackedWidget()
        self.pages.setObjectName("pages")
        self.pages.addWidget(self._overview_page())
        self.pages.addWidget(self._filter_page())
        self.pages.addWidget(self._cleanup_page())
        self.pages.addWidget(self._settings_page())
        root_layout.addWidget(self.pages, 1)
        self.setCentralWidget(root)

        self.status_label = QLabel("准备就绪")
        self.status_label.setObjectName("statusLabel")
        self.statusBar().addPermanentWidget(self.status_label)
        cancel_action = QAction("取消当前任务", self)
        cancel_action.triggered.connect(self.cancel_active_task)
        self.addAction(cancel_action)

    def _overview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("微信空间管理器")
        title.setStyleSheet("font-size: 22px; font-weight: 600;")
        layout.addWidget(title)
        self.overview_summary = QLabel("正在加载本地索引……")
        self.overview_summary.setWordWrap(True)
        layout.addWidget(self.overview_summary)
        self.overview_refresh = QPushButton("刷新索引")
        self.overview_refresh.setObjectName("overviewRefresh")
        layout.addWidget(self.overview_refresh)
        layout.addStretch(1)
        return page

    def _filter_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        header = QHBoxLayout()
        header.addWidget(QLabel("精确筛选"))
        self.page_size = QSpinBox()
        self.page_size.setRange(1, 200)
        self.page_size.setValue(5)
        self.page_size.setPrefix("每页 ")
        header.addWidget(self.page_size)
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("按空间大小", FilterSortKey.BYTES.value)
        self.sort_combo.addItem("按消息时间", FilterSortKey.MESSAGE_TIME.value)
        self.sort_combo.addItem("按观察时间", FilterSortKey.OBSERVED_AT.value)
        header.addWidget(self.sort_combo)
        self.apply_filter_button = QPushButton("应用筛选")
        header.addWidget(self.apply_filter_button)
        outer.addLayout(header)

        condition_row = QHBoxLayout()
        self.condition_input = QLineEdit()
        self.condition_input.setPlaceholderText("添加条件说明（仅保存在本次会话）")
        self.add_condition_button = QPushButton("添加条件")
        self.remove_condition_button = QPushButton("删除条件")
        condition_row.addWidget(self.condition_input, 1)
        condition_row.addWidget(self.add_condition_button)
        condition_row.addWidget(self.remove_condition_button)
        outer.addLayout(condition_row)
        self.conditions_list = QListWidget()
        self.conditions_list.setMaximumHeight(72)
        outer.addWidget(self.conditions_list)

        self.results_table = QTableWidget(0, 6)
        self.results_table.setObjectName("resultsTable")
        self.results_table.setHorizontalHeaderLabels(
            ["联系人", "类型", "大小", "消息时间", "映射", "相对路径"]
        )
        self.results_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.results_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.results_table.setSortingEnabled(False)
        self.results_table.itemSelectionChanged.connect(self._show_selected_details)
        outer.addWidget(self.results_table, 1)
        self.record_details = QLabel("选择一行查看记录详情。")
        self.record_details.setWordWrap(True)
        self.preview_summary = QLabel("预览尚未请求。")
        self.preview_summary.setWordWrap(True)
        outer.addWidget(self.record_details)
        outer.addWidget(self.preview_summary)

        footer = QHBoxLayout()
        self.page_label = QLabel("第 1 页")
        self.previous_page_button = QPushButton("上一页")
        self.next_page_button = QPushButton("下一页")
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("当前页", SelectionScope.CURRENT_PAGE.value)
        self.scope_combo.addItem("全部结果", SelectionScope.CURRENT_RESULT.value)
        self.scope_combo.addItem("手动选择", SelectionScope.MANUAL.value)
        self.select_button = QPushButton("建立选择")
        self.preview_button = QPushButton("请求预览")
        footer.addWidget(self.page_label)
        footer.addWidget(self.previous_page_button)
        footer.addWidget(self.next_page_button)
        footer.addStretch(1)
        footer.addWidget(QLabel("选择范围"))
        footer.addWidget(self.scope_combo)
        footer.addWidget(self.preview_button)
        footer.addWidget(self.select_button)
        outer.addLayout(footer)
        return page

    def _cleanup_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("清理任务"))
        self.selection_summary = QLabel("尚未建立选择。")
        self.plan_summary = QLabel("尚未生成计划。")
        self.preflight_summary = QLabel("尚未预检。")
        self.execution_summary = QLabel("尚未试运行。")
        for label in (
            self.selection_summary,
            self.plan_summary,
            self.preflight_summary,
            self.execution_summary,
        ):
            label.setWordWrap(True)
            layout.addWidget(label)

        form = QFormLayout()
        self.disposition_combo = QComboBox()
        self.disposition_combo.addItem("回收站", CleanupDisposition.RECYCLE_BIN.value)
        self.disposition_combo.addItem("隔离目录", CleanupDisposition.QUARANTINE.value)
        form.addRow("处理方式", self.disposition_combo)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.plan_button = QPushButton("生成计划")
        self.preflight_button = QPushButton("预检")
        self.dry_run_button = QPushButton("试运行")
        self.confirm_button = QPushButton("明确确认")
        self.recovery_button = QPushButton("恢复入口")
        for button in (
            self.plan_button,
            self.preflight_button,
            self.dry_run_button,
            self.confirm_button,
            self.recovery_button,
        ):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return page

    def _settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("设置 / 缓存"))
        self.cache_status = QLabel("本界面只保留会话内状态，不会自动写入微信目录。")
        self.cache_status.setWordWrap(True)
        layout.addWidget(self.cache_status)
        self.clear_session_button = QPushButton("清除本次会话状态")
        layout.addWidget(self.clear_session_button)
        layout.addStretch(1)
        return page

    def _wire_actions(self) -> None:
        self.navigation.currentRowChanged.connect(self._set_page)
        self.overview_refresh.clicked.connect(self._start_initial_load)
        self.add_condition_button.clicked.connect(lambda: self.add_condition())
        self.remove_condition_button.clicked.connect(lambda: self.remove_condition())
        self.condition_input.returnPressed.connect(lambda: self.add_condition())
        self.apply_filter_button.clicked.connect(self._apply_filter)
        self.previous_page_button.clicked.connect(self.previous_page)
        self.next_page_button.clicked.connect(self.next_page)
        self.select_button.clicked.connect(self._create_selection)
        self.preview_button.clicked.connect(self._request_preview)
        self.plan_button.clicked.connect(self._create_plan)
        self.preflight_button.clicked.connect(self._run_preflight)
        self.dry_run_button.clicked.connect(self._run_dry_run)
        self.confirm_button.clicked.connect(self._confirm_plan)
        self.recovery_button.clicked.connect(self._recover)
        self.clear_session_button.clicked.connect(self._clear_session)
        self.sort_combo.currentIndexChanged.connect(lambda: self._invalidate_plan("排序已修改。"))
        self.page_size.valueChanged.connect(lambda: self._invalidate_plan("分页已修改。"))
        self.scope_combo.currentIndexChanged.connect(
            lambda: self._invalidate_plan("选择范围已修改，请重新建立选择。")
        )
        self._update_flow_buttons()

    # ---------- navigation and background tasks ----------
    @Slot(int)
    def _set_page(self, index: int) -> None:
        if index < 0 or index >= self.pages.count():
            return
        self.pages.setCurrentIndex(index)
        if self.navigation.currentRow() != index:
            self.navigation.setCurrentRow(index)

    def _start_task(
        self,
        name: str,
        fn: Callable[[threading.Event, Callable[[int, str], None]], Any],
        on_success: Callable[[Any], None],
    ) -> None:
        previous = self._tasks.get(name)
        if previous is not None:
            previous.cancel()
        token = self._task_tokens.get(name, 0) + 1
        self._task_tokens[name] = token
        task = _Task(name, token, fn)
        self._tasks[name] = task
        self._active_task_name = name
        task.signals.progress.connect(self._on_progress)
        task.signals.succeeded.connect(
            lambda task_name, task_token, value: self._on_task_success(
                task_name, task_token, value, on_success
            )
        )
        task.signals.failed.connect(self._on_task_failed)
        task.signals.cancelled.connect(self._on_task_cancelled)
        task.signals.finished.connect(self._on_task_finished)
        self.status_label.setText(f"{name}：正在处理……")
        self.pool.start(task)

    @Slot(str, int, int, str)
    def _on_progress(self, name: str, token: int, percent: int, message: str) -> None:
        if self._is_current(name, token):
            self.status_label.setText(f"{name}：{percent}% {message}")

    def _on_task_success(
        self,
        name: str,
        token: int,
        value: Any,
        callback: Callable[[Any], None],
    ) -> None:
        if self._is_current(name, token):
            callback(value)

    @Slot(str, int, str)
    def _on_task_failed(self, name: str, token: int, message: str) -> None:
        if self._is_current(name, token):
            self.status_label.setText(f"{name}失败：{message}")
            self._show_error(message)

    @Slot(str, int)
    def _on_task_cancelled(self, name: str, token: int) -> None:
        if self._is_current(name, token):
            self.status_label.setText(f"{name}已取消")

    @Slot(str, int)
    def _on_task_finished(self, name: str, token: int) -> None:
        if self._is_current(name, token) and self._active_task_name == name:
            self._active_task_name = None

    def _is_current(self, name: str, token: int) -> bool:
        return self._task_tokens.get(name) == token

    def _start_initial_load(self) -> None:
        self._start_task(
            "扫描",
            lambda cancel, progress: self.facade.load_records(),
            self._records_loaded,
        )

    def _records_loaded(self, records: tuple) -> None:
        self._records = records
        self.overview_summary.setText(f"已加载 {len(records)} 条本地元数据记录。")
        self._apply_filter()

    # ---------- filtering and pagination ----------
    def _apply_filter(self) -> None:
        sort_key = FilterSortKey(self.sort_combo.currentData())
        self._filter_spec = FilterSpec(
            page_size=self.page_size.value(),
            sort_by=sort_key,
            descending=True,
        )
        self._page_cursors = [None]
        self._page_index = 0
        self._invalidate_plan("筛选已修改，请重新建立选择。")
        self._query_page(None)

    def _query_page(self, cursor: str | None) -> None:
        self._start_task(
            "筛选",
            lambda cancel, progress: self.facade.query(self._filter_spec, cursor=cursor),
            self._filter_loaded,
        )

    def _filter_loaded(self, result: FilterResult) -> None:
        self._filter_result = result
        self._render_results(result)
        self.overview_summary.setText(page_summary(result.page))
        self.status_label.setText("筛选完成")

    def _render_results(self, result: FilterResult) -> None:
        self.results_table.setSortingEnabled(False)
        self.results_table.setRowCount(0)
        for row, record in enumerate(result.items):
            self.results_table.insertRow(row)
            contact = record.contact.display_name if record.contact else "未映射"
            values = (
                contact or "未命名联系人",
                record.media_type.value,
                f"{record.file.byte_size / 1024:.1f} KiB",
                (record.message_time or record.observed_at)
                .astimezone(UTC)
                .strftime("%Y-%m-%d %H:%M"),
                record.mapping_confidence.value,
                record.file.relative_path,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, str(record.media_id))
                self.results_table.setItem(row, column, item)
        self.results_table.resizeColumnsToContents()
        self.page_label.setText(f"第 {self._page_index + 1} 页")
        self.previous_page_button.setEnabled(self._page_index > 0)
        self.next_page_button.setEnabled(result.next_cursor is not None)
        self.record_details.setText("选择一行查看记录详情。")
        self.preview_summary.setText("预览尚未请求。")

    def _selected_record(self):
        if self._filter_result is None:
            return None
        rows = sorted({index.row() for index in self.results_table.selectedIndexes()})
        if not rows:
            return None
        item = self.results_table.item(rows[0], 0)
        if item is None:
            return None
        media_id = UUID(item.data(Qt.ItemDataRole.UserRole))
        return next(
            (record for record in self._filter_result.items if record.media_id == media_id),
            None,
        )

    def _show_selected_details(self) -> None:
        record = self._selected_record()
        if record is None:
            self.record_details.setText("选择一行查看记录详情。")
            return
        contact = record.contact.display_name if record.contact else "未映射"
        self.record_details.setText(
            f"详情：{contact} · {record.media_type.value} · "
            f"{record.file.byte_size / 1024:.1f} KiB · {record.file.relative_path}"
        )

    def _request_preview(self) -> None:
        record = self._selected_record()
        if record is None:
            self._show_error("请先选择一条记录。")
            return
        self._start_task(
            "预览",
            lambda cancel, progress: self.facade.preview(record.media_id),
            self._preview_done,
        )

    def _preview_done(self, message: str) -> None:
        self.preview_summary.setText(f"预览：{message}")
        self.status_label.setText("预览完成")

    def next_page(self) -> None:
        if self._filter_result is None or self._filter_result.next_cursor is None:
            return
        self._page_cursors.append(self._filter_result.next_cursor)
        self._page_index += 1
        self._query_page(self._filter_result.next_cursor)

    def previous_page(self) -> None:
        if self._page_index <= 0:
            return
        self._page_index -= 1
        self._query_page(self._page_cursors[self._page_index])

    # ---------- selection and cleanup flow ----------
    def _create_selection(self) -> None:
        if self._filter_result is None:
            self._show_error("请先完成筛选。")
            return
        scope = SelectionScope(self.scope_combo.currentData())
        selected_ids = self._selected_media_ids() if scope is SelectionScope.MANUAL else ()
        self._start_task(
            "选择",
            lambda cancel, progress: self.facade.select(
                self._filter_result, scope, selected_media_ids=selected_ids
            ),
            self._selection_created,
        )

    def _selected_media_ids(self) -> tuple[UUID, ...]:
        ids: list[UUID] = []
        for row in sorted({index.row() for index in self.results_table.selectedIndexes()}):
            item = self.results_table.item(row, 0)
            if item is not None:
                ids.append(UUID(item.data(Qt.ItemDataRole.UserRole)))
        return tuple(ids)

    def _selection_created(self, selection: Any) -> None:
        self._selection = selection
        self._plan = None
        self._preflight = None
        self._receipt = None
        self._plan_signature = None
        self.selection_summary.setText(
            f"选择已建立：{selection.scope.value}，{selection.selected_count} 项，"
            f"{selection.selected_bytes / 1024:.1f} KiB。"
        )
        self.status_label.setText("选择已建立")
        self._set_page(2)
        self._update_flow_buttons()

    def _create_plan(self) -> None:
        if self._selection is None:
            self._show_error("请先建立选择。")
            return
        disposition = CleanupDisposition(self.disposition_combo.currentData())
        selection = self._selection
        self._start_task(
            "计划",
            lambda cancel, progress: self.facade.build_plan(selection, disposition=disposition),
            self._plan_created,
        )

    def _plan_created(self, result: PlanBuildResult) -> None:
        self._plan = result.plan
        self._preflight = None
        self._receipt = None
        self._plan_signature = self._signature(result.plan)
        if result.plan is None:
            self.plan_summary.setText(f"计划被拒绝：{len(result.rejected)} 项不满足安全条件。")
            self._show_error("没有可执行的安全目标，计划未生成。")
        else:
            self.plan_summary.setText(
                f"计划 {result.plan.plan_id}：{result.plan.summary.target_count} 项，"
                f"预计释放 {result.plan.summary.expected_reclaim_bytes / 1024:.1f} KiB。"
            )
            self.status_label.setText("计划已生成，等待预检")
        self._update_flow_buttons()

    def _run_preflight(self) -> None:
        if self._plan is None or self._signature(self._plan) != self._plan_signature:
            self._show_error("计划已变化，请重新生成。")
            return
        plan = self._plan
        self._start_task(
            "预检",
            lambda cancel, progress: self.facade.preflight(plan),
            self._preflight_done,
        )

    def _preflight_done(self, report: PreflightReport) -> None:
        self._preflight = report
        ready = sum(check.status is PreflightStatus.READY for check in report.checks)
        self.preflight_summary.setText(f"预检：{ready}/{len(report.checks)} 项通过。")
        self.status_label.setText("预检完成" if report.can_execute else "预检阻止执行")
        self._update_flow_buttons()

    def _run_dry_run(self) -> None:
        if not self._can_confirm():
            self._show_error("请先生成计划并通过预检。")
            return
        plan = self._plan
        assert plan is not None
        self._start_task(
            "试运行",
            lambda cancel, progress: self.facade.dry_run(plan),
            self._dry_run_done,
        )

    def _dry_run_done(self, receipt: CleanupReceipt) -> None:
        self._receipt = receipt
        self.execution_summary.setText(f"试运行状态：{receipt.status.value}，未发生文件变更。")
        self.status_label.setText("试运行完成，等待明确确认")
        self._update_flow_buttons()

    def _confirm_plan(self) -> None:
        if not self._can_confirm() or self._receipt is None:
            self._show_error("确认条件不满足：计划、预检和试运行必须保持一致。")
            return
        plan = self._plan
        assert plan is not None
        self._start_task(
            "确认",
            lambda cancel, progress: self.facade.confirm(plan),
            self._confirmation_done,
        )

    def _confirmation_done(self, result: Any) -> None:
        if result.accepted:
            self.execution_summary.setText("执行完成，已生成收据。")
            self.status_label.setText("执行完成")
        else:
            self.execution_summary.setText(result.message)
            self.status_label.setText("确认未执行任何文件操作")

    def _recover(self) -> None:
        if self._receipt is None:
            self._show_error("当前没有可查询的收据。")
            return
        receipt = self._receipt
        self._start_task(
            "恢复",
            lambda cancel, progress: self.facade.recovery(receipt),
            self._recovery_done,
        )

    def _recovery_done(self, result: Any) -> None:
        self.execution_summary.setText(result.message)
        self.status_label.setText("恢复状态已查询")

    def _can_confirm(self) -> bool:
        return bool(
            self._plan is not None
            and self._preflight is not None
            and self._preflight.can_execute
            and self._signature(self._plan) == self._plan_signature
        )

    @staticmethod
    def _signature(plan: CleanupPlan | None) -> tuple[object, ...] | None:
        if plan is None:
            return None
        return (
            str(plan.plan_id),
            plan.selection_digest,
            plan.filter_digest,
            plan.summary.target_count,
            plan.summary.expected_reclaim_bytes,
        )

    def _invalidate_plan(self, message: str) -> None:
        self._selection = None
        self._plan = None
        self._preflight = None
        self._receipt = None
        self._plan_signature = None
        if hasattr(self, "selection_summary"):
            self.selection_summary.setText("尚未建立选择。")
            self.plan_summary.setText(message)
            self.preflight_summary.setText("尚未预检。")
            self.execution_summary.setText("尚未试运行。")
            self._update_flow_buttons()

    def _update_flow_buttons(self) -> None:
        if not hasattr(self, "plan_button"):
            return
        self.plan_button.setEnabled(self._selection is not None)
        self.preflight_button.setEnabled(self._plan is not None)
        self.dry_run_button.setEnabled(self._can_confirm())
        self.confirm_button.setEnabled(
            self._can_confirm()
            and self._receipt is not None
            and self._receipt.status.value == "dry_run"
        )
        self.recovery_button.setEnabled(self._receipt is not None)

    def _clear_session(self) -> None:
        self._invalidate_plan("会话状态已清除。")
        self.conditions_list.clear()
        self.cache_status.setText("会话状态已清除；未触碰微信目录或任何文件。")

    def _show_error(self, message: str) -> None:
        self.status_label.setText(f"错误：{message}")
        if not self.isVisible():
            return
        box = QMessageBox(QMessageBox.Icon.Warning, "无法继续", message, parent=self)
        box.setModal(False)
        box.show()
        self._message_box = box

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override naming
        close = getattr(self.facade, "close", None)
        if callable(close):
            close()
        event.accept()


def run(argv: list[str] | None = None, facade: ApplicationFacadePort | None = None) -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("wechat-space-manager")
    window = MainWindow(facade or create_application_facade_from_args(argv))
    window.show()
    try:
        return app.exec()
    finally:
        # Ensure the production adapter's dedicated workflow thread and
        # thread-affine SQLite connection are closed on normal GUI exit.
        window.close()
