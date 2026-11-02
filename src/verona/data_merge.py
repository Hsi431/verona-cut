"""CSV parsing, validation, preview and document generation for data merges."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from string import Formatter
from uuid import uuid4

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QGridLayout, QHBoxLayout,
                               QLabel, QPlainTextEdit, QTableWidget, QTableWidgetItem,
                               QPushButton, QVBoxLayout)

from .editor_actions import editable_indices, finite_number
from .geometry import copy_items as _copy_items, flatten_path as _flatten
from .grouping import remap_group_ids
from .i18n import tr


DATA_MERGE_PARAMETER_NAMES = {
    "rows", "csv_text", "auto_pack", "gap_mm", "margin_mm", "allow_rotate",
    "shape_aware", "fill", "rotation_step_deg", "time_limit_seconds",
    "new_page_for_unplaced",
}

PACKING_PARAMETER_NAMES = DATA_MERGE_PARAMETER_NAMES - {"rows", "csv_text", "auto_pack"}


def parse_merge_table(parameters):
    if not isinstance(parameters, dict):
        raise ValueError(tr("資料組參數必須是物件。"))
    has_rows, has_csv = "rows" in parameters, "csv_text" in parameters
    if has_rows == has_csv:
        raise ValueError(tr("請提供 rows 或 csv_text 其中一種資料。"))
    if has_rows:
        records = parameters["rows"]
        if not isinstance(records, list) or not records:
            raise ValueError(tr("資料組至少需要一列資料。"))
        if len(records) > 1000:
            raise ValueError(tr("資料組最多支援 1000 列。"))
        if any(not isinstance(row, dict) or not row for row in records):
            raise ValueError(tr("每列資料都必須是非空欄位物件。"))
        if any(not isinstance(key, str) or not key.strip() for row in records for key in row):
            raise ValueError(tr("欄位名稱必須是非空文字。"))
        normalized_records = [{key.strip(): value for key, value in row.items()} for row in records]
        if any(len(normalized) != len(original)
               for normalized, original in zip(normalized_records, records)):
            raise ValueError(tr("欄位名稱不可空白或重複。"))
        headers = list(dict.fromkeys(key for row in normalized_records for key in row))
        if any(not header for header in headers) or len(headers) != len(set(headers)):
            raise ValueError(tr("欄位名稱不可空白或重複。"))
        if len(headers) > 100:
            raise ValueError(tr("資料組最多支援 100 個欄位。"))
        normalized = []
        for row in normalized_records:
            values = {}
            for header in headers:
                value = row.get(header, "")
                if value is None:
                    value = ""
                elif not isinstance(value, (str, int, float)) or isinstance(value, bool):
                    raise ValueError(tr("欄位「{field}」的值必須是文字或數字。", field=header))
                values[header.strip()] = str(value)
            normalized.append(values)
        return [header.strip() for header in headers], normalized

    text = parameters["csv_text"]
    if not isinstance(text, str) or not text.strip():
        raise ValueError(tr("請貼上或載入 CSV 資料。"))
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError as error:
        raise ValueError(tr("CSV 必須是有效的 UTF-8 文字。")) from error
    if size > 2 * 1024 * 1024:
        raise ValueError(tr("資料表最多支援 2 MiB。"))
    text = text.removeprefix("\ufeff")
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = "\t" if first_line.count("\t") > first_line.count(",") else ","
    try:
        table = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True))
    except csv.Error as error:
        raise ValueError(tr("CSV 格式錯誤：{reason}", reason=error)) from error
    table = [row for row in table if any(cell.strip() for cell in row)]
    if len(table) < 2:
        raise ValueError(tr("CSV 必須包含欄位名稱與至少一列資料。"))
    headers = [cell.strip() for cell in table[0]]
    if (not headers or any(not name for name in headers)
            or len(headers) != len(set(headers))):
        raise ValueError(tr("CSV 欄位名稱不可空白或重複。"))
    if len(headers) > 100:
        raise ValueError(tr("資料組最多支援 100 個欄位。"))
    if len(table) - 1 > 1000:
        raise ValueError(tr("資料組最多支援 1000 列。"))
    records = []
    for row_number, values in enumerate(table[1:], 1):
        if len(values) > len(headers):
            raise ValueError(tr("第 {row} 列比欄位名稱多出資料。", row=row_number))
        values += [""] * (len(headers) - len(values))
        records.append(dict(zip(headers, values)))
    return headers, records


def _template_fields(content):
    fields = []
    try:
        parts = list(Formatter().parse(content))
    except ValueError as error:
        raise ValueError(tr("範本中的大括號格式無效。")) from error
    for _literal, field, format_spec, conversion in parts:
        if field is None:
            continue
        if not field or format_spec or conversion:
            raise ValueError(tr("範本只支援簡單欄位佔位，例如 {placeholder}。",
                                placeholder="{name}"))
        fields.append(field)
    return fields


def _substitute(content, row):
    output, spans, cursor = [], [], 0
    for literal, field, format_spec, conversion in Formatter().parse(content):
        output.append(literal)
        cursor += len(literal)
        if field is None:
            continue
        if not field or format_spec or conversion:
            raise ValueError(tr("範本只支援簡單欄位佔位，例如 {placeholder}。",
                                placeholder="{name}"))
        if field not in row:
            raise KeyError(field)
        value = row[field]
        if not isinstance(value, str) or not value.strip():
            raise LookupError(field)
        output.append(value)
        spans.append((cursor, cursor+len(value), field))
        cursor += len(value)
    return "".join(output), spans


def _unsupported_characters(text, font_family, font_style, spans):
    from PySide6.QtGui import QFontMetricsF
    from .text_tool import _font

    metrics = QFontMetricsF(_font(font_family, font_style))
    missing = {}
    for index, character in enumerate(text):
        if character.isspace() or character in ("\u200d", "\ufe0e", "\ufe0f"):
            continue
        field = next((name for start, end, name in spans if start <= index < end), "範本文字")
        if not metrics.inFontUcs4(ord(character)):
            missing.setdefault(field, set()).add(character)
    return missing


def preview_merge(items, indices, parameters):
    try:
        headers, rows = parse_merge_table(parameters)
    except ValueError as error:
        return {"headers": [], "rows": [], "row_count": 0, "errors": [str(error)]}
    try:
        selected = editable_indices(items, indices)
    except ValueError as error:
        return {"headers": headers, "rows": rows[:5], "row_count": len(rows), "errors": [str(error)]}
    template = [items[index] for index in selected]
    from .text_tool import installed_font_family, text_transform
    errors = []
    text_items = [(index, item) for index, item in zip(selected, template) if "text_data" in item]
    if not text_items:
        errors.append(tr("範本至少需要一個可編輯文字物件。"))
    for row_number, row in enumerate(rows, 1):
        for index, item in text_items:
            name, data = item.get("name", str(index)), item.get("text_data")
            if not isinstance(data, dict) or not isinstance(data.get("content"), str):
                errors.append(tr("範本物件「{name}」的文字資料無效。", name=name))
                continue
            if not isinstance(item.get("text_source_contours"), list):
                errors.append(tr("範本物件「{name}」缺少可重算的文字輪廓。", name=name))
                continue
            try:
                fields = _template_fields(data["content"])
                text_transform(item["text_source_contours"], item["contours"])
                content, spans = _substitute(data["content"], row)
            except KeyError as error:
                errors.append(tr("第 {row} 列欄位「{field}」不存在於資料表。",
                                  row=row_number, field=error.args[0]))
                continue
            except LookupError as error:
                errors.append(tr("第 {row} 列欄位「{field}」是空值。",
                                  row=row_number, field=error.args[0]))
                continue
            except ValueError as error:
                errors.append(str(error))
                continue
            if not content.strip():
                errors.append(tr("第 {row} 列代入後的文字是空白。", row=row_number))
                continue
            family = data.get("font_family", "")
            style = data.get("font_style", "")
            if not installed_font_family(family):
                errors.append(tr("第 {row} 列欄位「{field}」使用的字型「{family}」未安裝。",
                                  row=row_number, field=fields[0] if fields else "範本文字",
                                  family=family))
            for field, characters in _unsupported_characters(content, family, style, spans).items():
                errors.append(tr("第 {row} 列欄位「{field}」含有字型「{family}」無法顯示的字元：{characters}",
                                  row=row_number, field=field, family=family,
                                  characters="".join(sorted(characters))))
    return {"headers": headers, "rows": rows[:5], "row_count": len(rows), "errors": errors}


def build_data_merge(items, metadata, indices, parameters):
    if not isinstance(parameters, dict) or set(parameters) - DATA_MERGE_PARAMETER_NAMES:
        raise ValueError(tr("資料組包含未知參數。"))
    auto_pack = parameters.get("auto_pack", True)
    if type(auto_pack) is not bool:
        raise ValueError(tr("auto_pack 必須是布林值。"))
    if type(parameters.get("new_page_for_unplaced", False)) is not bool:
        raise ValueError(tr("new_page_for_unplaced 必須是布林值。"))
    if parameters.get("new_page_for_unplaced") and not auto_pack:
        raise ValueError(tr("開新分頁需要先啟用省紙排版。"))
    for name in ("allow_rotate", "shape_aware", "fill"):
        if name in parameters and type(parameters[name]) is not bool:
            raise ValueError(tr("{name} 必須是布林值。", name=name))
    if "gap_mm" in parameters:
        finite_number(parameters["gap_mm"], "gap_mm", 0, 300)
    if "margin_mm" in parameters:
        finite_number(parameters["margin_mm"], "margin_mm", 0, 150)
    if "rotation_step_deg" in parameters:
        value = parameters["rotation_step_deg"]
        if type(value) is not int or not 5 <= value <= 90:
            raise ValueError(tr("rotation_step_deg 必須是 5 至 90 度的整數。"))
    if "time_limit_seconds" in parameters:
        finite_number(parameters["time_limit_seconds"], "time_limit_seconds", .05, 60)

    headers, rows = parse_merge_table(parameters)
    selected = editable_indices(items, indices)
    template = [items[index] for index in selected]
    preview = preview_merge(items, selected, parameters)
    if preview["errors"]:
        raise ValueError("\n".join(dict.fromkeys(preview["errors"])))
    text_slots = [position for position, item in enumerate(template) if "text_data" in item]
    if len(template) * len(rows) + len(items) > 2000:
        raise ValueError(tr("資料組超過 2000 個物件上限。"))

    from .text_tool import make_text_data, render_text_path, text_transform, transform_text_contours
    generated, generated_indices = [], []
    for row in rows:
        copies = _copy_items(template)
        remap_group_ids(copies)
        row_group = str(uuid4())
        for item in copies:
            path = item.get("group_path", [])
            item["group_path"] = [row_group, *path] if isinstance(path, list) else [row_group]
        for position in text_slots:
            item = copies[position]
            old_template = template[position]
            old_data = old_template["text_data"]
            old_source = old_template["text_source_contours"]
            content, _spans = _substitute(old_data["content"], row)
            updated = dict(old_data, content=content)
            transform = text_transform(old_source, old_template["contours"])
            source_path = render_text_path(updated, tuple(updated["origin_mm"]))
            source_contours = _flatten(source_path)
            item["text_data"] = updated
            item["text_source_contours"] = source_contours
            item["contours"] = transform_text_contours(source_contours, transform)
        generated_indices.extend(range(len(items)+len(generated), len(items)+len(generated)+len(copies)))
        generated.extend(copies)

    result = _copy_items(items) + generated
    if sum(len(contour) for item in result for contour in item.get("contours", [])) > 2_000_000:
        raise ValueError(tr("資料組超過 200 萬個切割點上限。"))
    details = {"action": "data_merge", "rows": len(rows), "generated_objects": len(generated),
               "fields": headers, "auto_pack": auto_pack, "page_count": 1,
               "new_pages": []}
    selection = generated_indices
    if auto_pack:
        from .packing import pack_items
        packing = {key: parameters[key] for key in PACKING_PARAMETER_NAMES if key in parameters}
        packing["copies_per_unit"] = [1] * len(rows)
        packing.setdefault("new_page_for_unplaced", True)
        result, selection, layout = pack_items(result, metadata, generated_indices, packing)
        if layout["unplaced_count"]:
            unplaced = sorted({entry["unit"] for entry in layout["unplaced"]})
            raise ValueError("\n".join(tr("第 {row} 列無法排入版面。", row=index+1)
                                        for index in unplaced))
        details.update({key: value for key, value in layout.items() if key not in ("action", "new_pages")})
        details["new_pages"] = layout.get("new_pages", [])
    return result, selection, details


class DataMergeDialog(QDialog):
    """Review a template and the first five data rows before creating copies."""

    def __init__(self, items, indices, parent=None):
        super().__init__(parent)
        self.items = items
        self.indices = list(indices)
        self.setWindowTitle(tr("資料組批次產生"))
        self.resize(820, 700)

        layout = QVBoxLayout(self)
        self.template_label = QLabel(tr("範本物件：{count}", count=len(self.indices)), self)
        self.template_label.setObjectName("dataMergeTemplate")
        layout.addWidget(self.template_label)

        source_row = QHBoxLayout()
        source_label = QLabel(tr("CSV 或貼上的表格文字"), self)
        source_label.setObjectName("dataMergeSourceLabel")
        source_row.addWidget(source_label)
        source_row.addStretch(1)
        self.load_button = QPushButton(tr("載入 CSV…"), self)
        self.load_button.setObjectName("dataMergeLoadButton")
        source_row.addWidget(self.load_button)
        layout.addLayout(source_row)

        self.csv_edit = QPlainTextEdit(self)
        self.csv_edit.setObjectName("dataMergeCsv")
        self.csv_edit.setPlaceholderText(tr("第一列輸入欄位名稱，下一列開始貼上資料…"))
        self.csv_edit.setMinimumHeight(130)
        layout.addWidget(self.csv_edit)

        preview_title = QLabel(tr("偵測欄位與前 5 列預覽"), self)
        preview_title.setObjectName("dataMergePreviewTitle")
        layout.addWidget(preview_title)
        self.preview_table = QTableWidget(0, 0, self)
        self.preview_table.setObjectName("dataMergePreview")
        self.preview_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.preview_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.preview_table.setAlternatingRowColors(True)
        self.preview_table.horizontalHeader().setStretchLastSection(True)
        self.preview_table.setMinimumHeight(170)
        layout.addWidget(self.preview_table)

        self.preview_summary = QLabel(self)
        self.preview_summary.setObjectName("dataMergeSummary")
        layout.addWidget(self.preview_summary)
        self.validation_output = QPlainTextEdit(self)
        self.validation_output.setObjectName("dataMergeValidation")
        self.validation_output.setReadOnly(True)
        self.validation_output.setMaximumHeight(76)
        layout.addWidget(self.validation_output)

        self.auto_pack = QCheckBox(tr("省紙排版"), self)
        self.auto_pack.setChecked(True)
        self.new_page = QCheckBox(tr("放不下開新分頁"), self)
        self.new_page.setChecked(True)
        self.allow_rotate = QCheckBox(tr("允許旋轉"), self)
        self.shape_aware = QCheckBox(tr("依輪廓排版"), self)
        self.fill = QCheckBox(tr("填滿剩餘範圍"), self)
        self.gap = self._spin(2, 0, 300, .5)
        self.margin = self._spin(0, 0, 150, .5)
        packing_layout = QGridLayout()
        packing_layout.addWidget(self.auto_pack, 0, 0)
        packing_layout.addWidget(self.new_page, 0, 1)
        packing_layout.addWidget(self.allow_rotate, 1, 0)
        packing_layout.addWidget(self.shape_aware, 1, 1)
        packing_layout.addWidget(self.fill, 1, 2)
        packing_layout.addWidget(QLabel(tr("間距（mm）"), self), 2, 0)
        packing_layout.addWidget(self.gap, 2, 1)
        packing_layout.addWidget(QLabel(tr("留邊（mm）"), self), 2, 2)
        packing_layout.addWidget(self.margin, 2, 3)
        layout.addLayout(packing_layout)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                        QDialogButtonBox.StandardButton.Cancel, self)
        self.apply_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.apply_button.setText(tr("產生"))
        self.buttons.accepted.connect(self._accept_if_valid)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.load_button.clicked.connect(self._load_csv)
        self.csv_edit.textChanged.connect(self.refresh_preview)
        self.auto_pack.toggled.connect(self._toggle_packing)
        self._toggle_packing()
        self.refresh_preview()

    @staticmethod
    def _spin(value, low, high, step):
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setDecimals(1)
        spin.setSingleStep(step)
        spin.setValue(value)
        return spin

    def _toggle_packing(self, _checked=False):
        enabled = self.auto_pack.isChecked()
        for widget in (self.new_page, self.allow_rotate, self.shape_aware, self.fill,
                       self.gap, self.margin):
            widget.setEnabled(enabled)

    def _load_csv(self, _event):
        filename, _ = QFileDialog.getOpenFileName(
            self, tr("載入 CSV"), "", tr("CSV 檔案 (*.csv);;所有檔案 (*)"))
        if not filename:
            return
        path = Path(filename)
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError(tr("資料表最多支援 2 MiB。"))
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            self.validation_output.setPlainText(tr("CSV 必須使用 UTF-8 編碼。"))
            self.apply_button.setEnabled(False)
            return
        except (OSError, ValueError) as error:
            self.validation_output.setPlainText(str(error))
            self.apply_button.setEnabled(False)
            return
        self.csv_edit.setPlainText(content)

    def refresh_preview(self):
        source = self.csv_edit.toPlainText()
        preview = (preview_merge(self.items, self.indices, {"csv_text": source})
                   if source.strip() else {"headers": [], "rows": [], "row_count": 0, "errors": []})
        headers, rows, errors = preview["headers"], preview["rows"], preview["errors"]
        self.preview_table.clear()
        self.preview_table.setColumnCount(len(headers))
        self.preview_table.setRowCount(len(rows))
        self.preview_table.setHorizontalHeaderLabels(headers)
        for row_number, row in enumerate(rows):
            for column, header in enumerate(headers):
                self.preview_table.setItem(row_number, column,
                                           QTableWidgetItem(str(row.get(header, ""))))
        self.preview_table.resizeColumnsToContents()
        if errors:
            self.preview_summary.setText(tr("預覽有 {count} 個問題", count=len(errors)))
            self.validation_output.setPlainText("\n".join(dict.fromkeys(errors)))
        elif preview["row_count"]:
            self.preview_summary.setText(tr("偵測到 {fields} · 共 {rows} 列",
                                            fields="、".join(headers), rows=preview["row_count"]))
            self.validation_output.setPlainText(tr("文字欄位與字型檢查通過。"))
        else:
            self.preview_summary.setText(tr("尚未載入資料"))
            self.validation_output.clear()
        self.apply_button.setEnabled(bool(preview["row_count"]) and not errors)
        self._preview = preview

    def parameters(self):
        if not self._preview.get("row_count") or self._preview.get("errors"):
            raise ValueError(tr("請先修正資料預覽中的問題。"))
        return {
            "csv_text": self.csv_edit.toPlainText(),
            "auto_pack": self.auto_pack.isChecked(),
            "gap_mm": self.gap.value(), "margin_mm": self.margin.value(),
            "allow_rotate": self.allow_rotate.isChecked(),
            "shape_aware": self.shape_aware.isChecked(), "fill": self.fill.isChecked(),
            "new_page_for_unplaced": self.auto_pack.isChecked() and self.new_page.isChecked(),
        }

    def _accept_if_valid(self):
        self.refresh_preview()
        if self.apply_button.isEnabled():
            self.accept()
