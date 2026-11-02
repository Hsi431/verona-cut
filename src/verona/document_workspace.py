"""Document and tab lifecycle for the editor window."""

import ipaddress
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QSignalBlocker
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from .document_state import DocumentState
from .file_io import write_document
from .i18n import tr
from .transport import TCP_PORT
from .view_constants import DEFAULT_ZOOM


class DocumentWorkspace:
    @property
    def document(self) -> DocumentState:
        return self._documents[self._active_document]

    @property
    def items(self):
        return self.document.items

    @items.setter
    def items(self, value):
        self.document.items = value

    @property
    def metadata(self):
        return self.document.metadata

    @metadata.setter
    def metadata(self, value):
        self.document.metadata = value

    @property
    def current_file(self):
        return self.document.current_file

    @current_file.setter
    def current_file(self, value):
        self.document.current_file = value

    @property
    def selected(self):
        return self.document.selected

    @selected.setter
    def selected(self, value):
        self.document.selected = value

    @property
    def selection(self):
        return self.document.selection

    @selection.setter
    def selection(self, value):
        self.document.selection = value

    @property
    def undo_stack(self):
        return self.document.undo_stack

    @undo_stack.setter
    def undo_stack(self, value):
        self.document.undo_stack = value

    @property
    def redo_stack(self):
        return self.document.redo_stack

    @redo_stack.setter
    def redo_stack(self, value):
        self.document.redo_stack = value

    @property
    def saved_snapshot(self):
        return self.document.saved_snapshot

    @saved_snapshot.setter
    def saved_snapshot(self, value):
        self.document.saved_snapshot = value

    def _tab_state(self) -> DocumentState:
        if getattr(self.canvas, "_nudge_before", None) is not None:
            self.canvas.finish_nudge()
        self.document.view = {
            "zoom": self.canvas.zoom,
            "pan_offset": (self.canvas._view_offset.x(), self.canvas._view_offset.y()),
            "fit_height": self._fit_page_height,
            "view": (self.image_opacity.value(), self.cutline_view.isChecked()),
            "scroll": (self.canvas_scroll.horizontalScrollBar().value(),
                       self.canvas_scroll.verticalScrollBar().value())}
        return self.document

    def _switch_document(self, index: int) -> None:
        if not 0 <= index < len(self._documents) or index == self._active_document:
            return
        self._documents[self._active_document] = self._tab_state()
        self._active_document = index
        self._restore_tab_state(self._documents[index])

    def _restore_tab_state(self, document: DocumentState) -> None:
        state = document.view
        self._set_cut_settings(document.settings)
        self.cutline_view.setChecked(state.get("view", (100, False))[1])
        self.image_opacity.setValue(state.get("view", (100, False))[0])
        self.canvas.set_zoom(state["zoom"])
        self.canvas._view_offset = QPointF(*state.get("pan_offset", (0, 0)))
        self._fit_page_height = state.get("fit_height", False)
        self.fit_height_action.setChecked(self._fit_page_height)
        self._fit_timer.stop()
        self._schedule_page_fit()
        self.zoom_label.setText(f"{round(self.canvas.zoom * 100)}%")
        self.canvas_scroll.horizontalScrollBar().setValue(state["scroll"][0])
        self.canvas_scroll.verticalScrollBar().setValue(state["scroll"][1])
        self.native_preview_action.setEnabled(bool(self.metadata.get("source_preview")))
        self.refresh_all()

    def _refresh_tab_titles(self) -> None:
        if not hasattr(self, "tab_bar") or not self._documents:
            return
        self._documents[self._active_document] = self._tab_state()
        self.tab_bar.blockSignals(True)
        while self.tab_bar.count() < len(self._documents):
            self.tab_bar.addTab("")
        while self.tab_bar.count() > len(self._documents):
            self.tab_bar.removeTab(self.tab_bar.count()-1)
        for index, state in enumerate(self._documents):
            dirty = state.is_dirty()
            name = Path(state.current_file).name if state.current_file else tr("未命名設計")
            self.tab_bar.setTabText(index, name + (" *" if dirty else ""))
            self.tab_bar.setTabToolTip(index, state.current_file or name)
        self.tab_bar.setCurrentIndex(self._active_document)
        self.tab_bar.blockSignals(False)

    def close_document(self, index: int) -> bool:
        if not 0 <= index < len(self._documents):
            return False
        if getattr(self.canvas, "_nudge_before", None) is not None:
            self.canvas.finish_nudge()
        self.tab_bar.setCurrentIndex(index)
        if not self._maybe_save_changes():
            return False
        if len(self._documents) == 1:
            self._documents[0] = DocumentState(settings=self._cut_settings())
            self.native_preview_action.setEnabled(False)
            self.refresh_all()
            self._checkpoint_recovery()
            return True
        del self._documents[index]
        next_index = min(index, len(self._documents)-1)
        self._active_document = next_index
        self._restore_tab_state(self._documents[next_index])
        self._checkpoint_recovery()
        return True

    def _cut_settings(self) -> dict[str, Any]:
        return dict(self.document.settings)

    def _snapshot(self) -> dict[str, Any]:
        return self.document.snapshot()

    def push_undo(self, previous: dict[str, Any]) -> None:
        if getattr(self.canvas, "_nudge_before", None) is not None:
            self.canvas.finish_nudge()
        self.document.push_undo(previous)
        self._refresh_history_actions()
        self._refresh_tab_titles()

    def _refresh_history_actions(self) -> None:
        if hasattr(self, "undo_action"):
            self.undo_action.setEnabled(bool(self.undo_stack))
            self.redo_action.setEnabled(bool(self.redo_stack))

    def undo(self) -> None:
        if getattr(self.canvas, "_nudge_before", None) is not None:
            self.canvas.finish_nudge()
        if self.document.undo():
            self._set_cut_settings(self.document.settings)
            self.native_preview_action.setEnabled(bool(self.metadata.get("source_preview")))
            self.refresh_all()

    def redo(self) -> None:
        if getattr(self.canvas, "_nudge_before", None) is not None:
            self.canvas.finish_nudge()
        if self.document.redo():
            self._set_cut_settings(self.document.settings)
            self.native_preview_action.setEnabled(bool(self.metadata.get("source_preview")))
            self.refresh_all()

    def selected_indices(self) -> set[int]:
        return self.document.selected_indices()

    def _restore(self, snapshot: dict[str, Any]) -> None:
        self.document.restore(snapshot)
        self._set_cut_settings(self.document.settings)
        if hasattr(self, "native_preview_action"):
            self.native_preview_action.setEnabled(bool(self.metadata.get("source_preview")))
        self.refresh_all()

    def _set_cut_settings(self, settings: dict[str, Any]) -> None:
        controls = (self.custom_settings, self.speed_spin, self.force_spin, self.passes_spin,
                    self.tool_combo, self.connection_mode, self.wifi_host, self.wifi_port)
        blockers = [QSignalBlocker(control) for control in controls]
        try:
            self.custom_settings.setChecked(settings["custom"])
            self.speed_spin.setValue(settings["speed"])
            self.force_spin.setValue(settings["force"])
            self.passes_spin.setValue(settings.get("passes", 1))
            self.tool_combo.setCurrentIndex(self.tool_combo.findData(settings["tool"]))
            self.connection_mode.setCurrentIndex(self.connection_mode.findData(settings["connection"]))
            self.wifi_host.setText(settings["host"])
            self.wifi_port.setValue(settings["port"])
        finally:
            for blocker in blockers:
                blocker.unblock()
        self._toggle_cut_settings(self.custom_settings.isChecked())
        self._update_connection_mode()
        self._update_cut_summary()
        self._sync_cut_settings()

    def new_design(self) -> None:
        settings = self._cut_settings()
        self._tab_state()
        self._active_document = len(self._documents)
        self._documents.append(DocumentState(settings=settings))
        self.native_preview_action.setEnabled(False)
        self.canvas._view_offset = QPointF()
        self.canvas.set_zoom(DEFAULT_ZOOM)
        self._fit_page_height = True
        self.fit_height_action.setChecked(True)
        self._schedule_page_fit()
        self.cutline_view.setChecked(False)
        self.image_opacity.setValue(100)
        self.zoom_label.setText(f"{round(DEFAULT_ZOOM * 100)}%")
        self.refresh_all()

    def open_design(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, tr("開啟設計"), "", tr("Verona 設計 (*.jdesign *.json *.lds)"))
        if not filename:
            return
        self.open_path(filename)

    def open_path(self, filename: str) -> bool:
        """Use the same tab flow for desktop launches and the Open action."""
        self._refresh_tab_titles()
        for index, state in enumerate(self._documents):
            if state.current_file and Path(state.current_file).resolve() == Path(filename).resolve():
                self.tab_bar.setCurrentIndex(index)
                return True
        # Read first so a bad file cannot disturb the current document or settings.
        try:
            document = self._read_document(filename)
        except Exception as error:
            QMessageBox.critical(self, tr("開啟失敗"), str(error))
            return False
        new_tab = bool(self.items or self.current_file or self.is_dirty())
        if new_tab:
            self.new_design()
        self._install_document(filename, *document)
        return True

    def open_paths(self, paths):
        for path in paths:
            self.open_path(path)
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _load_document(self, filename: str) -> bool:
        try:
            self._install_document(filename, *self._read_document(filename))
            return True
        except Exception as error:
            QMessageBox.critical(self, tr("開啟失敗"), str(error))
            return False

    def _read_document(self, filename):
        if Path(filename).suffix.lower() == ".lds":
            from .lds import load_lds
            native = load_lds(filename)
            settings = native.settings
            if settings is None:
                _, settings = self._validate_document({"format": "verona", "version": 1,
                                                       "units": "mm", "items": []})
                settings.update({key: self._cut_settings()[key] for key in ("connection", "host", "port")})
            return native.items, settings, native.metadata
        from .input_safety import read_design
        data = read_design(filename)
        items, settings = self._validate_document(data)
        document_settings = data.get("settings", {})
        if "host" not in document_settings:
            settings["host"] = self.settings.value("wifi/host", "", type=str)
        if "port" not in document_settings:
            settings["port"] = self.settings.value("wifi/port", TCP_PORT, type=int)
        return items, settings, data.get("metadata", {})

    def _install_document(self, filename, items, settings, metadata):
        self.document.install(filename, items, settings, metadata)
        self.native_preview_action.setEnabled(bool(self.metadata.get("source_preview")))
        self._set_cut_settings(settings)
        self.refresh_all()
        self._fit_page_height = True
        self.fit_height_action.setChecked(True)
        self._schedule_page_fit()

    def save_design(self, *, save_as=False) -> bool:
        filename = None if save_as else self.current_file
        if not filename:
            default_name = self.current_file or "design.jdesign"
            native_filter = tr("LDS 設計（原廠相容性未驗證） (*.lds)")
            filename, selected_filter = QFileDialog.getSaveFileName(self, tr("儲存設計"), default_name,
                tr("Verona 設計 (*.jdesign)") + ";;" + native_filter,
                selectedFilter=native_filter if Path(default_name).suffix.lower() == ".lds" else tr("Verona 設計 (*.jdesign)"))
            if filename and Path(filename).suffix.lower() not in (".jdesign", ".json", ".lds"):
                filename += ".lds" if selected_filter == native_filter else ".jdesign"
        if not filename:
            return False
        try:
            self._write_document(filename)
            return True
        except (OSError, TypeError, ValueError) as error:
            QMessageBox.critical(self, tr("儲存失敗"), str(error))
            return False

    def _write_document(self, filename, *, overwrite=True):
        target = Path(filename)
        write_document(filename, self.document.design(), overwrite=overwrite)
        self.document.mark_saved(filename)
        self._refresh_tab_titles()
        self._checkpoint_recovery()
        if target.suffix.lower() == ".lds":
            self.status_label.setText(tr("已儲存 {name} · 原廠 LDS 相容性尚未驗證", name=target.name))

    def _document_state(self) -> dict[str, Any]:
        return self.document.state()

    def is_dirty(self) -> bool:
        return self.document.is_dirty()

    def _confirm_unsaved_changes(self, filename=None) -> str:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle(tr("未儲存的變更"))
        dialog.setText(tr("「{name}」有尚未儲存的變更。要先儲存嗎？", name=Path(filename).name)
                       if filename else tr("這份設計有尚未儲存的變更。要先儲存嗎？"))
        save = dialog.addButton(tr("儲存"), QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton(tr("捨棄"), QMessageBox.ButtonRole.DestructiveRole)
        dialog.addButton(tr("取消"), QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(save)
        dialog.exec()
        clicked = dialog.clickedButton()
        choice = "cancel"
        if clicked is save:
            choice = "save"
        elif clicked is not None and clicked.text() == tr("捨棄"):
            choice = "discard"
        dialog.deleteLater()
        return choice

    def _maybe_save_changes(self) -> bool:
        if not self.is_dirty():
            return True
        choice = self._confirm_unsaved_changes()
        if choice == "discard":
            return True
        if choice == "save":
            return self.save_design()
        return False

    def closeEvent(self, event: Any) -> None:
        if self._closing:
            event.ignore()
            return
        self._closing = True
        original = self._active_document
        self._documents[original] = self._tab_state()
        try:
            for index, state in enumerate(self._documents):
                if not state.is_dirty():
                    continue
                choice = self._confirm_unsaved_changes(state.current_file)
                if choice == "discard":
                    continue
                if choice == "save":
                    self.tab_bar.setCurrentIndex(index)
                    if self.save_design():
                        continue
                self.tab_bar.setCurrentIndex(original)
                event.ignore()
                return
            self._release_alt_grab()
            self._closed = True
            self.stop_sending()
            QApplication.instance().removeEventFilter(self)
            if getattr(self, "recovery", None) is not None:
                self.recovery.close()
            event.accept()
        finally:
            self._closing = False

    def _sync_cut_settings(self, *_args: Any) -> None:
        host = self.wifi_host.text().strip()
        if host:
            try:
                address = ipaddress.IPv4Address(host)
                host = "" if address.is_unspecified or address.is_multicast else str(address)
            except ipaddress.AddressValueError:
                host = ""
        self.document.settings = {
            "custom": self.custom_settings.isChecked(),
            "speed": self.speed_spin.value(),
            "force": self.force_spin.value(),
            "passes": self.passes_spin.value(),
            "tool": int(self.tool_combo.currentData()),
            "connection": self.connection_mode.currentData(),
            "host": host,
            "port": self.wifi_port.value(),
        }

        self._refresh_tab_titles()

    def _checkpoint_recovery(self):
        if getattr(self, "recovery", None) is not None:
            self.recovery.checkpoint()
