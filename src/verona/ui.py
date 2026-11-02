"""Qt Widgets interface for the Linux Verona design workspace."""

from __future__ import annotations

import argparse
import base64
import binascii
import ipaddress
import json
import math
import os
import sys
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPointF, QRunnable, QRectF, Qt, QThreadPool, QSettings, QTimer, Signal, Slot
from PySide6.QtGui import (
    QAction, QActionGroup, QCursor, QFont, QIcon, QMouseEvent, QPainterPath, QPixmap, QTransform,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QInputDialog, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QDoubleSpinBox, QSplitter, QToolBar, QToolButton, QVBoxLayout, QWidget, QTabBar, QSizePolicy,
)
from .i18n import get_language, set_language, tr
# Keep existing imports working while implementations live in their owning modules.
from .canvas import CanvasRuler, CanvasScrollArea, DesignCanvas, _qoi_image
from .design_validation import validate_document, validate_contours
from .file_io import publish_new_file
from .document_state import DocumentState
from .document_workspace import DocumentWorkspace
from .geometry import (contour_path as _path_from_contours, flatten_path as _flatten,
    item_path as _item_path, copy_items as _copy_items, copy_snapshot as _copy_snapshot,
    transform_item as _transform_item)
from .preview_dialogs import PreviewDialog, BleedDialog, PackingDialog, PlotPreviewCanvas
from .view_constants import (PAGE_WIDTH_MM, PAGE_HEIGHT_MM, PAGE_MARGIN, RULER_SIZE,
    DEFAULT_ZOOM, ACCENT, PAPER, GRID, GRID_MAJOR)
from .transport import TCP_PORT
from . import commands
from .layers import cuttable, editable, printable
from .device_lock import device_lock
from .editor_widgets import CollapsibleSection, SafeDoubleSpinBox, SafeSpinBox, WheelGuard
from .themes import THEMES, palette as theme_palette, stylesheet as theme_stylesheet
from .grouping import expand_selection, group_items, ungroup_items, remap_group_ids, selection_units as group_units


def _display_name(name: str) -> str:
    return tr(name) if name in {"矩形", "圓形", "文字輪廓", "路徑"} else name


_PAPER_SIZES_MM = {
    "a4": (210., 297.), "a5": (148., 210.), "a6": (105., 148.),
    "b5": (176., 250.), "letter": (215.9, 279.4),
    "4x6": (101.6, 152.4), "5x7": (127., 177.8),
}


class TaskSignals(QObject):
    succeeded = Signal(object, object)
    failed = Signal(object, str)


class FunctionTask(QRunnable):
    def __init__(self, function: Any) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.function = function
        self.signals = TaskSignals()

    def run(self) -> None:
        try:
            self.signals.succeeded.emit(self, self.function())
        except Exception as error:
            self.signals.failed.emit(self, str(error))


class VeronaWindow(DocumentWorkspace, QMainWindow):
    sendProgress = Signal(int, int)

    def __init__(self, *, host=None, port=None) -> None:
        super().__init__()
        app = QApplication.instance()
        if not hasattr(app, "_verona_wheel_guard"):
            app._verona_wheel_guard = WheelGuard(app)
            app.installEventFilter(app._verona_wheel_guard)
        # Application-wide style and font changes re-polish every open widget.
        if app.style().name().lower() != "fusion":
            app.setStyle("Fusion")
        if app.font() != QFont("Noto Sans", 10):
            app.setFont(QFont("Noto Sans", 10))
        self.setWindowTitle(tr("Verona"))
        self.setWindowIcon(QIcon(str(Path(__file__).with_name("icons") / "verona.svg")))
        self.resize(1280, 800)
        self._documents: list[DocumentState] = [DocumentState()]
        self._active_document = 0
        self._closing = False
        self._closed = False
        self._tasks: dict[FunctionTask, tuple[Any, Any]] = {}
        self.usb_devices: list[Any] = []
        self.sending = False
        self._send_cancel_event: threading.Event | None = None
        self._fit_page_height = True
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._apply_page_fit)
        self._ratio_locked = True
        self._clipboard_items: list[dict[str, Any]] = []
        self.settings = QSettings("Verona", "Workspace")
        self.theme_name = self.settings.value("theme", "light", type=str)
        self._build_ui()
        self.setAcceptDrops(True)
        self.sendProgress.connect(self._send_progress)
        self.wifi_host.setText(host if host is not None else
                               os.environ.get("VERONA_HOST") or self.settings.value("wifi/host", "", type=str))
        self.wifi_port.setValue(port if port is not None else self.settings.value("wifi/port", TCP_PORT, type=int))
        self._build_menu()
        self._refresh_history_actions()
        self._sync_cut_settings()
        self.saved_snapshot = self._document_state()
        self._tab_state()
        self._refresh_tab_titles()
        self._alt_mouse_grab = False
        self._alt_mouse_target = None

    def showEvent(self, event) -> None:
        # Route file drops from child widgets and track viewport resizing.
        # Keep the application-wide filter limited to visible editor windows.
        QApplication.instance().installEventFilter(self)
        super().showEvent(event)
        self._schedule_page_fit()

    def hideEvent(self, event) -> None:
        self._release_alt_grab()
        QApplication.instance().removeEventFilter(self)
        super().hideEvent(event)

    def _release_alt_grab(self) -> None:
        if QWidget.mouseGrabber() is self.canvas:
            self.canvas.releaseMouse()
        self._alt_mouse_grab = False
        self._alt_mouse_target = None

    @staticmethod
    def _forward_mouse_event(target: QWidget, event: QMouseEvent) -> None:
        global_pos = event.globalPosition()
        forwarded = QMouseEvent(
            event.type(), QPointF(target.mapFromGlobal(global_pos.toPoint())), global_pos,
            event.button(), event.buttons(), event.modifiers())
        QApplication.sendEvent(target, forwarded)

    def _handle_alt_mouse_grab_event(self, watched, event) -> bool:
        kind = event.type()
        target = self._alt_mouse_target
        if target is not None and kind in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease):
            if watched is target:
                if kind == QEvent.Type.MouseButtonRelease:
                    self._alt_mouse_target = None
                return False
            if kind == QEvent.Type.MouseButtonRelease:
                self._alt_mouse_target = None
            self._forward_mouse_event(target, event)
            event.accept()
            return True

        if not self._alt_mouse_grab:
            return False
        if kind == QEvent.Type.KeyRelease:
            alt_is_down = bool(QApplication.queryKeyboardModifiers() & Qt.KeyboardModifier.AltModifier)
            if event.key() == Qt.Key.Key_Alt or not alt_is_down:
                self._release_alt_grab()
                if event.key() == Qt.Key.Key_Alt:
                    event.accept()
                    return True
        if kind == QEvent.Type.MouseButtonPress:
            if event.modifiers() & Qt.KeyboardModifier.AltModifier:
                return False
            global_pos = event.globalPosition()
            target = self.childAt(self.mapFromGlobal(global_pos.toPoint()))
            self._release_alt_grab()
            if target is None or target is self.canvas:
                return False
            self._alt_mouse_target = target
            self._forward_mouse_event(target, event)
            event.accept()
            return True
        return False

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if (kind in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop)
                and isinstance(watched, QWidget)
                and (watched is self or self.isAncestorOf(watched))
                and event.mimeData().hasUrls()):
            if kind == QEvent.Type.Drop:
                self.dropEvent(event)
            elif kind == QEvent.Type.DragEnter:
                self.dragEnterEvent(event)
            else:
                self.dragMoveEvent(event)
            return True
        if (kind == QEvent.Type.Resize and hasattr(self, "canvas_scroll")
                and watched is self.canvas_scroll.viewport()):
            self._schedule_page_fit()
        if self._handle_alt_mouse_grab_event(watched, event):
            return True
        if kind in (QEvent.Type.WindowDeactivate, QEvent.Type.Hide, QEvent.Type.FocusOut):
            if watched in (self, self.canvas) and self._alt_mouse_grab:
                self._release_alt_grab()
        if (kind == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Alt
                and QApplication.platformName() == "xcb" and self.isActiveWindow()
                and isinstance(watched, QWidget) and watched.window() is self
                and self.canvas.rect().contains(self.canvas.mapFromGlobal(QCursor.pos()))):
            # X11 desktops can reserve Alt-drag for moving windows. Capture
            # only this canvas gesture; release on Alt-up, focus loss or hide.
            self.canvas.setFocus()
            self.canvas.grabMouse()
            self._alt_mouse_grab = True
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def _schedule_page_fit(self) -> None:
        if self._fit_page_height:
            self._fit_timer.start(0)

    def fit_page_height(self) -> None:
        self._fit_page_height = True
        self.fit_height_action.setChecked(True)
        self._apply_page_fit()

    def _apply_page_fit(self) -> None:
        if not self._fit_page_height or not hasattr(self, "canvas_scroll"):
            return
        self.canvas._zoom_timer.stop()
        self.canvas._zoom_anchor = None
        if not self.canvas._view_offset.isNull():
            self.canvas._view_offset = QPointF()
            self.canvas.update()
        height = self.metadata.get("print_cut", {}).get("paper_height", PAGE_HEIGHT_MM)
        if not isinstance(height, (int, float)) or not math.isfinite(height) or not 0 < height <= PAGE_HEIGHT_MM:
            height = PAGE_HEIGHT_MM
        available = self.canvas_scroll.maximumViewportSize()
        if available.height() <= PAGE_MARGIN * 2:
            return
        zoom = (available.height() - PAGE_MARGIN * 2) / height
        needs_vertical = PAGE_HEIGHT_MM * zoom + PAGE_MARGIN * 2 > available.height() + 1
        width = available.width() - (self.canvas_scroll.verticalScrollBar().sizeHint().width() if needs_vertical else 0)
        if PAGE_WIDTH_MM * zoom + PAGE_MARGIN * 2 > width:
            zoom = (available.height() - self.canvas_scroll.horizontalScrollBar().sizeHint().height()
                    - PAGE_MARGIN * 2) / height
        if abs(self.canvas.zoom - zoom) > 1e-6:
            self.canvas.set_zoom(zoom)
            self.zoom_label.setText(f"{round(self.canvas.zoom * 100)}%")
            self.canvas_scroll.verticalScrollBar().setValue(0)

    def _build_ui(self) -> None:
        self._apply_theme(self.theme_name)
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(12)

        actions = QToolBar(tr("檔案工具"), self)
        actions.setObjectName("designTools")
        actions.setMovable(False)
        actions.setFloatable(False)
        actions.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.design_toolbar = actions
        self._tool(actions, tr("新增設計"), self.new_design)
        self._tool(actions, tr("開啟…"), self.open_design)
        self.import_button = QToolButton()
        self.import_button.setText(tr("匯入"))
        self.import_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        import_menu = QMenu(self.import_button)
        for label, callback in (("匯入圖片", self.import_image), ("匯入 SVG", self.import_svg),
                                ("匯入 PDF／SVG 圖稿", self.import_document_artwork)):
            import_menu.addAction(tr(label), callback)
        self.import_button.setMenu(import_menu)
        actions.addWidget(self.import_button)
        self.print_button = self._tool(actions, tr("列印…"), self.print_design)
        self.undo_action = QAction(tr("復原"), self)
        self.undo_action.setShortcut("Ctrl+Z")
        self.undo_action.triggered.connect(self.undo)
        self.redo_action = QAction(tr("重做"), self)
        self.redo_action.setShortcut("Ctrl+Shift+Z")
        self.redo_action.triggered.connect(self.redo)
        self.view_toolbar = QToolBar(tr("檢視工具"), self)
        self.view_toolbar.setObjectName("viewTools")
        self.view_toolbar.setMovable(False)
        self.view_toolbar.setFloatable(False)
        self.view_toolbar.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._tool(self.view_toolbar, "−", lambda: self.change_zoom(0.8))
        self.zoom_label = QLabel(f"{round(DEFAULT_ZOOM * 100)}%")
        self.zoom_label.setMinimumWidth(44)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view_toolbar.addWidget(self.zoom_label)
        self._tool(self.view_toolbar, "+", lambda: self.change_zoom(1.25))
        self.fit_height_action = QAction(tr("符合頁高"), self)
        self.fit_height_action.setCheckable(True)
        self.fit_height_action.setChecked(self._fit_page_height)
        self.fit_height_action.setShortcut("Ctrl+0")
        self.fit_height_action.triggered.connect(self.fit_page_height)
        self.view_toolbar.addAction(self.fit_height_action)
        top_row = QHBoxLayout()
        top_row.addWidget(actions)
        top_row.addStretch(1)
        top_row.addWidget(self.view_toolbar)
        outer.addLayout(top_row)

        self.tab_bar = QTabBar()
        self.tab_bar.setTabsClosable(True)
        self.tab_bar.setExpanding(False)
        self.tab_bar.setMovable(False)
        self.tab_bar.currentChanged.connect(self._switch_document)
        self.tab_bar.tabCloseRequested.connect(self.close_document)
        outer.addWidget(self.tab_bar)
        self.bounds_warning = QLabel(objectName="boundsWarning")
        self.bounds_warning.setWordWrap(True)
        self.bounds_warning.setTextFormat(Qt.TextFormat.PlainText)
        self.bounds_warning.hide()
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.canvas = DesignCanvas(self)
        self.canvas.geometryChanged.connect(self.refresh_properties)
        scroll = CanvasScrollArea(self.canvas)
        self.canvas_scroll = scroll
        splitter.addWidget(scroll)
        self.inspector_scroll = QScrollArea()
        self.inspector_scroll.setWidgetResizable(True)
        self.inspector_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.inspector_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inspector_scroll.setMinimumWidth(286)
        self.inspector_scroll.setWidget(self._build_inspector())
        inspector_panel = QWidget()
        inspector_layout = QVBoxLayout(inspector_panel)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_layout.addWidget(self.bounds_warning)
        from .layer_panel import LayerPanel
        self.layer_panel = LayerPanel(self)
        self.layer_section = CollapsibleSection(tr("圖層"), self.layer_panel, expanded=True)
        side_splitter = QSplitter(Qt.Orientation.Vertical)
        side_splitter.addWidget(self.layer_section)
        side_splitter.addWidget(self.inspector_scroll)
        side_splitter.setSizes([220, 500])
        side_splitter.setChildrenCollapsible(False)
        self.layer_section.expandedChanged.connect(lambda expanded: side_splitter.setSizes(
            [220, 500] if expanded else [self.layer_section.minimumSizeHint().height(), 720]))
        inspector_panel.setMinimumWidth(370)
        inspector_layout.addWidget(side_splitter)
        output_row = QHBoxLayout()
        self.preview_button = QPushButton(tr("切割預覽"))
        self.preview_button.clicked.connect(self.show_preview)
        output_row.addWidget(self.preview_button)
        self.output_button = QPushButton(tr("匯出 PLT…"))
        self.output_button.clicked.connect(self.export_plt)
        output_row.addWidget(self.output_button)
        inspector_layout.addLayout(output_row)
        self.send_button = QPushButton(tr("傳送至 Juliet…"))
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self.send_to_cutter)
        inspector_layout.addWidget(self.send_button)
        splitter.addWidget(inspector_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([840, 390])
        outer.addWidget(splitter, 1)

        footer = QHBoxLayout()
        self.status_label = QLabel(tr("工作區 300 × 300 mm"))
        self.status_label.setObjectName("muted")
        footer.addWidget(self.status_label)
        footer.addStretch(1)
        self.stop_send_button = QPushButton(tr("停止傳送"))
        self.stop_send_button.setObjectName("stopTransfer")
        self.stop_send_button.setToolTip(tr("停止電腦繼續傳送；已送進機器的資料仍可能執行。"))
        self.stop_send_button.clicked.connect(self.stop_sending)
        footer.addWidget(self.stop_send_button)
        self._update_send_controls()
        outer.addLayout(footer)
        self.setCentralWidget(root)

    def _tool(self, toolbar: QToolBar, label: str, callback: Any) -> QToolButton:
        action = QAction(label, toolbar)
        action.triggered.connect(callback)
        toolbar.addAction(action)
        return toolbar.widgetForAction(action)

    def _build_inspector(self) -> QWidget:
        panel = QFrame(objectName="inspector")
        panel.setMinimumWidth(270)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)
        path_content = QWidget()
        path_layout = QVBoxLayout(path_content)
        path_layout.setContentsMargins(0, 4, 0, 4)
        self.path_section = CollapsibleSection(tr("路徑設定"), path_content)
        self.path_section.setToolTip(tr("Ctrl／Shift 點選可多選；Ctrl／Alt 滾輪以游標為中心縮放；"
                                        "Shift 滾輪水平捲動。名稱、X、Y、寬度與高度按 Enter "
                                        "或移開焦點即可套用。"))
        layout.addWidget(self.path_section)
        geometry = QFormLayout()
        geometry.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        geometry.setVerticalSpacing(4)
        self.object_name = QLineEdit()
        geometry.addRow(tr("名稱"), self.object_name)
        self.width_spin = self._mm_spin()
        self.height_spin = self._mm_spin()
        self.x_spin = self._mm_spin()
        self.x_spin.setRange(-PAGE_WIDTH_MM, PAGE_WIDTH_MM * 2)
        self.y_spin = self._mm_spin()
        self.y_spin.setRange(-PAGE_HEIGHT_MM, 99999)
        dimensions = QGridLayout()
        dimensions.setHorizontalSpacing(6)
        dimensions.setVerticalSpacing(4)
        for index, (label, control, accessible) in enumerate((
                ("X", self.x_spin, "X 起點"), ("Y", self.y_spin, "Y 起點"),
                ("寬度", self.width_spin, "寬度"), ("高度", self.height_spin, "高度"))):
            name = QLabel(tr(label))
            name.setBuddy(control)
            control.setAccessibleName(tr(accessible))
            dimensions.addWidget(name, index // 2, (index % 2) * 2)
            dimensions.addWidget(control, index // 2, (index % 2) * 2 + 1)
        geometry.addRow(dimensions)
        path_layout.addLayout(geometry)
        self.lock_ratio = QCheckBox(tr("鎖定比例"))
        self.lock_ratio.setChecked(True)
        path_layout.addWidget(self.lock_ratio)
        self.cut_checkbox = QCheckBox(tr("包含於切割"))
        self.cut_checkbox.toggled.connect(self._cut_toggled)
        path_layout.addWidget(self.cut_checkbox)
        self.output_mode = QComboBox()
        for label, mode in (("僅列印", "print_only"), ("僅切割", "cut_only"),
                            ("列印＋切割", "both"), ("不輸出", "none")):
            self.output_mode.addItem(tr(label), mode)
        self.output_mode.setPlaceholderText(tr("多種輸出設定"))
        self.output_mode.currentIndexChanged.connect(self._output_mode_changed)
        output = QFormLayout()
        output.addRow(tr("輸出用途"), self.output_mode)
        path_layout.addLayout(output)
        self.width_spin.valueChanged.connect(lambda value: self._dimension_changed("width", value))
        self.height_spin.valueChanged.connect(lambda value: self._dimension_changed("height", value))
        self.selection_edit_buttons = []
        for control in (self.object_name, self.x_spin, self.y_spin,
                        self.width_spin, self.height_spin):
            control.editingFinished.connect(self.apply_size)
        transform_content = QWidget()
        transform_layout = QVBoxLayout(transform_content)
        transform_layout.setContentsMargins(0, 4, 0, 4)
        self.transform_section = CollapsibleSection(tr("變形與群組"), transform_content)
        layout.addWidget(self.transform_section)
        transforms = QGridLayout()
        transforms.setSpacing(6)
        for index, (label, callback) in enumerate((
                ("水平鏡像", lambda: self.transform_selected(mirror_x=True)),
                ("垂直鏡像", lambda: self.transform_selected(mirror_y=True)),
                ("旋轉 −90°", lambda: self.transform_selected(rotation=-90)),
                ("旋轉 +90°", lambda: self.transform_selected(rotation=90)),
                ("群組", self.group_selected), ("解散群組", self.ungroup_selected))):
            button = QPushButton(tr(label))
            if index < 4:
                self.selection_edit_buttons.append(button)
            button.setMinimumHeight(30)
            button.clicked.connect(callback)
            transforms.addWidget(button, index // 2, index % 2)
            if label == "群組":
                self.group_button = button
            elif label == "解散群組":
                self.ungroup_button = button
        for column in range(2):
            transforms.setColumnStretch(column, 1)
        transform_layout.addLayout(transforms)
        alignment_content = QWidget()
        alignment_layout = QVBoxLayout(alignment_content)
        alignment_layout.setContentsMargins(0, 4, 0, 4)
        self.alignment_section = CollapsibleSection(tr("對齊與均分"), alignment_content)
        layout.addWidget(self.alignment_section)
        align_grid = QGridLayout()
        align_grid.setSpacing(6)
        self.alignment_buttons = {}
        for index, (mode, label) in enumerate((
                ("left", "靠左"), ("hcenter", "水平置中"), ("right", "靠右"),
                ("top", "靠上"), ("vcenter", "垂直置中"), ("bottom", "靠下"))):
            button = QPushButton()
            button.setText(tr(label))
            button.setMinimumHeight(30)
            button.clicked.connect(lambda _checked=False, value=mode: self.align_selected(value))
            align_grid.addWidget(button, index // 3, index % 3)
            self.alignment_buttons[mode] = button
        for column in range(3):
            align_grid.setColumnStretch(column, 1)
        alignment_layout.addLayout(align_grid)
        distribution = QHBoxLayout()
        distribution.setSpacing(6)
        for mode, label in (("distribute_h", "水平均分"), ("distribute_v", "垂直均分")):
            button = QPushButton(tr(label))
            button.setMinimumHeight(30)
            button.clicked.connect(lambda _checked=False, value=mode: self.align_selected(value))
            distribution.addWidget(button)
            self.alignment_buttons[mode] = button
        alignment_layout.addLayout(distribution)
        post_edit_content = QWidget()
        post_edit_layout = QVBoxLayout(post_edit_content)
        post_edit_layout.setContentsMargins(0, 4, 0, 4)
        self.post_edit_section = CollapsibleSection(tr("後編輯"), post_edit_content)
        layout.addWidget(self.post_edit_section)
        self.post_edit_summary = QLabel(tr("選取圖片"), objectName="muted")
        self.post_edit_summary.setWordWrap(True)
        post_edit_layout.addWidget(self.post_edit_summary)
        self.smooth_cutline_button = QPushButton(tr("平滑切割線…"))
        self.smooth_cutline_button.clicked.connect(self.smooth_selected_cutlines)
        self.smooth_cutline_button.setEnabled(False)
        post_edit_layout.addWidget(self.smooth_cutline_button)
        self.edit_bleed_button = QPushButton(tr("新增／調整出血…"))
        self.edit_bleed_button.setToolTip(tr("出血可同時套用到多張圖片，不改動切割線。"))
        self.edit_bleed_button.clicked.connect(self.edit_selected_bleed)
        post_edit_layout.addWidget(self.edit_bleed_button)
        self.remove_bleed_button = QPushButton(tr("移除出血"))
        self.remove_bleed_button.clicked.connect(self.remove_selected_bleed)
        post_edit_layout.addWidget(self.remove_bleed_button)
        self.restore_bleed_button = QPushButton(tr("還原原始出血"))
        self.restore_bleed_button.setToolTip(tr("還原所選圖片的原始出血；可選擇原始 LDS 或備份，保留目前圖案與切割線。"))
        self.restore_bleed_button.clicked.connect(self.restore_selected_native_bleed)
        post_edit_layout.addWidget(self.restore_bleed_button)
        self.edit_image_button = QPushButton(tr("編輯圖片遮罩…"))
        self.edit_image_button.setToolTip(tr("遮罩編輯限單張圖片。"))
        self.edit_image_button.clicked.connect(self.edit_selected_image)
        post_edit_layout.addWidget(self.edit_image_button)
        for button in (self.edit_bleed_button, self.remove_bleed_button, self.restore_bleed_button, self.edit_image_button):
            button.setEnabled(False)
        view_content = QWidget()
        view_layout = QVBoxLayout(view_content)
        view_layout.setContentsMargins(0, 4, 0, 4)
        self.view_section = CollapsibleSection(tr("畫布顯示"), view_content)
        layout.addWidget(self.view_section)
        self.cutline_view = QCheckBox(tr("凸顯裁切線"))
        self.cutline_view.setToolTip(tr("淡化圖片並凸顯裁切線"))
        self.cutline_view.toggled.connect(self._toggle_cutline_view)
        view_layout.addWidget(self.cutline_view)
        image_view = QFormLayout()
        self.image_opacity = SafeSpinBox()
        self.image_opacity.setRange(0, 100)
        self.image_opacity.setValue(100)
        self.image_opacity.setSuffix(" %")
        self.image_opacity.valueChanged.connect(lambda: self.canvas.update())
        image_view.addRow(tr("圖片不透明度"), self.image_opacity)
        view_layout.addLayout(image_view)

        cut_content = QWidget()
        cut_layout = QVBoxLayout(cut_content)
        cut_layout.setContentsMargins(0, 4, 0, 4)
        self.cut_section = CollapsibleSection(tr("切割工具"), cut_content)
        layout.addWidget(self.cut_section)
        from .material_presets import MaterialPresetWidget
        self.material_presets = MaterialPresetWidget(self)
        cut_layout.addWidget(self.material_presets)
        settings = QFormLayout()
        settings.setVerticalSpacing(4)
        self.tool_combo = QComboBox()
        self.tool_combo.addItem(tr("刀具"), 0)
        self.tool_combo.addItem(tr("記號筆"), 1)
        settings.addRow(tr("工具"), self.tool_combo)
        self.passes_spin = SafeSpinBox()
        self.passes_spin.setRange(1, 10)
        self.passes_spin.setValue(1)
        self.passes_spin.setToolTip(tr("每條輪廓連續切割指定次數；Print & Cut 只定位一次。"))
        settings.addRow(tr("切割次數"), self.passes_spin)
        cut_layout.addLayout(settings)
        self.custom_settings = QCheckBox(tr("指定速度與壓力"))
        self.custom_settings.toggled.connect(self._toggle_cut_settings)
        cut_layout.addWidget(self.custom_settings)
        cut = QFormLayout()
        cut.setVerticalSpacing(4)
        self.speed_spin = SafeSpinBox()
        self.speed_spin.setRange(1, 13)
        self.speed_spin.setValue(5)
        self.force_spin = SafeSpinBox()
        self.force_spin.setRange(1, 160)
        self.force_spin.setValue(10)
        cut.addRow(tr("速度"), self.speed_spin)
        cut.addRow(tr("壓力"), self.force_spin)
        cut_layout.addLayout(cut)
        self.speed_spin.setEnabled(False)
        self.force_spin.setEnabled(False)
        self.registration_checkbox = QCheckBox(tr("Print & Cut 自動對位").replace("&", "&&"))
        self.registration_checkbox.toggled.connect(self._registration_toggled)
        self.registration_info = QLabel(objectName="muted")
        self.registration_info.setWordWrap(True)
        mark_content = QWidget()
        mark_layout = QVBoxLayout(mark_content)
        mark_layout.setContentsMargins(0, 4, 0, 4)
        self.mark_section = CollapsibleSection(tr("紙張與定位設定"), mark_content)
        layout.addWidget(self.mark_section)
        paper_form = QFormLayout()
        self.paper_size_combo = QComboBox()
        for key, label in (("a4", "A4 · 210 × 297 mm"), ("a5", "A5 · 148 × 210 mm"),
                           ("a6", "A6 · 105 × 148 mm"), ("b5", "B5 · 176 × 250 mm"),
                           ("letter", "Letter · 215.9 × 279.4 mm"),
                           ("4x6", "4 × 6 in · 101.6 × 152.4 mm"),
                           ("5x7", "5 × 7 in · 127 × 177.8 mm"), ("custom", "自訂")):
            self.paper_size_combo.addItem(tr(label), key)
        self.paper_size_combo.currentIndexChanged.connect(self._paper_size_selection_changed)
        paper_form.addRow(tr("紙張大小"), self.paper_size_combo)
        self.paper_orientation_combo = QComboBox()
        self.paper_orientation_combo.addItem(tr("直向"), "portrait")
        self.paper_orientation_combo.addItem(tr("橫向"), "landscape")
        self.paper_orientation_combo.currentIndexChanged.connect(self._paper_orientation_changed)
        paper_form.addRow(tr("方向"), self.paper_orientation_combo)
        custom_paper = QWidget()
        custom_paper_layout = QHBoxLayout(custom_paper)
        custom_paper_layout.setContentsMargins(0, 0, 0, 0)
        self.paper_width_spin = self._mm_spin()
        self.paper_height_spin = self._mm_spin()
        self.paper_width_spin.setRange(1, 300)
        self.paper_height_spin.setRange(1, 300)
        for spin, label in ((self.paper_width_spin, "寬"), (self.paper_height_spin, "高")):
            custom_paper_layout.addWidget(QLabel(tr(label)))
            custom_paper_layout.addWidget(spin)
            spin.valueChanged.connect(self._custom_paper_changed)
        self.custom_paper_fields = custom_paper
        paper_form.addRow(tr("自訂尺寸"), custom_paper)
        mark_layout.addLayout(paper_form)
        self.paper_warning = QLabel()
        self.paper_warning.setObjectName("warning")
        self.paper_warning.setWordWrap(True)
        self.paper_warning.setTextFormat(Qt.TextFormat.PlainText)
        mark_layout.addWidget(self.paper_warning)
        mark_layout.addWidget(self.registration_checkbox)
        mark_layout.addWidget(self.registration_info)
        mark_layout.addWidget(QLabel(tr("標記距紙邊")))
        self.mark_margins = []
        margins_grid = QGridLayout()
        for index, label in enumerate(("左", "上", "右", "下")):
            spin = self._mm_spin()
            spin.setRange(0.75, 299)
            self.mark_margins.append(spin)
            margins_grid.addWidget(QLabel(tr(label)), index // 2, (index % 2) * 2)
            margins_grid.addWidget(spin, index // 2, (index % 2) * 2 + 1)
        mark_layout.addLayout(margins_grid)
        mark_style = QFormLayout()
        mark_style.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.mark_stroke = self._mm_spin()
        self.mark_stroke.setRange(0.5, 1.5)
        self.mark_stroke.setSingleStep(0.1)
        self.mark_stroke.valueChanged.connect(self._update_mark_minimums)
        self.mark_short_length = self._mm_spin()
        self.mark_short_length.setRange(4, 12.5)
        mark_style.addRow(tr("定位線寬"), self.mark_stroke)
        mark_style.addRow(tr("其餘三個角臂長"), self.mark_short_length)
        mark_layout.addLayout(mark_style)
        self.mark_short_length.setToolTip(tr("左下角臂長固定 12.5 mm；無方向三角形。"))
        self.apply_marks_button = QPushButton(tr("套用定位標記"))
        self.apply_marks_button.clicked.connect(self.apply_mark_margins)
        mark_layout.addWidget(self.apply_marks_button)
        self.maximize_marks_button = QPushButton(tr("依印表機邊界最大化"))
        self.maximize_marks_button.clicked.connect(self.maximize_mark_area)
        mark_layout.addWidget(self.maximize_marks_button)
        adjustment = QFormLayout()
        self.registration_offsets = []
        for label in ("切割微調 X", "切割微調 Y"):
            spin = self._mm_spin()
            spin.setRange(-5, 5)
            spin.setDecimals(3)
            spin.setSingleStep(0.025)
            self.registration_offsets.append(spin)
            adjustment.addRow(tr(label), spin)
        mark_layout.addLayout(adjustment)
        for spin in self.registration_offsets:
            spin.setToolTip(tr("正 X 向右、正 Y 向下；請依量測結果調整。"))
        self.apply_offsets_button = QPushButton(tr("套用切割微調"))
        self.apply_offsets_button.clicked.connect(self.apply_registration_offset)
        mark_layout.addWidget(self.apply_offsets_button)
        self._sync_registration_controls()

        connection_content = QWidget()
        connection_layout = QVBoxLayout(connection_content)
        connection_layout.setContentsMargins(0, 4, 0, 4)
        self.connection_section = CollapsibleSection(tr("傳送連線"), connection_content)
        layout.addWidget(self.connection_section)
        self.connection_form = QFormLayout()
        self.connection_form.setVerticalSpacing(4)
        self.connection_mode = QComboBox()
        self.connection_mode.addItem(tr("Wi‑Fi"), "wifi")
        self.connection_mode.addItem(tr("USB"), "usb")
        self.connection_form.addRow(tr("方式"), self.connection_mode)
        self.wifi_host = QLineEdit()
        self.wifi_host.setPlaceholderText(tr("Juliet 螢幕顯示的 IPv4"))
        self.connection_form.addRow(tr("IP 位址"), self.wifi_host)
        self.wifi_port = SafeSpinBox()
        self.wifi_port.setRange(1, 65535)
        self.wifi_port.setValue(TCP_PORT)
        self.connection_form.addRow(tr("連接埠"), self.wifi_port)
        connection_layout.addLayout(self.connection_form)
        self.wifi_check_button = QPushButton(tr("檢查連線"))
        self.wifi_check_button.clicked.connect(self.check_wifi_connection)
        wifi_buttons = QHBoxLayout()
        wifi_buttons.setSpacing(6)
        wifi_buttons.addWidget(self.wifi_check_button)
        self.system_info_button = QPushButton(tr("系統資訊…"))
        self.system_info_button.clicked.connect(self.show_system_info)
        wifi_buttons.addWidget(self.system_info_button)
        connection_layout.addLayout(wifi_buttons)
        self.usb_row = QWidget()
        usb_layout = QHBoxLayout(self.usb_row)
        usb_layout.setContentsMargins(0, 0, 0, 0)
        usb_layout.setSpacing(5)
        self.usb_combo = QComboBox()
        self.usb_combo.addItem(tr("請重新整理 USB 裝置"), None)
        usb_layout.addWidget(self.usb_combo, 1)
        self.usb_refresh = QPushButton(tr("重新整理"))
        self.usb_refresh.clicked.connect(self.refresh_usb_devices)
        usb_layout.addWidget(self.usb_refresh)
        connection_layout.addWidget(self.usb_row)
        self.connection_mode.currentIndexChanged.connect(self._update_connection_mode)
        self._update_connection_mode()
        self.firmware_button = QPushButton(tr("韌體更新（實驗功能）…"))
        self.firmware_button.clicked.connect(self.show_firmware_update)
        connection_layout.addWidget(self.firmware_button)
        for signal in (self.passes_spin.valueChanged, self.speed_spin.valueChanged,
                       self.force_spin.valueChanged, self.custom_settings.toggled,
                       self.tool_combo.currentIndexChanged, self.connection_mode.currentIndexChanged,
                       self.wifi_host.textChanged, self.wifi_port.valueChanged):
            signal.connect(self._sync_cut_settings)
            signal.connect(self._update_cut_summary)
        self.cut_section.expandedChanged.connect(self._update_cut_summary)
        self._update_cut_summary()
        layout.addStretch(1)
        return panel

    @staticmethod
    def _mm_spin() -> QDoubleSpinBox:
        spin = SafeDoubleSpinBox()
        spin.setRange(0.1, PAGE_WIDTH_MM)
        spin.setDecimals(2)
        spin.setSingleStep(1.0)
        spin.setSuffix(" mm")
        return spin

    def _toggle_cut_settings(self, enabled: bool) -> None:
        self.speed_spin.setEnabled(enabled)
        self.force_spin.setEnabled(enabled)

    def _update_cut_summary(self, *_args: Any) -> None:
        summary = ""
        if not self.cut_section.isExpanded():
            if self.custom_settings.isChecked():
                summary = tr("{passes} 次 · 速度 {speed} · 壓力 {force}",
                    passes=self.passes_spin.value(), speed=self.speed_spin.value(), force=self.force_spin.value())
            else:
                summary = tr("{passes} 次 · 機器設定", passes=self.passes_spin.value())
        self.cut_section.setSummary(summary)

    def _cut_toggled(self, checked: bool) -> None:
        self.set_layer_property(sorted(self.selected_indices()), "cut", checked)

    def _update_connection_mode(self, *_args: Any) -> None:
        wifi = self.connection_mode.currentData() == "wifi"
        self.connection_form.setRowVisible(self.wifi_host, wifi)
        self.connection_form.setRowVisible(self.wifi_port, wifi)
        self.wifi_check_button.setVisible(wifi)
        self.system_info_button.setVisible(wifi)
        self.usb_row.setVisible(not wifi)

    def refresh_usb_devices(self) -> None:
        from . import transport
        self.usb_refresh.setEnabled(False)
        self.usb_combo.clear()
        self.usb_combo.addItem(tr("正在列舉 USB 裝置…"), None)
        self._dispatch(transport.usb_devices, self._usb_devices_ready,
                       lambda error: self._usb_devices_failed(error))

    def _usb_devices_ready(self, devices: list[Any]) -> None:
        self.usb_devices = devices
        self.usb_combo.clear()
        if not devices:
            self.usb_combo.addItem(tr("未找到 Juliet USB 裝置"), None)
        for index, device in enumerate(devices, 1):
            bus = getattr(device, "bus", "?")
            address = getattr(device, "address", "?")
            self.usb_combo.addItem(tr("Juliet USB {index} · 匯流排 {bus} / 裝置 {address}", index=index, bus=bus, address=address), index - 1)
        self.usb_refresh.setEnabled(True)

    def _usb_devices_failed(self, error: str) -> None:
        self.usb_combo.clear()
        self.usb_combo.addItem(tr("USB 裝置列舉失敗"), None)
        self.usb_refresh.setEnabled(True)
        QMessageBox.warning(self, tr("USB 列舉失敗"), error)

    def _dispatch(self, function: Any, succeeded: Any, failed: Any) -> None:
        task = FunctionTask(function)
        self._tasks[task] = (succeeded, failed)
        task.signals.succeeded.connect(self._task_succeeded)
        task.signals.failed.connect(self._task_failed)
        QThreadPool.globalInstance().start(task)

    def _remember_wifi(self, host: str, port: int) -> None:
        self.settings.setValue("wifi/host", host)
        self.settings.setValue("wifi/port", port)

    def check_wifi_connection(self) -> None:
        from . import transport
        host, port = self.wifi_host.text().strip(), self.wifi_port.value()
        self.wifi_check_button.setEnabled(False)
        self.status_label.setText(tr("正在檢查 Juliet 連線…"))

        def succeeded(version: str) -> None:
            self.wifi_check_button.setEnabled(True)
            self._remember_wifi(host, port)
            self.status_label.setText(tr("已連線 · 韌體 {version}", version=version))

        def failed(error: str) -> None:
            self.wifi_check_button.setEnabled(True)
            self.status_label.setText(tr("連線檢查失敗"))
            QMessageBox.warning(self, tr("連線檢查失敗"), error)

        def query():
            with device_lock():
                return transport.query_tcp_version(host, port)

        self._dispatch(query, succeeded, failed)

    def show_system_info(self) -> None:
        from .system_info import SystemInfoDialog
        dialog = SystemInfoDialog(self)
        try:
            dialog.refresh()
            dialog.exec()
        finally:
            dialog.deleteLater()

    def show_firmware_update(self) -> None:
        from .firmware_dialog import FirmwareUpdateDialog
        dialog = FirmwareUpdateDialog(self)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    @Slot(object, object)
    def _task_succeeded(self, task: FunctionTask, result: Any) -> None:
        callbacks = self._tasks.pop(task, None)
        if callbacks:
            callbacks[0](result)

    @Slot(object, str)
    def _task_failed(self, task: FunctionTask, error: str) -> None:
        callbacks = self._tasks.pop(task, None)
        if callbacks:
            callbacks[1](error)

    def _send_confirmation(self, payload: bytes, job=None) -> str:
        if job is None:
            from .commands import encode_document_job
            _, job = encode_document_job(self.items, self.metadata, self._cut_settings())
        chosen = {id(contour) for contour in job["contours"]}
        paths = [(item, _path_from_contours([contour for contour in item["contours"]
                  if id(contour) in chosen]).boundingRect()) for item in self.items
                 if any(id(contour) in chosen for contour in item["contours"])]
        tool = tr("刀具") if job["tool"] == 0 else tr("記號筆")
        settings = (tr("速度 {speed} / 壓力 {force}", speed=job["speed"], force=job["force"])
                    if job["force"] is not None else tr("沿用機器目前的速度與壓力"))
        settings += tr(" · 每條輪廓 {passes} 次", passes=job["passes"])
        path_lines = [
            tr("• {name}：X {x}, Y {y} mm；{width} × {height} mm", name=_display_name(item['name']),
               x=f"{rect.left():.2f}", y=f"{rect.top():.2f}", width=f"{rect.width():.2f}", height=f"{rect.height():.2f}")
            for item, rect in paths
        ]
        connection = self._cut_settings()
        target = (tr("Wi‑Fi {host}:{port}", host=connection["host"], port=connection["port"])
                  if connection["connection"] == "wifi" else tr("USB 裝置清單中的 Juliet"))
        details = tr("即將透過 {target} 傳送 {bytes} bytes。\n\n工具：{tool}\n切割設定：{settings}\n\n路徑與尺寸：\n{paths}\n\n傳送成功只表示資料已送出，切割結果仍需在機器端確認。",
                  target=target, bytes=len(payload), tool=tool, settings=settings, paths="\n".join(path_lines))
        if job["registration_bounds"]:
            details = tr("此工作使用 Print & Cut 定位指令，請使用與目前標記設定一致的列印紙張。") + "\n\n" + details
            offset = job["registration_offset"]
            if any(offset):
                details += "\n\n" + tr("切割微調：X {x} mm、Y {y} mm（正值向右／向下）。",
                                             x=f"{offset[0]:+.3f}", y=f"{offset[1]:+.3f}")
        return details

    def send_to_cutter(self) -> None:
        if self.sending:
            return
        if not self.items:
            QMessageBox.information(self, tr("沒有切割路徑"), tr("先匯入 SVG 或新增圖形，再傳送。"))
            return
        settings = self._cut_settings()
        mode = settings["connection"]
        device = None
        host = settings["host"]
        port = settings["port"]
        if mode == "wifi":
            try:
                address = ipaddress.IPv4Address(host)
                if address.is_unspecified or address.is_multicast or str(address) == "255.255.255.255":
                    raise ValueError
            except ValueError:
                QMessageBox.warning(self, tr("IP 位址無效"), tr("請輸入 Juliet 螢幕顯示的單一 IPv4 位址。"))
                return
        else:
            index = self.usb_combo.currentData()
            if index is None or not 0 <= int(index) < len(self.usb_devices):
                QMessageBox.warning(self, tr("未選取 USB 裝置"), tr("請先重新整理並選取 Juliet USB 裝置。"))
                return
            device = self.usb_devices[int(index)]
        try:
            from .commands import encode_document_job
            payload, job = encode_document_job(self.items, self.metadata, settings)
        except Exception as error:
            QMessageBox.warning(self, tr("設計無法傳送"), str(error))
            return

        if not self._confirm_send(self._send_confirmation(payload, job)):
            return

        from . import transport
        self.sending = True
        cancel_event = threading.Event()
        self._send_cancel_event = cancel_event
        self._update_send_controls()
        self.status_label.setText(tr("正在傳送資料…"))
        if mode == "wifi":
            task = lambda: transport.send_tcp(payload, host, port, on_progress=self.sendProgress.emit,
                                              cancel_event=cancel_event)
            self._pending_wifi = (host, port)
        else:
            task = lambda: transport.send_usb(payload, device, cancel_event=cancel_event)
        def send():
            with device_lock():
                return task()

        self._dispatch(send, self._send_succeeded, self._send_failed)

    def _update_send_controls(self) -> None:
        stopping = self._send_cancel_event is not None and self._send_cancel_event.is_set()
        self.send_button.setEnabled(not self.sending)
        self.import_button.setEnabled(not self.sending)
        self.stop_send_button.setVisible(self.sending)
        self.stop_send_button.setEnabled(self.sending and not stopping)
        self.material_presets.refresh_actions()

    def stop_sending(self) -> None:
        if not self.sending or self._send_cancel_event is None:
            return
        self._send_cancel_event.set()
        self._update_send_controls()
        self.status_label.setText(tr("正在停止傳送…"))

    def _confirm_send(self, details: str) -> bool:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle(tr("確認傳送至 Juliet"))
        dialog.setText(details)
        yes = dialog.addButton(tr("是"), QMessageBox.ButtonRole.YesRole)
        dialog.addButton(tr("否"), QMessageBox.ButtonRole.NoRole)
        dialog.setDefaultButton(dialog.buttons()[-1])
        dialog.exec()
        return dialog.clickedButton() is yes

    @Slot(int, int)
    def _send_progress(self, sent: int, total: int) -> None:
        if self.sending and not (self._send_cancel_event and self._send_cancel_event.is_set()):
            self.status_label.setText(tr("正在傳送：{percent}%（{sent}/{total} bytes）",
                                         percent=sent * 100 // total, sent=sent, total=total))

    def _send_succeeded(self, sent: int) -> None:
        if self._send_cancel_event is not None and self._send_cancel_event.is_set():
            self._send_failed(tr("傳送已停止。"))
            return
        if hasattr(self, "_pending_wifi"):
            self._remember_wifi(*self._pending_wifi)
            del self._pending_wifi
        self.sending = False
        self._send_cancel_event = None
        self._update_send_controls()
        self.status_label.setText(tr("已傳送 {sent} bytes", sent=sent))

    def _send_failed(self, error: str) -> None:
        cancelled = self._send_cancel_event is not None and self._send_cancel_event.is_set()
        if hasattr(self, "_pending_wifi"):
            del self._pending_wifi
        self.sending = False
        self._send_cancel_event = None
        self._update_send_controls()
        title = tr("已停止傳送") if cancelled else tr("傳送失敗")
        self.status_label.setText(title)
        if self._closed:
            return
        if cancelled:
            QMessageBox.warning(self, title,
                tr("電腦已停止後續傳送。機器已收到的資料仍可能執行；請確認機器端也已停止。"))
            return
        QMessageBox.warning(
            self, tr("傳送失敗"),
            tr("{error}\n\n傳輸中斷前可能已有部分資料送出，請先檢查 Juliet 狀態。此介面不會自動重送。", error=error),
        )

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu(tr("檔案"))
        new_action = QAction(tr("新增設計"), self)
        new_action.setShortcut("Ctrl+N")
        new_action.triggered.connect(self.new_design)
        file_menu.addAction(new_action)
        for label, shortcut, callback in (
            (tr("開啟設計…"), "Ctrl+O", self.open_design),
            (tr("儲存設計…"), "Ctrl+S", self.save_design),
            (tr("另存設計…"), "Ctrl+Shift+S", lambda: self.save_design(save_as=True)),
            (tr("匯入 SVG…"), "Ctrl+I", self.import_svg),
            (tr("匯入圖片…"), "Ctrl+Shift+I", self.import_image),
            (tr("匯入 PDF／SVG 圖稿…"), "", self.import_document_artwork),
            (tr("匯出 PLT…"), "Ctrl+E", self.export_plt),
        ):
            action = QAction(label, self)
            action.setShortcut(shortcut)
            action.triggered.connect(callback)
            file_menu.addAction(action)
        self.native_preview_action = QAction(tr("原廠預覽…"), self)
        self.native_preview_action.triggered.connect(self.show_native_preview)
        file_menu.addAction(self.native_preview_action)
        self.native_preview_action.setEnabled(bool(self.metadata.get("source_preview")))
        print_action = QAction(tr("列印…"), self)
        print_action.setShortcut("Ctrl+P")
        print_action.triggered.connect(self.print_design)
        file_menu.addAction(print_action)
        print_pdf_action = QAction(tr("匯出列印 PDF…"), self)
        print_pdf_action.triggered.connect(self.export_print_pdf)
        file_menu.addAction(print_pdf_action)
        close_action = QAction(tr("關閉分頁"), self)
        close_action.setShortcut("Ctrl+W")
        close_action.triggered.connect(lambda: self.close_document(self._active_document))
        file_menu.addAction(close_action)
        for label, shortcut, step in (("下一個分頁", "Ctrl+Tab", 1),
                                       ("上一個分頁", "Ctrl+Shift+Tab", -1)):
            action = QAction(tr(label), self)
            action.setShortcut(shortcut)
            action.triggered.connect(lambda _checked=False, delta=step:
                                     self.tab_bar.setCurrentIndex((self._active_document + delta) % len(self._documents)))
            file_menu.addAction(action)
        edit_menu = self.menuBar().addMenu(tr("編輯"))
        self.copy_action = QAction(tr("複製所選物件"), self)
        self.copy_action.setShortcut("Ctrl+C")
        self.copy_action.triggered.connect(self.copy_selected)
        edit_menu.addAction(self.copy_action)
        self.cut_action = QAction(tr("剪下所選物件"), self)
        self.cut_action.setShortcut("Ctrl+X")
        self.cut_action.triggered.connect(self.cut_selected)
        edit_menu.addAction(self.cut_action)
        self.paste_action = QAction(tr("貼上物件"), self)
        self.paste_action.setShortcut("Ctrl+V")
        self.paste_action.triggered.connect(self.paste_selected)
        edit_menu.addAction(self.paste_action)
        self.paste_in_place_action = QAction(tr("原位貼上物件"), self)
        self.paste_in_place_action.setShortcut("Ctrl+Shift+V")
        self.paste_in_place_action.triggered.connect(lambda: self.paste_selected(in_place=True))
        edit_menu.addAction(self.paste_in_place_action)
        edit_menu.addSeparator()
        self.duplicate_action = QAction(tr("複製物件"), self)
        self.duplicate_action.setShortcut("Ctrl+D")
        self.duplicate_action.triggered.connect(self.duplicate_selected)
        edit_menu.addAction(self.duplicate_action)
        self.delete_action = QAction(tr("刪除物件"), self)
        self.delete_action.setShortcuts(["Delete", "Backspace"])
        self.delete_action.triggered.connect(self.delete_selected)
        edit_menu.addAction(self.delete_action)
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        self.deselect_action = QAction(tr("取消選取"), self)
        self.deselect_action.setShortcut("Ctrl+Shift+A")
        self.deselect_action.triggered.connect(lambda: self.select_many([]))
        edit_menu.addAction(self.deselect_action)
        edit_menu.addSeparator()
        order_menu = edit_menu.addMenu(tr("排列順序"))
        self.bring_forward_action = QAction(tr("上移一層"), self)
        self.bring_forward_action.setShortcut("Ctrl+]")
        self.bring_forward_action.triggered.connect(lambda: self.move_layers(1))
        order_menu.addAction(self.bring_forward_action)
        self.send_backward_action = QAction(tr("下移一層"), self)
        self.send_backward_action.setShortcut("Ctrl+[")
        self.send_backward_action.triggered.connect(lambda: self.move_layers(-1))
        order_menu.addAction(self.send_backward_action)
        self.bring_to_front_action = QAction(tr("移至最上層"), self)
        self.bring_to_front_action.setShortcut("Ctrl+Shift+]")
        self.bring_to_front_action.triggered.connect(lambda: self.move_layers(2))
        order_menu.addAction(self.bring_to_front_action)
        self.send_to_back_action = QAction(tr("移至最下層"), self)
        self.send_to_back_action.setShortcut("Ctrl+Shift+[")
        self.send_to_back_action.triggered.connect(lambda: self.move_layers(-2))
        order_menu.addAction(self.send_to_back_action)
        self.group_action = QAction(tr("群組"), self)
        self.group_action.setShortcut("Ctrl+G")
        self.group_action.triggered.connect(self.group_selected)
        edit_menu.addAction(self.group_action)
        self.ungroup_action = QAction(tr("解散群組"), self)
        self.ungroup_action.setShortcut("Ctrl+Shift+G")
        self.ungroup_action.triggered.connect(self.ungroup_selected)
        edit_menu.addAction(self.ungroup_action)
        link_cut = QAction(tr("將選取切線附到圖片"), self)
        link_cut.triggered.connect(self.link_selected_cut_paths)
        edit_menu.addAction(link_cut)
        edit_mask = QAction(tr("編輯圖片遮罩…"), self)
        edit_mask.triggered.connect(self.edit_selected_image)
        edit_menu.addAction(edit_mask)
        nodes = QAction(tr("編輯節點與曲線…"), self)
        nodes.triggered.connect(self.edit_nodes)
        edit_menu.addAction(nodes)
        printed = QAction(tr("設定向量列印…"), self)
        printed.triggered.connect(self.edit_vector_print)
        edit_menu.addAction(printed)
        bleed = QAction(tr("設定列印出血…"), self)
        bleed.triggered.connect(self.edit_selected_bleed)
        edit_menu.addAction(bleed)
        pack = QAction(tr("自動排版…"), self)
        pack.triggered.connect(self.pack_selected)
        edit_menu.addAction(pack)
        self.path_order_action = QAction(tr("自動排序切割路徑（內孔先切）"), self)
        self.path_order_action.setCheckable(True)
        self.path_order_action.setChecked(self.metadata.get("optimize_paths", False))
        self.path_order_action.triggered.connect(self.set_path_order)
        edit_menu.addAction(self.path_order_action)
        self.select_all_action = QAction(tr("全選物件"), self.canvas)
        self.select_all_action.setShortcut("Ctrl+A")
        self.select_all_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self.select_all_action.triggered.connect(lambda: self.select_many(range(len(self.items))))
        self.canvas.addAction(self.select_all_action)
        edit_menu.addAction(self.select_all_action)
        self.insert_menu = self.menuBar().addMenu(tr("插入"))
        for label, callback in (("矩形", self.add_rectangle), ("圓形", self.add_ellipse),
                                ("文字輪廓", self.add_text)):
            self.insert_menu.addAction(tr(label), callback)
        appearance_menu = self.menuBar().addMenu(tr("外觀"))
        self.zoom_in_action = QAction(tr("放大"), self)
        self.zoom_in_action.setShortcuts(["Ctrl+=", "Ctrl++"])
        self.zoom_in_action.triggered.connect(lambda: self.change_zoom(1.25))
        appearance_menu.addAction(self.zoom_in_action)
        self.zoom_out_action = QAction(tr("縮小"), self)
        self.zoom_out_action.setShortcut("Ctrl+-")
        self.zoom_out_action.triggered.connect(lambda: self.change_zoom(0.8))
        appearance_menu.addAction(self.zoom_out_action)
        self.zoom_100_action = QAction(tr("顯示 100%"), self)
        self.zoom_100_action.setShortcut("Ctrl+1")
        self.zoom_100_action.triggered.connect(self.zoom_actual_size)
        appearance_menu.addAction(self.zoom_100_action)
        appearance_menu.addSeparator()
        appearance_menu.addAction(self.fit_height_action)
        appearance_menu.addSeparator()
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        self.theme_actions = {}
        for name, label in (("light", "淺色"), ("dark", "暗色")):
            action = QAction(tr(label), self)
            action.setCheckable(True)
            action.setChecked(self.theme_name == name)
            theme_group.addAction(action)
            appearance_menu.addAction(action)
            action.triggered.connect(lambda _checked=False, value=name: self._change_theme(value))
            self.theme_actions[name] = action
        language_menu = self.menuBar().addMenu(tr("語言"))
        group = QActionGroup(self)
        group.setExclusive(True)
        for code, label in (("en", "English"), ("zh_TW", "繁體中文")):
            action = QAction(tr(label), self)
            action.setCheckable(True)
            action.setChecked(get_language() == code)
            group.addAction(action)
            language_menu.addAction(action)
            action.triggered.connect(lambda _checked=False, value=code: self._change_language(value))
        help_menu = self.menuBar().addMenu(tr("說明"))
        self.system_info_action = QAction(tr("系統資訊…"), self)
        self.system_info_action.triggered.connect(self.show_system_info)
        help_menu.addAction(self.system_info_action)
        self.firmware_action = QAction(tr("韌體更新（實驗功能）…"), self)
        self.firmware_action.triggered.connect(self.show_firmware_update)
        help_menu.addAction(self.firmware_action)
        self.refresh_properties()

    def canvas_context_menu(self) -> QMenu:
        menu = QMenu(self.canvas)
        for action in (self.undo_action, self.redo_action):
            menu.addAction(action)
        menu.addSeparator()
        for action in (self.duplicate_action, self.delete_action):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self.group_action)
        menu.addAction(self.ungroup_action)
        for label, buttons in (
                ("排列順序", (self.layer_panel.up, self.layer_panel.down)),
                ("對齊與均分", self.alignment_buttons.values()),
                ("變形", self.selection_edit_buttons)):
            submenu = menu.addMenu(tr(label))
            for button in buttons:
                action = QAction(button.text(), submenu)
                action.triggered.connect(button.click)
                submenu.addAction(action)
                action.setEnabled(button.isEnabled())
                action.setToolTip(button.toolTip())
            submenu.setEnabled(any(action.isEnabled() for action in submenu.actions()))
        menu.addSeparator()
        indices = self.selected_indices()
        for key, value, label in (
                ("locked", not self._selection_blocked(), "解除鎖定" if self._selection_blocked() else "鎖定"),
                ("visible", not any(self.items[i].get("visible", True) for i in indices),
                 "隱藏" if any(self.items[i].get("visible", True) for i in indices) else "顯示")):
            action = menu.addAction(tr(label))
            action.setEnabled(bool(indices))
            action.triggered.connect(lambda _checked=False, k=key, v=value:
                                     self.set_layer_property(self.selected_indices(), k, v))
        menu.addSeparator()
        menu.addAction(self.select_all_action)
        return menu

    def _apply_theme(self, name: str) -> None:
        self.theme_name = name if name in THEMES else "light"
        self.theme_colors = THEMES[self.theme_name]
        palette = theme_palette(self.theme_colors)
        # An application palette change re-polishes every widget in every
        # window; opening another window with the same theme must not.
        if QApplication.instance().palette() != palette:
            QApplication.instance().setPalette(palette)
        self.setStyleSheet(theme_stylesheet(self.theme_colors))
        for value, action in getattr(self, "theme_actions", {}).items():
            action.setChecked(value == self.theme_name)
        if hasattr(self, "canvas"):
            self.canvas.update()

    def _change_theme(self, name: str) -> None:
        self.settings.setValue("theme", name)
        for window in QApplication.topLevelWidgets():
            if isinstance(window, VeronaWindow):
                window._apply_theme(name)

    def _change_language(self, language: str) -> None:
        if language == get_language():
            return
        self._release_alt_grab()
        snapshot = self._snapshot()
        undo, redo = self.undo_stack, self.redo_stack
        section_names = ("layer_section", "path_section", "transform_section", "alignment_section", "post_edit_section",
                         "view_section", "cut_section", "mark_section", "connection_section")
        sections = {name: getattr(self, name).isExpanded() for name in section_names}
        image_view = self.image_opacity.value(), self.cutline_view.isChecked()
        preset_name = self.material_presets.combo.currentData()
        scroll_position = (self.canvas_scroll.horizontalScrollBar().value(),
                           self.canvas_scroll.verticalScrollBar().value())
        zoom = self.canvas.zoom
        pan_offset = QPointF(self.canvas._view_offset)
        ratio_locked = self.lock_ratio.isChecked()
        was_sending = self.sending
        usb_devices = self.usb_devices[:]
        usb_index = self.usb_combo.currentData()
        set_language(language)
        QSettings("Verona", "Workspace").setValue("language", language)
        self.menuBar().clear()
        self._build_ui()
        self._build_menu()
        self._restore(snapshot)
        self.material_presets.reload(preset_name)
        for name, expanded in sections.items():
            getattr(self, name).setExpanded(expanded)
        self.cutline_view.setChecked(image_view[1])
        self.image_opacity.setValue(image_view[0])
        self.usb_devices = usb_devices
        self.usb_combo.clear()
        if usb_devices:
            for index, device in enumerate(usb_devices, 1):
                self.usb_combo.addItem(tr("Juliet USB {index} · 匯流排 {bus} / 裝置 {address}",
                                          index=index, bus=getattr(device, "bus", "?"),
                                          address=getattr(device, "address", "?")), index - 1)
            if usb_index is not None:
                self.usb_combo.setCurrentIndex(self.usb_combo.findData(usb_index))
        else:
            self.usb_combo.addItem(tr("請重新整理 USB 裝置"), None)
        self.undo_stack, self.redo_stack = undo, redo
        self.canvas.set_zoom(zoom)
        self.canvas._view_offset = pan_offset
        self.zoom_label.setText(f"{round(zoom * 100)}%")
        self.canvas_scroll.horizontalScrollBar().setValue(scroll_position[0])
        self.canvas_scroll.verticalScrollBar().setValue(scroll_position[1])
        self.lock_ratio.setChecked(ratio_locked)
        self.sending = was_sending
        self._update_send_controls()
        if was_sending:
            stopping = self._send_cancel_event is not None and self._send_cancel_event.is_set()
            self.status_label.setText(tr("正在停止傳送…") if stopping else tr("正在傳送資料…"))
        self._refresh_history_actions()
        self._refresh_tab_titles()

    def _selection_blocked(self) -> bool:
        return any(not editable(self.items[i]) for i in self.selected_indices())

    def set_layer_property(self, indices, key, value):
        if key not in ("name", "visible", "locked", "print", "cut"):
            raise ValueError("Unknown layer property.")
        before = self._snapshot()
        for index in indices:
            item = self.items[index]
            if key not in ("visible", "locked") and (not editable(item) or item.get("registration_guide")):
                continue
            if key == "name":
                if value.strip():
                    item[key] = value.strip()
            elif key == "print":
                self._set_layer_print(item, value)
            elif (key == "visible" and value) or (key == "locked" and not value):
                item.pop(key, None)
            else:
                item[key] = bool(value)
        if before["items"] != self.items:
            if key == "print" and value:
                self._ensure_print_paper()
            self.push_undo(before)
        self.layer_panel.fingerprint = None
        self.refresh_all()

    @staticmethod
    def _set_layer_print(item, enabled):
        if enabled:
            item.pop("print", None)
            if "image_data" not in item and not item.get("print_style"):
                item["print_style"] = {"fill": None, "stroke": "#000000", "width_mm": .2}
        else:
            item["print"] = False

    def _ensure_print_paper(self):
        if not self.metadata.get("print_cut", {}).get("bounds"):
            self.metadata["print_cut"] = {"enabled": False, "paper_width": 210., "paper_height": 297.,
                                          "bounds": [6., 6., 204., 291.]}

    def _output_mode_changed(self, _index):
        mode = self.output_mode.currentData()
        if mode is None or self._selection_blocked():
            return
        before = self._snapshot()
        for index in self.selected_indices():
            item = self.items[index]
            if item.get("registration_guide"):
                continue
            self._set_layer_print(item, mode in ("print_only", "both"))
            item["cut"] = mode in ("cut_only", "both")
        if before["items"] != self.items:
            if mode in ("print_only", "both"):
                self._ensure_print_paper()
            self.push_undo(before)
            self.refresh_all()

    def move_layers(self, direction):
        selected = self.selected_indices()
        if not selected or self._selection_blocked():
            return
        order = list(range(len(self.items)))
        if direction > 1:
            order = [index for index in order if index not in selected] + [
                index for index in order if index in selected]
        elif direction < -1:
            order = [index for index in order if index in selected] + [
                index for index in order if index not in selected]
        else:
            positions = range(len(order) - 2, -1, -1) if direction > 0 else range(1, len(order))
            for position in positions:
                neighbor = position + (1 if direction > 0 else -1)
                if order[position] in selected and order[neighbor] not in selected:
                    order[position], order[neighbor] = order[neighbor], order[position]
        if order == list(range(len(self.items))):
            return
        before = self._snapshot()
        self.items = [self.items[index] for index in order]
        self.selection = {new for new, old in enumerate(order) if old in selected}
        self.selected = max(self.selection)
        self.push_undo(before)
        self.refresh_all()

    def select(self, index: int) -> None:
        self.select_many([index] if index >= 0 else [])

    def select_many(self, indices) -> None:
        if getattr(self.canvas, "_nudge_before", None) is not None:
            self.canvas.finish_nudge()
        self.selection = expand_selection(self.items, indices)
        self.selected = max(self.selection, default=-1)
        self.refresh_properties()
        self.canvas.update()

    def selection_units(self) -> list[list[int]]:
        return group_units(self.items, self.selected_indices())

    def selection_bounds(self, indices=None) -> QRectF:
        indices = sorted(self.selected_indices() if indices is None else indices)
        if not indices:
            return QRectF()
        bounds = self.canvas.item_path(self.items[indices[0]]).boundingRect()
        for index in indices[1:]:
            bounds = bounds.united(self.canvas.item_path(self.items[index]).boundingRect())
        return bounds

    def group_selected(self) -> None:
        if self._selection_blocked():
            return
        before = self._snapshot()
        if group_items(self.items, self.selected_indices()):
            self.push_undo(before)
            self.refresh_all()

    def ungroup_selected(self) -> None:
        if self._selection_blocked():
            return
        before = self._snapshot()
        if ungroup_items(self.items, self.selected_indices()):
            self.push_undo(before)
            self.refresh_all()

    def link_selected_cut_paths(self) -> None:
        if self._selection_blocked():
            return
        from .editor_actions import build_edit
        before = self._snapshot()
        try:
            items, selection, _ = build_edit(self.items, self.metadata,
                sorted(self.selected_indices()), "link_cut", {})
        except ValueError:
            QMessageBox.warning(self, tr("無法連動切線"),
                tr("請選取一張圖片與一個獨立切線物件。該切線物件的全部輪廓會取代圖片原本的切線；可復原。"))
            return
        try:
            from .edge_bleed import check_edge_bleed
            check_edge_bleed(items[selection[0]])
        except ValueError as error:
            QMessageBox.warning(self, tr("無法連動切線"), str(error))
            return
        self.items = items
        self.select_many(selection)
        self.push_undo(before)
        self.refresh_all()

    def edit_selected_image(self) -> None:
        if self._selection_blocked():
            return
        from .mask_edit import EditImageDialog
        selected = sorted(self.selected_indices())
        if len(selected) != 1 or "image_data" not in self.items[selected[0]]:
            QMessageBox.warning(self, tr("編輯圖片遮罩"), tr("請選取一張圖片。"))
            return
        try:
            index = selected[0]
            dialog = EditImageDialog(self.items[index], self)
            try:
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return
                item = dialog.build_items()[0]
                if item == self.items[index]:
                    return
                from .edge_bleed import check_edge_bleed
                check_edge_bleed(item)
                before = self._snapshot()
                self.items[index] = item
                self.push_undo(before)
                self.refresh_all()
            finally:
                dialog.deleteLater()
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, tr("編輯圖片遮罩"), str(error))

    def smooth_selected_cutlines(self) -> None:
        if self._selection_blocked() or self.sending:
            return
        indices = sorted(i for i in self.selected_indices()
                         if self.items[i].get("cut", True) and self.items[i].get("contours")
                         and not self.items[i].get("registration_guide"))
        if not indices:
            return
        from .smoothing_dialog import ContourSmoothingDialog
        dialog = ContourSmoothingDialog(self, indices)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            changes = {i: contours for i, contours in dialog.build_contours().items()
                       if contours != self.items[i]["contours"]}
            if changes:
                before = self._snapshot()
                for index, contours in changes.items():
                    item = dict(self.items[index])
                    item["contours"] = contours
                    if "node_paths" in item:
                        item["node_paths"] = {key: path for key, path in item["node_paths"].items()
                                              if contours[int(key)] == self.items[index]["contours"][int(key)]}
                    self.items[index] = item
                self.push_undo(before)
                self.refresh_all()
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, tr("平滑切割線"), str(error))
        finally:
            dialog.deleteLater()

    def edit_selected_bleed(self) -> None:
        if self._selection_blocked():
            return
        indices = sorted(i for i in self.selected_indices() if "image_data" in self.items[i]
                         and not self.items[i].get("registration_guide"))
        if not indices:
            QMessageBox.warning(self, tr("設定列印出血"), tr("請選取一張圖片。"))
            return
        dialog = BleedDialog(self, indices)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            items, selection, _ = dialog.build_items()
            if items != self.items:
                before = self._snapshot()
                self.items = items
                self.select_many(selection)
                self.push_undo(before)
                self.refresh_all()
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, tr("設定列印出血"), str(error))
        finally:
            dialog.deleteLater()

    def remove_selected_bleed(self) -> None:
        if self._selection_blocked():
            return
        indices = sorted(i for i in self.selected_indices() if "image_data" in self.items[i]
                         and not self.items[i].get("registration_guide"))
        if not indices:
            return
        from .editor_actions import build_edit
        items, _, _ = build_edit(self.items, self.metadata, indices, "bleed",
                                {"width_mm": 0, "inner_mm": 0, "color": "#ffffff"})
        if items != self.items:
            before = self._snapshot()
            self.items = items
            self.push_undo(before)
            self.refresh_all()

    def restore_selected_native_bleed(self) -> None:
        if self._selection_blocked():
            return
        indices = sorted(i for i in self.selected_indices() if "image_data" in self.items[i]
                         and not self.items[i].get("registration_guide"))
        if not indices:
            return
        backup = self.current_file + ".bak" if self.current_file else ""
        if not backup or not Path(backup).is_file():
            backup, _ = QFileDialog.getOpenFileName(self, tr("選擇原始 LDS 或備份"), self.current_file or "",
                                                  tr("LDS 原始檔與備份 (*.lds *.lds.bak)"))
            if not backup:
                return
        try:
            from .lds import restore_native_bleed
            items = restore_native_bleed(self.items, indices, backup)
            if items != self.items:
                before = self._snapshot()
                self.items = items
                self.push_undo(before)
                self.refresh_all()
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, tr("還原原始出血"), str(error))

    def pack_selected(self) -> None:
        if self._selection_blocked():
            return
        indices = sorted(self.selected_indices())
        if not indices:
            QMessageBox.warning(self, tr("自動排版"), tr("請先選取物件。"))
            return
        dialog = PackingDialog(self, indices)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            items, selection, _ = dialog.build_items()
            before = self._snapshot()
            self.items = items
            self.select_many(selection)
            self.push_undo(before)
            self.refresh_all()
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, tr("自動排版"), str(error))
        finally:
            dialog.deleteLater()

    def edit_nodes(self):
        if self._selection_blocked():
            return
        from .node_editor import NodeEditorDialog
        indices = sorted(self.selected_indices())
        if len(indices) != 1 or self.items[indices[0]].get("registration_guide"):
            QMessageBox.warning(self, tr("編輯節點與曲線"), tr("請選取一個可編輯的物件。"))
            return
        index = indices[0]
        dialog = NodeEditorDialog(self.items[index], self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.item != self.items[index]:
                from .edge_bleed import check_edge_bleed
                try:
                    check_edge_bleed(dialog.item)
                except ValueError as error:
                    QMessageBox.warning(self, tr("編輯節點與曲線"), str(error))
                    return
                before = self._snapshot()
                self.items[index] = dialog.item
                self.push_undo(before)
                self.refresh_all()
        finally:
            dialog.deleteLater()

    def edit_vector_print(self):
        if self._selection_blocked():
            return
        from .vector_print import VectorPrintDialog
        from .editor_document import build_document_edit
        indices = sorted(self.selected_indices())
        if not indices or any("image_data" in self.items[i] or self.items[i].get("registration_guide") for i in indices):
            QMessageBox.warning(self, tr("設定向量列印"), tr("請選取可列印的向量物件。"))
            return
        dialog = VectorPrintDialog(self.items[indices[0]].get("print_style"), self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                items, _, _, _, _ = build_document_edit(self.items, self.metadata, self._cut_settings(),
                    indices, "vector_print", dialog.print_style())
                if items != self.items:
                    before = self._snapshot()
                    self.items = items
                    self.push_undo(before)
                    self.refresh_all()
        finally:
            dialog.deleteLater()

    def set_path_order(self, enabled):
        from .path_order import analyze_path_order
        if enabled == self.metadata.get("optimize_paths", False):
            return
        try:
            _, details = analyze_path_order(self.items, self.metadata, enabled)
        except ValueError as error:
            self.refresh_all()
            QMessageBox.warning(self, tr("自動排序切割路徑（內孔先切）"), str(error))
            return
        before = self._snapshot()
        if enabled:
            self.metadata["optimize_paths"] = True
        else:
            self.metadata.pop("optimize_paths", None)
        self.push_undo(before)
        self.refresh_all()
        self.status_label.setText(tr("估計空走：{before} → {after} mm；不含定位搜尋與實機耗時。",
            before=f"{details['travel_before_mm']:.2f}", after=f"{details['travel_after_mm']:.2f}")
            if enabled else tr("已還原文件切線順序。"))

    def _toggle_cutline_view(self, enabled: bool) -> None:
        self.image_opacity.setValue(30 if enabled else 100)
        self.canvas.update()

    def align_selected(self, mode: str) -> None:
        if self._selection_blocked():
            return
        from .alignment import alignment_offsets
        units = self.selection_units()
        if len(units) < (3 if mode.startswith("distribute_") else 2):
            return
        bounds = [self.selection_bounds(unit) for unit in units]
        offsets = alignment_offsets([(b.left(), b.top(), b.right(), b.bottom()) for b in bounds], mode)
        if not any(dx or dy for dx, dy in offsets):
            return
        before = self._snapshot()
        for unit, (dx, dy) in zip(units, offsets):
            for index in unit:
                _transform_item(self.items[index], QTransform.fromTranslate(dx, dy))
        self.push_undo(before)
        self.refresh_all()

    def refresh_properties(self) -> None:
        count = len(self.selected_indices())
        enabled = count == 1
        blocked = self._selection_blocked()
        unit_count = 0 if blocked else len(self.selection_units())
        for mode, button in self.alignment_buttons.items():
            button.setEnabled(unit_count >= (3 if mode.startswith("distribute_") else 2))
        self.group_button.setEnabled(unit_count >= 2)
        has_group = not blocked and any(self.items[i].get("group_path") for i in self.selected_indices())
        self.ungroup_button.setEnabled(has_group)
        if hasattr(self, "group_action"):
            self.duplicate_action.setEnabled(count > 0 and not blocked)
            self.delete_action.setEnabled(count > 0 and not blocked)
            self.copy_action.setEnabled(count > 0 and not blocked)
            self.cut_action.setEnabled(count > 0 and not blocked)
            self.paste_action.setEnabled(bool(self._clipboard_items))
            self.paste_in_place_action.setEnabled(bool(self._clipboard_items))
            for action in (self.bring_forward_action, self.send_backward_action,
                           self.bring_to_front_action, self.send_to_back_action):
                action.setEnabled(count > 0 and not blocked)
            self.select_all_action.setEnabled(bool(self.items))
            self.group_action.setEnabled(unit_count >= 2)
            self.ungroup_action.setEnabled(has_group)
        self.object_name.setEnabled(enabled and not blocked)
        for control in (self.width_spin, self.height_spin, self.x_spin, self.y_spin, *self.selection_edit_buttons):
            control.setEnabled(count > 0 and not blocked)
        self.cut_checkbox.blockSignals(True)
        self.cut_checkbox.setChecked(self.items[self.selected].get("cut", True) if enabled else True)
        self.cut_checkbox.setEnabled(enabled and not blocked and not self.items[self.selected].get("registration_guide", False))
        self.cut_checkbox.blockSignals(False)
        modes = set()
        for index in self.selected_indices():
            item = self.items[index]
            prints = printable({**item, "visible": True})
            cuts = item.get("cut", True)
            modes.add("both" if prints and cuts else "print_only" if prints else "cut_only" if cuts else "none")
        self.output_mode.blockSignals(True)
        self.output_mode.setCurrentIndex(self.output_mode.findData(next(iter(modes))) if len(modes) == 1 else -1)
        self.output_mode.setEnabled(count > 0 and not blocked and not any(
            self.items[i].get("registration_guide") for i in self.selected_indices()))
        self.output_mode.blockSignals(False)
        image_indices = [i for i in self.selected_indices() if "image_data" in self.items[i]
                         and not self.items[i].get("registration_guide")]
        self.edit_bleed_button.setEnabled(bool(image_indices) and not blocked)
        self.smooth_cutline_button.setEnabled(not blocked and not self.sending and any(
            self.items[i].get("cut", True) and self.items[i].get("contours")
            and not self.items[i].get("registration_guide") for i in self.selected_indices()))
        self.remove_bleed_button.setEnabled(not blocked and any(self.items[i].get("bleed") for i in image_indices))
        self.restore_bleed_button.setEnabled(bool(image_indices) and not blocked)
        self.edit_image_button.setEnabled(count == 1 and len(image_indices) == 1 and not blocked)
        bleeds = [self.items[i].get("bleed", {}) for i in image_indices]
        if not bleeds:
            summary = tr("選取圖片")
        elif all(bleed == bleeds[0] for bleed in bleeds):
            values = {"outer": f"{bleeds[0].get('width_mm', 0):.3f}",
                      "inner": f"{bleeds[0].get('inner_mm', 0):.3f}"}
            summary = (tr("外側 {outer}／內側 {inner} mm", **values) if len(bleeds) == 1
                       else tr("{count} 張圖片 · 外側 {outer}／內側 {inner} mm", count=len(bleeds), **values))
        else:
            summary = tr("{count} 張圖片 · 多種出血設定", count=len(bleeds))
        self.post_edit_summary.setText(summary)
        self.layer_panel.refresh()
        if count:
            item = self.items[self.selected]
            bounds = self.selection_bounds()
            self.object_name.blockSignals(True)
            self.object_name.setText(_display_name(item.get("name", "路徑")) if enabled else "")
            self.object_name.blockSignals(False)
            self.width_spin.blockSignals(True)
            self.height_spin.blockSignals(True)
            self.width_spin.setValue(max(0.1, bounds.width()))
            self.height_spin.setValue(max(0.1, bounds.height()))
            self.width_spin.blockSignals(False)
            self.height_spin.blockSignals(False)
            self.x_spin.setValue(bounds.left())
            self.y_spin.setValue(bounds.top())
        else:
            self.object_name.clear()
            self.width_spin.setValue(0.1)
            self.height_spin.setValue(0.1)
            self.x_spin.setValue(0)
            self.y_spin.setValue(0)
        self.status_label.setText(
            tr("已選取 {selected} 個物件 · 共 {count} 個物件", selected=count, count=len(self.items))
            if count > 1 else tr("{count} 個路徑 · 工作區 300 × 300 mm", count=len(self.items)))
        self._refresh_bounds_warning()

    def _print_cut_violations(self) -> dict[int, str]:
        from .commands import registration_point_error
        registration = self.metadata.get("print_cut", {})
        if not registration.get("enabled") or not registration.get("bounds"):
            return {}
        violations = {}
        for index, item in enumerate(self.items):
            if not cuttable(item):
                continue
            path = self.canvas.item_path(item)
            if path.isEmpty():
                continue
            # All contours are straight segments. Extrema of the cached path
            # suffice for a rectangular frame, including zero-width/height paths.
            bounds = path.controlPointRect()
            for point in (bounds.topLeft(), bounds.bottomRight()):
                error = registration_point_error(point.x(), point.y(), registration["bounds"],
                                                 registration.get("offset", (0, 0)))
                if error:
                    violations[index] = error
                    break
        return violations

    def _refresh_bounds_warning(self) -> None:
        violations = self._print_cut_violations()
        self.bounds_warning.setVisible(bool(violations))
        self.bounds_warning.setText(tr(
            "Print & Cut：{count} 個物件的切割線出界，無法送件。請移回虛線框內或縮小；也請檢查切割微調。",
            count=len(violations)) if violations else "")
        self.bounds_warning.setToolTip("\n".join(
            f"{_display_name(self.items[index].get('name', '路徑'))}：{tr(error)}"
            for index, error in violations.items()))

    def refresh_all(self) -> None:
        if hasattr(self, "path_order_action"):
            self.path_order_action.blockSignals(True)
            self.path_order_action.setChecked(self.metadata.get("optimize_paths", False))
            self.path_order_action.blockSignals(False)
        # Entries validate themselves against the geometry objects they were
        # built from; only drop those of objects no longer in the document.
        live = {id(item) for item in self.items}
        for key in [key for key in self.canvas._path_cache if key[0] not in live]:
            del self.canvas._path_cache[key]
        self.canvas.update()
        self.refresh_properties()
        self._refresh_history_actions()
        self._sync_registration_controls()
        self._refresh_tab_titles()

    def _sync_registration_controls(self) -> None:
        registration = self.metadata.get("print_cut", {})
        width = registration.get("paper_width", 210.)
        height = registration.get("paper_height", 297.)
        dimensions = (width, height)
        preset = next((key for key, size in _PAPER_SIZES_MM.items()
                       if abs(sorted(dimensions)[0] - sorted(size)[0]) < .05
                       and abs(sorted(dimensions)[1] - sorted(size)[1]) < .05), "custom")
        self.paper_size_combo.blockSignals(True)
        self.paper_size_combo.setCurrentIndex(self.paper_size_combo.findData(preset))
        self.paper_size_combo.blockSignals(False)
        orientation = "landscape" if width > height else "portrait"
        self.paper_orientation_combo.blockSignals(True)
        self.paper_orientation_combo.setCurrentIndex(self.paper_orientation_combo.findData(orientation))
        self.paper_orientation_combo.blockSignals(False)
        for spin, value in ((self.paper_width_spin, width), (self.paper_height_spin, height)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self.custom_paper_fields.setEnabled(preset == "custom")
        self.custom_paper_fields.setVisible(preset == "custom")

        self.registration_checkbox.blockSignals(True)
        self.registration_checkbox.setEnabled(bool(registration))
        self.registration_checkbox.setChecked(registration.get("enabled", False))
        self.registration_checkbox.blockSignals(False)
        bounds = registration.get("bounds")
        if bounds:
            self.registration_info.setText(tr("定位範圍：{width} × {height} mm",
                width=f"{bounds[2] - bounds[0]:.2f}", height=f"{bounds[3] - bounds[1]:.2f}"))
        elif registration.get("enabled"):
            self.registration_info.setText(tr("此原生檔缺少可用的定位標記範圍。"))
        else:
            self.registration_info.setText(tr("一般切割：由機器設定原點。"))
        self.registration_info.setVisible(bool(registration.get("enabled")))
        enabled = bool(registration.get("enabled") and bounds and width > 0 and height > 0)
        margins = ([bounds[0], bounds[1], width - bounds[2], height - bounds[3]]
                   if bounds and width > 0 and height > 0 else [6] * 4)
        from .printing import registration_profile
        stroke, short_length = registration_profile(registration)
        self.mark_stroke.blockSignals(True)
        self.mark_stroke.setValue(stroke)
        self.mark_stroke.blockSignals(False)
        self.mark_short_length.setValue(short_length)
        self.mark_stroke.setEnabled(enabled)
        self.mark_short_length.setEnabled(enabled)
        minimums = registration.get("printer_margins", [0] * 4)
        for spin, value, minimum in zip(self.mark_margins, margins, minimums):
            spin.setMinimum(minimum + stroke / 2)
            spin.setValue(value)
            spin.setEnabled(enabled)
        self.apply_marks_button.setEnabled(enabled)
        self.maximize_marks_button.setEnabled(enabled)
        for spin, value in zip(self.registration_offsets, registration.get("offset", (0, 0))):
            spin.setValue(value)
            spin.setEnabled(enabled)
        self.apply_offsets_button.setEnabled(enabled)
        half = stroke / 2
        outside = bool(bounds and (bounds[0] - half < 0 or bounds[1] - half < 0
                                   or bounds[2] + half > width or bounds[3] + half > height))
        self.paper_warning.setText(tr(
            "定位標記超出設計紙張範圍；定位設定未變更。請調整標記距紙邊，或使用依印表機邊界最大化。"))
        self.paper_warning.setVisible(outside)

    def _paper_size_selection_changed(self, _index: int) -> None:
        key = self.paper_size_combo.currentData()
        custom = key == "custom"
        self.custom_paper_fields.setEnabled(custom)
        self.custom_paper_fields.setVisible(custom)
        if custom or not key:
            return
        width, height = _PAPER_SIZES_MM[key]
        if self.paper_orientation_combo.currentData() == "landscape":
            width, height = height, width
        self.set_design_paper_size(width, height)

    def _paper_orientation_changed(self, _index: int) -> None:
        direction = self.paper_orientation_combo.currentData()
        if not direction:
            return
        key = self.paper_size_combo.currentData()
        if key == "custom":
            registration = self.metadata.get("print_cut", {})
            width = registration.get("paper_width", self.paper_width_spin.value())
            height = registration.get("paper_height", self.paper_height_spin.value())
        else:
            width, height = _PAPER_SIZES_MM.get(key, (210., 297.))
        if direction == "landscape" and width < height or direction == "portrait" and width > height:
            width, height = height, width
        if key == "custom":
            for spin, value in ((self.paper_width_spin, width), (self.paper_height_spin, height)):
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)
        self.set_design_paper_size(width, height)

    def _custom_paper_changed(self, _value: float) -> None:
        if self.paper_size_combo.currentData() != "custom":
            self.paper_size_combo.setCurrentIndex(self.paper_size_combo.findData("custom"))
        width, height = self.paper_width_spin.value(), self.paper_height_spin.value()
        if self.paper_orientation_combo.currentData() == "landscape":
            width, height = max(width, height), min(width, height)
        elif self.paper_orientation_combo.currentData() == "portrait":
            width, height = min(width, height), max(width, height)
        for spin, value in ((self.paper_width_spin, width), (self.paper_height_spin, height)):
            if spin.value() != value:
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)
        self.set_design_paper_size(width, height)

    def set_design_paper_size(self, width: float, height: float) -> None:
        if (any(isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 1 <= value <= 300 for value in (width, height))):
            QMessageBox.warning(self, tr("紙張尺寸無效"), tr("紙張寬度與高度必須介於 1–300 mm。"))
            return
        registration = self.metadata.get("print_cut", {})
        if registration.get("paper_width") == width and registration.get("paper_height") == height:
            return
        before = self._snapshot()
        if not registration:
            registration = {"enabled": False, "paper_width": 210., "paper_height": 297.,
                            "bounds": [6., 6., 204., 291.]}
        registration["paper_width"] = float(width)
        registration["paper_height"] = float(height)
        self.metadata["print_cut"] = registration
        self.push_undo(before)
        self.refresh_all()
        self.status_label.setText(tr("紙張尺寸已變更；定位標記位置未移動。"))

    def apply_registration_offset(self) -> None:
        before = self._snapshot()
        registration = self.metadata["print_cut"]
        old_offset = registration.get("offset")
        registration["offset"] = [spin.value() for spin in self.registration_offsets]
        try:
            self._encoded_plt()
        except ValueError as error:
            if old_offset is None:
                registration.pop("offset")
            else:
                registration["offset"] = old_offset
            QMessageBox.warning(self, tr("無法套用切割微調"), str(error))
            return
        self.push_undo(before)
        self.refresh_all()
        self.status_label.setText(tr("已套用切割微調；列印圖案使用原設計位置。"))

    def _update_mark_minimums(self) -> None:
        minimums = self.metadata.get("print_cut", {}).get("printer_margins", [0] * 4)
        for spin, minimum in zip(self.mark_margins, minimums):
            spin.setMinimum(minimum + self.mark_stroke.value() / 2)

    def _registration_toggled(self, enabled: bool) -> None:
        registration = self.metadata.get("print_cut")
        if not registration:
            return
        before = self._snapshot()
        registration["enabled"] = enabled
        self.push_undo(before)
        self.refresh_all()

    def apply_mark_margins(self) -> None:
        registration = self.metadata["print_cut"]
        left, top, right_margin, bottom_margin = [spin.value() for spin in self.mark_margins]
        right = registration["paper_width"] - right_margin
        bottom = registration["paper_height"] - bottom_margin
        if left >= right or top >= bottom:
            QMessageBox.warning(self, tr("定位範圍無效"), tr("紙邊距太大，沒有剩餘的定位範圍。"))
            return
        before = self._snapshot()
        registration["bounds"] = [left, top, right, bottom]
        registration["mark_stroke"] = self.mark_stroke.value()
        registration["mark_short_length"] = self.mark_short_length.value()
        self.push_undo(before)
        self.refresh_all()
        self.status_label.setText(tr("定位標記已變更，請重新列印。"))

    def maximize_mark_area(self) -> None:
        from PySide6.QtGui import QPageSize, QPageLayout
        from PySide6.QtCore import QSizeF
        from PySide6.QtPrintSupport import QPrinter, QPrinterInfo
        printers = QPrinterInfo.availablePrinters()
        if not printers:
            QMessageBox.warning(self, tr("找不到印表機"), tr("先在 Linux 設定印表機，再讀取可列印邊界。"))
            return
        selected = printers[0]
        if len(printers) > 1:
            name, accepted = QInputDialog.getItem(self, tr("選擇印表機"), tr("印表機"),
                                                  [printer.printerName() for printer in printers], 0, False)
            if not accepted:
                return
            selected = next(printer for printer in printers if printer.printerName() == name)
        registration = self.metadata["print_cut"]
        printer = QPrinter(selected)
        width, height = registration["paper_width"], registration["paper_height"]
        printer.setPageSize(QPageSize(QSizeF(min(width, height), max(width, height)),
                                      QPageSize.Unit.Millimeter))
        set_orientation = getattr(printer, "setPageOrientation", None)
        if set_orientation:
            set_orientation(QPageLayout.Orientation.Landscape if width > height
                            else QPageLayout.Orientation.Portrait)
        page_layout = printer.pageLayout()
        page_layout.setUnits(QPageLayout.Unit.Millimeter)
        margins = page_layout.minimumMargins()
        minimums = [margins.left(), margins.top(), margins.right(), margins.bottom()]
        half = self.mark_stroke.value() / 2
        bounds = [minimums[0] + half, minimums[1] + half,
                  registration["paper_width"] - minimums[2] - half,
                  registration["paper_height"] - minimums[3] - half]
        if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            QMessageBox.warning(self, tr("定位範圍無效"), tr("紙邊距太大，沒有剩餘的定位範圍。"))
            return
        before = self._snapshot()
        registration["mark_stroke"] = self.mark_stroke.value()
        registration["mark_short_length"] = self.mark_short_length.value()
        registration["printer_margins"] = minimums
        registration["bounds"] = bounds
        self.push_undo(before)
        self.refresh_all()
        self.status_label.setText(tr("已依印表機邊界最大化，請重新列印並測試對位。"))

    def change_zoom(self, factor: float) -> None:
        self._fit_page_height = False
        self.fit_height_action.setChecked(False)
        self._fit_timer.stop()
        self.canvas.set_zoom(self.canvas.zoom * factor)
        self.zoom_label.setText(f"{round(self.canvas.zoom * 100)}%")

    def zoom_actual_size(self) -> None:
        self._fit_page_height = False
        self.fit_height_action.setChecked(False)
        self._fit_timer.stop()
        self.canvas.set_zoom(1.0)
        self.zoom_label.setText("100%")

    def _add_path(self, name: str, path: QPainterPath) -> None:
        path.translate(PAGE_WIDTH_MM / 2 - path.boundingRect().center().x(),
                       PAGE_HEIGHT_MM / 2 - path.boundingRect().center().y())
        before = self._snapshot()
        self.items.append({"name": name, "contours": _flatten(path), "cut": True})
        self.push_undo(before)
        self.select(len(self.items) - 1)

    def add_rectangle(self) -> None:
        path = QPainterPath()
        path.addRect(QRectF(0, 0, 40, 25))
        self._add_path(tr("矩形"), path)

    def add_ellipse(self) -> None:
        path = QPainterPath()
        path.addEllipse(QRectF(0, 0, 30, 30))
        self._add_path(tr("圓形"), path)

    def add_text(self) -> None:
        text, accepted = QInputDialog.getText(self, tr("建立文字輪廓"), tr("文字"))
        if not accepted or not text:
            return
        path = QPainterPath()
        font = QFont("Noto Sans")
        font.setPointSizeF(10 * 72 / 25.4)
        path.addText(QPointF(0, 10), font, text)
        self._add_path(tr("文字輪廓"), path)

    def import_svg(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, tr("匯入 SVG"), "", tr("SVG 向量圖 (*.svg)"))
        if not filename:
            return
        try:
            from . import core
            contours = core.load_svg(filename)
            if not contours:
                raise ValueError(tr("SVG 沒有可匯入的向量輪廓。"))
            path = _path_from_contours(contours)
            self._add_path(Path(filename).stem, path)
            self.current_file = None
            self.metadata = {}
            self.native_preview_action.setEnabled(False)
        except Exception as error:
            QMessageBox.critical(self, tr("匯入失敗"), str(error))

    def import_image(self) -> None:
        if self.sending or QApplication.activeModalWidget() is not None:
            return
        filename, _ = QFileDialog.getOpenFileName(
            self, tr("匯入圖片"), "",
            tr("點陣圖片 (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff)"))
        if not filename:
            return
        self.import_image_path(filename)

    @staticmethod
    def _drop_image_paths(mime_data) -> list[str]:
        if not mime_data.hasUrls():
            return []
        paths = []
        for url in mime_data.urls():
            if not url.isLocalFile():
                return []
            path = Path(url.toLocalFile())
            if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}:
                return []
            paths.append(str(path))
        return paths

    def dragEnterEvent(self, event) -> None:
        if not self.sending and QApplication.activeModalWidget() is None and self._drop_image_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        paths = self._drop_image_paths(event.mimeData())
        if self.sending or QApplication.activeModalWidget() is not None or not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        QTimer.singleShot(0, lambda: self._import_dropped_images(paths))

    def _import_dropped_images(self, paths) -> None:
        for filename in paths:
            if not self.import_image_path(filename):
                break

    def import_image_path(self, filename: str) -> bool:
        if self.sending or QApplication.activeModalWidget() is not None:
            return False
        try:
            from PySide6.QtGui import QImageReader
            from .raster_import import ImportImageDialog
            reader = QImageReader(filename)
            reader.setAutoTransform(True)
            size = reader.size()
            if size.width() * size.height() > 16_000_000:
                raise ValueError(tr("圖片尺寸超過 1600 萬像素"))
            image = reader.read()
            if image.isNull():
                raise ValueError(tr("無法讀取圖片或此格式未受支援。"))
            dialog = ImportImageDialog(image, Path(filename).stem, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            self._append_image_items(dialog.build_items(), dialog.mode_combo.currentData())
            return True
        except Exception as error:
            QMessageBox.critical(self, tr("匯入失敗"), str(error))
            return False

    def _append_image_items(self, items, mode):
        if not items:
            raise ValueError(tr("遮罩沒有可匯入的輪廓，請調整去背設定。"))
        before = self._snapshot()
        registration = self.metadata.get("print_cut", {})
        if mode in ("print_cut", "background") and not registration.get("bounds"):
            registration = {"enabled": mode == "print_cut", "paper_width": 210., "paper_height": 297.,
                            "bounds": [6., 6., 204., 291.]}
            self.metadata["print_cut"] = registration
        elif mode == "print_cut":
            registration["enabled"] = True
        if registration.get("bounds"):
            left, top, right, bottom = registration["bounds"]
            all_bounds = _item_path(items[0]).boundingRect()
            for item in items[1:]:
                all_bounds = all_bounds.united(_item_path(item).boundingRect())
            center = all_bounds.center()
            transform = QTransform.fromTranslate((left+right)/2-center.x(), (top+bottom)/2-center.y())
            for item in items:
                _transform_item(item, transform)
        start = len(self.items)
        self.items.extend(items)
        if len(items) > 1:
            group_items(self.items, range(start, len(self.items)))
        self.select_many(range(start, len(self.items)))
        self.push_undo(before)
        self.refresh_all()

    def import_document_artwork(self):
        from .document_import import pdf_page_count, render_artwork
        from .raster_import import ImportImageDialog
        filename, _ = QFileDialog.getOpenFileName(self, tr("匯入 PDF／SVG 圖稿"), "", "PDF / SVG (*.pdf *.svg)")
        if not filename:
            return
        dialog = None
        try:
            page = 1
            if Path(filename).suffix.lower() == ".pdf":
                count = pdf_page_count(filename)
                if count > 1:
                    page, accepted = QInputDialog.getInt(self, tr("匯入 PDF／SVG 圖稿"), tr("頁碼"), 1, 1, count)
                    if not accepted:
                        return
            image, dimensions, _ = render_artwork(filename, page)
            dialog = ImportImageDialog(image, Path(filename).stem, self)
            dialog.setWindowTitle(tr("匯入 PDF／SVG 圖稿"))
            for spin, value in ((dialog.width_spin, dimensions["width_mm"]), (dialog.height_spin, dimensions["height_mm"]),
                                (dialog.dpi_spin, image.width()*25.4/dimensions["width_mm"])):
                spin.blockSignals(True);spin.setValue(value);spin.blockSignals(False)
            dialog.update_preview()
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            self._append_image_items(dialog.build_items(), dialog.mode_combo.currentData())
        except (ValueError, OSError) as error:
            QMessageBox.critical(self, tr("匯入失敗"), str(error))
        finally:
            if dialog is not None:
                dialog.deleteLater()

    @staticmethod
    def _requested(spin: QDoubleSpinBox, actual: float) -> float:
        """The spin value, or the exact geometry when its rounded display was left unchanged."""
        shown = spin.textFromValue(min(max(actual, spin.minimum()), spin.maximum()))
        return actual if spin.textFromValue(spin.value()) == shown else spin.value()

    def apply_size(self) -> None:
        if self._selection_blocked():
            return
        indices = sorted(self.selected_indices())
        if not indices:
            return
        before = self._snapshot()
        bounds = self.selection_bounds()
        if bounds.width() <= 0 or bounds.height() <= 0:
            return
        sx = self._requested(self.width_spin, bounds.width()) / bounds.width()
        sy = self._requested(self.height_spin, bounds.height()) / bounds.height()
        left = self._requested(self.x_spin, bounds.left())
        top = self._requested(self.y_spin, bounds.top())
        if (sx, sy, left, top) != (1, 1, bounds.left(), bounds.top()):
            transform = QTransform()
            transform.translate(left, top)
            transform.scale(sx, sy)
            transform.translate(-bounds.left(), -bounds.top())
            for index in indices:
                _transform_item(self.items[index], transform)
        if len(indices) == 1:
            item = self.items[indices[0]]
            item["name"] = self.object_name.text() or item.get("name", "路徑")
        if before["items"] != self.items:
            self.push_undo(before)
        self.refresh_all()

    def _dimension_changed(self, axis: str, value: float) -> None:
        if not self.lock_ratio.isChecked() or self.selected < 0:
            return
        bounds = self.selection_bounds()
        if bounds.width() <= 0 or bounds.height() <= 0:
            return
        target = self.height_spin if axis == "width" else self.width_spin
        target.blockSignals(True)
        target.setValue(value * (bounds.height() / bounds.width() if axis == "width"
                                 else bounds.width() / bounds.height()))
        target.blockSignals(False)

    def copy_selected(self) -> None:
        if self._selection_blocked():
            return
        indices = sorted(self.selected_indices())
        if not indices:
            return
        self._clipboard_items = _copy_items([self.items[index] for index in indices])
        self.paste_action.setEnabled(True)
        self.paste_in_place_action.setEnabled(True)

    def cut_selected(self) -> None:
        if self._selection_blocked() or not self.selected_indices():
            return
        self.copy_selected()
        self.delete_selected()

    def paste_selected(self, *, in_place: bool = False) -> None:
        if not self._clipboard_items:
            return
        before = self._snapshot()
        copies = _copy_items(self._clipboard_items)
        remap_group_ids(copies)
        if not in_place:
            transform = QTransform.fromTranslate(5, 5)
            for item in copies:
                _transform_item(item, transform)
        for item in copies:
            item["name"] = tr("{name} 複本", name=_display_name(item.get("name", "路徑")))
        start = len(self.items)
        self.items.extend(copies)
        self.push_undo(before)
        self.select_many(range(start, len(self.items)))

    def duplicate_selected(self) -> None:
        if self._selection_blocked():
            return
        indices = sorted(self.selected_indices())
        if not indices:
            return
        before = self._snapshot()
        copies = _copy_items([self.items[i] for i in indices])
        remap_group_ids(copies)
        start = len(self.items)
        for source in copies:
            _transform_item(source, QTransform.fromTranslate(5, 5))
            source["name"] = tr("{name} 複本", name=_display_name(source.get("name", "路徑")))
        self.items.extend(copies)
        self.push_undo(before)
        self.select_many(range(start, len(self.items)))

    def delete_selected(self) -> None:
        if self._selection_blocked():
            return
        indices = self.selected_indices()
        if not indices:
            return
        before = self._snapshot()
        self.items = [item for index, item in enumerate(self.items) if index not in indices]
        self.selected, self.selection = -1, set()
        self.push_undo(before)
        self.refresh_all()

    def transform_selected(self, *, mirror_x: bool = False, mirror_y: bool = False,
                           rotation: float = 0) -> None:
        if self._selection_blocked():
            return
        indices = sorted(self.selected_indices())
        if not indices:
            return
        before = self._snapshot()
        bounds = self.canvas.item_path(self.items[indices[0]]).boundingRect()
        for index in indices[1:]:
            bounds = bounds.united(self.canvas.item_path(self.items[index]).boundingRect())
        center = bounds.center()
        transform = QTransform()
        transform.translate(center.x(), center.y())
        transform.rotate(rotation)
        transform.scale(-1 if mirror_x else 1, -1 if mirror_y else 1)
        transform.translate(-center.x(), -center.y())
        for index in indices:
            _transform_item(self.items[index], transform)
        self.push_undo(before)
        self.refresh_all()

    def show_preview(self) -> None:
        try:
            PreviewDialog(self).exec()
        except Exception as error:
            QMessageBox.warning(self, tr("無法建立切割預覽"), str(error))

    def show_native_preview(self) -> None:
        encoded = self.metadata.get("source_preview")
        if not encoded:
            return
        try:
            pixmap = QPixmap()
            if not pixmap.loadFromData(base64.b64decode(encoded, validate=True), "PNG"):
                raise ValueError(tr("原廠預覽圖片格式無效。"))
            dialog = QDialog(self)
            dialog.setWindowTitle(tr("原廠預覽"))
            layout = QVBoxLayout(dialog)
            label = QLabel()
            label.setPixmap(pixmap.scaled(1000, 800, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
            layout.addWidget(label)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.reject)
            buttons.accepted.connect(dialog.accept)
            layout.addWidget(buttons)
            dialog.exec()
        except (ValueError, binascii.Error) as error:
            QMessageBox.warning(self, tr("無法顯示原廠預覽"), str(error))

    _validate_document = staticmethod(validate_document)

    _validate_contours = staticmethod(validate_contours)

    def export_plt(self) -> None:
        if not self.items:
            QMessageBox.information(self, tr("沒有切割路徑"), tr("先匯入 SVG 或新增圖形，再匯出 PLT。"))
            return
        filename, _ = QFileDialog.getSaveFileName(self, tr("匯出 PLT"), "design.plt", tr("PLT 檔案 (*.plt)"))
        if not filename:
            return
        if not filename.lower().endswith(".plt"):
            filename += ".plt"
        try:
            payload = self._encoded_plt()
            Path(filename).write_bytes(payload)
            count = sum(len(item["contours"]) for item in self.items if cuttable(item))
            self.status_label.setText(tr("已匯出 {name} · {count} 條輪廓", name=Path(filename).name, count=count))
        except Exception as error:
            QMessageBox.critical(self, tr("PLT 匯出失敗"), str(error))

    def print_design(self) -> None:
        from PySide6.QtCore import QSizeF
        from PySide6.QtGui import QPageLayout, QPageSize
        from PySide6.QtPrintSupport import QPrinter, QPrinterInfo
        from .printing import print_document, print_page_size
        from .print_dialog import PrintDialog, apply_printer_settings, printer_settings, validate_printer_settings
        try:
            items, metadata = self.items, self.metadata
            calibration = metadata.get("source_format") == "calibration"
            size = print_page_size(items, metadata, calibration=calibration)
            printers = QPrinterInfo.availablePrinters()
            if not printers:
                QMessageBox.warning(self, tr("找不到印表機"), tr("請先在 Linux 設定印表機，並確認列印服務已啟動。"))
                return
            remembered = metadata.get("printer_settings")
            if remembered is None:
                try:
                    remembered = validate_printer_settings(json.loads(self.settings.value("printing/last", "{}", type=str)))
                except (TypeError, ValueError):
                    remembered = {}
            selected = next((info for info in printers if info.printerName() == remembered.get("printer_name")),
                            next((info for info in printers if info.isDefault()), printers[0]))
            printer = QPrinter(selected, QPrinter.PrinterMode.HighResolution)
            apply_printer_settings(printer, remembered)
            printer.setDocName(Path(self.current_file).stem if self.current_file else tr("未命名設計"))
            width, height = size.width(), size.height()
            printer.setPageSize(QPageSize(QSizeF(min(width, height), max(width, height)), QPageSize.Unit.Millimeter))
            printer.setPageOrientation(QPageLayout.Orientation.Landscape if width > height else QPageLayout.Orientation.Portrait)
            printer.setFullPage(True)
            printer.setDuplex(QPrinter.DuplexMode.DuplexNone)
            dialog = PrintDialog(printer, items, metadata, calibration=calibration,
                                 saved_printer_name=remembered.get("printer_name"), parent=self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            used_settings = printer_settings(printer)
            color_profile_path = getattr(dialog, "color_profile_path", "")
            if not isinstance(color_profile_path, str):
                color_profile_path = ""
            rendering_intent = dialog.intent_combo.currentData()
            if rendering_intent not in ("perceptual", "relative"):
                rendering_intent = "perceptual"
            if color_profile_path:
                print_document(printer, items, metadata, calibration=calibration,
                               color_profile_path=color_profile_path, rendering_intent=rendering_intent)
            else:
                print_document(printer, items, metadata, calibration=calibration)
            dialog.save_color_preferences(self.settings)
            metadata["printer_settings"] = used_settings
            self.settings.setValue("printing/last", json.dumps(used_settings, ensure_ascii=False))
            self._refresh_tab_titles()
            self.status_label.setText(tr("已送至 {name} · 100% 原尺寸", name=printer.printerName()))
        except Exception as error:
            QMessageBox.critical(self, tr("列印失敗"), str(error))

    def export_print_pdf(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(self, tr("匯出列印 PDF"), "print.pdf", tr("PDF 檔案 (*.pdf)"))
        if not filename:
            return
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"
        try:
            from .printing import write_print_pdf
            write_print_pdf(filename, self.items, self.metadata,
                            calibration=self.metadata.get("source_format") == "calibration")
            self.status_label.setText(tr("已匯出 {name} · 列印時選擇 100% 原尺寸", name=Path(filename).name))
        except Exception as error:
            QMessageBox.critical(self, tr("列印 PDF 匯出失敗"), str(error))

    def _encoded_plt(self) -> bytes:
        return commands.encode_document(self.items, self.metadata, self._cut_settings())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verona offline design and cutting workspace")
    parser.add_argument("--lang", choices=("en", "zh_TW"))
    parser.add_argument("--open", dest="open_path")
    parser.add_argument("--host", help="Single cutter IPv4 address; overrides VERONA_HOST")
    parser.add_argument("--port", type=int, help="Cutter TCP port (default: 8080)")
    options, qt_args = parser.parse_known_args(argv)
    from .transport import _ipv4_endpoint
    host = options.host if options.host is not None else os.environ.get("VERONA_HOST") or None
    if host is not None or options.port is not None:
        try:
            _ipv4_endpoint(host if host is not None else "127.0.0.1",
                           options.port if options.port is not None else TCP_PORT)
        except ValueError as error:
            parser.error(str(error))
    settings = QSettings("Verona", "Workspace")
    set_language(options.lang or settings.value("language", os.environ.get("VERONA_LANGUAGE", "en")))
    app = QApplication.instance() or QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("Verona")
    app.setDesktopFileName("verona-zh-TW" if get_language() == "zh_TW" else "verona-en")
    app.setWindowIcon(QIcon(str(Path(__file__).with_name("icons") / "verona.svg")))
    from .desktop_session import DesktopSession
    session = DesktopSession(app)
    try:
        if not session.start([options.open_path] if options.open_path else []):
            return 0
    except OSError as error:
        QMessageBox.critical(None, tr("開啟失敗"), str(error))
        return 1
    app.aboutToQuit.connect(session.close)
    set_language(get_language())
    app.setStyle("Fusion")
    app.setFont(QFont("Noto Sans", 10))
    window = VeronaWindow(host=host, port=options.port)
    session.openRequested.connect(window.open_paths)
    from .recovery import RecoveryController
    try:
        window.recovery = RecoveryController(window)
        QTimer.singleShot(0, window.recovery.restore_available)
    except OSError as error:
        window.status_label.setText(tr("無法啟動自動復原：{reason}", reason=str(error)))
    from .editor_bridge import EditorBridge
    try:
        window.editor_bridge = EditorBridge(window)
        app.aboutToQuit.connect(window.editor_bridge.close)
    except (OSError, RuntimeError) as error:
        window.status_label.setText(tr("AI 協作連線無法啟動：{reason}", reason=str(error)))
    window.show()
    if options.open_path:
        window.open_path(options.open_path)
        if host is not None:
            window.wifi_host.setText(host)
        if options.port is not None:
            window.wifi_port.setValue(options.port)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
