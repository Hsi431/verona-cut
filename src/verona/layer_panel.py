"""Object layers in front-to-back order, using the editor's document and undo."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHeaderView, QHBoxLayout, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from .i18n import tr
from .layers import printable


class LayerPanel(QFrame):
    def __init__(self, owner):
        super().__init__(objectName="layers")
        self.owner = owner
        self.fingerprint = None
        self.object_order = ()
        self.setMinimumHeight(145)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        heading = QHBoxLayout()
        heading.addWidget(QLabel(tr("圖層"), objectName="title"))
        heading.addStretch()
        self.up = QPushButton(tr("上移"))
        self.down = QPushButton(tr("下移"))
        self.up.setToolTip(tr("將選取圖層往前移一層"))
        self.down.setToolTip(tr("將選取圖層往後移一層"))
        self.up.clicked.connect(lambda: owner.move_layers(1))
        self.down.clicked.connect(lambda: owner.move_layers(-1))
        heading.addWidget(self.up)
        heading.addWidget(self.down)
        layout.addLayout(heading)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels([tr(value) for value in ("物件", "顯示", "鎖定", "列印", "切割")])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self.tree.itemDoubleClicked.connect(lambda item, column: self.tree.editItem(item, 0) if column == 0 else None)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            self.tree.header().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setAccessibleName(tr("圖層清單；最上方物件位於最前方"))
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        self.tree.itemChanged.connect(self._changed)
        layout.addWidget(self.tree)

    def refresh(self):
        items = self.owner.items
        fingerprint = tuple((id(item), item.get("name"), item.get("visible", True),
                             item.get("locked", False), bool(printable({**item, "visible": True})),
                             item.get("cut", True), item.get("registration_guide", False)) for item in items)
        self.tree.blockSignals(True)
        if fingerprint != self.fingerprint:
            position = self.tree.verticalScrollBar().value()
            order = tuple(id(item) for item in items)
            rebuild = order != self.object_order or self.tree.topLevelItemCount() != len(items)
            if rebuild:
                self.tree.clear()
            for index in range(len(items) - 1, -1, -1):
                item = items[index]
                row = QTreeWidgetItem() if rebuild else self.tree.topLevelItem(len(items) - 1 - index)
                row.setText(0, item["name"])
                row.setData(0, Qt.ItemDataRole.UserRole, index)
                flags = (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                         | Qt.ItemFlag.ItemIsUserCheckable)
                if not item.get("locked") and not item.get("registration_guide"):
                    flags |= Qt.ItemFlag.ItemIsEditable
                row.setFlags(flags)
                for column in (0, 3, 4):
                    row.setToolTip(column, tr("請先解鎖圖層再編輯。") if item.get("locked") else "")
                values = (item.get("visible", True), item.get("locked", False),
                          printable({**item, "visible": True}), item.get("cut", True))
                for column, value in enumerate(values, 1):
                    row.setCheckState(column, Qt.CheckState.Checked if value else Qt.CheckState.Unchecked)
                    row.setTextAlignment(column, Qt.AlignmentFlag.AlignCenter)
                if rebuild:
                    self.tree.addTopLevelItem(row)
            self.object_order = order
            self.fingerprint = fingerprint
            self.tree.verticalScrollBar().setValue(position)
        selected = self.owner.selected_indices()
        for index in range(self.tree.topLevelItemCount()):
            row = self.tree.topLevelItem(index)
            row.setSelected(row.data(0, Qt.ItemDataRole.UserRole) in selected)
        self.tree.blockSignals(False)
        movable = bool(selected) and not self.owner._selection_blocked()
        self.up.setEnabled(movable and any(i + 1 < len(items) and i + 1 not in selected for i in selected))
        self.down.setEnabled(movable and any(i > 0 and i - 1 not in selected for i in selected))

    def _selection_changed(self):
        self.owner.select_many([row.data(0, Qt.ItemDataRole.UserRole) for row in self.tree.selectedItems()])

    def _changed(self, row, column):
        index = row.data(0, Qt.ItemDataRole.UserRole)
        key = ("name", "visible", "locked", "print", "cut")[column]
        value = row.text(0) if column == 0 else row.checkState(column) == Qt.CheckState.Checked
        self.owner.set_layer_property([index], key, value)
