"""Experimental, single-attempt firmware update dialog."""
from pathlib import Path
import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QVBoxLayout, QWidget,
)

from . import firmware, firmware_history, transport
from .editor_widgets import SafeSpinBox
from .firmware import FirmwareUpdateError
from .i18n import tr


class FirmwareUpdateDialog(QDialog):
    progress = Signal(int, int)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self._closed = False
        self._busy = False
        self._updating = False
        self.image = None
        self.image_path = None
        self.identity = None
        self.setWindowTitle(tr("實驗性韌體更新"))
        self.resize(560, 440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(8)
        self.compatibility_note = QLabel(tr("實驗性傳輸 · 韌體相容性未確認"), objectName="muted")
        self.compatibility_note.setWordWrap(True)
        layout.addWidget(self.compatibility_note)

        file_row = QHBoxLayout()
        self.file_button = QPushButton(tr("選擇 BIN…"))
        self.file_button.clicked.connect(self.choose_file)
        file_row.addWidget(self.file_button)
        self.file_label = QLabel(tr("尚未選擇韌體檔案"))
        self.file_label.setTextFormat(Qt.TextFormat.PlainText)
        self.file_label.setWordWrap(True)
        file_row.addWidget(self.file_label, 1)
        layout.addLayout(file_row)
        self.image_details = QLabel(objectName="muted")
        self.image_details.setWordWrap(True)
        layout.addWidget(self.image_details)

        self.form = QFormLayout()
        self.form.setVerticalSpacing(8)
        self.mode = QComboBox()
        self.mode.addItem(tr("Wi‑Fi"), "wifi")
        self.mode.addItem(tr("USB"), "usb")
        initial_mode = owner.connection_mode.currentData() if hasattr(owner, "connection_mode") else "wifi"
        self.mode.setCurrentIndex(max(0, self.mode.findData(initial_mode)))
        self.form.addRow(tr("連線方式"), self.mode)
        self.host = QLineEdit(owner.wifi_host.text().strip())
        self.port = SafeSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(owner.wifi_port.value())
        wifi_row = QWidget()
        wifi_layout = QHBoxLayout(wifi_row)
        wifi_layout.setContentsMargins(0, 0, 0, 0)
        wifi_layout.addWidget(self.host, 1)
        wifi_layout.addWidget(self.port)
        self.wifi_row = wifi_row
        self.form.addRow(tr("Wi‑Fi IP／連接埠"), self.wifi_row)
        self.usb_row = QWidget()
        usb_layout = QHBoxLayout(self.usb_row)
        usb_layout.setContentsMargins(0, 0, 0, 0)
        self.usb_node = QComboBox()
        self.usb_node.addItem(tr("未選取 USB 裝置"), None)
        usb_layout.addWidget(self.usb_node, 1)
        self.usb_refresh = QPushButton(tr("重新整理 USB 節點"))
        self.usb_refresh.clicked.connect(self.refresh_usb_nodes)
        usb_layout.addWidget(self.usb_refresh)
        self.form.addRow(tr("USB 裝置節點"), self.usb_row)
        layout.addLayout(self.form)

        self.read_button = QPushButton(tr("讀取機器資訊"))
        self.read_button.setToolTip(tr("只讀取機器識別資料，不會傳送韌體。"))
        self.read_button.clicked.connect(self.read_identity)
        self.history_button = QPushButton(tr("查看上次更新紀錄"))
        self.history_button.setToolTip(tr("只讀取本機保存的紀錄，不會連線機器。"))
        self.history_button.clicked.connect(self.show_last_history)
        read_row = QHBoxLayout()
        read_row.addWidget(self.read_button)
        read_row.addWidget(self.history_button)
        read_row.addStretch()
        layout.addLayout(read_row)
        self.identity_label = QLabel(tr("尚未讀取機器資訊。"), objectName="muted")
        self.identity_label.setTextFormat(Qt.TextFormat.PlainText)
        self.identity_label.setWordWrap(True)
        layout.addWidget(self.identity_label)
        self.compatible = QCheckBox(tr("我已獨立確認這個 BIN 適用於目前機器。"))
        self.compatible.toggled.connect(self._update_enabled)
        layout.addWidget(self.compatible)
        self.status = QLabel(objectName="muted")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                            | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.history_view = QPlainTextEdit(readOnly=True)
        self.history_view.setMaximumHeight(170)
        self.history_view.hide()
        layout.addWidget(self.history_view)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.start_button = buttons.addButton(tr("開始單次更新…"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.start_button.clicked.connect(self.start_update)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(tr("關閉"))
        buttons.rejected.connect(self.reject)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        layout.addWidget(buttons)

        self.mode.currentIndexChanged.connect(self._connection_changed)
        self.host.textChanged.connect(self._connection_changed)
        self.port.valueChanged.connect(self._connection_changed)
        self.usb_node.currentIndexChanged.connect(self._connection_changed)
        self.progress.connect(self._progress)
        self.finished.connect(self._finished)
        self._update_mode_visibility()
        self._update_enabled()

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("選擇韌體 BIN"), "", tr("韌體檔案 (*.bin);;所有檔案 (*)"))
        if path:
            self.load_file(path)

    def load_file(self, path):
        self.image = None
        self.image_path = None
        self.compatible.setChecked(False)
        self._clear_identity()
        try:
            image = firmware.load_firmware(path)
        except (OSError, TypeError, ValueError) as error:
            self.file_label.setText(Path(path).name)
            self.image_details.setText(tr("韌體檔案檢查失敗：{error}", error=error))
            self.status.setText(tr("未送出任何資料。"))
            self._update_enabled()
            return
        self.image = image
        self.image_path = Path(path)
        self.file_label.setText(self.image_path.name)
        summary = image.summary()
        versions = ", ".join(summary["version_candidates"]) or tr("未找到版本候選字串")
        self.image_details.setText(tr(
            "格式檢查通過 · {bytes} bytes · 候選版本：{versions}",
            bytes=summary["file_bytes"], versions=versions))
        self.status.clear()
        self._update_enabled()

    def _connection_changed(self, *_args):
        self._update_mode_visibility()
        self.compatible.setChecked(False)
        self._clear_identity()
        self._update_enabled()

    def _update_mode_visibility(self):
        wifi = self.mode.currentData() == "wifi"
        self.form.setRowVisible(self.wifi_row, wifi)
        self.form.setRowVisible(self.usb_row, not wifi)

    def _clear_identity(self):
        self.identity = None
        self.identity_label.setText(tr("尚未讀取機器資訊。"))
        self.status.clear()
        self._update_enabled()

    def _endpoint(self):
        if self.mode.currentData() == "wifi":
            host, port = self.host.text().strip(), self.port.value()
            transport._ipv4_endpoint(host, port)
            return host, port, None
        node = self.usb_node.currentData()
        if not node:
            raise ValueError(tr("請先選取 USB 裝置節點。"))
        return None, self.port.value(), node

    def _submit(self, function, succeeded):
        def run():
            try:
                return "ok", function()
            except FirmwareUpdateError as error:
                return "firmware_error", error
            except Exception as error:
                return "error", error

        self.owner._dispatch(run, succeeded, lambda error: succeeded(("error", error)))

    def refresh_usb_nodes(self):
        if self._busy:
            return
        self._clear_identity()
        self._busy = True
        self._set_controls_enabled(False)
        self.usb_node.clear()
        self.usb_node.addItem(tr("正在列舉 USB 節點…"), None)
        self._submit(firmware.list_usb_nodes, self._usb_nodes_ready)

    def _usb_nodes_ready(self, result):
        if self._closed:
            return
        self._busy = False
        self._set_controls_enabled(True)
        self.usb_node.clear()
        if result[0] != "ok":
            self.usb_node.addItem(tr("USB 節點列舉失敗"), None)
            self.status.setText(tr("USB 節點列舉失敗：{error}", error=result[1]))
            return
        nodes = result[1]
        self.usb_node.addItem(tr("未選取 USB 裝置"), None)
        for node in nodes:
            self.usb_node.addItem(node, node)
        self.status.setText(tr("已列出 USB 節點；選取節點不會查詢或傳送資料。"))

    def read_identity(self):
        if self._busy or self._closed:
            return
        self.compatible.setChecked(False)
        self._clear_identity()
        try:
            host, port, node = self._endpoint()
        except ValueError as error:
            self.status.setText(str(error))
            return
        self._busy = True
        self._set_controls_enabled(False)
        self.status.setText(tr("正在讀取機器資訊…"))

        def query():
            return firmware.query_identity(host, port, usb_node=node)

        self._submit(query, self._identity_ready)

    def show_last_history(self):
        if self._busy or self._closed:
            return
        self.history_view.clear()
        self.history_view.hide()
        try:
            record = firmware_history.latest_attempt()
        except Exception as error:
            self.status.setText(tr("無法讀取更新紀錄：{error}", error=type(error).__name__))
            return
        if record is None:
            self.status.setText(tr("找不到先前保存的更新紀錄；舊次錯誤未知。"))
            return
        self.history_view.setPlainText(json.dumps(record, ensure_ascii=False, indent=2))
        self.history_view.show()
        self.status.setText(tr("已載入本機更新紀錄：{status}", status=record["status"]))

    def _identity_ready(self, result):
        if self._closed:
            return
        self._busy = False
        self._set_controls_enabled(True)
        if result[0] != "ok":
            self._clear_identity()
            self.status.setText(tr("讀取失敗：{error}", error=result[1]))
            return
        self.identity = dict(result[1])
        if not {"MODE", "HVER", "SVER"} <= self.identity.keys():
            self._clear_identity()
            self.status.setText(tr("機器回覆缺少必要識別欄位，不能開始更新。"))
            return
        self.identity_label.setText(tr("已讀取：MODE={mode} · HVER={hver} · SVER={sver}",
                                        mode=self.identity.get("MODE", "?"),
                                        hver=self.identity.get("HVER", "?"),
                                        sver=self.identity.get("SVER", "?")))
        self.status.clear()
        self._update_enabled()

    def start_update(self):
        if self._busy or self._closed or self.image is None or self.identity is None:
            return
        if not self.compatible.isChecked():
            return
        try:
            host, port, node = self._endpoint()
        except ValueError as error:
            self.status.setText(str(error))
            return
        answer = QMessageBox.warning(
            self, tr("確認開始韌體更新"),
            tr("這是實驗性單次傳輸，可能覆寫韌體並移動機頭。更新期間請勿關機或拔除 USB／電源。"
               "資料傳送完成不代表版本與正常運作已確認。確定開始嗎？"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            self.status.setText(tr("已取消；未傳送韌體資料。"))
            return
        identity = dict(self.identity)
        image = self.image
        progress_callback = self.progress.emit
        finish_history = self._finish_history
        summary = image.summary()
        file_name = self.image_path.name
        source_path = str(self.image_path)
        try:
            resolved_path = str(self.image_path.resolve())
        except OSError:
            resolved_path = source_path
        redactions = tuple(value for value in (host, self.host.text().strip(), node,
                                               source_path, resolved_path) if value)
        attempt = {
            "file_name": file_name,
            "sha256": image.sha256,
            "candidate_versions": summary["version_candidates"],
            "transport": "usb" if node is not None else "wifi",
            "expected_identity": identity,
        }
        self.history_view.clear()
        self.history_view.hide()
        self._clear_identity()
        self._busy = self._updating = True
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self._set_controls_enabled(False)
        self.close_button.setEnabled(False)
        self.status.setText(tr("更新傳輸中；請勿關機、拔線或關閉此視窗。"))

        def update_with_history():
            try:
                history_path = firmware_history.begin_attempt(**attempt)
            except Exception as error:
                return {"started": False, "history_error": type(error).__name__}
            try:
                result = ("ok", firmware.update_firmware(
                    image, host, expected_sha256=image.sha256,
                    expected_mode=identity["MODE"], expected_hver=identity["HVER"],
                    expected_sver=identity["SVER"], usb_node=node, port=port,
                    on_progress=progress_callback))
            except FirmwareUpdateError as error:
                result = ("firmware_error", error)
            except Exception as error:
                result = ("error", error)
            try:
                finish_history(history_path, result, redactions)
                history_error = None
            except Exception as error:
                history_error = type(error).__name__
            return {"started": True, "result": result, "history_error": history_error}

        self._submit(update_with_history, self._history_update_finished)

    def _history_update_finished(self, response):
        if self._closed:
            return
        if response[0] == "ok":
            response = response[1]
        else:
            self._busy = self._updating = False
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.hide()
            self._set_controls_enabled(True)
            self.close_button.setEnabled(True)
            self.status.setText(tr("更新結果未知；請先查看本機紀錄與機器狀態，不要自動重試。"))
            return
        if not response["started"]:
            self._busy = self._updating = False
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.hide()
            self._set_controls_enabled(True)
            self.close_button.setEnabled(True)
            self.status.setText(tr("更新未開始：無法保存本機紀錄（{error}）；沒有傳送韌體資料。",
                                   error=response["history_error"]))
            return
        self._update_finished(response["result"], response["history_error"])

    def _update_finished(self, result, history_error=None):
        if self._closed:
            return
        self._busy = self._updating = False
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.hide()
        self._set_controls_enabled(True)
        self.close_button.setEnabled(True)
        if result[0] == "ok":
            self.status.setText(tr("資料已傳送；韌體版本與機器正常運作尚待確認。請檢查機器狀態，再按「讀取機器資訊」確認版本。"))
        elif result[0] == "firmware_error":
            details = result[1].as_dict()
            self.status.setText(tr("更新失敗 · 階段：{stage} · 本機至少已送出 header {header} bytes、body {body} bytes。"
                                   "狀態未知；不會自動重試或重新讀取。\n{error}",
                                   stage=details.get("stage", "unknown"),
                                   header=details.get("bytes_header_sent", 0),
                                   body=details.get("bytes_body_sent", 0),
                                   error=details.get("error", "")))
        else:
            self.status.setText(tr("更新失敗；狀態未知，不會自動重試或重新讀取。\n{error}", error=result[1]))
        if history_error is not None:
            self.status.setText(self.status.text() + "\n" + tr(
                "傳輸結果如上；更新紀錄保存失敗（{error}）。", error=history_error))
        self._update_enabled()

    @staticmethod
    def _finish_history(history_path, result, redactions):
        if result[0] == "ok":
            outcome = result[1]
            firmware_history.finish_attempt(
                history_path, status="pgok_received", stage="completion",
                bytes_header_sent=outcome.get("bytes_header_sent"),
                bytes_body_sent=outcome.get("bytes_body_sent"),
                counts_lower_bounds=True)
            return
        error = result[1]
        details = error.as_dict() if isinstance(error, FirmwareUpdateError) else {}
        message = str(error) if isinstance(error, FirmwareUpdateError) else None
        error_errno = getattr(error, "errno", None)
        if error_errno is None and error.__cause__ is not None:
            error_errno = getattr(error.__cause__, "errno", None)
        for private_value in redactions:
            if private_value:
                message = message.replace(str(private_value), "[endpoint/path]") if message else message
        firmware_history.finish_attempt(
            history_path, status="failed", stage=details.get("stage", "unknown"),
            bytes_header_sent=details.get("bytes_header_sent"),
            bytes_body_sent=details.get("bytes_body_sent"),
            error_type=type(error).__name__, error_errno=error_errno,
            error_message=message, counts_lower_bounds=True)

    def _progress(self, sent, total):
        if total > 0:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(min(1000, sent * 1000 // total))

    def _set_controls_enabled(self, enabled):
        for widget in (self.file_button, self.mode, self.host, self.port, self.usb_node,
                       self.usb_refresh, self.read_button, self.history_button, self.compatible):
            widget.setEnabled(enabled)
        self._update_enabled()

    def _update_enabled(self, *_args):
        self.start_button.setEnabled(bool(
            not self._busy and self.image is not None and self.identity is not None
            and {"MODE", "HVER", "SVER"} <= self.identity.keys()
            and self.compatible.isChecked()))

    def reject(self):
        if self._updating:
            return
        super().reject()

    def closeEvent(self, event):
        if self._updating:
            event.ignore()
            return
        self._closed = True
        super().closeEvent(event)

    def _finished(self, _result):
        self._closed = True
