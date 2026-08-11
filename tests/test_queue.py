"""Тесты очереди песен."""

from prompter.models import Song, SongLine
from prompter.song_queue import SongQueue


def сделать_песню(название: str) -> Song:
    """Минимальная песня с одной поющейся строкой."""
    return Song(title=название, lines=[SongLine(text="строка выдуманного текста")])


class TestОчередь:
    def test_добавление_сохраняет_порядок(self):
        queue = SongQueue()
        queue.add("Первая", "Исполнитель")
        queue.add("Вторая")

        assert [item.title for item in queue.items] == ["Первая", "Вторая"]
        assert len(queue) == 2

    def test_удаление(self):
        queue = SongQueue()
        queue.add("Первая")
        queue.add("Вторая")

        queue.remove(0)

        assert [item.title for item in queue.items] == ["Вторая"]

    def test_перемещение_вверх_и_вниз(self):
        queue = SongQueue()
        queue.add("Первая")
        queue.add("Вторая")
        queue.add("Третья")

        assert queue.move_up(2) == 1
        assert [item.title for item in queue.items] == ["Первая", "Третья", "Вторая"]

        assert queue.move_down(0) == 1
        assert [item.title for item in queue.items] == ["Третья", "Первая", "Вторая"]

    def test_перемещение_за_границы_ничего_не_ломает(self):
        queue = SongQueue()
        queue.add("Единственная")

        assert queue.move_up(0) == 0
        assert queue.move_down(0) == 0
        assert len(queue) == 1

    def test_перетаскивание(self):
        queue = SongQueue()
        for название in ("Первая", "Вторая", "Третья"):
            queue.add(название)

        queue.move(0, 2)

        assert [item.title for item in queue.items] == ["Вторая", "Третья", "Первая"]


class TestИсполнение:
    def test_старт_идёт_с_первой_готовой(self):
        queue = SongQueue()
        queue.add("Без текста")  # текст не найден — пропускаем
        queue.add("С текстом", song=сделать_песню("С текстом"))

        current = queue.start()

        assert current is not None
        assert current.title == "С текстом"
        assert queue.position_label == "2 из 2"

    def test_переход_к_следующей(self):
        queue = SongQueue()
        queue.add("Первая", song=сделать_песню("Первая"))
        queue.add("Вторая", song=сделать_песню("Вторая"))

        queue.start()
        следующая = queue.advance()

        assert следующая is not None
        assert следующая.title == "Вторая"

    def test_конец_очереди(self):
        queue = SongQueue()
        queue.add("Единственная", song=сделать_песню("Единственная"))

        queue.start()

        assert queue.advance() is None
        assert queue.is_finished

    def test_очередь_без_готовых_песен(self):
        queue = SongQueue()
        queue.add("Текст не нашёлся")

        assert queue.start() is None
        assert not queue.has_ready_songs

    def test_указатель_едет_за_песней_при_перемещении(self):
        queue = SongQueue()
        queue.add("Первая", song=сделать_песню("Первая"))
        queue.add("Вторая", song=сделать_песню("Вторая"))
        queue.start()

        queue.move_down(0)

        # Играет по-прежнему «Первая», просто она теперь вторая в списке
        assert queue.current is not None
        assert queue.current.title == "Первая"
        assert queue.position_label == "2 из 2"

    def test_удаление_песни_выше_текущей_сдвигает_указатель(self):
        queue = SongQueue()
        queue.add("Первая", song=сделать_песню("Первая"))
        queue.add("Вторая", song=сделать_песню("Вторая"))
        queue.start()
        queue.advance()  # играет «Вторая»

        queue.remove(0)

        assert queue.current is not None
        assert queue.current.title == "Вторая"
        assert queue.position_label == "1 из 1"
