"""Получение текста с аккордами: абстракция источника и конкретные реализации.

Источник легко заменить: достаточно реализовать :class:`LyricsProvider`.
Готовые провайдеры:

* :class:`UltimateGuitarProvider` — поиск по названию и загрузка подбора;
* :class:`MyChordsProvider`, :class:`AmDmProvider` — загрузка по прямой ссылке;
* :class:`FileProvider` — файлы ``.txt`` / ``.pro`` / ``.cho``;
* :class:`CachingProvider` — обёртка, которая не ходит в сеть дважды;
* :class:`CompositeProvider` — цепочка источников.

О robots.txt. Гейт :class:`RobotsGate` спрашивает разрешения у сайта перед
автоматическим запросом. Он намеренно построен на ``requests``, а не на голом
``RobotFileParser.read()``: тот ходит через ``urllib`` с дефолтным User-Agent,
на который часть сайтов отвечает 403, а получив 403, парсер запрещает вообще всё —
включая страницы, которые сайт на самом деле открывает.
"""

from __future__ import annotations

import json
import re
import time
import urllib.robotparser
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from rapidfuzz import fuzz

from . import __version__
from .matcher import normalize_text
from .models import ChordMark, Song, SongLine, SongVersion
from .parser import (
    extract_ultimate_guitar_store,
    parse_chord_sheet,
    parse_section_header,
    parse_song_text,
    ultimate_guitar_to_chord_sheet,
)

# Только ASCII: HTTP-заголовки кодируются latin-1, и кириллица в User-Agent
# роняет любой запрос ещё до отправки.
USER_AGENT = f"SongPrompter/{__version__} (personal teleprompter for musicians)"
REQUEST_TIMEOUT = 20
DEFAULT_CRAWL_DELAY = 1.0

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "cache"


class ProviderError(Exception):
    """Источник не смог отдать текст: сеть, разметка или запрет сайта."""


@dataclass(frozen=True)
class SearchResult:
    """Найденный вариант песни — то, из чего пользователь выбирает."""

    title: str
    artist: str
    url: str
    source: str

    @property
    def display_name(self) -> str:
        """Исполнитель и название одной строкой."""
        return f"{self.artist} — {self.title}" if self.artist else self.title


# --- Соблюдение robots.txt --------------------------------------------------


class RobotsGate:
    """Проверка, разрешает ли сайт автоматический запрос к странице."""

    def __init__(self, user_agent: str = USER_AGENT, enabled: bool = True) -> None:
        self.user_agent = user_agent
        self.enabled = enabled
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_request: dict[str, float] = {}

    def can_fetch(self, url: str, user_initiated: bool = False) -> bool:
        """Можно ли запрашивать ``url``.

        ``user_initiated`` означает, что пользователь сам вставил эту ссылку.
        Такой запрос разрешаем даже когда robots.txt недоступен: человек
        открывает конкретную страницу, а не обходит сайт.
        """
        if not self.enabled:
            return True

        parser = self._parser_for(url)
        if parser is None:
            return user_initiated
        return parser.can_fetch(self.user_agent, url)

    def _parser_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        """Получить разобранный robots.txt для домена (с кэшем)."""
        root = "{0.scheme}://{0.netloc}".format(urlparse(url))
        if root in self._parsers:
            return self._parsers[root]

        parser: urllib.robotparser.RobotFileParser | None = None
        try:
            response = requests.get(
                f"{root}/robots.txt",
                headers={"User-Agent": self.user_agent},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 404:
                # Нет robots.txt — значит ограничений нет
                parser = urllib.robotparser.RobotFileParser()
                parser.parse([])
            elif response.ok:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(response.text.splitlines())
        except requests.RequestException:
            parser = None

        self._parsers[root] = parser
        return parser

    def wait_for_crawl_delay(self, url: str) -> None:
        """Выдержать паузу между запросами к одному хосту."""
        host = urlparse(url).netloc
        delay = DEFAULT_CRAWL_DELAY

        parser = self._parsers.get("{0.scheme}://{0.netloc}".format(urlparse(url)))
        if parser is not None:
            declared = parser.crawl_delay(self.user_agent)
            if declared:
                delay = float(declared)

        elapsed = time.monotonic() - self._last_request.get(host, 0.0)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request[host] = time.monotonic()


# --- Базовый провайдер ------------------------------------------------------


class LyricsProvider(ABC):
    """Источник текста с аккордами."""

    name: str = "источник"

    @abstractmethod
    def search(self, title: str, artist: str = "") -> Song | None:
        """Найти песню по названию и исполнителю. ``None``, если не нашлось."""

    def search_options(self, query: str) -> list[SearchResult]:
        """Найти несколько вариантов по свободному запросу.

        Возвращает список для выбора: одноимённых песен, каверов и версий
        обычно несколько, и угадывать за пользователя не стоит.
        """
        return []

    def supports_url(self, url: str) -> bool:
        """Умеет ли провайдер разбирать страницу по этой ссылке."""
        return False

    def fetch_url(self, url: str) -> Song | None:
        """Загрузить песню по прямой ссылке."""
        raise ProviderError(f"{self.name} не умеет загружать по ссылке")


class HttpProvider(LyricsProvider):
    """Общая часть сетевых провайдеров: сессия, гейт robots.txt, вежливые паузы."""

    domains: tuple[str, ...] = ()

    def __init__(self, gate: RobotsGate | None = None) -> None:
        self.gate = gate or RobotsGate()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            }
        )

    def supports_url(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return any(host == domain or host.endswith("." + domain) for domain in self.domains)

    def _get(
        self,
        url: str,
        params: dict | None = None,
        user_initiated: bool = False,
        allow_404: bool = False,
        headers: dict | None = None,
    ) -> requests.Response:
        """Запросить страницу, уважая robots.txt и паузы между запросами."""
        if not self.gate.can_fetch(url, user_initiated=user_initiated):
            raise ProviderError(
                f"{urlparse(url).netloc} запрещает автоматические запросы к этой странице "
                "(robots.txt). Вставьте текст вручную или укажите прямую ссылку."
            )

        self.gate.wait_for_crawl_delay(url)
        try:
            response = self.session.get(
                url, params=params, headers=headers, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as error:
            raise ProviderError(f"Сеть недоступна: {error}") from error

        if response.status_code == 404 and allow_404:
            return response
        if not response.ok:
            raise ProviderError(f"{urlparse(url).netloc} ответил {response.status_code}")

        # requests ставит ISO-8859-1, если сервер не указал кодировку явно
        if "charset" not in (response.headers.get("Content-Type") or "").lower():
            response.encoding = response.apparent_encoding
        return response


# --- Разбор HTML-разметки с аккордами ---------------------------------------


def chord_sheet_from_preformatted(block: Tag) -> list[SongLine]:
    """Разобрать блок, где аккорды стоят отдельной строкой над словами.

    Так устроены классические подборы: внутри ``<pre>`` выравнивание уже задано
    пробелами, поэтому достаточно взять текст как есть.
    """
    return parse_chord_sheet(block.get_text())


def chord_sheet_from_inline(block: Tag, chord_classes: tuple[str, ...]) -> list[SongLine]:
    """Разобрать разметку, где аккорд — отдельный тег внутри строки текста.

    Позиция аккорда считается по длине уже накопленного текста строки —
    ровно как в ChordPro.
    """
    lines: list[SongLine] = []
    current_text: list[str] = []
    current_chords: list[ChordMark] = []

    def flush() -> None:
        text = "".join(current_text).rstrip()
        if text or current_chords:
            lines.append(SongLine(text=text, chords=list(current_chords)))
        elif lines and not lines[-1].is_blank:
            lines.append(SongLine())
        current_text.clear()
        current_chords.clear()

    def is_chord(node: Tag) -> bool:
        classes = node.get("class") or []
        return any(cls in chord_classes for cls in classes)

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                text = str(child)
                # Реальные переводы строк внутри текста тоже разделяют строки
                parts = text.split("\n")
                for index, part in enumerate(parts):
                    if index:
                        flush()
                    current_text.append(part)
                continue

            if not isinstance(child, Tag):
                continue

            if child.name == "br":
                flush()
                continue

            if is_chord(child):
                position = len("".join(current_text))
                name = child.get_text(strip=True)
                if name:
                    current_chords.append(ChordMark(name=name, position=position))
                continue

            if child.name in ("div", "p", "li", "tr"):
                flush()
                walk(child)
                flush()
                continue

            walk(child)

    walk(block)
    flush()
    return _promote_section_lines(
        [line for line in lines if not (line.is_blank and (not lines or lines[-1].is_blank))]
    )


def _promote_section_lines(lines: list[SongLine]) -> list[SongLine]:
    """Превратить строки-заголовки в настоящие заголовки блоков.

    Разметка сайта не отличает «Вступление:» от строки песни — и то, и другое
    приходит обычным текстом. Без этой проверки заголовок остаётся строкой,
    которую суфлёр честно ждёт спетой, и человеку приходится проговаривать
    слово «вступление», чтобы уйти дальше.

    Аккорды у такой строки сохраняются: на некоторых сайтах их пишут
    прямо рядом с названием блока.
    """
    for line in lines:
        if not line.has_text:
            continue
        заголовок = parse_section_header(line.text)
        if заголовок is not None:
            line.section = заголовок
            line.text = ""
    return lines


# --- Ultimate Guitar --------------------------------------------------------


class UltimateGuitarProvider(HttpProvider):
    """Подборы с Ultimate Guitar.

    Данные страницы лежат в JSON внутри атрибута ``data-content`` у
    ``<div class="js-store">``. Текст подбора — по пути
    ``store.page.data.tab_view.wiki_tab.content``.
    """

    name = "Ultimate Guitar"
    domains = ("ultimate-guitar.com", "tabs.ultimate-guitar.com")

    SEARCH_URL = "https://www.ultimate-guitar.com/search.php"
    CHORDS_TYPE_ID = 300  # серверный фильтр «только аккорды»

    def search(self, title: str, artist: str = "") -> Song | None:
        query = f"{artist} {title}".strip() if artist else title.strip()
        if not query:
            return None

        # Пустая выдача отдаётся с кодом 404, но с корректным JSON внутри
        response = self._get(
            self.SEARCH_URL,
            params={"search_type": "title", "value": query, "type": self.CHORDS_TYPE_ID},
            allow_404=True,
        )
        store = extract_ultimate_guitar_store(response.text)
        if not store:
            return None

        data = self._page_data(store)
        results = data.get("results") or []
        best = self._pick_best(results, title, artist)
        if best is None:
            return None

        return self.fetch_url(best["tab_url"])

    def fetch_url(self, url: str) -> Song | None:
        response = self._get(url, user_initiated=True)
        store = extract_ultimate_guitar_store(response.text)
        if not store:
            raise ProviderError("На странице не нашлось данных подбора")

        data = self._page_data(store)
        tab_view = data.get("tab_view") or {}
        content = (tab_view.get("wiki_tab") or {}).get("content")
        if not content:
            # Так выглядят страницы Official/Pro — там подбора просто нет
            raise ProviderError("На этой странице нет текстового подбора")

        tab = data.get("tab") or {}
        song = parse_song_text(
            ultimate_guitar_to_chord_sheet(content),
            title=tab.get("song_name", ""),
            artist=tab.get("artist_name", ""),
            source=self.name,
            source_url=url,
        )
        capo = (tab_view.get("meta") or {}).get("capo")
        if isinstance(capo, int):
            song.capo = capo
        return song

    @staticmethod
    def _page_data(store: dict) -> dict:
        return ((store.get("store") or {}).get("page") or {}).get("data") or {}

    @staticmethod
    def _pick_best(results: list[dict], title: str, artist: str) -> dict | None:
        """Выбрать лучший подбор: совпадение по исполнителю, потом рейтинг."""
        wanted_artist = artist.strip().casefold()

        def usable(item: dict) -> bool:
            # У Official/Pro-записей тип пустой, а ссылка ведёт на страницу без подбора
            return item.get("type") == "Chords" and bool(item.get("tab_url"))

        candidates = [item for item in results if usable(item)]
        if not candidates:
            return None

        def score(item: dict) -> tuple[int, float, int]:
            same_artist = int(
                bool(wanted_artist)
                and wanted_artist in str(item.get("artist_name", "")).casefold()
            )
            return same_artist, float(item.get("rating") or 0), int(item.get("votes") or 0)

        return max(candidates, key=score)


# --- Сайты с классической разметкой подбора ---------------------------------


class MyChordsProvider(HttpProvider):
    """Подборы с mychords.net.

    Поиск идёт через тот же эндпоинт подсказок, которым пользуется сама
    страница сайта: обычная выдача рисуется скриптом и по GET-запросу не
    приходит, а подсказки отдают готовый JSON со ссылками на подборы.
    """

    name = "MyChords"
    domains = ("mychords.net",)

    BASE_URL = "https://mychords.net"
    SEARCH_URL = "https://mychords.net/ru/ajax/autocomplete"
    SONG_GROUP = "Песни"

    CONTAINER_SELECTORS = ("div.w-words__text", "div.b-words__text-background")
    CHORD_CLASSES = ("b-accord__symbol",)

    def search(self, title: str, artist: str = "") -> Song | None:
        query = f"{artist} {title}".strip() if artist else title.strip()
        if not query:
            return None

        response = self._get(
            self.SEARCH_URL,
            params={"q": query},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{self.BASE_URL}/ru/search"},
        )
        try:
            suggestions = response.json().get("suggestions") or []
        except ValueError:
            raise ProviderError("Поиск вернул неожиданный ответ")

        best = self._pick_best(suggestions, title, artist)
        if best is None:
            return None

        url = urljoin(self.BASE_URL, best["data"]["url"])
        return self.fetch_url(url)

    def search_options(self, query: str) -> list[SearchResult]:
        """Список вариантов по свободному запросу — из подсказок сайта."""
        query = query.strip()
        if not query:
            return []

        response = self._get(
            self.SEARCH_URL,
            params={"q": query},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{self.BASE_URL}/ru/search"},
        )
        try:
            suggestions = response.json().get("suggestions") or []
        except ValueError:
            return []

        варианты: list[SearchResult] = []
        for item in suggestions:
            data = item.get("data") or {}
            if data.get("group") != self.SONG_GROUP or not data.get("url"):
                continue
            исполнитель, _, название = str(item.get("value", "")).partition(" - ")
            варианты.append(
                SearchResult(
                    title=(название or исполнитель).strip(),
                    artist=(исполнитель if название else "").strip(),
                    url=urljoin(self.BASE_URL, data["url"]),
                    source=self.name,
                )
            )
        return варианты

    @classmethod
    def _pick_best(cls, suggestions: list[dict], title: str, artist: str) -> dict | None:
        """Выбрать подсказку, лучше всего совпадающую с запросом.

        Подсказка выглядит как «Исполнитель - Название», поэтому совпадение
        исполнителя весит больше: иначе легко уехать в пародию или кавер.
        """
        нужное_название = normalize_text(title)
        нужный_исполнитель = normalize_text(artist)

        подходящие = [
            item
            for item in suggestions
            if (item.get("data") or {}).get("group") == cls.SONG_GROUP
            and (item.get("data") or {}).get("url")
        ]
        if not подходящие:
            return None

        def оценка(item: dict) -> tuple[float, float]:
            значение = str(item.get("value", ""))
            исполнитель, _, название = значение.partition(" - ")
            совпадение_названия = fuzz.ratio(normalize_text(название), нужное_название)
            совпадение_исполнителя = (
                fuzz.ratio(normalize_text(исполнитель), нужный_исполнитель)
                if нужный_исполнитель
                else 0.0
            )
            return совпадение_исполнителя, совпадение_названия

        лучшая = max(подходящие, key=оценка)
        # Совсем непохожий результат лучше не подсовывать молча
        return лучшая if оценка(лучшая)[1] >= 55 else None

    def fetch_url(self, url: str) -> Song | None:
        response = self._get(url, user_initiated=True)
        soup = BeautifulSoup(response.text, "lxml")

        block = self._find_container(soup)
        if block is None:
            raise ProviderError("На странице не нашёлся блок с аккордами")

        lines = self._extract_lines(block)
        if not any(line.has_text for line in lines):
            raise ProviderError("Блок с аккордами оказался пустым")

        title, artist = self._extract_title_artist(soup)
        song = Song(title=title or "Без названия", artist=artist, source=self.name, source_url=url)
        song.lines = lines
        return song

    ROW_CLASSES = ("pline", "single-line")

    def _find_container(self, soup: BeautifulSoup) -> Tag | None:
        for selector in self.CONTAINER_SELECTORS:
            block = soup.select_one(selector)
            if block is not None:
                return block
        return None

    def _extract_lines(self, block: Tag) -> list[SongLine]:
        """Собрать строки, беря только строфы с аккордами.

        Тонкость страницы: у неё есть кнопка «скрыть аккорды», и ради неё сайт
        держит в разметке сразу две копии песни — строфы ``div.pline`` вместе с
        аккордами и отдельно голый текст прямыми потомками контейнера. Если
        разбирать контейнер целиком, каждая строка задваивается (в одном подборе
        строка повторялась до двенадцати раз), и автопрокрутка сходит с ума.
        """
        rows = [
            child
            for child in block.find_all(recursive=False)
            if child.name == "div" and set(child.get("class") or []) & set(self.ROW_CLASSES)
        ]
        if not rows:
            # Разметка изменилась — разбираем как есть, лучше так, чем никак
            return chord_sheet_from_inline(block, self.CHORD_CLASSES)

        lines: list[SongLine] = []
        for row in rows:
            lines.extend(chord_sheet_from_inline(row, self.CHORD_CLASSES))
        return lines

    @staticmethod
    def _extract_title_artist(soup: BeautifulSoup) -> tuple[str, str]:
        heading = soup.select_one("h1.b-title--song, h1.b-title, h1")
        if heading is None:
            return "", ""
        text = re.sub(r"\s+", " ", heading.get_text(strip=True))
        text = re.sub(r"\s*[-–—]\s*аккорды.*$", "", text, flags=re.IGNORECASE)
        if "-" in text:
            artist, _, title = text.partition("-")
            return title.strip(), artist.strip()
        return text, ""


class AmDmProvider(HttpProvider):
    """Подборы с amdm.ru (загрузка по прямой ссылке).

    Автопоиск отключён намеренно: сайт закрывает ``/search`` в robots.txt для
    всех роботов. Прямые ссылки на страницы подборов не запрещены, поэтому
    пользователь вставляет ссылку сам.

    Важная особенность, проверенная на сохранённой странице: контейнер
    ``div.b-podbor__text pre`` действительно содержит подбор, но обычному
    HTTP-запросу сайт отдаёт **только слова, без аккордовой сетки** — аккорды
    подставляются уже в браузере. Поэтому песня отсюда приходит с текстом, но
    почти наверняка без аккордов, и провайдер честно помечает это в источнике,
    чтобы на экране не выглядело поломкой.
    """

    name = "AmDm"
    domains = ("amdm.ru", "amdm.me")

    CONTAINER_SELECTORS = (
        "pre.field-name-field-pesnya",
        "div.b-podbor__text pre",
        "div.b-podbor__text",
        "div.podbor__text",
        "pre",
    )

    SEARCH_URL = "https://amdm.ru/search/"

    def search(self, title: str, artist: str = "") -> Song | None:
        запрос = f"{artist} {title}".strip() if artist else title.strip()
        варианты = self.search_options(запрос)
        if not варианты:
            return None
        return self.fetch_url(варианты[0].url)

    def search_options(self, query: str) -> list[SearchResult]:
        """Поиск по сайту.

        Работает только с выключенной проверкой robots.txt: сайт закрывает
        ``/search`` в блоке ``User-agent: *``, то есть для любых автоматических
        клиентов, а не только для поисковых роботов. Решение отступить от этого
        принимает пользователь в настройках, поэтому здесь мы просто честно
        спрашиваем разрешения у гейта и не пытаемся его обойти.
        """
        query = query.strip()
        if not query:
            return []

        response = self._get(self.SEARCH_URL, params={"q": query})
        return self.parse_search_page(response.text)

    @classmethod
    def parse_search_page(cls, page_html: str) -> list[SearchResult]:
        """Разобрать страницу выдачи. Вынесено отдельно ради тестов.

        Каждый результат — строка таблицы, где в ячейке с названием лежат две
        ссылки: на исполнителя и на сам подбор. Нужна вторая — та, что с номером.
        """
        soup = BeautifulSoup(page_html, "lxml")
        таблица = soup.select_one("div.content-table, table.content-table")
        if таблица is None:
            return []

        результаты: list[SearchResult] = []
        for строка in таблица.select("tr"):
            ячейка = строка.select_one("td.artist_name")
            if ячейка is None:
                continue

            # В ячейке две ссылки: на страницу исполнителя и на сам подбор.
            # Берём подписи из них по отдельности — надёжнее, чем разбирать
            # общий текст ячейки, который держится на разделителе-тире
            ссылка_на_подбор = None
            ссылка_на_исполнителя = None
            for кандидат in ячейка.select("a[href]"):
                if cls._SONG_URL_RE.match(кандидат.get("href", "")):
                    ссылка_на_подбор = ссылка_на_подбор or кандидат
                elif ссылка_на_исполнителя is None:
                    ссылка_на_исполнителя = кандидат

            if ссылка_на_подбор is None:
                continue

            название = re.sub(r"\s+", " ", ссылка_на_подбор.get_text(strip=True))
            исполнитель = (
                re.sub(r"\s+", " ", ссылка_на_исполнителя.get_text(strip=True))
                if ссылка_на_исполнителя is not None
                else ""
            )
            результаты.append(
                SearchResult(
                    title=название or "Без названия",
                    artist=исполнитель,
                    url=urljoin("https://amdm.ru", ссылка_на_подбор["href"]),
                    source=cls.name,
                )
            )
        return результаты

    def fetch_url(self, url: str) -> Song | None:
        response = self._get(url, user_initiated=True)
        return self.parse_page(response.text, url)

    @classmethod
    def parse_page(cls, page_html: str, url: str = "") -> Song:
        """Разобрать HTML страницы подбора. Вынесено отдельно ради тестов."""
        soup = BeautifulSoup(page_html, "lxml")

        block = None
        for selector in cls.CONTAINER_SELECTORS:
            block = soup.select_one(selector)
            if block is not None and block.get_text(strip=True):
                break
        if block is None:
            raise ProviderError("На странице не нашёлся блок с аккордами")

        # Cloudflare прячет адреса почты в ссылку-заглушку, иначе она попадёт
        # в текст строкой вида «[email protected]»
        for заглушка in block.select("a.__cf_email__"):
            заглушка.decompose()

        # Аккорды здесь стоят отдельной строкой над словами, а внутри <pre>
        # выравнивание уже задано пробелами
        lines = chord_sheet_from_preformatted(block)
        if not lines:
            raise ProviderError("Блок с аккордами оказался пустым")

        heading = soup.select_one("h1")
        raw_title = re.sub(r"\s+", " ", heading.get_text(strip=True)) if heading else ""
        title_text, artist_text = cls._split_heading(raw_title)

        # На сайте есть страницы двух видов: аккордовый подбор и табулатура.
        # У второй аккордов может не быть вовсе — говорим об этом прямо,
        # иначе пустое место над словами выглядит как поломка суфлёра
        источник = cls.name
        if not any(line.has_chords for line in lines):
            есть_таб = any(line.has_tab for line in lines)
            источник = f"{cls.name} ({'табулатура' if есть_таб else 'только текст'}, без аккордов)"

        song = Song(
            title=title_text or "Без названия",
            artist=artist_text,
            source=источник,
            source_url=url,
            alternatives=cls._find_alternatives(soup, url),
        )
        song.lines = lines
        return song

    # Ссылка на подбор: /akkordi/<исполнитель>/<номер>/<песня>/
    _SONG_URL_RE = re.compile(r"^(?:https?://[^/]+)?/akkordi/([^/]+)/(\d+)/([^/]+)/?$")

    @classmethod
    def _find_alternatives(cls, soup: BeautifulSoup, url: str) -> list[SongVersion]:
        """Найти другие подборы этой же песни, перечисленные на странице.

        У популярных песен на сайте лежит по несколько разборов — например,
        один с табулатурой, другой с аккордовой сеткой. Страница ссылается на
        соседние версии, и грех этим не воспользоваться: если в текущем разборе
        нет аккордов, рядом может лежать тот, где они есть.
        """
        текущая = cls._SONG_URL_RE.match(url)
        if текущая is None:
            return []
        _, текущий_номер, песня = текущая.groups()

        версии: dict[str, SongVersion] = {}
        for ссылка in soup.select("a[href]"):
            совпадение = cls._SONG_URL_RE.match(ссылка.get("href", ""))
            if совпадение is None:
                continue
            исполнитель, номер, slug = совпадение.groups()
            if slug != песня or номер == текущий_номер:
                continue

            подпись = re.sub(r"\s+", " ", ссылка.get_text(strip=True))[:60]
            полный = urljoin("https://amdm.ru", ссылка["href"])
            версии.setdefault(
                номер, SongVersion(url=полный, label=подпись or f"Вариант {номер}")
            )

        return list(версии.values())

    @staticmethod
    def _split_heading(heading: str) -> tuple[str, str]:
        """Разобрать заголовок вида «Исполнитель-Название, аккорды».

        Хвост про аккорды сайт дописывает и через запятую, и через тире,
        поэтому срезаются оба варианта.
        """
        text = re.sub(r"\s*[,\-–—]\s*(аккорды|текст|табы).*$", "", heading, flags=re.IGNORECASE)
        # Разделяем по первому дефису: слева исполнитель, справа название
        match = re.match(r"^(.{2,60}?)\s*[-–—]\s*(.+)$", text)
        if match:
            return match.group(2).strip(), match.group(1).strip()
        return text.strip(), ""


# --- Локальные источники ----------------------------------------------------


class FileProvider(LyricsProvider):
    """Загрузка песни из файла ``.txt``, ``.pro``, ``.cho``, ``.crd``."""

    name = "файл"
    SUFFIXES = (".txt", ".pro", ".cho", ".chopro", ".crd", ".chordpro")

    def search(self, title: str, artist: str = "") -> Song | None:
        return None

    def load(self, path: Path, title: str = "", artist: str = "") -> Song:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Старые подборы из рунета часто лежат в windows-1251
            text = path.read_text(encoding="cp1251", errors="replace")
        except OSError as error:
            raise ProviderError(f"Не удалось прочитать файл: {error}") from error

        song = parse_song_text(
            text,
            title=title or path.stem,
            artist=artist,
            source=f"файл {path.name}",
        )
        if not song.singable_indexes:
            raise ProviderError("В файле не нашлось текста песни")
        return song


class ManualProvider(LyricsProvider):
    """Текст, который пользователь вставил руками."""

    name = "вручную"

    def search(self, title: str, artist: str = "") -> Song | None:
        return None

    def load_text(self, text: str, title: str = "", artist: str = "") -> Song:
        song = parse_song_text(text, title=title, artist=artist, source=self.name)
        if not song.singable_indexes:
            raise ProviderError("В тексте не нашлось строк со словами")
        return song


# --- Композиция и кэш -------------------------------------------------------


@dataclass
class CompositeProvider(LyricsProvider):
    """Цепочка источников: пробуем по очереди, берём первый удавшийся."""

    providers: list[LyricsProvider]
    name: str = "поиск"

    def search_options(self, query: str) -> list[SearchResult]:
        """Собрать варианты из всех источников, которые умеют искать."""
        варианты: list[SearchResult] = []
        for provider in self.providers:
            try:
                варианты.extend(provider.search_options(query))
            except (ProviderError, Exception):
                continue  # один источник отвалился — остальные всё равно опросим
        return варианты

    def search(self, title: str, artist: str = "") -> Song | None:
        errors: list[str] = []
        for provider in self.providers:
            try:
                song = provider.search(title, artist)
            except ProviderError as error:
                errors.append(f"{provider.name}: {error}")
                continue
            except Exception as error:  # разметка сайта могла поменяться
                errors.append(f"{provider.name}: неожиданная ошибка ({error})")
                continue
            if song is not None and song.singable_indexes:
                return song

        if errors:
            raise ProviderError("; ".join(errors))
        return None

    def supports_url(self, url: str) -> bool:
        return any(provider.supports_url(url) for provider in self.providers)

    def fetch_url(self, url: str) -> Song | None:
        for provider in self.providers:
            if provider.supports_url(url):
                return provider.fetch_url(url)
        raise ProviderError(
            "Не знаю, как разобрать эту ссылку. Поддерживаются: "
            + ", ".join(sorted({d for p in self.providers for d in getattr(p, "domains", ())}))
        )


class CachingProvider(LyricsProvider):
    """Обёртка, которая помнит уже найденные песни и не ходит в сеть дважды."""

    name = "кэш"

    def __init__(self, inner: LyricsProvider, cache_dir: Path = CACHE_DIR) -> None:
        self.inner = inner
        self.cache_dir = cache_dir

    def search(self, title: str, artist: str = "") -> Song | None:
        key = self._key(f"{artist}|{title}")
        cached = self._read(key)
        if cached is not None:
            return cached

        song = self.inner.search(title, artist)
        if song is not None:
            self._write(key, song)
        return song

    def supports_url(self, url: str) -> bool:
        return self.inner.supports_url(url)

    def search_options(self, query: str) -> list[SearchResult]:
        # Список вариантов не кэшируем: он дешёвый и должен быть свежим
        return self.inner.search_options(query)

    def fetch_url(self, url: str) -> Song | None:
        key = self._key(url)
        cached = self._read(key)
        if cached is not None:
            return cached

        song = self.inner.fetch_url(url)
        if song is not None:
            self._write(key, song)
        return song

    @staticmethod
    def _key(value: str) -> str:
        normalized = re.sub(r"\s+", " ", value.strip().casefold())
        return quote(normalized, safe="")[:150] or "empty"

    def _read(self, key: str) -> Song | None:
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            return Song.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            return None  # битый кэш просто игнорируем

    def _write(self, key: str, song: Song) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path = self.cache_dir / f"{key}.json"
            path.write_text(
                json.dumps(song.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError:
            pass  # не смогли закэшировать — не повод падать


def build_default_provider(respect_robots: bool = True) -> CachingProvider:
    """Собрать источник по умолчанию: сеть с кэшем и уважением к robots.txt."""
    gate = RobotsGate(enabled=respect_robots)
    return CachingProvider(
        CompositeProvider(
            providers=[
                UltimateGuitarProvider(gate),
                MyChordsProvider(gate),
                AmDmProvider(gate),
            ]
        )
    )
