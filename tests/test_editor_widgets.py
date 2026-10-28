import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QScrollArea, QSpinBox, QVBoxLayout, QWidget

from verona.editor_widgets import CollapsibleSection, SafeDoubleSpinBox, SafeSpinBox, WheelGuard


class EditorWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _wheel(self, widget):
        event = QWheelEvent(
            QPointF(2, 2), QPointF(widget.mapToGlobal(QPoint(2, 2))),
            QPoint(0, 0), QPoint(0, 120), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False,
        )
        QApplication.sendEvent(widget, event)
        return event

    def test_spin_boxes_ignore_wheel_with_or_without_focus(self):
        for spin_type in (SafeSpinBox, SafeDoubleSpinBox):
            for focused in (False, True):
                with self.subTest(spin_type=spin_type.__name__, focused=focused):
                    spin = spin_type()
                    spin.setRange(0, 10)
                    spin.setValue(5)
                    spin.show()
                    if focused:
                        spin.setFocus()
                        self.app.processEvents()
                    event = self._wheel(spin)
                    self.assertFalse(event.isAccepted())
                    self.assertEqual(spin.value(), 5)
                    spin.close()

    def test_spin_boxes_still_change_from_keyboard(self):
        for spin_type in (SafeSpinBox, SafeDoubleSpinBox):
            spin = spin_type()
            spin.setRange(0, 10)
            spin.setValue(5)
            spin.show()
            spin.setFocus()
            self.app.processEvents()
            QTest.keyClick(spin, Qt.Key.Key_Up)
            self.assertEqual(spin.value(), 6)
            spin.close()

    def test_global_guard_keeps_values_and_scrolls_parent_even_with_focus(self):
        guard = WheelGuard(self.app)
        self.app.installEventFilter(guard)
        self.addCleanup(lambda: self.app.removeEventFilter(guard))
        scroll = QScrollArea()
        content = QWidget()
        layout = QVBoxLayout(content)
        combo, spin = QComboBox(), QSpinBox()
        combo.addItems(["one", "two", "three"])
        combo.setCurrentIndex(1)
        spin.setValue(5)
        layout.addWidget(combo)
        layout.addWidget(spin)
        layout.addSpacing(1500)
        scroll.setWidget(content)
        scroll.resize(250, 150)
        scroll.show()
        self.app.processEvents()
        self.addCleanup(scroll.close)
        for widget, getter in ((combo, combo.currentIndex), (spin, spin.value)):
            widget.setFocus()
            self.app.processEvents()
            value = getter()
            bar = scroll.verticalScrollBar()
            bar.setValue(200)
            self._wheel(widget)
            self.assertEqual(getter(), value)
            self.assertLess(bar.value(), 200)
        QTest.keyClick(combo, Qt.Key.Key_Down)
        self.assertEqual(combo.currentIndex(), 2)

    def test_collapsible_section_toggles_content_and_shows_title(self):
        content = QWidget()
        section = CollapsibleSection("連線設定", content)
        self.assertEqual(section._toggle.text(), "連線設定")
        self.assertFalse(section.isExpanded())
        self.assertTrue(content.isHidden())

        section.setExpanded(True)
        self.assertTrue(section.isExpanded())
        self.assertFalse(content.isHidden())
        section.setExpanded(False)
        self.assertTrue(content.isHidden())

        section._toggle.click()
        self.assertTrue(section.isExpanded())
        self.assertFalse(content.isHidden())


if __name__ == "__main__":
    unittest.main()
