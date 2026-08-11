"""Главное окно: переключение между очередью и исполнением, вся связка модулей."""

from __future__ import annotations

import threading

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QKeyEvent
from PyQt6.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QStackedWidget,
)

from .. import apple_speech
from ..matcher import LineMatcher
from ..models import QueueItem, Song
from ..parser import parse_section_timing
from ..song_queue import SongQueue
from ..settings import AppSettings
from ..speech import SpeechRecognizer
from ..vosk_models import ModelError, download_model, find_model, model_spec
from .lyrics_dialog import LyricsDialog
from .performance_screen import PerformanceScreen
from .queue_screen import QueueScreen, SongLoader
from .settings_dialog import SettingsDialog


class ModelDownloader(QObject):
    """Загрузка модели vosk в фоне с показом прогресса."""

    progress = pyqtSignal(int, int)
    finished = pyqtSignal(str)  # путь к модели
    failed = pyqtSignal(str)

    def __init__(
        self, language: str, accurate: bool = False, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.language = language
        self.accurate = accurate
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def start(self) -> None:
        threading.Thread(target=self._run, name="ModelDownloader", daemon=True).start()

    def _run(self) -> None:
        try:
            path = download_model(
                self.language,
                progress=lambda done, total: self.progress.emit(done, total),
                should_cancel=lambda: self._cancelled,
                accurate=self.accurate,
            )
        except ModelError as error:
            self.failed.emit(str(error))
            return
        self.finished.emit(str(path))


class MainWindow(QMainWindow):
    """Окно приложения. Держит очередь, распознавание и оба экрана."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Суфлёр для музыкантов")
        self.resize(1180, 760)

        self.settings = AppSettings.load()
        self.queue = SongQueue()
        self.matcher: LineMatcher | None = None

        # Таймер ухода со служебных строк: заголовков и проигрышей
        self._service_timer = QTimer(self)
        self._service_timer.setSingleShot(True)
        self._service_timer.timeout.connect(self._on_service_timeout)
        self._pending_skip_index = -1

        self.loader = SongLoader(self.settings.respect_robots, self)
        self.queue_screen = QueueScreen(self.queue, self.loader, self)
        self.performance_screen = PerformanceScreen(self)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.queue_screen)
        self.stack.addWidget(self.performance_screen)
        self.setCentralWidget(self.stack)

        # Два движка с одинаковым набором сигналов: системный точнее, но
        # доступен только в .app, поэтому vosk остаётся всегда под рукой
        self.recognizer = SpeechRecognizer(self)
        self.apple_recognizer = apple_speech.AppleSpeechRecognizer(self)
        for движок in (self.recognizer, self.apple_recognizer):
            движок.partial_ready.connect(self._on_partial)
            движок.final_ready.connect(self._on_final)
            движок.level_changed.connect(self.performance_screen.set_level)
            движок.listening_changed.connect(self.performance_screen.set_listening)
            движок.error_occurred.connect(self._on_speech_error)
            движок.warning_occurred.connect(self._on_speech_warning)
            движок.vocabulary_applied.connect(self._on_vocabulary_applied)

        self.queue_screen.start_requested.connect(self.start_performance)
        self.queue_screen.settings_requested.connect(self.open_settings)
        self.queue_screen.manual_lyrics_requested.connect(self.open_lyrics_dialog)

        self._apply_display_settings()

    # --- Экраны ------------------------------------------------------------

    def show_queue(self) -> None:
        """Вернуться к очереди и заглушить микрофон."""
        self._stop_listening()
        self.stack.setCurrentWidget(self.queue_screen)
        self.queue_screen.refresh()

    def start_performance(self) -> None:
        """Начать исполнение с первой готовой песни очереди."""
        item = self.queue.start()
        if item is None:
            QMessageBox.information(
                self,
                "Нечего исполнять",
                "Ни у одной песни в очереди нет текста. Добавьте текст вручную "
                "или дайте ссылку на подбор.",
            )
            return
        self.stack.setCurrentWidget(self.performance_screen)
        self._load_current_song()

    def _load_current_song(self) -> None:
        """Показать текущую песню очереди и запустить под неё распознавание."""
        item = self.queue.current
        if item is None or item.song is None:
            self._finish_queue()
            return

        song = item.song
        self.matcher = LineMatcher(
            [line.text for line in song.lines],
            self.settings.matcher_config(),
            navigable=song.navigable_indexes,
        )
        # Начинаем с самой первой видимой строки, а не с первой поющейся:
        # иначе вступление и схема боя просто не будут показаны
        строки = song.navigable_indexes
        if строки:
            self.matcher.set_position(строки[0])
        self.performance_screen.show_song(song, self.queue.position_label)
        self.performance_screen.set_recognized("")
        self._update_line_display()

        if self.settings.voice_scroll_enabled:
            self._start_listening(song)
        else:
            self._stop_listening()
            self.performance_screen.set_listening(False)

    def _finish_queue(self) -> None:
        """Очередь пройдена — возвращаемся к экрану ввода."""
        self._stop_listening()
        self.stack.setCurrentWidget(self.queue_screen)
        self.queue_screen.refresh()
        QMessageBox.information(self, "Очередь пройдена", "Все песни исполнены.")

    def next_song(self) -> None:
        """Перейти к следующей песне очереди."""
        if self.queue.advance() is None:
            self._finish_queue()
        else:
            self._load_current_song()

    # --- Распознавание -----------------------------------------------------

    def _start_listening(self, song: Song) -> None:
        """Поднять распознавание на языке этой песни."""
        language = song.detect_language()
        словарь = song.vocabulary() if self.settings.limit_vocabulary else None

        if self._системный_движок_готов(language):
            self.apple_recognizer.start(language, словарь)
            return

        model_path = find_model(language, prefer_accurate=self.settings.accurate_model)
        if model_path is None:
            self._offer_model_download(language)
            return

        # Ограничиваем словарь словами этой песни — резко поднимает точность
        self.recognizer.start(
            model_path, self.settings.device_index, словарь, self.settings.denoise
        )

    def _системный_движок_готов(self, language: str) -> bool:
        """Можно ли взять системное распознавание macOS.

        Молча откатываемся на vosk, если приложение запущено не как ``.app``
        или разрешение не выдано: суфлёр должен работать, а не падать.
        """
        if self.settings.speech_engine != "apple":
            return False

        if not apple_speech.разрешение_выдано():
            self.performance_screen.set_recognized(
                "Системное распознавание недоступно — работает vosk. "
                "Запустите приложение как .app и разрешите доступ."
            )
            return False

        if not apple_speech.поддерживает_язык(language):
            self.performance_screen.set_recognized(
                f"Система не знает язык «{language}» — работает vosk."
            )
            return False
        return True

    def _stop_listening(self) -> None:
        """Заглушить оба движка разом — какой бы ни работал."""
        self.recognizer.stop()
        self.apple_recognizer.stop()

    def _offer_model_download(self, language: str) -> None:
        """Предложить скачать недостающую модель распознавания."""
        spec = model_spec(language, self.settings.accurate_model)
        if spec is None:
            return

        ответ = QMessageBox.question(
            self,
            "Нужна модель распознавания",
            f"{spec.description} ещё не скачана (~{spec.size_mb} МБ).\n\n"
            "Скачать сейчас? Без неё текст можно листать вручную "
            "пробелом и стрелками.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ответ != QMessageBox.StandardButton.Yes:
            return

        диалог = QProgressDialog(
            f"Скачиваю {spec.name}…", "Отмена", 0, 100, self
        )
        диалог.setWindowModality(Qt.WindowModality.WindowModal)
        диалог.setAutoClose(False)
        диалог.setValue(0)

        загрузчик = ModelDownloader(language, self.settings.accurate_model, self)

        def на_прогресс(done: int, total: int) -> None:
            диалог.setMaximum(100 if total else 0)
            if total:
                диалог.setValue(int(done * 100 / total))

        def на_готово(_path: str) -> None:
            диалог.close()
            песня = self.queue.current
            if песня is not None and песня.song is not None:
                self._start_listening(песня.song)

        def на_ошибку(текст: str) -> None:
            диалог.close()
            QMessageBox.warning(self, "Не удалось скачать модель", текст)

        загрузчик.progress.connect(на_прогресс)
        загрузчик.finished.connect(на_готово)
        загрузчик.failed.connect(на_ошибку)
        диалог.canceled.connect(загрузчик.cancel)
        загрузчик.start()

    def _on_partial(self, text: str) -> None:
        if self.settings.show_debug_log:
            self.performance_screen.set_recognized(text)
        self._apply_decision(self.matcher.feed_partial(text) if self.matcher else None)

    def _on_final(self, text: str) -> None:
        if self.settings.show_debug_log:
            self.performance_screen.set_recognized(text)
        self._apply_decision(self.matcher.feed_final(text) if self.matcher else None)

    def _apply_decision(self, decision) -> None:  # noqa: ANN001 (MatchDecision | None)
        if decision is None or not self.settings.voice_scroll_enabled:
            return
        if decision.song_finished:
            self.next_song()
            return
        self._update_line_display()

    def _on_speech_error(self, message: str) -> None:
        self.performance_screen.set_listening(False)
        QMessageBox.warning(
            self,
            "Проблема с микрофоном",
            f"{message}\n\nТекст можно листать вручную: пробел, стрелки.",
        )

    def _on_speech_warning(self, message: str) -> None:
        self.performance_screen.set_recognized(message)

    def _on_vocabulary_applied(self, количество: int) -> None:
        """Показать, что распознаватель слушает только слова этой песни."""
        if self.settings.show_debug_log:
            self.performance_screen.set_recognized(
                f"словарь песни: {количество} слов"
            )

    # --- Отображение и ручное управление -----------------------------------

    def _update_line_display(self) -> None:
        item = self.queue.current
        if item is None or item.song is None or self.matcher is None:
            return

        song = item.song
        index = self.matcher.position
        поющиеся = song.singable_indexes
        порядок = поющиеся.index(index) + 1 if index in поющиеся else 0
        self.performance_screen.set_line(index, len(поющиеся), порядок)
        self._schedule_service_skip(song, index)

    def _schedule_service_skip(self, song: Song, index: int) -> None:
        """Завести таймер ухода со служебной строки.

        Заголовки блоков и проигрыши спеть нельзя, поэтому голос их никогда не
        сдвинет — на них суфлёр завис бы навсегда. Держим такую строку столько,
        сколько указано в ней самой («Проигрыш 8 сек»), а если ничего не
        указано — совсем недолго, только чтобы человек успел её заметить.
        """
        self._service_timer.stop()

        if not self.settings.auto_skip_service_lines:
            return
        if not 0 <= index < len(song.lines):
            return

        line = song.lines[index]
        if line.has_text:
            return  # обычную строку уводит голос или рука

        задержка = None
        if line.section:
            задержка = parse_section_timing(line.section)
        if задержка is None:
            задержка = self.settings.service_line_delay

        self._pending_skip_index = index
        self._service_timer.start(int(задержка * 1000))

    def _on_service_timeout(self) -> None:
        """Сработал таймер служебной строки — уходим дальше."""
        if self.matcher is None or self.matcher.position != self._pending_skip_index:
            return  # человек уже ушёл сам
        self._step_line(1)

    def _step_line(self, direction: int) -> None:
        """Листание строк. С последней строки уходим к следующей песне.

        Идём по всем видимым строкам, включая заголовки и проигрыши: их тоже
        надо показать, просто уходят они по таймеру, а не по голосу.
        """
        item = self.queue.current
        if item is None or item.song is None or self.matcher is None:
            return

        строки = item.song.navigable_indexes
        if not строки:
            return

        текущая = self.matcher.position
        if текущая in строки:
            позиция = строки.index(текущая)
        else:
            # Позиция могла оказаться на пустом разделителе — берём ближайшую
            позиция = min(range(len(строки)), key=lambda i: abs(строки[i] - текущая))
        новая = позиция + direction

        if новая >= len(строки):
            self.next_song()
            return
        новая = max(0, новая)

        self.matcher.set_position(строки[новая])
        self._update_line_display()

    def _apply_display_settings(self) -> None:
        view = self.performance_screen.lyrics_view
        view.set_font_size(self.settings.font_size)
        view.set_context_lines(self.settings.context_lines)
        view.set_animation_duration(self.settings.scroll_animation_ms)

    def open_settings(self) -> None:
        диалог = SettingsDialog(self.settings, self)
        if диалог.exec():
            self._apply_display_settings()
            self.loader.set_respect_robots(self.settings.respect_robots)
            if self.matcher is not None:
                self.matcher.config = self.settings.matcher_config()
            # Переподнять микрофон, если настройки звука поменялись
            if self.stack.currentWidget() is self.performance_screen:
                item = self.queue.current
                if item is not None and item.song is not None:
                    if self.settings.voice_scroll_enabled:
                        self._start_listening(item.song)
                    else:
                        self._stop_listening()

    def open_lyrics_dialog(self, item: QueueItem | None) -> None:
        """Ручная вставка текста: для нового пункта или для уже существующего."""
        диалог = LyricsDialog(
            title=item.title if item else "",
            artist=item.artist if item else "",
            parent=self,
        )
        if not диалог.exec() or диалог.song is None:
            return

        if item is None:
            self.queue_screen.add_ready_song(диалог.song)
        else:
            self.queue_screen.apply_song(item, диалог.song)

    # --- Клавиатура и закрытие ---------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (имя из Qt)
        """Горячие клавиши экрана исполнения."""
        if self.stack.currentWidget() is not self.performance_screen:
            super().keyPressEvent(event)
            return

        key = event.key()
        if key in (Qt.Key.Key_Space, Qt.Key.Key_Right, Qt.Key.Key_Down):
            self._step_line(1)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self._step_line(-1)
        elif key == Qt.Key.Key_N:
            self.next_song()
        elif key == Qt.Key.Key_Escape:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.show_queue()
        elif key == Qt.Key.Key_F:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Остановить распознавание, иначе приложение падает на выходе."""
        self._stop_listening()
        self.settings.save()
        super().closeEvent(event)
