"""Тесты источников текста: разбор разметки и кэш. Сеть не используется.

Вся разметка в тестах выдумана и повторяет только структуру страниц,
а не их содержимое.
"""

import json

import pytest
from bs4 import BeautifulSoup

from prompter.lyrics_provider import (
    USER_AGENT,
    AmDmProvider,
    CachingProvider,
    FileProvider,
    LyricsProvider,
    ManualProvider,
    MyChordsProvider,
    ProviderError,
    RobotsGate,
    UltimateGuitarProvider,
    chord_sheet_from_inline,
    chord_sheet_from_preformatted,
)
from prompter.models import Song, SongLine
from prompter.parser import extract_ultimate_guitar_store


class TestРазборРазметки:
    def test_блок_pre_разбирается_как_классический_подбор(self):
        html = "<pre><b>Am</b>        <b>C</b>\nпервая строка выдуманного текста</pre>"
        block = BeautifulSoup(html, "lxml").find("pre")

        lines = chord_sheet_from_preformatted(block)

        assert lines[0].text == "первая строка выдуманного текста"
        assert [c.name for c in lines[0].chords] == ["Am", "C"]

    def test_инлайновая_разметка_даёт_позиции_аккордов(self):
        # Так устроены сайты, где аккорд — отдельный тег внутри строки
        html = (
            '<div class="w-words__text">'
            '<div class="pline"><span class="b-accord__symbol">Am</span>'
            "<span>первая </span>"
            '<span class="b-accord__symbol">C</span><span>строка</span></div>'
            '<div class="pline"><span>вторая строка</span></div>'
            "</div>"
        )
        block = BeautifulSoup(html, "lxml").select_one("div.w-words__text")

        lines = chord_sheet_from_inline(block, ("b-accord__symbol",))

        assert lines[0].text == "первая строка"
        assert [(c.name, c.position) for c in lines[0].chords] == [("Am", 0), ("C", 7)]
        assert lines[1].text == "вторая строка"

    def test_перенос_строки_внутри_блока(self):
        html = '<div class="x">первая строка<br/>вторая строка</div>'
        block = BeautifulSoup(html, "lxml").select_one("div.x")

        lines = chord_sheet_from_inline(block, ("chord",))

        assert [line.text for line in lines] == ["первая строка", "вторая строка"]


class TestUltimateGuitar:
    def test_json_достаётся_из_data_content(self):
        # На реальной странице кавычки экранированы HTML-сущностями
        payload = {"store": {"page": {"data": {"results": []}}}}
        raw = json.dumps(payload).replace('"', "&quot;")
        html = f'<div class="js-store" data-content="{raw}"></div>'

        store = extract_ultimate_guitar_store(html)

        assert store == payload

    def test_битый_json_не_роняет_разбор(self):
        html = '<div class="js-store" data-content="{не json"></div>'
        assert extract_ultimate_guitar_store(html) is None

    def test_выбирается_подбор_нужного_исполнителя(self):
        results = [
            {"type": "Chords", "artist_name": "Другие", "rating": 5.0, "votes": 900,
             "tab_url": "https://tabs.ultimate-guitar.com/tab/a"},
            {"type": "Chords", "artist_name": "Нужный", "rating": 4.0, "votes": 10,
             "tab_url": "https://tabs.ultimate-guitar.com/tab/b"},
        ]

        best = UltimateGuitarProvider._pick_best(results, "Песня", "Нужный")

        assert best is not None
        assert best["tab_url"].endswith("/b")

    def test_официальные_записи_отбрасываются(self):
        # У Pro/Official записей тип пустой, а на странице нет текстового подбора
        results = [
            {"type": None, "marketing_type": "official",
             "tab_url": "https://www.ultimate-guitar.com/pro/?id=1"},
        ]

        assert UltimateGuitarProvider._pick_best(results, "Песня", "") is None

    def test_при_пустой_выдаче_возвращается_none(self):
        assert UltimateGuitarProvider._pick_best([], "Песня", "") is None


class TestMyChords:
    def test_двойная_копия_текста_не_задваивает_строки(self):
        # Ради кнопки «скрыть аккорды» сайт держит в разметке две копии песни:
        # строфы с аккордами и отдельно голый текст прямыми потомками контейнера
        html = (
            '<div class="w-words__text">'
            '<div class="pline"><div class="c-subline">'
            '<span class="b-accord__symbol">Am</span><span class="subline">первая строка</span>'
            "</div></div>"
            '<div class="single-line"><span class="subline">вторая строка</span></div>'
            '<span class="subline">первая строка</span>'
            '<span class="subline">вторая строка</span>'
            "</div>"
        )
        block = BeautifulSoup(html, "lxml").select_one("div.w-words__text")

        lines = MyChordsProvider()._extract_lines(block)
        тексты = [line.text for line in lines if line.has_text]

        assert тексты == ["первая строка", "вторая строка"]

    def test_при_смене_вёрстки_разбирается_блок_целиком(self):
        # Если знакомых строф нет, лучше разобрать как получится, чем ничего
        html = '<div class="w-words__text"><span class="subline">одинокая строка</span></div>'
        block = BeautifulSoup(html, "lxml").select_one("div.w-words__text")

        lines = MyChordsProvider()._extract_lines(block)

        assert [line.text for line in lines if line.has_text] == ["одинокая строка"]

    def test_выбирается_подсказка_нужного_исполнителя(self):
        suggestions = [
            {"value": "Пародии - Выдуманная песня", "data": {"group": "Песни", "url": "/ru/a/1.html"}},
            {"value": "Нужный - Выдуманная песня", "data": {"group": "Песни", "url": "/ru/b/2.html"}},
        ]

        best = MyChordsProvider._pick_best(suggestions, "Выдуманная песня", "Нужный")

        assert best is not None
        assert best["data"]["url"] == "/ru/b/2.html"

    def test_непохожий_результат_отбрасывается(self):
        suggestions = [
            {"value": "Кто-то - Совсем другое название",
             "data": {"group": "Песни", "url": "/ru/c/3.html"}},
        ]

        assert MyChordsProvider._pick_best(suggestions, "Выдуманная песня", "") is None

    def test_подсказки_не_про_песни_игнорируются(self):
        suggestions = [
            {"value": "Выдуманная песня", "data": {"group": "Исполнители", "url": "/ru/d/4.html"}},
        ]

        assert MyChordsProvider._pick_best(suggestions, "Выдуманная песня", "") is None


class TestAmDm:
    """Разметка проверена на реально сохранённой странице подбора."""

    ШАБЛОН = (
        '<h1>Выдуманная группа - Проба пера (аккорды)</h1>'
        '<div class="b-podbor"><div class="b-podbor__text">'
        '<pre class="field__podbor_new podbor__text">{}</pre>'
        "</div></div>"
    )

    def test_подбор_с_аккордами_разбирается(self):
        html = self.ШАБЛОН.format("Am        C\nпервая выдуманная строка")

        song = AmDmProvider.parse_page(html, "https://amdm.ru/akkordi/x/1/y/")

        assert song.lines[0].text == "первая выдуманная строка"
        assert [c.name for c in song.lines[0].chords] == ["Am", "C"]
        assert song.source == "AmDm"

    def test_страница_без_аккордов_помечается(self):
        # Обычному запросу сайт отдаёт только слова: аккорды подставляет
        # уже браузер. Молча вернуть текст без аккордов — выглядело бы поломкой
        html = self.ШАБЛОН.format("первая выдуманная строка\nвторая выдуманная строка")

        song = AmDmProvider.parse_page(html, "https://amdm.ru/akkordi/x/1/y/")

        assert len(song.singable_indexes) == 2
        assert "без аккордов" in song.source

    def test_заглушка_cloudflare_не_попадает_в_текст(self):
        html = self.ШАБЛОН.format(
            'первая выдуманная строка\n<a href="/cdn-cgi/l/email-protection" '
            'class="__cf_email__" data-cfemail="0d79">[email&#160;protected]</a>'
        )

        song = AmDmProvider.parse_page(html, "")
        весь_текст = " ".join(line.text for line in song.lines)

        assert "protected" not in весь_текст

    def test_пустая_страница_даёт_понятную_ошибку(self):
        with pytest.raises(ProviderError):
            AmDmProvider.parse_page("<html><body>ничего нет</body></html>", "")

    ВЫДАЧА = """
        <div class="content-table">
          <tr class="top_label"><td></td><td>Исполнитель</td></tr>
          <tr>
            <td class="i">1.</td>
            <td class="photo"><a class="photo" href="/akkordi/gruppa/"></a></td>
            <td class="artist_name">
              <a class="artist" href="/akkordi/gruppa/">Выдуманная группа</a>
              <a class="artist" href="/akkordi/gruppa/111/proba/">Проба пера</a>
            </td>
          </tr>
          <tr>
            <td class="i">2.</td>
            <td class="photo"><a class="photo" href="/akkordi/gruppa/"></a></td>
            <td class="artist_name">
              <a class="artist" href="/akkordi/gruppa/">Выдуманная группа</a>
              <a class="artist" href="/akkordi/gruppa/222/proba/">Проба пера</a>
            </td>
          </tr>
        </div>
    """

    def test_выдача_поиска_разбирается(self):
        # Обе строки — разные подборы одной песни, ровно то, ради чего поиск и нужен
        результаты = AmDmProvider.parse_search_page(self.ВЫДАЧА)

        assert len(результаты) == 2
        assert результаты[0].url.endswith("/111/proba/")
        assert результаты[1].url.endswith("/222/proba/")
        assert результаты[0].artist == "Выдуманная группа"
        assert результаты[0].title == "Проба пера"

    def test_ссылка_на_исполнителя_не_считается_результатом(self):
        # В ячейке две ссылки: на исполнителя и на подбор. Нужна вторая
        результаты = AmDmProvider.parse_search_page(self.ВЫДАЧА)

        assert all("/111/" in r.url or "/222/" in r.url for r in результаты)

    def test_пустая_выдача_не_ломает_разбор(self):
        assert AmDmProvider.parse_search_page("<html><body>пусто</body></html>") == []

    def test_поиск_блокируется_гейтом_по_умолчанию(self):
        # Сайт закрывает /search для любых автоматических клиентов,
        # и обходить это молча программа не должна
        провайдер = AmDmProvider(RobotsGate(enabled=True))

        assert not провайдер.gate.can_fetch("https://amdm.ru/search/?q=проба")

    def test_страница_подбора_гейтом_не_блокируется(self):
        провайдер = AmDmProvider(RobotsGate(enabled=True))

        assert провайдер.gate.can_fetch(
            "https://amdm.ru/akkordi/gruppa/111/proba/", user_initiated=True
        )

    def test_другие_версии_подбора_находятся(self):
        # У популярных песен на сайте лежит несколько разборов: с табулатурой
        # и с аккордами. Страница ссылается на соседние — их и подхватываем
        html = self.ШАБЛОН.format("первая выдуманная строка") + (
            '<a href="/akkordi/artist/166677/proba/">Проба</a>'
            '<a href="/akkordi/artist/87252/proba/">Эта же страница</a>'
            '<a href="/akkordi/artist/999/drugaya_pesnya/">Другая песня</a>'
        )

        song = AmDmProvider.parse_page(html, "https://amdm.ru/akkordi/artist/87252/proba/")

        assert len(song.alternatives) == 1
        assert song.alternatives[0].url.endswith("/166677/proba/")

    def test_ссылка_на_саму_себя_не_считается_версией(self):
        html = self.ШАБЛОН.format("первая выдуманная строка") + (
            '<a href="/akkordi/artist/87252/proba/">Эта же страница</a>'
        )

        song = AmDmProvider.parse_page(html, "https://amdm.ru/akkordi/artist/87252/proba/")

        assert song.alternatives == []

    def test_версии_переживают_сериализацию(self):
        from prompter.models import Song, SongVersion

        песня = Song(title="Проба", alternatives=[SongVersion(url="https://x/1", label="Вариант")])

        восстановленная = Song.from_dict(песня.to_dict())

        assert восстановленная.alternatives[0].url == "https://x/1"
        assert восстановленная.alternatives[0].label == "Вариант"

    @pytest.mark.parametrize(
        "заголовок,название,исполнитель",
        [
            # Хвост про аккорды сайт дописывает и через запятую, и через тире
            ("Выдуманная группа-Проба пера, аккорды", "Проба пера", "Выдуманная группа"),
            ("Выдуманная группа - Проба пера", "Проба пера", "Выдуманная группа"),
            ("Проба пера, аккорды", "Проба пера", ""),
        ],
    )
    def test_заголовок_разбирается_на_название_и_исполнителя(
        self, заголовок, название, исполнитель
    ):
        assert AmDmProvider._split_heading(заголовок) == (название, исполнитель)


class TestЗаголовки:
    def test_user_agent_кодируется_в_latin1(self):
        # HTTP-заголовки кодируются latin-1: кириллица в User-Agent роняет
        # любой запрос ещё до отправки
        USER_AGENT.encode("latin-1")


class TestФайлы:
    def test_загрузка_txt(self, tmp_path):
        файл = tmp_path / "проба.txt"
        файл.write_text("Am   C\nвыдуманная строка текста", encoding="utf-8")

        song = FileProvider().load(файл)

        assert song.lines[0].text == "выдуманная строка текста"
        assert song.title == "проба"

    def test_файл_в_кодировке_cp1251(self, tmp_path):
        файл = tmp_path / "старый.txt"
        файл.write_bytes("Am\nвыдуманная строка текста".encode("cp1251"))

        song = FileProvider().load(файл)

        assert "выдуманная" in song.lines[0].text

    def test_пустой_файл_даёт_понятную_ошибку(self, tmp_path):
        файл = tmp_path / "пусто.txt"
        файл.write_text("Am  C  G\n", encoding="utf-8")  # одни аккорды, слов нет

        with pytest.raises(ProviderError):
            FileProvider().load(файл)

    def test_ручной_ввод_без_слов_отвергается(self):
        with pytest.raises(ProviderError):
            ManualProvider().load_text("Am C G")


class _ЗапоминающийИсточник(LyricsProvider):
    """Подставной источник: считает, сколько раз к нему обратились."""

    name = "тест"

    def __init__(self) -> None:
        self.calls = 0

    def search(self, title: str, artist: str = "") -> Song | None:
        self.calls += 1
        return Song(title=title, artist=artist, lines=[SongLine(text="выдуманная строка")])


class TestКэш:
    def test_повторный_поиск_не_ходит_в_источник(self, tmp_path):
        источник = _ЗапоминающийИсточник()
        кэш = CachingProvider(источник, cache_dir=tmp_path)

        первый = кэш.search("Название", "Исполнитель")
        второй = кэш.search("Название", "Исполнитель")

        assert источник.calls == 1
        assert первый is not None and второй is not None
        assert второй.lines[0].text == "выдуманная строка"

    def test_битый_файл_кэша_игнорируется(self, tmp_path):
        источник = _ЗапоминающийИсточник()
        кэш = CachingProvider(источник, cache_dir=tmp_path)
        кэш.search("Название", "")

        for файл in tmp_path.glob("*.json"):
            файл.write_text("{битый", encoding="utf-8")

        assert кэш.search("Название", "") is not None
        assert источник.calls == 2  # сходили в источник заново

    def test_песня_переживает_сериализацию(self):
        песня = Song(title="Проба", artist="Никто", lines=[])
        песня.lines = [SongLine(text="строка")]
        песня.lines[0].chords = []

        восстановленная = Song.from_dict(песня.to_dict())

        assert восстановленная.title == "Проба"
        assert восстановленная.lines[0].text == "строка"
