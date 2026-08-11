"""Экран очереди: ввод песен, порядок исполнения, запуск.

Поиск текста запускается сразу после добавления песни и идёт в фоне, поэтому
можно набивать список дальше, не дожидаясь загрузки.
"""

from __future__ import annotations

import threading

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..lyrics_provider import ProviderError, SearchResult, build_default_provider
from ..models import QueueItem, Song
from ..song_queue import SongQueue

SEARCH_DELAY_MS = 150
"""Пауза после последней нажатой клавиши перед запросом подсказок.

Меньше делать смысла нет: при быстром наборе запрос успеет уйти на каждую
вторую букву, а выигрыша по ощущениям не будет — основное время съедает
не эта пауза, а сам поход в сеть.
"""

MIN_QUERY_LENGTH = 3
"""Короче этого искать бессмысленно: выдача будет случайной."""

_OPTIONS_CACHE_SIZE = 40
"""Сколько запросов помнить, чтобы не ходить в сеть за уже виденным."""

_МАКС_ВИДИМЫХ_ПОДСКАЗОК = 6
_ОТСТУП_ПОДСКАЗОК = 16  # поля рамки списка


class SongLoader(QObject):
    """Фоновый поиск текста песни.

    Сигналы испускаются из обычных потоков — Qt сам переносит вызов в поток
    интерфейса, потому что приёмник живёт там.
    """

    loaded = pyqtSignal(object, object)  # QueueItem, Song
    failed = pyqtSignal(object, str)  # QueueItem, текст ошибки
    options_ready = pyqtSignal(str, object)  # запрос, список SearchResult
    options_failed = pyqtSignal(str, str)  # запрос, текст ошибки
    versions_ready = pyqtSignal(object, object)  # QueueItem, список SearchResult

    def __init__(self, respect_robots: bool = True, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._provider = build_default_provider(respect_robots)
        # Подсказки для уже набранных запросов помним: при правке строки
        # человек постоянно возвращается к тому, что печатал секунду назад,
        # и повторный поход в сеть там — чистое ожидание на пустом месте
        self._options_cache: dict[str, list[SearchResult]] = {}

    def set_respect_robots(self, value: bool) -> None:
        self._provider = build_default_provider(value)
        self._options_cache.clear()

    def fetch(self, item: QueueItem) -> None:
        """Запустить поиск текста для пункта очереди."""
        threading.Thread(
            target=self._fetch_blocking, args=(item,), name="SongLoader", daemon=True
        ).start()

    def fetch_url(self, item: QueueItem, url: str) -> None:
        """Загрузить песню по прямой ссылке."""
        threading.Thread(
            target=self._fetch_url_blocking, args=(item, url), name="SongLoaderUrl", daemon=True
        ).start()

    def find_options(self, query: str) -> None:
        """Найти варианты песни по свободному запросу.

        Уже виденный запрос отдаём сразу, не заходя в сеть, — подсказки
        появляются мгновенно.
        """
        готовые = self._options_cache.get(query)
        if готовые is not None:
            self.options_ready.emit(query, готовые)
            return

        threading.Thread(
            target=self._find_options_blocking, args=(query,), name="SongSearch", daemon=True
        ).start()

    def find_versions(self, item: QueueItem) -> None:
        """Найти другие подборы уже добавленной песни."""
        threading.Thread(
            target=self._find_versions_blocking, args=(item,), name="SongVersions", daemon=True
        ).start()

    def _find_versions_blocking(self, item: QueueItem) -> None:
        """Собрать версии: и перечисленные на странице, и найденные поиском.

        Одной страницы мало: соседние версии перечисляет не каждый сайт,
        а вот поиск по названию обычно находит и другие подборы, и каверы.
        """
        версии: list[SearchResult] = []
        текущий_url = item.song.source_url if item.song else ""

        if item.song is not None:
            for альтернатива in item.song.alternatives:
                версии.append(
                    SearchResult(
                        title=альтернатива.label or item.title,
                        artist=item.artist,
                        url=альтернатива.url,
                        source="со страницы подбора",
                    )
                )

        запрос = f"{item.artist} {item.title}".strip()
        try:
            версии.extend(self._provider.search_options(запрос))
        except Exception:
            pass  # поиск мог отвалиться — покажем хотя бы то, что есть

        # Текущая версия в списке не нужна, дубликаты тоже
        видели = {текущий_url}
        уникальные: list[SearchResult] = []
        for версия in версии:
            if версия.url in видели:
                continue
            видели.add(версия.url)
            уникальные.append(версия)

        self.versions_ready.emit(item, уникальные)

    def _find_options_blocking(self, query: str) -> None:
        try:
            варианты = self._provider.search_options(query)
        except ProviderError as error:
            self.options_failed.emit(query, str(error))
            return
        except Exception as error:
            self.options_failed.emit(query, f"неожиданная ошибка: {error}")
            return

        # Кэш держим небольшим: он живёт только на время набора
        if len(self._options_cache) > _OPTIONS_CACHE_SIZE:
            self._options_cache.clear()
        self._options_cache[query] = варианты

        self.options_ready.emit(query, варианты)

    def _fetch_blocking(self, item: QueueItem) -> None:
        try:
            song = self._provider.search(item.title, item.artist)
        except ProviderError as error:
            self.failed.emit(item, str(error))
            return
        except Exception as error:  # разметка сайта могла измениться
            self.failed.emit(item, f"неожиданная ошибка: {error}")
            return

        if song is None:
            self.failed.emit(item, "текст не найден")
        else:
            self.loaded.emit(item, song)

    def _fetch_url_blocking(self, item: QueueItem, url: str) -> None:
        try:
            song = self._provider.fetch_url(url)
        except ProviderError as error:
            self.failed.emit(item, str(error))
            return
        except Exception as error:
            self.failed.emit(item, f"неожиданная ошибка: {error}")
            return

        if song is None:
            self.failed.emit(item, "по ссылке ничего не разобралось")
        else:
            self.loaded.emit(item, song)


class QueueScreen(QWidget):
    """Экран ввода песен и управления очередью."""

    start_requested = pyqtSignal()
    settings_requested = pyqtSignal()
    manual_lyrics_requested = pyqtSignal(object)  # QueueItem или None

    def __init__(self, queue: SongQueue, loader: SongLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.queue = queue
        self.loader = loader
        self.loader.loaded.connect(self._on_loaded)
        self.loader.failed.connect(self._on_failed)
        self.loader.options_ready.connect(self._on_options_ready)
        self.loader.options_failed.connect(self._on_options_failed)
        self.loader.versions_ready.connect(self._on_versions_ready)

        заголовок = QLabel("Суфлёр")
        заголовок.setObjectName("Title")

        подзаголовок = QLabel(
            "Добавьте песни в очередь. Текст ищется автоматически; "
            "если не нашёлся — вставьте его вручную или дайте ссылку на подбор."
        )
        подзаголовок.setObjectName("Subtitle")
        подзаголовок.setWordWrap(True)

        # Одно поле: исполнитель и название пишутся вместе, как их и ищут.
        # Поиск идёт прямо во время набора, нажимать ничего не нужно.
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Исполнитель и название — одной строкой")
        self.search_edit.textEdited.connect(self._on_query_changed)
        self.search_edit.returnPressed.connect(self._take_first_suggestion)
        self.search_edit.installEventFilter(self)

        # Подсказки живут отдельным списком под полем, а не в модальном окне:
        # иначе каждый набранный символ перекрывал бы экран диалогом
        self.suggestions_list = QListWidget()
        self.suggestions_list.setObjectName("Suggestions")
        self.suggestions_list.itemActivated.connect(self._on_suggestion_chosen)
        self.suggestions_list.itemClicked.connect(self._on_suggestion_chosen)
        self.suggestions_list.hide()

        # Задержка перед запросом: пока человек печатает, дёргать сеть на каждую
        # букву незачем — и невежливо по отношению к сайту
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self._run_live_search)
        self._current_options: list[SearchResult] = []

        manual_button = QPushButton("Вставить текст…")
        manual_button.clicked.connect(lambda: self.manual_lyrics_requested.emit(None))

        url_button = QPushButton("По ссылке…")
        url_button.clicked.connect(self._add_by_url)

        self.list_widget = QListWidget()
        self.list_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.model().rowsMoved.connect(self._on_rows_moved)
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)
        # Без этого кнопки остаются в состоянии, посчитанном при прошлой
        # перерисовке, и не реагируют на выбор другого пункта
        self.list_widget.currentRowChanged.connect(lambda _: self._update_version_button())

        up_button = QPushButton("↑ Выше")
        up_button.clicked.connect(lambda: self._move(-1))

        down_button = QPushButton("↓ Ниже")
        down_button.clicked.connect(lambda: self._move(1))

        remove_button = QPushButton("Удалить")
        remove_button.clicked.connect(self._remove_selected)

        fix_button = QPushButton("Задать текст…")
        fix_button.clicked.connect(self._fix_selected)

        self.version_button = QPushButton("Другая версия…")
        self.version_button.clicked.connect(self._choose_version)
        self.version_button.setToolTip(
            "У песни бывает несколько подборов: с табулатурой и с аккордами, "
            "в разных тональностях."
        )
        self.version_button.setEnabled(False)

        settings_button = QPushButton("Настройки…")
        settings_button.clicked.connect(self.settings_requested.emit)

        self.start_button = QPushButton("Начать")
        self.start_button.setObjectName("Primary")
        self.start_button.clicked.connect(self.start_requested.emit)
        self.start_button.setEnabled(False)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Hint")

        ввод = QHBoxLayout()
        ввод.addWidget(self.search_edit, 3)
        ввод.addWidget(manual_button)
        ввод.addWidget(url_button)

        управление = QHBoxLayout()
        управление.addWidget(up_button)
        управление.addWidget(down_button)
        управление.addWidget(remove_button)
        управление.addWidget(fix_button)
        управление.addWidget(self.version_button)
        управление.addStretch(1)
        управление.addWidget(settings_button)
        управление.addWidget(self.start_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(14)
        layout.addWidget(заголовок)
        layout.addWidget(подзаголовок)
        layout.addLayout(ввод)
        layout.addWidget(self.suggestions_list)
        layout.addWidget(self.list_widget, 1)
        layout.addWidget(self.status_label)
        layout.addLayout(управление)

    # --- Добавление песен --------------------------------------------------

    def _on_query_changed(self, text: str) -> None:
        """Пользователь печатает — откладываем запрос, пока набор не затихнет."""
        if len(text.strip()) < MIN_QUERY_LENGTH:
            self._search_timer.stop()
            self._hide_suggestions()
            return
        self._search_timer.start()  # перезапуск сбрасывает предыдущий отсчёт

    def _run_live_search(self) -> None:
        запрос = self.search_edit.text().strip()
        if len(запрос) >= MIN_QUERY_LENGTH:
            self.loader.find_options(запрос)

    def _on_options_ready(self, запрос: str, варианты: list[SearchResult]) -> None:
        """Показать подсказки под полем ввода."""
        # Пока запрос летал по сети, человек мог набрать дальше — старый ответ
        # уже не про то, что в поле, и подсказки от него будут прыгать
        if запрос != self.search_edit.text().strip():
            return

        self._current_options = варианты
        self.suggestions_list.clear()

        if not варианты:
            self.status_label.setText(
                f"По запросу «{запрос}» ничего не нашлось. "
                "Попробуйте иначе, вставьте текст вручную или дайте ссылку."
            )
            self._hide_suggestions()
            return

        for вариант in варианты:
            элемент = QListWidgetItem(f"{вариант.display_name}   ·   {вариант.source}")
            элемент.setToolTip(вариант.url)
            self.suggestions_list.addItem(элемент)

        self.suggestions_list.setCurrentRow(0)
        self._подогнать_высоту_подсказок(len(варианты))
        self.suggestions_list.show()
        self.status_label.setText(
            f"Нашлось: {len(варианты)}. Enter — добавить первое, ↓ — выбрать другое."
        )

    def _подогнать_высоту_подсказок(self, сколько: int) -> None:
        """Подстроить список под содержимое: пустая полоса под ним выглядит небрежно."""
        строка = self.suggestions_list.sizeHintForRow(0) if сколько else 0
        высота = строка * min(сколько, _МАКС_ВИДИМЫХ_ПОДСКАЗОК) + _ОТСТУП_ПОДСКАЗОК
        self.suggestions_list.setFixedHeight(высота)

    def _on_options_failed(self, запрос: str, ошибка: str) -> None:
        if запрос != self.search_edit.text().strip():
            return
        self._hide_suggestions()
        self.status_label.setText(f"Поиск не удался ({ошибка}). Можно вставить текст вручную.")

    def _hide_suggestions(self) -> None:
        self.suggestions_list.hide()
        self.suggestions_list.clear()
        self._current_options = []

    def _take_first_suggestion(self) -> None:
        """Enter в поле ввода — берём выбранную подсказку."""
        if not self._current_options:
            return
        строка = max(0, self.suggestions_list.currentRow())
        self._add_from_option(строка)

    def _on_suggestion_chosen(self, item: QListWidgetItem) -> None:
        self._add_from_option(self.suggestions_list.row(item))

    def _add_from_option(self, index: int) -> None:
        """Добавить в очередь выбранный вариант и загрузить его текст."""
        if not 0 <= index < len(self._current_options):
            return

        выбранный = self._current_options[index]
        item = self.queue.add(выбранный.title, выбранный.artist)

        self._search_timer.stop()
        self.search_edit.clear()
        self.search_edit.setFocus()
        self._hide_suggestions()

        self.refresh()
        self.status_label.setText(f"Загружаю: {выбранный.display_name}")
        self.loader.fetch_url(item, выбранный.url)

    def eventFilter(self, obj, event):  # noqa: N802 (имя из Qt)
        """Стрелка вниз из поля ввода уводит фокус в список подсказок."""
        if (
            obj is self.search_edit
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Down
            and self.suggestions_list.isVisible()
        ):
            self.suggestions_list.setFocus()
            return True
        return super().eventFilter(obj, event)

    def _add_by_url(self) -> None:
        ссылка, ok = QInputDialog.getText(
            self,
            "Ссылка на подбор",
            "Вставьте ссылку на страницу с аккордами:",
        )
        if not ok or not ссылка.strip():
            return

        item = self.queue.add("Загрузка по ссылке…", "")
        self.refresh()
        self.loader.fetch_url(item, ссылка.strip())

    def add_ready_song(self, song: Song) -> None:
        """Добавить песню, текст которой уже есть (ручной ввод или файл)."""
        item = self.queue.add(song.title, song.artist, song)
        item.error = None
        self.refresh()
        self.status_label.setText(f"Добавлено: {item.display_name}")

    def apply_song(self, item: QueueItem, song: Song) -> None:
        """Подставить текст в уже существующий пункт очереди."""
        item.song = song
        item.error = None
        if song.title:
            item.title = song.title
        if song.artist:
            item.artist = song.artist
        self.refresh()

    # --- Реакция на загрузку ----------------------------------------------

    def _on_loaded(self, item: QueueItem, song: Song) -> None:
        item.song = song
        item.error = None
        # У поиска по ссылке название заранее неизвестно
        if song.title and item.title in ("", "Загрузка по ссылке…"):
            item.title = song.title
        if song.artist and not item.artist:
            item.artist = song.artist
        self.refresh()
        self.status_label.setText(f"Готово: {item.display_name}")

    def _on_failed(self, item: QueueItem, error: str) -> None:
        item.error = error
        self.refresh()
        self.status_label.setText(
            f"{item.display_name}: {error}. Нажмите «Задать текст…» и вставьте его вручную."
        )

    # --- Список ------------------------------------------------------------

    def refresh(self) -> None:
        """Перерисовать список из очереди — единственный источник правды."""
        выбранная = self.list_widget.currentRow()

        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for позиция, item in enumerate(self.queue.items, start=1):
            widget_item = QListWidgetItem(f"{позиция}.  {item.display_name}   {_status(item)}")
            widget_item.setToolTip(_tooltip(item))
            self.list_widget.addItem(widget_item)
        self.list_widget.blockSignals(False)

        if 0 <= выбранная < self.list_widget.count():
            self.list_widget.setCurrentRow(выбранная)

        self.start_button.setEnabled(self.queue.has_ready_songs)
        self._update_version_button()

    def _update_version_button(self) -> None:
        """Смена версии доступна для любой выбранной песни.

        Версии ищутся по запросу, а не только среди перечисленных на странице:
        соседние подборы указывает не каждый сайт, а поиск обычно находит.
        """
        self.version_button.setEnabled(self.selected_item() is not None)

    def selected_item(self) -> QueueItem | None:
        строка = self.list_widget.currentRow()
        пункты = self.queue.items
        return пункты[строка] if 0 <= строка < len(пункты) else None

    def _move(self, direction: int) -> None:
        строка = self.list_widget.currentRow()
        if строка < 0:
            return
        новая = self.queue.move_up(строка) if direction < 0 else self.queue.move_down(строка)
        self.refresh()
        self.list_widget.setCurrentRow(новая)

    def _remove_selected(self) -> None:
        строка = self.list_widget.currentRow()
        if строка < 0:
            return
        self.queue.remove(строка)
        self.refresh()

    def _choose_version(self) -> None:
        """Поискать другие подборы этой песни и переключиться на выбранный."""
        item = self.selected_item()
        if item is None:
            QMessageBox.information(self, "Выберите песню", "Сначала выберите пункт в списке.")
            return

        self.status_label.setText(f"Ищу другие версии: {item.display_name}…")
        self.loader.find_versions(item)

    def _on_versions_ready(self, item: QueueItem, версии: list[SearchResult]) -> None:
        """Показать найденные версии подбора."""
        if not версии:
            # Поиск охватывает не все сайты: на amdm.ru он закрыт robots.txt,
            # хотя версий там может быть несколько. Прямая ссылка — рабочий путь
            self._ask_version_url(item)
            return

        подписи = [f"{в.display_name}   ·   {в.source}" for в in версии]
        подписи.append("Вставить ссылку на другой подбор…")
        выбор, ok = QInputDialog.getItem(
            self, "Другая версия подбора", "Доступные варианты:", подписи, 0, False
        )
        if not ok:
            self.status_label.setText("Версия не менялась")
            return

        if выбор == подписи[-1]:
            self._ask_version_url(item)
            return

        выбранная = версии[подписи.index(выбор)]
        item.song = None  # пока грузится, песня считается неготовой
        item.error = None
        self.refresh()
        self.status_label.setText(f"Загружаю: {выбранная.display_name}")
        self.loader.fetch_url(item, выбранная.url)

    def _ask_version_url(self, item: QueueItem) -> None:
        """Спросить прямую ссылку на другой подбор этой же песни."""
        ссылка, ok = QInputDialog.getText(
            self,
            "Другая версия подбора",
            f"Автопоиск других версий «{item.display_name}» ничего не дал.\n"
            "Если знаете ссылку на другой подбор — вставьте её:",
        )
        if not ok or not ссылка.strip():
            self.status_label.setText("Версия не менялась")
            return

        item.song = None
        item.error = None
        self.refresh()
        self.status_label.setText(f"Загружаю другую версию: {item.display_name}")
        self.loader.fetch_url(item, ссылка.strip())

    def _fix_selected(self) -> None:
        item = self.selected_item()
        if item is None:
            QMessageBox.information(self, "Выберите песню", "Сначала выберите пункт в списке.")
            return
        self.manual_lyrics_requested.emit(item)

    def _on_item_double_clicked(self, _item: QListWidgetItem) -> None:
        self._fix_selected()

    def _on_rows_moved(
        self, _parent, source_start: int, _source_end: int, _dest_parent, dest_row: int
    ) -> None:
        """Синхронизировать очередь после перетаскивания мышью.

        Qt сообщает позицию вставки в координатах списка до удаления строки,
        поэтому при движении вниз индекс нужно уменьшить на единицу.
        """
        target = dest_row - 1 if dest_row > source_start else dest_row
        self.queue.move(source_start, target)
        self.refresh()
        self.list_widget.setCurrentRow(target)


def _status(item: QueueItem) -> str:
    if item.is_ready:
        assert item.song is not None
        строк = len(item.song.singable_indexes)
        статус = f"— готово, строк: {строк}"
        # Подбор без аккордов — не поломка, но об этом надо сказать прямо:
        # рядом может лежать другая версия, где аккорды есть
        if not item.song.has_chords:
            статус += ", без аккордов"
            if item.song.alternatives:
                статус += f" (есть другие версии: {len(item.song.alternatives)})"
        return статус
    if item.error:
        return f"— {item.error}"
    return "— ищу текст…"


def _tooltip(item: QueueItem) -> str:
    if item.song is not None and item.song.source:
        источник = item.song.source_url or item.song.source
        return f"Источник: {источник}"
    if item.error:
        return item.error
    return "Идёт поиск текста"
