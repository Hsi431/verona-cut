"""Drop raster files into the existing image import flow."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, QPointF, QSettings, Qt, QUrl
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QImage, QPainter, QWheelEvent
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog

from verona.i18n import set_language
from verona.ui import VeronaWindow


class MockImportDialog:
    def __init__(self, _image, name, _parent, result):
        self.name = name
        self.result = result
        self.mode_combo = self

    def currentData(self):
        return "cut_only"

    def exec(self):
        return self.result

    def build_items(self):
        return [{"name": self.name, "cut": True,
                 "contours": [[(1., 1.), (3., 1.), (3., 3.), (1., 1.)]]}]

    def deleteLater(self):
        pass


class ImageDropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings_dir = tempfile.TemporaryDirectory()
        cls.previous_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = cls.settings_dir.name
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope,
                          cls.settings_dir.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        if cls.previous_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = cls.previous_xdg
        cls.settings_dir.cleanup()

    def setUp(self):
        set_language("zh_TW")
        QSettings("Verona", "Workspace").clear()
        self.temp = tempfile.TemporaryDirectory()
        self.window = VeronaWindow()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.window.close()
        self.temp.cleanup()

    def image_path(self, name="art.png"):
        path = Path(self.temp.name) / name
        image = QImage(16, 12, QImage.Format.Format_RGBA8888)
        image.fill(QColor("white"))
        painter = QPainter(image)
        painter.fillRect(3, 3, 8, 6, QColor("black"))
        painter.end()
        self.assertTrue(image.save(str(path), "PNG"))
        return path

    def mock_dialogs(self, results):
        results = iter(results)
        dialogs = []

        def create(image, name, parent):
            dialog = MockImportDialog(image, name, parent, next(results))
            dialogs.append(dialog)
            return dialog

        patcher = patch("verona.raster_import.ImportImageDialog", side_effect=create)
        patcher.start()
        self.addCleanup(patcher.stop)
        return dialogs

    @staticmethod
    def mime_for(*urls):
        mime = QMimeData()
        mime.setUrls(list(urls))
        return mime

    def send_drop(self, target, mime):
        enter = QDragEnterEvent(QPoint(5, 5), Qt.DropAction.CopyAction, mime,
                                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(target, enter)
        drop = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(target, drop)
        self.app.processEvents()
        return enter, drop

    def seed_existing_design(self):
        self.window.items = [{"name": "existing", "cut": True,
                              "contours": [[(10., 10.), (20., 10.), (20., 20.), (10., 10.)]]}]
        self.window.metadata = {"print_cut": {"enabled": True, "paper_width": 210.,
            "paper_height": 297., "bounds": [6., 6., 204., 291.]}, "kept": "metadata"}
        self.window.current_file = str(Path(self.temp.name) / "original.jdesign")
        self.window.custom_settings.setChecked(True)
        self.window.speed_spin.setValue(11)
        self.window.force_spin.setValue(85)
        self.window.passes_spin.setValue(2)
        self.window.saved_snapshot = self.window._document_state()
        self.window._documents[self.window._active_document] = self.window._tab_state()
        self.window._refresh_tab_titles()

    def test_drop_routes_from_window_canvas_and_scroll_viewport_to_preview(self):
        path = self.image_path()
        targets = (self.window, self.window.canvas, self.window.canvas_scroll.viewport())
        for target in targets:
            with self.subTest(target=type(target).__name__):
                dialogs = self.mock_dialogs([QDialog.DialogCode.Accepted])
                before_count = len(self.window.items)
                mime = self.mime_for(QUrl.fromLocalFile(str(path)))
                enter, drop = self.send_drop(target, mime)
                self.assertTrue(enter.isAccepted())
                self.assertTrue(drop.isAccepted())
                self.assertEqual(len(dialogs), 1)
                self.assertEqual(len(self.window.items), before_count + 1)
                self.assertEqual(len(self.window.undo_stack), 1)
                self.assertEqual(self.window.items[-1]["name"], "art")
                self.window.undo()
                self.assertEqual(len(self.window.items), before_count)
                self.window.undo_stack.clear()
                self.window.redo_stack.clear()

    def test_drop_uses_preview_without_opening_file_picker_and_preserves_document_state(self):
        self.seed_existing_design()
        path = self.image_path()
        dialogs = self.mock_dialogs([QDialog.DialogCode.Accepted])
        old_items = copy.deepcopy(self.window.items)
        old_metadata = copy.deepcopy(self.window.metadata)
        old_settings = self.window._cut_settings()
        old_file = self.window.current_file
        old_tab_count = self.window.tab_bar.count()
        old_tab_tooltip = self.window.tab_bar.tabToolTip(0)
        with patch.object(QFileDialog, "getOpenFileName") as picker:
            enter, drop = self.send_drop(self.window.canvas,
                                         self.mime_for(QUrl.fromLocalFile(str(path))))
        self.assertTrue(enter.isAccepted())
        self.assertTrue(drop.isAccepted())
        picker.assert_not_called()
        self.assertEqual(len(dialogs), 1)
        self.assertEqual(len(self.window.items), len(old_items) + 1)
        self.assertEqual(len(self.window.undo_stack), 1)
        self.assertEqual(self.window.metadata, old_metadata)
        self.assertEqual(self.window._cut_settings(), old_settings)
        self.assertEqual(self.window.current_file, old_file)
        self.assertEqual(self.window.tab_bar.count(), old_tab_count)
        self.assertEqual(self.window.tab_bar.tabToolTip(0), old_tab_tooltip)
        self.window.undo()
        self.assertEqual(self.window.items, old_items)
        self.assertEqual(self.window.metadata, old_metadata)
        self.assertEqual(self.window._cut_settings(), old_settings)
        self.assertEqual(self.window.current_file, old_file)

    def test_cancelled_preview_does_not_change_design_or_open_another_dialog(self):
        self.seed_existing_design()
        path = self.image_path()
        self.mock_dialogs([QDialog.DialogCode.Rejected])
        before = self.window._snapshot()
        undo_count = len(self.window.undo_stack)
        with patch.object(QFileDialog, "getOpenFileName") as picker:
            enter, drop = self.send_drop(self.window.canvas_scroll.viewport(),
                                         self.mime_for(QUrl.fromLocalFile(str(path))))
        self.assertTrue(enter.isAccepted())
        self.assertTrue(drop.isAccepted())
        picker.assert_not_called()
        self.assertEqual(self.window._snapshot(), before)
        self.assertEqual(len(self.window.undo_stack), undo_count)

    def test_any_remote_nonimage_or_directory_url_rejects_the_entire_drop(self):
        image = self.image_path()
        text = Path(self.temp.name) / "notes.txt"
        text.write_text("not an image", encoding="utf-8")
        directory = Path(self.temp.name) / "folder.png"
        directory.mkdir()
        cases = (
            (QUrl("https://example.invalid/art.png"),),
            (QUrl.fromLocalFile(str(text)),),
            (QUrl.fromLocalFile(str(directory)),),
            (QUrl.fromLocalFile(str(image)), QUrl.fromLocalFile(str(text))),
        )
        dialogs = self.mock_dialogs([QDialog.DialogCode.Accepted] * len(cases))
        for urls in cases:
            with self.subTest(urls=urls):
                mime = self.mime_for(*urls)
                enter = QDragEnterEvent(QPoint(5, 5), Qt.DropAction.CopyAction, mime,
                                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
                self.app.sendEvent(self.window, enter)
                drop = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                                  Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
                self.app.sendEvent(self.window, drop)
                self.assertFalse(enter.isAccepted())
                self.assertFalse(drop.isAccepted())
        self.assertEqual(dialogs, [])

    def test_drop_filter_accepts_only_supported_local_image_extensions(self):
        original = self.image_path().read_bytes()
        extensions = ("png", "jpg", "jpeg", "bmp", "webp", "tif", "tiff")
        paths = []
        for extension in extensions:
            path = Path(self.temp.name) / f"image.{extension}"
            path.write_bytes(original)
            paths.append(path)
        mime = self.mime_for(*(QUrl.fromLocalFile(str(path)) for path in paths))
        self.assertEqual(self.window._drop_image_paths(mime), [str(path) for path in paths])
        self.assertEqual(self.window._drop_image_paths(QMimeData()), [])

    def test_multiple_drop_stops_after_the_first_cancelled_preview(self):
        paths = [self.image_path(f"art{i}.png") for i in range(3)]
        dialogs = self.mock_dialogs([QDialog.DialogCode.Accepted, QDialog.DialogCode.Rejected,
                                     QDialog.DialogCode.Accepted])
        before_items = copy.deepcopy(self.window.items)
        mime = self.mime_for(*(QUrl.fromLocalFile(str(path)) for path in paths))
        enter, drop = self.send_drop(self.window, mime)
        self.assertTrue(enter.isAccepted())
        self.assertTrue(drop.isAccepted())
        self.assertEqual(len(dialogs), 2)
        self.assertEqual(len(self.window.items), len(before_items) + 1)
        self.assertEqual(len(self.window.undo_stack), 1)

    def test_sending_or_an_active_modal_rejects_drop(self):
        path = self.image_path()
        dialogs = self.mock_dialogs([QDialog.DialogCode.Accepted])
        mime = self.mime_for(QUrl.fromLocalFile(str(path)))
        self.window.sending = True
        enter, drop = self.send_drop(self.window, mime)
        self.assertFalse(enter.isAccepted())
        self.assertFalse(drop.isAccepted())
        self.window.sending = False

        modal = QDialog(self.window)
        modal.setWindowModality(Qt.WindowModality.ApplicationModal)
        modal.show()
        self.app.processEvents()
        enter, drop = self.send_drop(self.window, mime)
        self.assertFalse(enter.isAccepted())
        self.assertFalse(drop.isAccepted())
        self.assertEqual(dialogs, [])
        modal.close()

    def test_wheel_guard_still_leaves_cut_speed_unchanged(self):
        self.window.custom_settings.setChecked(True)
        before = self.window.speed_spin.value()
        position = self.window.speed_spin.mapToGlobal(self.window.speed_spin.rect().center())
        wheel = QWheelEvent(QPointF(self.window.speed_spin.rect().center()), QPointF(position),
                            QPoint(0, 0), QPoint(0, 120), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(self.window.speed_spin, wheel)
        self.assertEqual(self.window.speed_spin.value(), before)


if __name__ == "__main__":
    unittest.main()
