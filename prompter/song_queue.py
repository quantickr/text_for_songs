"""Очередь песен: что играем, в каком порядке и что идёт следующим.

Класс намеренно не зависит от Qt — так его легко тестировать и он не тянет
интерфейс в бизнес-логику. Экран очереди сам перерисовывается после операций.
"""

from __future__ import annotations

from .models import QueueItem, Song


class SongQueue:
    """Упорядоченный список песен с указателем на текущую."""

    def __init__(self) -> None:
        self._items: list[QueueItem] = []
        self._current: int = -1  # -1 означает, что исполнение не начато

    # --- Содержимое --------------------------------------------------------

    @property
    def items(self) -> list[QueueItem]:
        """Копия списка: снаружи очередь меняют только через методы."""
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def add(self, title: str, artist: str = "", song: Song | None = None) -> QueueItem:
        """Добавить песню в конец очереди.

        Текст может быть ещё не найден — поиск идёт в фоне, пока пользователь
        набивает остальные пункты.
        """
        item = QueueItem(title=title.strip(), artist=artist.strip(), song=song)
        self._items.append(item)
        return item

    def remove(self, index: int) -> None:
        """Удалить пункт, сохранив корректный указатель на текущую песню."""
        if not 0 <= index < len(self._items):
            return
        self._items.pop(index)

        if self._current > index:
            self._current -= 1
        elif self._current == index:
            # Удалили ту, что играет: указатель встаёт на её место в списке,
            # но не должен вылезти за конец
            self._current = min(self._current, len(self._items) - 1)

    def move_up(self, index: int) -> int:
        """Поднять пункт на позицию выше. Возвращает новый индекс пункта."""
        return self._swap(index, index - 1)

    def move_down(self, index: int) -> int:
        """Опустить пункт на позицию ниже. Возвращает новый индекс пункта."""
        return self._swap(index, index + 1)

    def move(self, source: int, target: int) -> None:
        """Переставить пункт (используется при перетаскивании мышью)."""
        if not 0 <= source < len(self._items) or not 0 <= target < len(self._items):
            return
        item = self._items.pop(source)
        self._items.insert(target, item)

    def _swap(self, first: int, second: int) -> int:
        if not 0 <= first < len(self._items) or not 0 <= second < len(self._items):
            return first
        self._items[first], self._items[second] = self._items[second], self._items[first]

        # Указатель текущей песни должен ездить вместе с ней
        if self._current == first:
            self._current = second
        elif self._current == second:
            self._current = first
        return second

    def clear(self) -> None:
        self._items.clear()
        self._current = -1

    # --- Исполнение --------------------------------------------------------

    @property
    def current_index(self) -> int:
        return self._current

    @property
    def current(self) -> QueueItem | None:
        if 0 <= self._current < len(self._items):
            return self._items[self._current]
        return None

    @property
    def position_label(self) -> str:
        """Подпись «песня X из N» для верхней панели экрана исполнения."""
        if self._current < 0 or not self._items:
            return ""
        return f"{self._current + 1} из {len(self._items)}"

    def start(self) -> QueueItem | None:
        """Начать с первой песни, у которой есть готовый текст."""
        self._current = -1
        return self.advance()

    def advance(self) -> QueueItem | None:
        """Перейти к следующей готовой песне.

        Пункты без текста (не нашлось, ошибка загрузки) пропускаются: посреди
        выступления показывать пустой экран бессмысленно.
        """
        index = self._current + 1
        while index < len(self._items):
            if self._items[index].is_ready:
                self._current = index
                return self._items[index]
            index += 1

        self._current = len(self._items)  # очередь пройдена
        return None

    @property
    def is_finished(self) -> bool:
        return self._current >= len(self._items)

    @property
    def has_ready_songs(self) -> bool:
        """Есть ли вообще что исполнять."""
        return any(item.is_ready for item in self._items)
