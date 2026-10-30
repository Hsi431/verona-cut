"""Read-only cutter identity dialog using the editor's background dispatcher."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout,
)

from . import transport
from .device_lock import device_lock
from .i18n import tr


_FIELDS = (
    ("model", "機型代碼"),
    ("software_version", "韌體版本"),
    ("hardware_version", "硬體版本"),
    ("bs_version", "版本欄位（BSVER）"),
    ("date_code", "日期代碼（MDATE）"),
    ("program_header", "程式標記（PGHEAD）"),
)


class SystemInfoDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.host = owner.wifi_host.text().strip()
        self.port = owner.wifi_port.value()
        self._closed = self._busy = False
        self.info = None
        self.setWindowTitle(tr("系統資訊"))
        self.resize(520, 390)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(8)
        self.endpoint = QLabel(tr("Wi‑Fi {host}:{port}", host=self.host, port=self.port)
                               if self.host else tr("尚未設定 Wi-Fi 位址"), objectName="muted")
        self.endpoint.setTextFormat(Qt.TextFormat.PlainText)
        self.endpoint.setWordWrap(True)
        layout.addWidget(self.endpoint)
        self.values = {}
        form = QFormLayout()
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(8)
        for key, label in _FIELDS:
            field_label = QLabel(tr(label))
            if key in {"bs_version", "date_code", "program_header"}:
                field_label.setToolTip(tr("此欄位為機器回報原值，含義尚未確認。"))
            value = QLabel("—")
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                          | Qt.TextInteractionFlag.TextSelectableByKeyboard)
            value.setWordWrap(True)
            form.addRow(field_label, value)
            self.values[key] = value
        layout.addLayout(form)
        layout.addStretch()
        self.status = QLabel(objectName="muted")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.refresh_button = buttons.addButton(tr("重新讀取"), QDialogButtonBox.ButtonRole.ActionRole)
        self.refresh_button.setToolTip(tr("只讀取資訊；不會移動機器，也不能確認機器閒置或裁切完成。"))
        self.refresh_button.clicked.connect(self.refresh)
        self.copy_button = buttons.addButton(tr("複製資訊"), QDialogButtonBox.ButtonRole.ActionRole)
        self.copy_button.setToolTip(tr("複製內容會排除 IP 位址與裝置識別資料。"))
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_info)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(tr("關閉"))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.finished.connect(self._finished)

    def refresh(self):
        if self._closed or self._busy:
            return
        self.info = None
        self.copy_button.setEnabled(False)
        for value in self.values.values():
            value.setText("—")
        try:
            transport._ipv4_endpoint(self.host, self.port)
        except ValueError:
            self.status.setText(tr("請先在「傳送連線」填入切割機的有效 Wi-Fi IP 位址。"))
            return
        self._busy = True
        self.refresh_button.setEnabled(False)
        self.status.setText(tr("正在讀取切割機資訊…"))
        host, port = self.host, self.port

        def query():
            with device_lock():
                return transport.query_tcp_info(host, port)

        self.owner._dispatch(query, self._ready, self._failed)

    def _ready(self, result):
        if self._closed:
            return
        self._busy = False
        self.refresh_button.setEnabled(True)
        self.info = {key: result["identity"][key] for key, _ in _FIELDS if key in result["identity"]}
        for key, value in self.values.items():
            value.setText(self.info.get(key, tr("未回報")))
        self.copy_button.setEnabled(True)
        self.status.setText(tr("已讀取切割機資訊"))

    def _failed(self, error):
        if self._closed:
            return
        self._busy = False
        self.refresh_button.setEnabled(True)
        self.status.setText(tr("讀取失敗：{error}\n請確認機器已開機、IP 正確，並連到相同區域網路。", error=error))

    def copy_info(self):
        if self._closed or self.info is None:
            return
        lines = [tr("Verona 系統資訊")]
        lines.extend(f"{tr(label)}: {self.info.get(key, tr('未回報'))}" for key, label in _FIELDS)
        QApplication.clipboard().setText("\n".join(lines))
        self.status.setText(tr("已複製資訊，未包含 IP 位址與裝置識別資料。"))

    def _finished(self, _result):
        self._closed = True
