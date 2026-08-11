"""Диалог ручного ввода текста с аккордами — запасной путь, когда поиск не помог."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..lyrics_provider import FileProvider, ManualProvider, ProviderError
from ..models import Song

ПРИМЕР_ФОРМАТА = """Am        C         G
здесь идёт первая строка текста

Am           C
здесь вторая строка

Можно вставить и формат ChordPro:
[Am]аккорд стоит [C]прямо в строке"""


class LyricsDialog(QDialog):
    """Вставка текста руками или загрузка файла ``.txt`` / ``.pro``."""

    def __init__(
        self, title: str = "", artist: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Текст с аккордами")
        self.setMinimumSize(720, 560)

        self.song: Song | None = None
        self._manual = ManualProvider()
        self._files = FileProvider()

        self.title_edit = QLineEdit(title)
        self.title_edit.setPlaceholderText("Название трека")

        self.artist_edit = QLineEdit(artist)
        self.artist_edit.setPlaceholderText("Исполнитель или группа")

        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText(ПРИМЕР_ФОРМАТА)

        подсказка = QLabel(
            "Вставьте текст, где строка аккордов стоит над строкой слов — "
            "пробелы в начале строки важны, они и задают, над каким слогом аккорд."
        )
        подсказка.setObjectName("Hint")
        подсказка.setWordWrap(True)

        файл_кнопка = QPushButton("Загрузить файл…")
        файл_кнопка.clicked.connect(self._load_file)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Готово")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("Primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        поля = QHBoxLayout()
        поля.addWidget(self.title_edit, 1)
        поля.addWidget(self.artist_edit, 1)

        низ = QHBoxLayout()
        низ.addWidget(файл_кнопка)
        низ.addStretch(1)
        низ.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        layout.addLayout(поля)
        layout.addWidget(подсказка)
        layout.addWidget(self.text_edit, 1)
        layout.addLayout(низ)

    def set_text(self, text: str) -> None:
        self.text_edit.setPlainText(text)

    def _load_file(self) -> None:
        путь, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите файл с аккордами",
            "",
            "Тексты с аккордами (*.txt *.pro *.cho *.chopro *.crd *.chordpro);;Все файлы (*)",
        )
        if not путь:
            return

        try:
            песня = self._files.load(
                Path(путь), self.title_edit.text(), self.artist_edit.text()
            )
        except ProviderError as ошибка:
            QMessageBox.warning(self, "Не удалось прочитать файл", str(ошибка))
            return

        # Показываем разобранный текст, чтобы можно было поправить руками
        self.text_edit.setPlainText(_song_to_text(песня))
        if not self.title_edit.text():
            self.title_edit.setText(песня.title)
        if not self.artist_edit.text() and песня.artist:
            self.artist_edit.setText(песня.artist)

    def _accept(self) -> None:
        текст = self.text_edit.toPlainText().strip()
        if not текст:
            QMessageBox.warning(self, "Пусто", "Вставьте текст песни или загрузите файл.")
            return

        try:
            self.song = self._manual.load_text(
                текст,
                title=self.title_edit.text().strip(),
                artist=self.artist_edit.text().strip(),
            )
        except ProviderError as ошибка:
            QMessageBox.warning(self, "Не получилось разобрать текст", str(ошибка))
            return

        self.accept()


def _song_to_text(song: Song) -> str:
    """Собрать из разобранной песни обратно классический аккордовый лист."""
    строки: list[str] = []
    for line in song.lines:
        if line.section:
            строки.append(f"{line.section}:")
        chord_line = line.chord_line()
        if chord_line.strip():
            строки.append(chord_line)
        строки.append(line.text)
    return "\n".join(строки)
