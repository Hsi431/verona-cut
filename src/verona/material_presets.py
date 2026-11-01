"""Named, user-tested cut settings and their inspector controls."""
import json

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QInputDialog, QMessageBox,
                              QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from .design_validation import validate_document
from .i18n import tr


_KEY = "material_presets/v1"
_PARAMETERS = ("custom", "speed", "force", "passes", "tool")


def preset_name(name):
    if not isinstance(name, str):
        raise ValueError(tr("預設名稱需為 1–80 個字元。"))
    name = name.strip()
    if not 1 <= len(name) <= 80 or any(ord(character) < 32 for character in name):
        raise ValueError(tr("預設名稱需為 1–80 個字元。"))
    return name


def preset_parameters(values):
    if not isinstance(values, dict) or set(values) != set(_PARAMETERS):
        raise ValueError(tr("材料預設的切割設定無效。"))
    _, settings = validate_document({"format": "verona", "version": 1, "units": "mm",
                                     "items": [], "settings": values})
    return {key: settings[key] for key in _PARAMETERS}


class MaterialPresetStore:
    def __init__(self, settings):
        self.settings = settings

    def load(self):
        raw = self.settings.value(_KEY, "", type=str)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            if (not isinstance(data, dict) or type(data.get("version")) is not int
                    or data["version"] != 1 or not isinstance(data.get("presets"), dict)):
                raise ValueError
            presets = {}
            for name, values in data["presets"].items():
                if name != preset_name(name) or name.casefold() in {key.casefold() for key in presets}:
                    raise ValueError
                presets[name] = preset_parameters(values)
            return presets
        except (ValueError, TypeError) as error:
            raise ValueError(tr("無法讀取材料預設，已保留原設定。")) from error

    def _write(self, presets):
        previous = self.settings.value(_KEY)
        self.settings.setValue(_KEY, json.dumps({"version": 1, "presets": presets}, ensure_ascii=False))
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            if previous is None:
                self.settings.remove(_KEY)
            else:
                self.settings.setValue(_KEY, previous)
            raise OSError(tr("無法儲存材料預設。"))

    def save(self, name, values, *, overwrite=False):
        name = preset_name(name)
        values = preset_parameters(values)
        presets = self.load()
        existing = next((key for key in presets if key.casefold() == name.casefold()), None)
        if existing is not None and not overwrite:
            raise FileExistsError(name)
        name = existing or name
        presets[name] = values
        self._write(presets)
        return name

    def delete(self, name):
        presets = self.load()
        if name not in presets:
            raise ValueError(tr("請先選擇材料預設。"))
        del presets[name]
        self._write(presets)


class MaterialPresetWidget(QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.store = MaterialPresetStore(owner.settings)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.combo = QComboBox()
        self.combo.setAccessibleName(tr("材料／刀具預設"))
        self.combo.setMinimumContentsLength(8)
        self.combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.combo)
        row = QHBoxLayout()
        self.apply_button = QPushButton(tr("套用"))
        self.save_button = QPushButton(tr("儲存預設…"))
        self.delete_button = QPushButton(tr("刪除"))
        for button in (self.apply_button, self.save_button, self.delete_button):
            row.addWidget(button)
        layout.addLayout(row)
        self.apply_button.clicked.connect(self.apply_selected)
        self.save_button.clicked.connect(self.save_current)
        self.delete_button.clicked.connect(self.delete_selected)
        self.combo.currentIndexChanged.connect(self.refresh_actions)
        self.reload()

    def reload(self, selected=None):
        self.combo.blockSignals(True)
        self.combo.clear()
        self.error = None
        try:
            presets = self.store.load()
            self.combo.addItem(tr("選擇材料／刀具預設"), None)
            for name in sorted(presets, key=str.casefold):
                self.combo.addItem(name, name)
            if selected is not None:
                self.combo.setCurrentIndex(max(0, self.combo.findData(selected)))
        except ValueError as error:
            self.error = str(error)
            self.combo.addItem(tr("無法讀取材料預設"), None)
        self.combo.setToolTip(self.error or tr("選擇後按「套用」；不會變更連線設定。"))
        self.combo.blockSignals(False)
        self.refresh_actions()

    def refresh_actions(self, *_args):
        selected = self.combo.currentData() is not None and self.error is None
        self.apply_button.setEnabled(selected and not self.owner.sending)
        self.delete_button.setEnabled(selected)
        self.save_button.setEnabled(self.error is None)

    def apply_selected(self):
        if self.owner.sending or self.combo.currentData() is None:
            return
        try:
            values = self.store.load().get(self.combo.currentData())
            if values is None:
                raise ValueError(tr("請先選擇材料預設。"))
            settings = {**self.owner._cut_settings(), **values}
            if settings == self.owner._cut_settings():
                return
            before = self.owner._snapshot()
            self.owner._set_cut_settings(settings)
            self.owner.push_undo(before)
            self.owner.refresh_all()
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, tr("材料／刀具預設"), str(error))

    def save_current(self):
        try:
            presets = self.store.load()
            name, accepted = QInputDialog.getText(self, tr("儲存材料預設"), tr("名稱"),
                                                 text=self.combo.currentData() or "")
            if not accepted:
                return
            name = preset_name(name)
            exists = any(key.casefold() == name.casefold() for key in presets)
            if exists and QMessageBox.question(self, tr("覆寫材料預設"),
                    tr("要覆寫「{name}」嗎？", name=name),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            values = {key: self.owner._cut_settings()[key] for key in _PARAMETERS}
            name = self.store.save(name, values, overwrite=exists)
            self.reload(name)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, tr("材料／刀具預設"), str(error))

    def delete_selected(self):
        name = self.combo.currentData()
        if name is None:
            return
        if QMessageBox.question(self, tr("刪除材料預設"), tr("要刪除「{name}」嗎？", name=name),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.delete(name)
            self.reload()
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, tr("材料／刀具預設"), str(error))
