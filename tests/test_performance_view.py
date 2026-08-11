"""Тесты отрисовки экрана исполнения.

Проверяют окно строк вокруг текущей, перенос длинных строк по ширине
и плавность перехода между строками.
"""

from prompter.parser import parse_song_text

ПОДБОР = """Am
первая выдуманная строка

C
вторая выдуманная строка

G
третья выдуманная строка

Am
четвёртая выдуманная строка

F
пятая выдуманная строка"""


def сделать_вид(qt_app, index: int, ширина: int = 900):
    from prompter.ui.performance_screen import LyricsView

    song = parse_song_text(ПОДБОР, title="Проба")
    view = LyricsView()
    view.set_animation_duration(0)  # без анимации расстановка сразу конечная
    view.resize(ширина, 600)
    view.set_song(song)
    view.set_index(song.singable_indexes[index])
    return song, view


class TestОкноСтрок:
    def test_текущая_строка_в_центре_и_самая_крупная(self, qt_app):
        song, view = сделать_вид(qt_app, 2)
        текущая = song.singable_indexes[2]

        места = view._placements(текущая)

        assert места[текущая].fade == 0
        самый_крупный = max(м.size for м in места.values())
        assert места[текущая].size == самый_крупный

    def test_соседние_строки_приглушены_тем_сильнее_чем_дальше(self, qt_app):
        song, view = сделать_вид(qt_app, 2)
        поющиеся = song.singable_indexes

        места = view._placements(поющиеся[2])

        assert места[поющиеся[1]].fade == 1
        assert места[поющиеся[0]].fade == 2

    def test_строки_идут_сверху_вниз_по_порядку(self, qt_app):
        song, view = сделать_вид(qt_app, 2)
        поющиеся = song.singable_indexes

        места = view._placements(поющиеся[2])
        сверху_вниз = [места[i].top for i in поющиеся if i in места]

        assert сверху_вниз == sorted(сверху_вниз)

    def test_пустые_разделители_и_табулатура_не_показываются(self, qt_app):
        song, view = сделать_вид(qt_app, 2)

        показываемые = view._visible_indexes()

        assert all(not song.lines[i].is_blank for i in показываемые)
        assert all(not song.lines[i].has_tab for i in показываемые)

    def test_песня_без_строк_не_роняет_отрисовку(self, qt_app):
        from prompter.models import Song
        from prompter.ui.performance_screen import LyricsView

        view = LyricsView()
        view.set_song(Song(title="Пустая"))

        assert view._placements(0) == {}


class TestПереносДлинныхСтрок:
    ДЛИННАЯ = (
        "Am              C                 G                F\n"
        "это очень длинная выдуманная строка которая заведомо не помещается "
        "в одну экранную строку и должна быть перенесена"
    )

    def test_длинная_строка_разбивается(self, qt_app):
        from prompter.ui.performance_screen import LyricsView

        song = parse_song_text(self.ДЛИННАЯ)
        view = LyricsView()
        view.resize(600, 400)
        view.set_song(song)

        layout = view._row_layout(song.singable_indexes[0], 34)

        assert len(layout.segments) > 1

    def test_короткая_строка_не_разбивается(self, qt_app):
        from prompter.ui.performance_screen import LyricsView

        song = parse_song_text("Am\nкороткая строка")
        view = LyricsView()
        view.resize(900, 400)
        view.set_song(song)

        layout = view._row_layout(song.singable_indexes[0], 34)

        assert len(layout.segments) == 1

    def test_текст_при_переносе_не_теряется(self, qt_app):
        from prompter.ui.performance_screen import LyricsView

        song = parse_song_text(self.ДЛИННАЯ)
        view = LyricsView()
        view.resize(600, 400)
        view.set_song(song)
        строка = song.lines[song.singable_indexes[0]]

        layout = view._row_layout(song.singable_indexes[0], 34)
        собранное = " ".join(с.text for с in layout.segments)

        assert собранное.split() == строка.text.split()

    def test_аккорды_распределяются_по_кускам(self, qt_app):
        from prompter.ui.performance_screen import LyricsView

        song = parse_song_text(self.ДЛИННАЯ)
        view = LyricsView()
        view.resize(600, 400)
        view.set_song(song)
        строка = song.lines[song.singable_indexes[0]]

        layout = view._row_layout(song.singable_indexes[0], 34)
        всего = sum(len(с.chords) for с in layout.segments)

        # Ни один аккорд не должен потеряться при разбиении
        assert всего == len(строка.chords)

    def test_перенесённая_строка_выше_обычной(self, qt_app):
        from prompter.ui.performance_screen import LyricsView

        song = parse_song_text(self.ДЛИННАЯ)
        view = LyricsView()
        view.resize(600, 400)
        view.set_song(song)

        узкая = view._row_layout(song.singable_indexes[0], 34)
        view.resize(1600, 400)
        view._layout_cache.clear()
        широкая = view._row_layout(song.singable_indexes[0], 34)

        assert узкая.height > широкая.height


class TestПлавностьПерехода:
    def test_во_время_перехода_размер_промежуточный(self, qt_app):
        from prompter.ui.performance_screen import LyricsView

        song = parse_song_text(ПОДБОР)
        view = LyricsView()
        view.resize(900, 600)
        view.set_song(song)
        поющиеся = song.singable_indexes

        view.set_index(поющиеся[1])
        view.set_index(поющиеся[2])
        view._progress = 0.5  # середина перехода

        места = view._interpolated_placements()

        # Строка, которая становится текущей, на полпути between размеров:
        # без этого она меняла бы масштаб скачком, и переход выглядел бы рывком
        мелкий = view._size_for(1)
        крупный = view._size_for(0)
        размер = места[поющиеся[2]].size
        assert мелкий < размер < крупный

    def test_после_перехода_размеры_конечные(self, qt_app):
        from prompter.ui.performance_screen import LyricsView

        song = parse_song_text(ПОДБОР)
        view = LyricsView()
        view.resize(900, 600)
        view.set_song(song)
        поющиеся = song.singable_indexes

        view.set_index(поющиеся[2])
        view._progress = 1.0

        места = view._interpolated_placements()

        assert места[поющиеся[2]].size == float(view._size_for(0))

    def test_без_анимации_переход_мгновенный(self, qt_app):
        from prompter.ui.performance_screen import LyricsView

        song = parse_song_text(ПОДБОР)
        view = LyricsView()
        view.resize(900, 600)
        view.set_animation_duration(0)
        view.set_song(song)

        view.set_index(song.singable_indexes[2])

        assert view._progress == 1.0


class TestПанельТабулатуры:
    ПОДБОР_С_ТАБОМ = (
        "e|---------------|\n"
        "B|-------0-------|\n"
        "G|---0-------0---|\n"
        "D|-2-----------2-|\n"
        "\n"
        "Am\nпервая выдуманная строка\n"
        "\nC\nвторая выдуманная строка"
    )

    def test_табулатура_не_попадает_в_окно_текста(self, qt_app):
        from prompter.ui.performance_screen import LyricsView

        song = parse_song_text(self.ПОДБОР_С_ТАБОМ)
        view = LyricsView()
        view.resize(900, 600)
        view.set_song(song)

        показываемые = view._visible_indexes()

        # Четыре строки дефисов вытеснили бы слова из окна
        assert all(not song.lines[i].has_tab for i in показываемые)
        assert len(показываемые) == 2

    def test_панель_показывается_когда_табулатура_есть(self, qt_app):
        from prompter.ui.performance_screen import PerformanceScreen

        song = parse_song_text(self.ПОДБОР_С_ТАБОМ)
        screen = PerformanceScreen()
        screen.show_song(song, "1 из 1")

        assert screen.tab_panel._lines != []

    def test_панель_прячется_когда_табулатуры_нет(self, qt_app):
        from prompter.ui.performance_screen import PerformanceScreen

        song = parse_song_text("Am\nпервая выдуманная строка")
        screen = PerformanceScreen()
        screen.show_song(song, "1 из 1")

        assert screen.tab_panel._lines == []
        assert screen.tab_panel.isHidden()
