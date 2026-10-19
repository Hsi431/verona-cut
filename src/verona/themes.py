"""Editor chrome colors; artwork and physical paper colors stay independent."""
from pathlib import Path

from PySide6.QtGui import QColor, QPalette


THEMES = {
    "light": {
        "background": "#f2f5f2", "surface": "#ffffff", "text": "#202b30",
        "input": "#fffefa", "border": "#d6dfda", "panel_border": "#dbe3df",
        "muted": "#718086", "disabled": "#899795", "hover": "#f3faf7",
        "accent": "#087e72", "primary": "#087e72", "primary_hover": "#076c62",
        "selection": "#087e72", "selection_text": "#ffffff",
        "warning_text": "#9c1c29", "warning_background": "#fff0f1", "warning_border": "#ebc0c5",
        "separator": "#e2e8e4", "canvas": "#e9eeeb", "ruler": "#82918c",
    },
    "dark": {
        "background": "#1b2023", "surface": "#242c30", "text": "#e6eeeb",
        "input": "#20272b", "border": "#455159", "panel_border": "#39454b",
        "muted": "#a2b0b0", "disabled": "#79888a", "hover": "#2e3e3e",
        "accent": "#65c7b4", "primary": "#167f70", "primary_hover": "#209582",
        "selection": "#315f59", "selection_text": "#e6eeeb",
        "warning_text": "#ffb4b5", "warning_background": "#3d242b", "warning_border": "#8b4b56",
        "separator": "#39454b", "canvas": "#151a1d", "ruler": "#a1b0b1",
    },
}


def palette(colors):
    result = QPalette()
    for role, token in {
        "Window": "background", "WindowText": "text", "Base": "input", "AlternateBase": "surface",
        "ToolTipBase": "surface", "ToolTipText": "text", "Text": "text", "Button": "surface",
        "ButtonText": "text", "Highlight": "selection", "HighlightedText": "selection_text",
        "Link": "accent", "Accent": "accent", "PlaceholderText": "muted", "Mid": "border",
        "Dark": "border", "Light": "surface", "Midlight": "surface", "Shadow": "canvas",
    }.items():
        result.setColor(getattr(QPalette.ColorRole, role), QColor(colors[token]))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        result.setColor(QPalette.ColorGroup.Disabled, role, QColor(colors["disabled"]))
    return result


def stylesheet(c):
    icons = Path(__file__).resolve().parent / "icons"
    return f"""
        QMainWindow, QWidget {{ background: {c['background']}; color: {c['text']}; }}
        QLabel {{ background: transparent; }}
        QFrame#header, QFrame#inspector, QFrame#layers {{ background: {c['surface']}; border: 1px solid {c['panel_border']}; border-radius: 10px; }}
        QTreeView {{ background: {c['input']}; border: 1px solid {c['border']}; border-radius: 6px; selection-background-color: {c['selection']}; selection-color: {c['selection_text']}; }}
        QTreeView::item {{ min-height: 26px; }}
        QHeaderView::section {{ background: {c['surface']}; color: {c['muted']}; border: 0; padding: 4px; }}
        QToolButton, QPushButton {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 7px; padding: 5px 8px; }}
        QToolButton:hover, QPushButton:hover {{ border-color: {c['accent']}; background: {c['hover']}; }}
        QToolButton:checked {{ background: {c['selection']}; color: {c['selection_text']}; border-color: {c['accent']}; }}
        QToolButton:focus, QPushButton:focus {{ border-color: {c['accent']}; }}
        QPushButton#primary {{ color: white; background: {c['primary']}; border-color: {c['primary']}; font-weight: 600; padding: 7px; }}
        QPushButton#primary:hover {{ background: {c['primary_hover']}; }}
        QToolButton:disabled, QPushButton:disabled, QPushButton#primary:disabled {{ color: {c['disabled']}; background: {c['surface']}; border-color: {c['border']}; }}
        QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ background: {c['input']}; border: 1px solid {c['border']}; border-radius: 6px; padding: 3px 6px; selection-background-color: {c['selection']}; selection-color: {c['selection_text']}; }}
        QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {c['accent']}; }}
        QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{ color: {c['disabled']}; }}
        QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{icons / 'chevron-up.svg'}"); width: 10px; height: 10px; }}
        QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QComboBox::down-arrow {{ image: url("{icons / 'chevron-down.svg'}"); width: 10px; height: 10px; }}
        QComboBox QAbstractItemView {{ background: {c['surface']}; color: {c['text']}; selection-background-color: {c['selection']}; selection-color: {c['selection_text']}; }}
        QMenu {{ background: {c['surface']}; border: 1px solid {c['border']}; }}
        QMenu::item:selected, QMenuBar::item:selected {{ background: {c['selection']}; color: {c['selection_text']}; }}
        QMenu::item:disabled {{ color: {c['disabled']}; }}
        QMenu::separator {{ height: 1px; background: {c['separator']}; margin: 4px; }}
        QToolTip {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['border']}; }}
        QLabel#eyebrow {{ color: {c['muted']}; font-size: 11px; font-weight: 600; }}
        QLabel#title {{ font-size: 18px; font-weight: 650; }}
        QLabel#muted {{ color: {c['muted']}; }}
        QLabel#boundsWarning {{ color: {c['warning_text']}; background: {c['warning_background']}; border: 1px solid {c['warning_border']}; border-radius: 7px; padding: 8px; }}
        QFrame#separator {{ color: {c['separator']}; }}
    """
