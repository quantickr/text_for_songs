"""Тесты разбора аккордовых листов.

Во всех тестах используется выдуманный текст-заглушка, а не настоящие песни.
"""

import pytest

from prompter.parser import (
    parse_section_timing,
    detect_format,
    extract_chords,
    is_chord_line,
    is_chord_token,
    is_tab_line,
    parse_chordpro,
    parse_chord_sheet,
    parse_section_header,
    parse_song_text,
    ultimate_guitar_to_chord_sheet,
)


class TestChordRecognition:
    def test_простые_аккорды_распознаются(self):
        for token in ["Am", "C", "G", "F#m", "Bb", "H", "Em7", "Csus4", "D/F#", "Gmaj7"]:
            assert is_chord_token(token), token

    def test_слова_не_считаются_аккордами(self):
        for token in ["привет", "hello", "Add", "Cat", "Best", "мама", "Love"]:
            assert not is_chord_token(token), token

    def test_строка_аккордов_отличается_от_текста(self):
        assert is_chord_line("Am        C       G")
        assert is_chord_line("  Em   |   Am   x2")
        assert not is_chord_line("первая строка выдуманного текста")

    def test_одинокий_аккорд_из_одной_буквы_распознаётся(self):
        # Частый случай в подборах: над строкой стоит единственный аккорд
        assert is_chord_line("C")
        assert is_chord_line("G")
        assert is_chord_line("  F  ")

    def test_строчная_a_считается_артиклем_а_не_аккордом(self):
        # Иначе строка английского текста «a» была бы съедена как аккордовая
        assert not is_chord_line("a")

    def test_позиции_аккордов_считаются_в_символах(self):
        chords = extract_chords("Am    C")
        assert [(c.name, c.position) for c in chords] == [("Am", 0), ("C", 6)]


class TestSectionHeaders:
    def test_русские_и_английские_заголовки(self):
        assert parse_section_header("Припев:") == "Припев"
        assert parse_section_header("Куплет 2:") == "Куплет 2"
        assert parse_section_header("[Chorus]") == "Chorus"
        assert parse_section_header("[Verse 1]") == "Verse 1"

    def test_аккорд_в_скобках_не_заголовок(self):
        assert parse_section_header("[Am]") is None


class TestChordSheet:
    def test_аккорды_привязываются_к_строке_под_ними(self):
        text = "Am        C\nпервая строка текста\n\nG         D\nвторая строка текста"
        lines = parse_chord_sheet(text)

        assert len(lines) == 3  # две строки текста и пустой разделитель
        assert lines[0].text == "первая строка текста"
        assert [(c.name, c.position) for c in lines[0].chords] == [("Am", 0), ("C", 10)]
        assert lines[1].is_blank
        assert lines[2].text == "вторая строка текста"

    def test_аккорды_без_текста_это_проигрыш(self):
        lines = parse_chord_sheet("Am   C   G\n\nAm   C\nстрока текста")

        assert lines[0].text == ""
        assert lines[0].has_chords
        assert not lines[0].has_text

    def test_текст_без_аккордов_сохраняется(self):
        lines = parse_chord_sheet("строка один\nстрока два")

        assert [line.text for line in lines] == ["строка один", "строка два"]
        assert all(not line.has_chords for line in lines)

    def test_общий_отступ_убирается_а_позиции_пересчитываются(self):
        text = "    Am        C\n    первая строка текста"
        lines = parse_chord_sheet(text)

        assert lines[0].text == "первая строка текста"
        assert lines[0].chords[0].position == 0
        assert lines[0].chords[1].position == 10

    def test_заголовок_блока_становится_отдельной_строкой(self):
        # Отдельной строкой — чтобы суфлёр мог на ней постоять и уйти по таймеру:
        # спеть заголовок нельзя, и голос его никогда не сдвинет
        lines = parse_chord_sheet("Припев:\nAm\nстрока припева")

        assert lines[0].section == "Припев"
        assert not lines[0].has_text
        assert lines[1].text == "строка припева"

    def test_заголовок_без_двоеточия_тоже_опознаётся(self):
        # В подборах сплошь и рядом просто «Verse» или «SOLO» отдельной строкой
        for заголовок in ("Verse", "CHORUS", "SOLO", "Припев", "Куплет 2"):
            assert parse_section_header(заголовок) == заголовок, заголовок

    def test_обычный_текст_не_принимается_за_заголовок(self):
        assert parse_section_header("первая выдуманная строка примера") is None
        assert parse_section_header("переход через дорогу был долгим") is None

    @pytest.mark.parametrize(
        "строка,ожидание",
        [
            # Двоеточие в конце короткой строки — самый надёжный признак
            ("Вступление:", "Вступление"),
            ("Соло гитары:", "Соло гитары"),
            ("Кода:", "Кода"),
            ("Мелодия:", "Мелодия"),
            # А длинная фраза с двоеточием — уже часть текста песни
            ("и вот что он сказал ей тогда в тот вечер:", None),
        ],
    )
    def test_двоеточие_выдаёт_заголовок(self, строка, ожидание):
        assert parse_section_header(строка) == ожидание

    @pytest.mark.parametrize(
        "заголовок",
        [
            # Составные названия пишут через дефис, пробел и слитно
            "Pre-Chorus:", "Pre chorus", "PRE CHORUS", "PRECHORUS",
            "Post-Chorus:", "Предприпев", "Предприпев:",
            "Инструментал", "Interlude", "Instrumental",
        ],
    )
    def test_составные_заголовки_опознаются(self, заголовок):
        # Иначе такая строка попадёт в поток как «спеваемая» и суфлёр на ней зависнет
        assert parse_section_header(заголовок) is not None, заголовок

    def test_подряд_идущие_пустые_строки_схлопываются(self):
        lines = parse_chord_sheet("строка один\n\n\n\nстрока два")

        assert len(lines) == 3
        assert lines[1].is_blank

    def test_восстановление_строки_аккордов(self):
        lines = parse_chord_sheet("Am        C\nпервая строка текста")

        assert lines[0].chord_line() == "Am        C"


class TestТабулатура:
    """Перебор по струнам приходит вместо аккордов на части страниц."""

    БЛОК = (
        "e|---------------|\n"
        "B|---------------|\n"
        "G|-------0-------|\n"
        "D|---2-------2---|\n"
        "A|-------------3-|\n"
        "E|-0-------------|"
    )

    def test_строка_табулатуры_опознаётся(self):
        assert is_tab_line("e|---3---5---|")
        assert is_tab_line("|-0-2-3-2-0-|")  # подписи струны может не быть

    def test_текст_песни_не_принимается_за_табулатуру(self):
        assert not is_tab_line("первая выдуманная строка примера")
        assert not is_tab_line("Am   C   G")
        assert not is_tab_line("что-то через дефис")

    def test_блок_собирается_целиком(self):
        lines = parse_chord_sheet(self.БЛОК + "\n\nAm\nпервая выдуманная строка")

        табы = [line for line in lines if line.has_tab]
        assert len(табы) == 1
        assert len(табы[0].tab_lines) == 6

    def test_табулатура_не_попадает_в_поющиеся_строки(self):
        # Иначе голосовая прокрутка спотыкалась бы о строки без слов
        song = parse_song_text(self.БЛОК + "\n\nAm\nпервая выдуманная строка")

        assert len(song.singable_indexes) == 1
        assert song.has_tabs

    def test_табулатура_находится_для_текущей_строки(self):
        song = parse_song_text(self.БЛОК + "\n\nAm\nпервая выдуманная строка")
        строка = song.singable_indexes[0]

        assert len(song.tab_for_line(строка)) == 6

    def test_песня_без_табулатуры_отдаёт_пустой_список(self):
        song = parse_song_text("Am\nпервая выдуманная строка")

        assert not song.has_tabs
        assert song.tab_for_line(0) == []


class TestСхемыБоя:
    """Строки боя и табов с приписками — по признаку тактовых черт."""

    def test_схема_боя_не_попадает_в_текст(self):
        # Иначе она встанет в поток строк и её попытаются «спеть»
        assert is_tab_line("| v ^ v | v ^ v |")
        assert is_tab_line("|-3-3-|-2-2-|")

    def test_таб_с_припиской_опознаётся(self):
        # Из-за комментария доля букв велика, но тактовые черты выдают схему
        assert is_tab_line("Ab|-5-5-5-5-2-2-2-2-| just keep banging on this")
        assert is_tab_line("Db|-7-7-7-7-4-4-4-4-|(x2)")

    def test_текст_с_одной_чертой_остаётся_текстом(self):
        assert not is_tab_line("строка текста | с одной чертой")


class TestДлительностьБлоков:
    @pytest.mark.parametrize(
        "строка,ожидание",
        [
            ("Вступление 8 сек", 8.0),
            ("Соло 12 секунд", 12.0),
            ("Вступление 4", 4.0),
            ("Проигрыш x2", 8.0),  # повторы переводим в секунды по такту
            ("Проигрыш 3 раза", 12.0),
            ("Припев", None),
            # Число у нумеруемых блоков — это номер, а не длительность
            ("Куплет 2", None),
            ("Verse 1", None),
        ],
    )
    def test_длительность_разбирается(self, строка, ожидание):
        assert parse_section_timing(строка) == ожидание

    def test_заголовок_с_длительностью_опознаётся(self):
        assert parse_section_header("Вступление 8 сек") == "Вступление 8 сек"
        assert parse_section_header("Проигрыш x2") == "Проигрыш x2"


class TestChordPro:
    def test_инлайновые_аккорды_и_метаданные(self):
        text = "{title: Выдуманная песня}\n{artist: Никто}\n[Am]первая [C]строка текста"
        lines, meta = parse_chordpro(text)

        assert meta["title"] == "Выдуманная песня"
        assert meta["artist"] == "Никто"
        assert lines[0].text == "первая строка текста"
        assert [(c.name, c.position) for c in lines[0].chords] == [("Am", 0), ("C", 7)]

    def test_директива_припева_становится_заголовком(self):
        lines, _ = parse_chordpro("{start_of_chorus}\n[Am]строка припева")

        assert lines[0].section == "Припев"


class TestUltimateGuitar:
    def test_маркеры_убираются_и_выравнивание_сохраняется(self):
        content = "[tab][ch]Am[/ch]        [ch]C[/ch]\r\nпервая строка текста[/tab]"
        sheet = ultimate_guitar_to_chord_sheet(content)

        assert sheet == "Am        C\nпервая строка текста"

    def test_разбор_целиком(self):
        content = "[Verse 1]\r\n[tab][ch]Am[/ch]   [ch]C[/ch]\r\nпервая строка текста[/tab]"
        song = parse_song_text(content, title="Тест")

        assert song.lines[0].section == "Verse 1"
        assert song.lines[1].text == "первая строка текста"
        assert [c.name for c in song.lines[1].chords] == ["Am", "C"]

    def test_заголовки_секций_не_вырезаются_как_теги(self):
        # Наивное удаление всего в квадратных скобках убило бы [Chorus]
        sheet = ultimate_guitar_to_chord_sheet("[Chorus]\r\n[ch]Am[/ch]\r\nстрока")
        assert "[Chorus]" in sheet


class TestFormatDetection:
    def test_форматы_определяются(self):
        assert detect_format("[ch]Am[/ch]\nстрока") == "ug"
        assert detect_format("{title: Что-то}\n[Am]строка") == "chordpro"
        assert detect_format("Am   C\nстрока текста") == "sheet"

    def test_язык_песни_определяется_по_алфавиту(self):
        русская = parse_song_text("Am\nвыдуманная строка на русском языке")
        английская = parse_song_text("Am\nmade up line of english text here")

        assert русская.detect_language() == "ru"
        assert английская.detect_language() == "en"
