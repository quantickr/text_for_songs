"""Модель данных: песня, строка песни, аккорд, привязанный к позиции в тексте.

Ключевое решение: аккорд хранится не как «строка символов над текстом», а как имя
плюс позиция символа в строке текста. Это позволяет рисовать аккорды над нужными
словами любым шрифтом (включая пропорциональный), а не только моноширинным.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Доля кириллицы, начиная с которой считаем текст русским
_CYRILLIC_SHARE_FOR_RU = 0.15

_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


@dataclass(frozen=True)
class ChordMark:
    """Аккорд, привязанный к позиции в строке текста.

    Атрибуты:
        name: обозначение аккорда, например ``Am`` или ``F#m7/C#``.
        position: индекс символа в ``SongLine.text``, над которым стоит аккорд.
            Может превышать длину текста — так бывает у аккордов в конце строки
            и у строк, где аккорды идут вообще без слов.
    """

    name: str
    position: int


@dataclass
class SongLine:
    """Одна строка песни: текст плюс аккорды над ним.

    Строка может быть только текстовой (аккордов нет), только аккордовой
    (проигрыш — текста нет) или пустой (разделитель между блоками).
    """

    text: str = ""
    chords: list[ChordMark] = field(default_factory=list)
    section: str | None = None  # заголовок блока: «Припев», «Verse 1» и т.п.
    tab_lines: list[str] = field(default_factory=list)
    """Блок табулатуры (перебор по струнам). Хранится целиком, как есть.

    Спеть табулатуру нельзя, поэтому в поток строк она не встраивается —
    интерфейс показывает её сбоку, моноширинным шрифтом.
    """

    @property
    def has_text(self) -> bool:
        """Есть ли в строке хоть что-то, что можно спеть."""
        return bool(self.text.strip())

    @property
    def has_chords(self) -> bool:
        return bool(self.chords)

    @property
    def has_tab(self) -> bool:
        return bool(self.tab_lines)

    @property
    def is_blank(self) -> bool:
        """Полностью пустая строка — ни текста, ни аккордов, ни заголовка."""
        return not self.has_text and not self.has_chords and not self.section and not self.has_tab

    def chord_line(self) -> str:
        """Восстановить классическую строку аккордов, выровненную по тексту.

        Нужна для экспорта и отладки; интерфейс рисует аккорды по позициям.
        """
        result = ""
        for chord in sorted(self.chords, key=lambda c: c.position):
            if len(result) > chord.position:
                # Аккорды налезают друг на друга — раздвигаем минимум одним пробелом
                result += " "
            else:
                result += " " * (chord.position - len(result))
            result += chord.name
        return result

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "chords": [{"name": c.name, "position": c.position} for c in self.chords],
            "section": self.section,
            "tab_lines": self.tab_lines,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SongLine:
        return cls(
            text=data.get("text", ""),
            chords=[
                ChordMark(name=c["name"], position=int(c["position"]))
                for c in data.get("chords", [])
            ],
            section=data.get("section"),
            tab_lines=list(data.get("tab_lines", [])),
        )


@dataclass(frozen=True)
class SongVersion:
    """Ссылка на другой подбор той же песни.

    У популярных песен на сайтах лежит по несколько разборов: с табулатурой и
    без, в разных тональностях, упрощённые. Держим их под рукой, чтобы можно
    было переключиться, не начиная поиск заново.
    """

    url: str
    label: str = ""

    @property
    def display_name(self) -> str:
        return self.label or self.url


@dataclass
class Song:
    """Песня целиком: метаданные и список строк."""

    title: str
    artist: str = ""
    lines: list[SongLine] = field(default_factory=list)
    source: str = ""  # откуда взят текст: имя провайдера или «вручную»
    source_url: str = ""
    capo: int | None = None
    alternatives: list[SongVersion] = field(default_factory=list)
    """Другие подборы этой же песни, найденные на странице источника."""

    @property
    def display_name(self) -> str:
        return f"{self.artist} — {self.title}" if self.artist else self.title

    @property
    def singable_indexes(self) -> list[int]:
        """Индексы строк, в которых есть текст (по ним идёт голосовая прокрутка)."""
        return [i for i, line in enumerate(self.lines) if line.has_text]

    @property
    def navigable_indexes(self) -> list[int]:
        """Индексы строк, по которым идёт листание.

        Кроме строк со словами сюда попадают заголовки блоков и проигрыши:
        на них суфлёр останавливается, но спеть их нельзя, поэтому уходит
        дальше по таймеру, а не по голосу.
        """
        return [
            i
            for i, line in enumerate(self.lines)
            if line.has_text or line.section or line.has_chords
        ]

    @property
    def has_tabs(self) -> bool:
        return any(line.has_tab for line in self.lines)

    @property
    def has_chords(self) -> bool:
        return any(line.has_chords for line in self.lines)

    def tab_for_line(self, index: int) -> list[str]:
        """Табулатура, относящаяся к текущему месту песни.

        Берём ближайший блок выше по тексту: схема перебора обычно стоит перед
        куплетом, к которому относится. Если выше ничего нет — показываем
        первый блок ниже, чтобы панель не пустовала в начале песни.
        """
        предыдущая: list[str] = []
        for position, line in enumerate(self.lines):
            if line.has_tab and position <= index:
                предыдущая = line.tab_lines
        if предыдущая:
            return предыдущая

        for line in self.lines[index:]:
            if line.has_tab:
                return line.tab_lines
        return []

    def vocabulary(self) -> list[str]:
        """Все слова песни — словарь для распознавателя.

        Зная заранее, что человек собирается петь, глупо заставлять
        распознаватель выбирать из десятков тысяч слов языка.

        Буква «ё» здесь сохраняется, хотя при сравнении она сводится к «е»:
        словарь модели содержит слова именно с «ё», и подменённые формы она
        молча выбрасывает — то есть половина словаря песни пропадала бы зря.
        """
        from .matcher import tokenize

        слова: set[str] = set()
        for line in self.lines:
            слова.update(tokenize(line.text, fold_yo=False))

        # Однобуквенные огрызки и служебные пометки вроде «x2» словарю только
        # мешают: модель их всё равно не знает, а место в грамматике занимают
        return sorted(
            с for с in слова
            if len(с) > 1 and not any(ch.isdigit() for ch in с)
        )

    def plain_text(self) -> str:
        """Весь текст песни без аккордов — для определения языка и отладки."""
        return "\n".join(line.text for line in self.lines if line.has_text)

    def detect_language(self) -> str:
        """Определить язык песни по алфавиту: ``ru`` или ``en``.

        Считаем долю кириллических символов среди букв. Порог низкий (15 %),
        потому что в русских песнях часто попадаются английские вставки,
        а вот обратное встречается заметно реже.
        """
        text = self.plain_text()
        letters = _LETTER_RE.findall(text)
        if not letters:
            return "ru"
        cyrillic = len(_CYRILLIC_RE.findall(text))
        return "ru" if cyrillic / len(letters) >= _CYRILLIC_SHARE_FOR_RU else "en"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "artist": self.artist,
            "source": self.source,
            "source_url": self.source_url,
            "capo": self.capo,
            "lines": [line.to_dict() for line in self.lines],
            "alternatives": [
                {"url": v.url, "label": v.label} for v in self.alternatives
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Song:
        return cls(
            title=data.get("title", ""),
            artist=data.get("artist", ""),
            source=data.get("source", ""),
            source_url=data.get("source_url", ""),
            capo=data.get("capo"),
            lines=[SongLine.from_dict(item) for item in data.get("lines", [])],
            alternatives=[
                SongVersion(url=v["url"], label=v.get("label", ""))
                for v in data.get("alternatives", [])
            ],
        )


@dataclass
class QueueItem:
    """Пункт очереди: что играем и загружен ли уже текст.

    Песню в очередь можно добавить до того, как найден её текст, — поиск идёт
    в фоне, а пользователь тем временем набивает остальные пункты.
    """

    title: str
    artist: str = ""
    song: Song | None = None
    error: str | None = None

    @property
    def display_name(self) -> str:
        return f"{self.artist} — {self.title}" if self.artist else self.title

    @property
    def is_ready(self) -> bool:
        return self.song is not None and bool(self.song.singable_indexes)
