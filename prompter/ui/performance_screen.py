"""Экран исполнения: окно из нескольких строк с аккордами над словами."""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPointF,
    QRectF,
    Qt,
    QVariantAnimation,
)
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPaintEvent
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..models import ChordMark, Song, SongLine
from . import theme

# Во сколько раз соседние строки мельче текущей. Разница должна быть заметной:
# на сцене взгляд цепляется именно за перепад размера, а не за оттенок
_NEIGHBOUR_SCALE = 0.54

# Следующая строка — тоже подсвеченная, но чуть скромнее текущей: по ней
# заранее видно продолжение, и при этом понятно, какую строку поют сейчас
_NEXT_SCALE = 0.78
_NEXT_FADE = 0.45
# Отступ между строкой аккордов и строкой текста, в долях высоты шрифта
_CHORD_GAP = 0.18

# Поля вокруг текста: слева место под полоску-указатель, справа — воздух,
# чтобы длинные строки не упирались в край
_LEFT_MARGIN = 48
_RIGHT_MARGIN = 32

# Сколько строк держать сверх окна: во время перехода они приезжают из-за края
_EXTRA_ROWS = 2

# До какой доли исходного размера разрешено ужимать длинную строку, прежде чем
# переносить её. Мельче — уже плохо читается с расстояния
_MIN_SHRINK = 0.72

# Длительность доезда строки. Короче — движение выглядит рывком, длиннее —
# начинает отставать от пения и мешать читать
SCROLL_ANIMATION_MS = 280


def _scroll_easing() -> QEasingCurve:
    """Кривая движения строки: мягкий старт и долгий плавный доезд.

    Готовые кривые Qt для этого не годятся: у ``InOutCubic`` симметричный
    профиль, из-за чего конец движения кажется резковатым. Здесь та же
    кубическая кривая, что используют в интерфейсах для «жидкого» скольжения —
    строка трогается почти незаметно и долго успокаивается в конце.
    """
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    # Первая точка прижата к нулю по вертикали — строка трогается плавно,
    # вторая вынесена к единице — так же плавно останавливается
    curve.addCubicBezierSegment(QPointF(0.4, 0.0), QPointF(0.2, 1.0), QPointF(1.0, 1.0))
    return curve


@dataclass
class _Segment:
    """Одна экранная строка после переноса: текст и аккорды над ним."""

    text: str
    chords: list[ChordMark]


@dataclass
class _RowLayout:
    """Как строка песни выглядит на экране при заданном размере шрифта."""

    segments: list[_Segment]
    text_size: int
    chord_size: int
    height: float


@dataclass
class _Placement:
    """Положение строки на экране: где, каким размером и насколько ярко."""

    top: float
    size: float
    fade: float


class LyricsView(QWidget):
    """Рисует окно строк вокруг текущей: соседние приглушены, текущая выделена.

    Аккорды рисуются над теми словами, к которым привязаны: позиция аккорда
    измеряется в символах, а на экране пересчитывается через метрики шрифта.
    Поэтому шрифт может быть любым, не обязательно моноширинным.

    Переход между строками анимируется целиком, а не только сдвигом. Если
    двигать лишь позицию, размер строк всё равно меняется скачком — соседняя
    мгновенно становится крупной, и глаз читает это как рывок, какую кривую
    ни подбирай. Поэтому на время перехода положение, размер и яркость каждой
    строки берутся промежуточными между «как было» и «как станет».
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._song: Song | None = None
        self._index = 0
        self._previous_index = 0
        self._font_size = 34
        self._context = 2
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(320)

        # Раскладка строк считается заново на каждом кадре, поэтому переносы
        # кэшируются: измерять ширину текста 60 раз в секунду накладно
        self._layout_cache: dict[tuple[int, int, int], _RowLayout] = {}

        self._progress = 1.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(SCROLL_ANIMATION_MS)
        self._animation.setEasingCurve(_scroll_easing())
        self._animation.valueChanged.connect(self._on_progress)

    def _on_progress(self, value: object) -> None:
        self._progress = float(value)  # type: ignore[arg-type]
        self.update()

    # --- Данные ------------------------------------------------------------

    def set_song(self, song: Song | None) -> None:
        self._song = song
        self._index = 0
        self._previous_index = 0
        self._progress = 1.0
        self._layout_cache.clear()
        self.update()

    def set_index(self, index: int) -> None:
        """Перевести окно на другую строку, доехав до неё плавно."""
        if index == self._index:
            return

        self._previous_index = self._index
        self._index = index

        if self._animation.duration() > 0 and self._song is not None:
            self._animation.stop()
            self._animation.setStartValue(0.0)
            self._animation.setEndValue(1.0)
            self._progress = 0.0
            self._animation.start()
        else:
            self._progress = 1.0
        self.update()

    def set_font_size(self, size: int) -> None:
        self._font_size = max(14, size)
        self._layout_cache.clear()
        self.update()

    def set_context_lines(self, count: int) -> None:
        self._context = max(1, count)
        self.update()

    def set_animation_duration(self, milliseconds: int) -> None:
        """Задать длительность перехода. Ноль — переключать мгновенно."""
        self._animation.setDuration(max(0, milliseconds))

    def resizeEvent(self, event) -> None:  # noqa: N802, ANN001 (сигнатура из Qt)
        # Ширина изменилась — переносы надо пересчитать
        self._layout_cache.clear()
        super().resizeEvent(event)

    # --- Раскладка строки --------------------------------------------------

    @property
    def _text_width(self) -> int:
        """Сколько places по ширине отведено под текст песни."""
        return max(120, self.width() - _LEFT_MARGIN - _RIGHT_MARGIN)

    def _row_layout(self, line_index: int, text_size: int) -> _RowLayout:
        """Разложить строку по ширине экрана.

        Сначала пробуем слегка ужать шрифт: строка на пару слов длиннее нормы
        читается лучше чуть мельче, чем разорванной пополам. Если и при
        минимальном размере не помещается — переносим, потому что дальше
        уменьшать значит сделать текст нечитаемым с расстояния.
        """
        ключ = (line_index, text_size, self._text_width)
        готовое = self._layout_cache.get(ключ)
        if готовое is not None:
            return готовое

        assert self._song is not None
        line = self._song.lines[line_index]

        подобранный = text_size
        предел = max(12, int(text_size * _MIN_SHRINK))
        while подобранный > предел and not _fits(
            line.text, self.font(), подобранный, self._text_width, self
        ):
            подобранный -= 1

        text_size = подобранный
        chord_size = max(11, int(text_size * 0.55))

        segments = _wrap_line(
            line, QFontMetrics(_measuring_font(self.font(), text_size), self),
            self._text_width,
        )

        # Место под аккорды резервируем только там, где они есть: у второй
        # половины перенесённой строки аккордов обычно нет, и пустая полоса
        # над ней выглядела бы разрывом
        layout = _RowLayout(
            segments=segments,
            text_size=text_size,
            chord_size=chord_size,
            height=sum(_segment_height(с, text_size, chord_size) for с in segments),
        )
        self._layout_cache[ключ] = layout
        return layout

    def _size_for(self, offset: int) -> int:
        """Размер шрифта строки, отстоящей от текущей на ``offset``."""
        if offset == 0:
            return self._font_size
        if offset == 1:
            return max(12, int(self._font_size * _NEXT_SCALE))
        return max(12, int(self._font_size * _NEIGHBOUR_SCALE))

    @staticmethod
    def _fade_for(offset: int) -> float:
        """Насколько приглушить строку, отстоящую от текущей на ``offset``.

        Следующая строка держится заметно ярче остальных: по ней человек
        заранее видит, что петь дальше, и не приходится ждать перехода.
        """
        if offset == 0:
            return 0.0
        if offset == 1:
            return _NEXT_FADE
        return float(abs(offset))

    # --- Расстановка строк на экране ---------------------------------------

    def _visible_indexes(self) -> list[int]:
        """Строки, которые вообще можно показывать.

        Пустые разделители и табулатуру не берём: первые занимали бы место
        в окне вместо текста, вторую показывает отдельная панель сбоку.
        """
        assert self._song is not None
        return [
            i
            for i, line in enumerate(self._song.lines)
            if not line.is_blank and not line.has_tab
        ]

    def _placements(self, current: int) -> dict[int, _Placement]:
        """Где, каким размером и насколько ярко стоят строки при данной текущей."""
        assert self._song is not None
        видимые = self._visible_indexes()
        if not видимые:
            return {}

        if current in видимые:
            позиция = видимые.index(current)
        else:
            позиция = min(range(len(видимые)), key=lambda i: abs(видимые[i] - current))

        # Берём с запасом: во время перехода строка приезжает из-за края экрана
        запас = self._context + _EXTRA_ROWS
        начало = max(0, позиция - запас)
        конец = min(len(видимые), позиция + запас + 1)

        # Высоты нужны все сразу: положение строки зависит от того,
        # что лежит выше неё
        строки = [(i - позиция, видимые[i]) for i in range(начало, конец)]
        высоты = {
            индекс: self._row_layout(индекс, self._size_for(смещение)).height
            for смещение, индекс in строки
        }

        # Центрируем текущую строку по вертикали и раскладываем остальные от неё
        центр = self.height() / 2.0
        текущая_высота = высоты[видимые[позиция]]
        y = центр - текущая_высота / 2.0

        расстановка: dict[int, _Placement] = {}
        for смещение, индекс in строки:
            if смещение < 0:
                continue
            расстановка[индекс] = _Placement(
                top=y, size=float(self._size_for(смещение)), fade=self._fade_for(смещение)
            )
            y += высоты[индекс]

        # Строки выше текущей отсчитываем вверх от неё
        y = центр - текущая_высота / 2.0
        for смещение, индекс in reversed([с for с in строки if с[0] < 0]):
            y -= высоты[индекс]
            расстановка[индекс] = _Placement(
                top=y, size=float(self._size_for(смещение)), fade=self._fade_for(смещение)
            )
        return расстановка

    # --- Отрисовка ---------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (имя из Qt)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        # Без этого текст при движении прыгает по целым пикселям
        painter.setRenderHint(QPainter.RenderHint.VerticalSubpixelPositioning)
        painter.fillRect(self.rect(), theme.BACKGROUND)

        if self._song is None or not self._song.lines:
            self._draw_placeholder(painter, "Текст песни не загружен")
            return

        расстановка = self._interpolated_placements()
        if not расстановка:
            self._draw_placeholder(painter, "Нет строк для показа")
            return

        for индекс, место in sorted(расстановка.items()):
            self._draw_row(painter, индекс, место)

        painter.end()

    def _interpolated_placements(self) -> dict[int, _Placement]:
        """Положение строк с учётом незавершённого перехода.

        Пока идёт переход, каждая строка находится между тем, где она была,
        и тем, где окажется, — включая размер и яркость. Именно это и делает
        движение плавным: без интерполяции размера строки скачком меняли бы
        масштаб в момент переключения.
        """
        новое = self._placements(self._index)
        if self._progress >= 1.0 or self._previous_index == self._index:
            return новое

        старое = self._placements(self._previous_index)
        t = self._progress
        смешанное: dict[int, _Placement] = {}

        for индекс in set(новое) | set(старое):
            было = старое.get(индекс)
            станет = новое.get(индекс)

            if было is None and станет is not None:
                # Строка появляется: приезжает снизу и разгорается
                было = _Placement(
                    top=станет.top + self.height() * 0.25,
                    size=станет.size,
                    fade=станет.fade + 1.0,
                )
            elif станет is None and было is not None:
                # Строка уходит вверх за край и гаснет
                станет = _Placement(
                    top=было.top - self.height() * 0.25, size=было.size, fade=было.fade + 1.0
                )
            if было is None or станет is None:
                continue

            смешанное[индекс] = _Placement(
                top=было.top + (станет.top - было.top) * t,
                size=было.size + (станет.size - было.size) * t,
                fade=было.fade + (станет.fade - было.fade) * t,
            )
        return смешанное

    def _draw_row(self, painter: QPainter, line_index: int, место: _Placement) -> None:
        """Нарисовать строку: сперва аккорды, под ними текст."""
        assert self._song is not None
        if место.top > self.height() or место.top < -self.height() * 0.5:
            return  # строка за пределами экрана

        line = self._song.lines[line_index]
        # Раскладку считаем для того размера, которым рисуем: иначе на
        # промежуточных кадрах текст окажется крупнее рассчитанного и уедет
        # за правый край. Сама раскладка может ужать шрифт — берём её решение
        layout = self._row_layout(line_index, max(10, int(round(место.size))))
        размер = layout.text_size
        chord_size = layout.chord_size
        текущая = место.fade < 0.25
        следующая = 0.25 <= место.fade < 0.9

        text_font = QFont(self.font())
        text_font.setPixelSize(размер)
        if текущая:
            text_font.setWeight(QFont.Weight.DemiBold)
        elif следующая:
            # Промежуточный вес: строка читается как активная, но не спорит
            # с текущей за внимание
            text_font.setWeight(QFont.Weight.Medium)
        else:
            text_font.setWeight(QFont.Weight.Normal)

        chord_font = QFont(self.font())
        chord_font.setPixelSize(chord_size)
        chord_font.setWeight(QFont.Weight.Bold)

        основной = theme.TEXT_PRIMARY if (текущая or следующая) else theme.TEXT_MUTED
        text_color = _fade(основной, место.fade)
        chord_color = _fade(theme.CHORD, место.fade)

        metrics_text = QFontMetrics(text_font, self)
        metrics_chord = QFontMetrics(chord_font, self)

        # Полоска-указатель у подсвеченных строк — видно боковым зрением.
        # У следующей она бледнее, чтобы взгляд всё равно цеплялся за текущую
        if текущая or следующая:
            цвет = QColor(theme.ACCENT)
            цвет.setAlpha(int(255 * max(0.0, 1.0 - место.fade * 1.6)))
            высота = sum(
                _segment_height(с, размер, chord_size) for с in layout.segments
            )
            painter.fillRect(QRectF(16.0, место.top + 4, 5.0, высота - 12), цвет)

        y = место.top
        for сегмент in layout.segments:
            есть_аккорды = bool(сегмент.chords)
            chord_baseline = y + chord_size * 1.1
            подъём = chord_size * _CHORD_GAP if есть_аккорды else -chord_size * 1.1
            text_baseline = chord_baseline + подъём + размер * 1.05

            if сегмент.chords:
                painter.setFont(chord_font)
                painter.setPen(chord_color)
                занято = float(_LEFT_MARGIN)
                for chord in sorted(сегмент.chords, key=lambda c: c.position):
                    prefix = сегмент.text[: chord.position]
                    if chord.position > len(сегмент.text):
                        лишнее = chord.position - len(сегмент.text)
                        x = (
                            _LEFT_MARGIN
                            + metrics_text.horizontalAdvance(сегмент.text)
                            + лишнее * (размер / 3.0)
                        )
                    else:
                        x = _LEFT_MARGIN + metrics_text.horizontalAdvance(prefix)
                    x = max(x, занято)
                    painter.drawText(QPointF(x, chord_baseline), chord.name)
                    занято = x + metrics_chord.horizontalAdvance(chord.name) + 12

            if сегмент.text:
                painter.setFont(text_font)
                painter.setPen(text_color)
                painter.drawText(QPointF(float(_LEFT_MARGIN), text_baseline), сегмент.text)
            elif line.section:
                painter.setFont(chord_font)
                painter.setPen(_fade(theme.TEXT_MUTED, место.fade))
                painter.drawText(QPointF(float(_LEFT_MARGIN), text_baseline), line.section)

            y += _segment_height(сегмент, размер, chord_size)

    def _draw_placeholder(self, painter: QPainter, message: str) -> None:
        painter.setPen(theme.TEXT_MUTED)
        font = QFont(self.font())
        font.setPixelSize(20)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, message)
        painter.end()


def _measuring_font(base_font: QFont, size: int) -> QFont:
    """Шрифт для измерения ширины строки.

    Меряем всегда полужирным, хотя соседние строки рисуются обычным
    начертанием: текущая строка — полужирная и заметно шире, и если считать
    по обычному, при переходе в текущую она вылезет за край экрана.
    """
    font = QFont(base_font)
    font.setPixelSize(size)
    font.setWeight(QFont.Weight.DemiBold)
    return font


def _fits(text: str, base_font: QFont, size: int, max_width: int, device: QWidget) -> bool:
    """Помещается ли текст в одну экранную строку при данном размере шрифта."""
    if not text.strip():
        return True
    metrics = QFontMetrics(_measuring_font(base_font, size), device)
    return metrics.horizontalAdvance(text) <= max_width


def _segment_height(segment: _Segment, text_size: float, chord_size: float) -> float:
    """Высота одной экранной строки: с местом под аккорды или без него."""
    return text_size * 1.5 + (chord_size * 1.4 if segment.chords else 0.0)


def _wrap_line(line: SongLine, metrics: QFontMetrics, max_width: int) -> list[_Segment]:
    """Разбить строку по ширине экрана, растащив аккорды по кускам.

    Длинную строку иначе пришлось бы обрезать или мельчить весь текст.
    Перенос делается только для показа: на голосовую прокрутку он не влияет,
    строка песни остаётся одной строкой.
    """
    if not line.text.strip():
        return [_Segment(text=line.text, chords=list(line.chords))]

    if metrics.horizontalAdvance(line.text) <= max_width:
        return [_Segment(text=line.text, chords=list(line.chords))]

    сегменты: list[_Segment] = []
    начало = 0  # индекс символа, с которого начинается текущий кусок
    текущий = ""

    for слово, конец_слова in _words_with_positions(line.text):
        пробный = f"{текущий} {слово}".strip() if текущий else слово
        if текущий and metrics.horizontalAdvance(пробный) > max_width:
            сегменты.append(_segment_for(line, начало, начало + len(текущий), текущий))
            # Новый кусок начинается там, где кончился предыдущий
            начало = конец_слова - len(слово)
            текущий = слово
        else:
            текущий = пробный

    if текущий:
        сегменты.append(_segment_for(line, начало, len(line.text), текущий))
    return сегменты or [_Segment(text=line.text, chords=list(line.chords))]


def _words_with_positions(text: str):
    """Слова строки вместе с позицией их последнего символа."""
    позиция = 0
    for слово in text.split(" "):
        позиция += len(слово) + 1
        if слово:
            yield слово, позиция - 1


def _segment_for(line: SongLine, начало: int, конец: int, текст: str) -> _Segment:
    """Собрать кусок строки, отобрав относящиеся к нему аккорды."""
    аккорды = [
        ChordMark(name=chord.name, position=max(0, chord.position - начало))
        for chord in line.chords
        if начало <= chord.position < конец or (начало == 0 and chord.position < начало)
    ]
    return _Segment(text=текст, chords=аккорды)


def _fade(color: QColor, distance: float) -> QColor:
    """Приглушить цвет тем сильнее, чем дальше строка от текущей."""
    faded = QColor(color)
    faded.setAlpha(max(40, int(255 - distance * 75)))
    return faded




class TabPanel(QWidget):
    """Панель табулатуры справа от текста.

    Схему перебора нельзя спеть, поэтому в поток строк она не встраивается —
    иначе окно текста забивалось бы шестью строками дефисов, а голосовая
    прокрутка спотыкалась бы о строки без слов. Панель просто показывает блок,
    относящийся к текущему месту песни, и прячется, если табулатуры нет.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._lines: list[str] = []

        self.caption = QLabel("Перебор")
        self.caption.setObjectName("Subtitle")

        self.body = QLabel("")
        # Табулатура держится только на моноширинном шрифте: в нём номера ладов
        # встают ровно под нужными долями такта
        шрифт = QFont("Menlo")
        шрифт.setStyleHint(QFont.StyleHint.Monospace)
        шрифт.setPixelSize(13)
        self.body.setFont(шрифт)
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        область = QScrollArea()
        область.setWidget(self.body)
        область.setWidgetResizable(True)
        область.setFrameShape(QFrame.Shape.NoFrame)
        область.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.caption)
        layout.addWidget(область, 1)

        self.setFixedWidth(330)
        self.setMaximumHeight(260)
        self.hide()

    def set_tab(self, lines: list[str]) -> None:
        """Показать блок табулатуры; пустой список прячет панель."""
        if lines == self._lines:
            return
        self._lines = list(lines)

        if not lines:
            self.hide()
            return

        self.body.setText("\n".join(lines))
        self.body.setStyleSheet(f"color: {theme.TEXT_MUTED.name()};")
        self.show()


class PerformanceScreen(QWidget):
    """Полный экран исполнения: шапка, окно текста и строка состояния."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._song: Song | None = None

        self.title_label = QLabel("—")
        self.title_label.setObjectName("Title")

        self.artist_label = QLabel("")
        self.artist_label.setObjectName("Subtitle")

        self.queue_label = QLabel("")
        self.queue_label.setObjectName("Subtitle")
        self.queue_label.setAlignment(Qt.AlignmentFlag.AlignRight)

        self.line_label = QLabel("")
        self.line_label.setObjectName("Subtitle")
        self.line_label.setAlignment(Qt.AlignmentFlag.AlignRight)

        self.mic_label = QLabel("Микрофон выключен")
        self.mic_label.setObjectName("Subtitle")

        self.level_bar = _LevelIndicator()

        self.recognized_label = QLabel("")
        self.recognized_label.setObjectName("Hint")
        self.recognized_label.setWordWrap(False)

        self.lyrics_view = LyricsView()
        self.tab_panel = TabPanel()

        header_left = QVBoxLayout()
        header_left.setSpacing(2)
        header_left.addWidget(self.title_label)
        header_left.addWidget(self.artist_label)

        header_right = QVBoxLayout()
        header_right.setSpacing(2)
        header_right.addWidget(self.queue_label)
        header_right.addWidget(self.line_label)

        header = QHBoxLayout()
        header.addLayout(header_left, 1)
        header.addLayout(header_right)

        status = QHBoxLayout()
        status.addWidget(self.mic_label)
        status.addWidget(self.level_bar)
        status.addSpacing(12)
        status.addWidget(self.recognized_label, 1)

        hint = QLabel(
            "Пробел / → следующая строка   ←  предыдущая   N следующая песня   "
            "Esc к очереди   F полный экран"
        )
        hint.setObjectName("Hint")

        центр = QHBoxLayout()
        центр.setSpacing(0)
        центр.addWidget(self.lyrics_view, 1)
        # По центру по вертикали — так панель стоит рядом с текущей строкой,
        # а не висит в отрыве под самой шапкой
        центр.addWidget(self.tab_panel, 0, Qt.AlignmentFlag.AlignVCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 18)
        layout.setSpacing(14)
        layout.addLayout(header)
        layout.addLayout(центр, 1)
        layout.addLayout(status)
        layout.addWidget(hint)

    # --- Обновление состояния ---------------------------------------------

    def show_song(self, song: Song, queue_position: str) -> None:
        self.title_label.setText(song.title or "Без названия")
        capo = f"   •   каподастр: {song.capo}" if song.capo else ""
        source = f"   •   {song.source}" if song.source else ""
        self.artist_label.setText(f"{song.artist}{source}{capo}".strip(" •"))
        self.queue_label.setText(f"Песня {queue_position}" if queue_position else "")
        self._song = song
        self.lyrics_view.set_song(song)
        self.tab_panel.set_tab(song.tab_for_line(0))

    def set_line(self, index: int, total_singable: int, order: int) -> None:
        self.lyrics_view.set_index(index)
        if self._song is not None:
            self.tab_panel.set_tab(self._song.tab_for_line(index))
        if total_singable:
            self.line_label.setText(f"Строка {order} из {total_singable}")
        else:
            self.line_label.setText("")

    def set_listening(self, listening: bool) -> None:
        self.mic_label.setText("Слушаю" if listening else "Микрофон выключен")
        self.level_bar.setVisible(listening)
        if not listening:
            self.level_bar.set_level(0.0)

    def set_level(self, level: float) -> None:
        self.level_bar.set_level(level)

    def set_recognized(self, text: str) -> None:
        # Показываем хвост: начало длинной фразы на сцене всё равно не читают
        self.recognized_label.setText(f"слышу: {text[-90:]}" if text else "")


class _LevelIndicator(QWidget):
    """Полоска уровня сигнала — видно, что микрофон действительно слышит."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._level = 0.0
        self.setFixedSize(110, 10)

    def set_level(self, level: float) -> None:
        self._level = max(0.0, min(1.0, level))
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        painter.setBrush(theme.BORDER)
        painter.drawRoundedRect(self.rect(), 5, 5)

        # Уровень растягиваем корнем: тихая речь иначе почти не видна
        width = int(self.width() * min(1.0, self._level ** 0.5 * 2.2))
        if width > 0:
            painter.setBrush(theme.SUCCESS if self._level > 0.02 else theme.TEXT_DIM)
            painter.drawRoundedRect(0, 0, width, self.height(), 5, 5)
        painter.end()
