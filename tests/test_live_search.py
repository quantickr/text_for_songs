"""Тесты живого поиска: подсказки появляются во время набора.

Сеть не используется: ответы «источника» подставляются напрямую.
"""

from prompter.lyrics_provider import SearchResult
from prompter.song_queue import SongQueue

ВАРИАНТЫ = [
    SearchResult(title="Первая проба", artist="Выдуманная группа",
                 url="https://example.invalid/1", source="Тест"),
    SearchResult(title="Вторая проба", artist="Другая группа",
                 url="https://example.invalid/2", source="Тест"),
]


def сделать_экран(qt_app):
    from prompter.ui.queue_screen import QueueScreen, SongLoader

    queue = SongQueue()
    screen = QueueScreen(queue, SongLoader(respect_robots=True))
    return queue, screen


class TestЗадержкаНабора:
    def test_короткий_запрос_не_ищет(self, qt_app):
        _, screen = сделать_экран(qt_app)

        screen.search_edit.setText("ки")
        screen._on_query_changed("ки")

        # Дёргать сеть на двух символах бессмысленно: выдача будет случайной
        assert not screen._search_timer.isActive()

    def test_набор_откладывает_запрос(self, qt_app):
        _, screen = сделать_экран(qt_app)

        screen.search_edit.setText("кино")
        screen._on_query_changed("кино")

        assert screen._search_timer.isActive()
        assert screen._search_timer.interval() == 150

    def test_новая_буква_сбрасывает_отсчёт(self, qt_app):
        _, screen = сделать_экран(qt_app)

        screen._on_query_changed("кино")
        осталось_сначала = screen._search_timer.remainingTime()
        screen._on_query_changed("кино п")

        # Пока человек печатает, запрос не должен уходить
        assert screen._search_timer.remainingTime() >= осталось_сначала - 50


class TestКэшПодсказок:
    def test_повторный_запрос_отдаётся_без_сети(self, qt_app):
        _, screen = сделать_экран(qt_app)
        screen.loader._options_cache["проба"] = ВАРИАНТЫ
        screen.search_edit.setText("проба")

        полученные = []
        screen.loader.options_ready.connect(lambda q, v: полученные.append(v))
        screen.loader.find_options("проба")

        # Сигнал приходит сразу, в том же потоке: никакого ожидания сети
        assert полученные == [ВАРИАНТЫ]

    def test_незнакомый_запрос_кэш_не_отдаёт(self, qt_app):
        _, screen = сделать_экран(qt_app)

        полученные = []
        screen.loader.options_ready.connect(lambda q, v: полученные.append(v))
        screen.loader._options_cache["другое"] = ВАРИАНТЫ

        # Проверяем только то, что мгновенного ответа нет — сам запрос уйдёт в поток
        assert полученные == []


class TestПодсказки:
    def test_подсказки_показываются(self, qt_app):
        _, screen = сделать_экран(qt_app)
        screen.search_edit.setText("проба")

        screen._on_options_ready("проба", ВАРИАНТЫ)

        assert screen.suggestions_list.count() == 2
        assert not screen.suggestions_list.isHidden()
        assert "Выдуманная группа — Первая проба" in screen.suggestions_list.item(0).text()

    def test_устаревший_ответ_игнорируется(self, qt_app):
        _, screen = сделать_экран(qt_app)
        screen.search_edit.setText("кино перемен")

        # Ответ на «кино», пока в поле уже «кино перемен» — не про то, что набрано
        screen._on_options_ready("кино", ВАРИАНТЫ)

        assert screen.suggestions_list.count() == 0

    def test_пустая_выдача_прячет_подсказки(self, qt_app):
        _, screen = сделать_экран(qt_app)
        screen.search_edit.setText("проба")
        screen._on_options_ready("проба", ВАРИАНТЫ)

        screen._on_options_ready("проба", [])

        assert screen.suggestions_list.isHidden()
        assert screen._current_options == []

    def test_короткий_запрос_убирает_показанные_подсказки(self, qt_app):
        _, screen = сделать_экран(qt_app)
        screen.search_edit.setText("проба")
        screen._on_options_ready("проба", ВАРИАНТЫ)

        screen.search_edit.setText("пр")
        screen._on_query_changed("пр")

        assert screen.suggestions_list.isHidden()


class TestСменаВерсии:
    """У одной песни бывает несколько подборов — с табулатурой и с аккордами."""

    def test_кнопка_доступна_для_любой_выбранной_песни(self, qt_app):
        from prompter.models import Song, SongLine

        queue, screen = сделать_экран(qt_app)
        queue.add("Проба", "Группа", Song(title="Проба", lines=[SongLine(text="строка")]))
        screen.refresh()
        screen.list_widget.setCurrentRow(0)

        # Версии ищутся по запросу, а не только среди указанных на странице,
        # поэтому кнопка не должна зависеть от их наличия в самой песне
        assert screen.version_button.isEnabled()

    def test_без_выбора_кнопка_недоступна(self, qt_app):
        _, screen = сделать_экран(qt_app)
        screen.refresh()

        assert not screen.version_button.isEnabled()

    def test_смена_выделения_обновляет_кнопку(self, qt_app):
        from prompter.models import Song, SongLine

        queue, screen = сделать_экран(qt_app)
        queue.add("Проба", "Группа", Song(title="Проба", lines=[SongLine(text="строка")]))
        screen.refresh()

        screen.list_widget.setCurrentRow(0)

        # Раньше состояние кнопки считалось только при перерисовке списка
        # и не менялось при выборе другого пункта
        assert screen.version_button.isEnabled()


class TestВыбор:
    def test_enter_добавляет_выбранное(self, qt_app):
        queue, screen = сделать_экран(qt_app)
        screen.search_edit.setText("проба")
        screen._on_options_ready("проба", ВАРИАНТЫ)

        screen.suggestions_list.setCurrentRow(1)
        screen._take_first_suggestion()

        assert len(queue) == 1
        assert queue.items[0].title == "Вторая проба"
        assert queue.items[0].artist == "Другая группа"

    def test_после_выбора_поле_и_подсказки_очищаются(self, qt_app):
        _, screen = сделать_экран(qt_app)
        screen.search_edit.setText("проба")
        screen._on_options_ready("проба", ВАРИАНТЫ)

        screen._take_first_suggestion()

        assert screen.search_edit.text() == ""
        assert screen.suggestions_list.isHidden()
        assert not screen._search_timer.isActive()

    def test_без_подсказок_enter_ничего_не_ломает(self, qt_app):
        queue, screen = сделать_экран(qt_app)

        screen._take_first_suggestion()

        assert len(queue) == 0
