"""Тёмное оформление: экран суфлёра смотрят с расстояния и часто в полумраке.

Палитра взята из музыкальных плееров — глубокий графит и насыщенный зелёный
акцент. Такая гамма привычна глазу на сцене: почти чёрный фон не бликует,
а единственный яркий цвет работает указателем, за который цепляется взгляд.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor

# --- Фон и поверхности ------------------------------------------------------

# Не чистый чёрный: на сцене он даёт резкий контраст, от которого устают глаза
BACKGROUND = QColor("#121212")
SURFACE = QColor("#1c1c1c")
SURFACE_HOVER = QColor("#282828")
BORDER = QColor("#2f2f2f")

# --- Текст ------------------------------------------------------------------

TEXT_PRIMARY = QColor("#ffffff")
TEXT_MUTED = QColor("#b3b3b3")
TEXT_DIM = QColor("#6a6a6a")

# --- Акценты ----------------------------------------------------------------

# Зелёный — единственный яркий цвет интерфейса: им отмечено то, что сейчас
# важно (текущая строка, главная кнопка, уровень микрофона)
ACCENT = QColor("#1ed760")
ACCENT_HOVER = QColor("#3be477")
ACCENT_PRESSED = QColor("#169c46")

# Аккорды держим в тёплом янтаре: рядом с зелёным они не сливаются
# ни с текстом, ни с указателем текущей строки
CHORD = QColor("#ffc862")

SUCCESS = QColor("#1ed760")
DANGER = QColor("#f26d6d")

STYLESHEET = f"""
QWidget {{
    background-color: {BACKGROUND.name()};
    color: {TEXT_PRIMARY.name()};
    font-size: 15px;
}}

QLabel#Title {{
    font-size: 30px;
    font-weight: 700;
    letter-spacing: -0.5px;
}}
QLabel#Subtitle {{
    color: {TEXT_MUTED.name()};
    font-size: 14px;
}}
QLabel#Hint {{
    color: {TEXT_DIM.name()};
    font-size: 13px;
}}
QLabel#Error {{
    color: {DANGER.name()};
}}

/* --- Поля ввода --- */

QLineEdit, QPlainTextEdit {{
    background-color: {SURFACE.name()};
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 12px 14px;
    font-size: 15px;
    selection-background-color: {ACCENT.name()};
    selection-color: #0b0b0b;
}}
QLineEdit:hover, QPlainTextEdit:hover {{
    background-color: {SURFACE_HOVER.name()};
}}
QLineEdit:focus, QPlainTextEdit:focus {{
    background-color: {SURFACE_HOVER.name()};
    border-color: {ACCENT.name()};
}}

QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {SURFACE.name()};
    border: 1px solid {BORDER.name()};
    border-radius: 6px;
    padding: 8px 12px;
}}
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT.name()};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background-color: {SURFACE_HOVER.name()};
    border: 1px solid {BORDER.name()};
    border-radius: 6px;
    selection-background-color: {ACCENT.name()};
    selection-color: #0b0b0b;
    padding: 4px;
}}

/* --- Списки --- */

QListWidget {{
    background-color: transparent;
    border: none;
    outline: none;
}}
QListWidget::item {{
    background-color: {SURFACE.name()};
    border-radius: 6px;
    padding: 14px 12px;
    margin-bottom: 6px;
    color: {TEXT_MUTED.name()};
}}
QListWidget::item:hover {{
    background-color: {SURFACE_HOVER.name()};
    color: {TEXT_PRIMARY.name()};
}}
QListWidget::item:selected {{
    background-color: {SURFACE_HOVER.name()};
    color: {ACCENT.name()};
    border-left: 3px solid {ACCENT.name()};
}}

/* Подсказки поиска выпадают под полем ввода прямо во время набора */
QListWidget#Suggestions {{
    background-color: {SURFACE.name()};
    border: 1px solid {BORDER.name()};
    border-radius: 8px;
    padding: 6px;
}}
QListWidget#Suggestions::item {{
    background-color: transparent;
    padding: 10px 12px;
    margin-bottom: 2px;
}}
QListWidget#Suggestions::item:selected {{
    background-color: {ACCENT.name()};
    color: #0b0b0b;
    border-left: none;
}}

/* --- Кнопки --- */

QPushButton {{
    background-color: transparent;
    border: 1px solid {BORDER.name()};
    border-radius: 500px;
    padding: 10px 20px;
    color: {TEXT_MUTED.name()};
    font-weight: 600;
}}
QPushButton:hover {{
    border-color: {TEXT_MUTED.name()};
    color: {TEXT_PRIMARY.name()};
}}
QPushButton:pressed {{
    background-color: {SURFACE.name()};
}}
QPushButton:disabled {{
    color: {TEXT_DIM.name()};
    border-color: {BORDER.name()};
}}

/* Главное действие — зелёная пилюля, как кнопка воспроизведения в плеере */
QPushButton#Primary {{
    background-color: {ACCENT.name()};
    color: #0b0b0b;
    font-size: 15px;
    font-weight: 700;
    border: none;
    padding: 12px 30px;
}}
QPushButton#Primary:hover {{
    background-color: {ACCENT_HOVER.name()};
}}
QPushButton#Primary:pressed {{
    background-color: {ACCENT_PRESSED.name()};
}}
QPushButton#Primary:disabled {{
    background-color: {BORDER.name()};
    color: {TEXT_DIM.name()};
}}

/* --- Прочие элементы --- */

QCheckBox {{
    spacing: 10px;
    color: {TEXT_MUTED.name()};
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border: 1px solid {TEXT_DIM.name()};
    border-radius: 4px;
    background-color: {SURFACE.name()};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT.name()};
    border-color: {ACCENT.name()};
}}
QCheckBox::indicator:hover {{
    border-color: {ACCENT.name()};
}}

QSlider::groove:horizontal {{
    height: 4px;
    background: {BORDER.name()};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT.name()};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {TEXT_PRIMARY.name()};
    width: 14px;
    margin: -6px 0;
    border-radius: 7px;
}}

QProgressBar {{
    background-color: {SURFACE.name()};
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
    color: {TEXT_MUTED.name()};
}}
QProgressBar::chunk {{
    background-color: {ACCENT.name()};
    border-radius: 3px;
}}

QScrollArea {{
    border: none;
    background-color: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER.name()};
    border-radius: 6px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{
    background: {TEXT_DIM.name()};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
}}

QDialog {{
    background-color: {BACKGROUND.name()};
}}
QMessageBox {{
    background-color: {SURFACE.name()};
}}
"""
