"""Разбор аккордовых листов в структуру :class:`~prompter.models.Song`.

Поддерживаются три формата:

1. **Классический** — «строка аккордов над строкой слов», как на сайтах с подборами::

       Am        C        G
       первая строка текста

2. **ChordPro** (``.pro``, ``.cho``) — аккорды в квадратных скобках прямо в тексте::

       {title: Название}
       [Am]первая строка [C]текста

3. **Разметка Ultimate Guitar** — ``[ch]Am[/ch]`` и блоки ``[tab]…[/tab]``.
   Приводится к классическому формату и разбирается им же.

Главная тонкость всех трёх: ведущие пробелы значимы, потому что аккорд стоит ровно
над нужным слогом. Поэтому нигде не применяется ``strip()`` к началу строки —
только аккуратный общий сдвиг влево в самом конце, с пересчётом позиций аккордов.
"""

from __future__ import annotations

import html
import json
import re

from .models import ChordMark, Song, SongLine

# --- Распознавание аккордов -------------------------------------------------

# Тоника: латинские A–G плюс H (немецкая нотация, обычная для рунета).
# Дальше — качество аккорда, цифровая надстройка и опциональный бас после «/».
_CHORD_RE = re.compile(
    r"""^
    [A-H][#b♯♭]?
    (?:maj|Maj|MAJ|min|Min|MIN|aug|dim|sus|add|alt|m|M|\+|-|°|o|ø|Δ)?
    \d{0,2}
    (?:(?:maj|min|sus|add|no|omit|b|\#|\+|-)\d{1,2})*
    (?:\([^()]{1,12}\))?
    (?:/[A-H][#b♯♭]?)?
    $""",
    re.VERBOSE,
)

# Служебные пометки, которые встречаются в строке аккордов наравне с аккордами
_SERVICE_TOKENS = frozenset(
    {"|", "||", "|:", ":|", "/", "//", "-", "--", "%", ".", "*", "(", ")",
     "n.c.", "nc", "N.C.", "х2", "х3", "х4"}
)
_REPEAT_RE = re.compile(r"^\(?[xх]\s*\d{1,2}\)?$|^\(?\d{1,2}\s*[xх]\)?$", re.IGNORECASE)

# Названия блоков песни. Двоеточие после них ставят не всегда: в подборах
# сплошь и рядом встречается просто «Verse» или «SOLO» отдельной строкой.
# Составные названия пишут и через дефис, и через пробел, и слитно:
# «Pre-chorus», «Pre chorus», «PRECHORUS» — всё это один и тот же блок.
_SECTION_WORDS = (
    r"(?:пред|пост)?[\s-]?припев|припев|куплет|проигрыш|бридж|вступление|кода|"
    r"интро|аутро|соло|запев|переход|окончание|финал|вступ|отыгрыш|связка|"
    r"бой|перебор|инструментал\w*|распев|речитатив|"
    r"(?:pre|post)[\s-]?chorus|verse|chorus|bridge|intro|outro|solo|refrain|"
    r"interlude|instrumental|coda|hook|breakdown|riff|tab|vamp|turnaround|"
    r"ending|middle[\s-]?8|tag"
)

_SECTION_RE = re.compile(
    rf"""^\s*(?:
        \[(?P<bracket>[^\]]{{1,40}})\]
        |
        (?P<plain>(?:{_SECTION_WORDS})[^\n:]{{0,24}})\s*[:：]
        |
        # Тот же заголовок, но без двоеточия. Кроме номера блока допускаем
        # указание длительности: «Вступление 8 сек», «Проигрыш x2»
        (?P<bare>(?:{_SECTION_WORDS})
            (?:[\s№#]*[xх*]?\s*\d{{1,3}})?
            (?:\s*(?:сек\w*|секунд\w*|раз\w*|повтор\w*|такт\w*))?
            (?:\s*\([^)]{{1,20}}\))?
        )
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# Указание длительности в конце строки. Множитель пишут и до числа («x2»),
# и после него («2 раза»), поэтому ловим оба варианта.
_TIMING_RE = re.compile(
    r"(?P<before>[xх*])?\s*(?P<number>\d{1,3})\s*"
    r"(?P<after>сек\w*|s\b|с\b|раз\w*|повтор\w*|[xх])?\s*\)?\s*$",
    re.IGNORECASE,
)

# Сколько секунд считать за один повтор, когда указано «x2» вместо секунд
_SECONDS_PER_REPEAT = 4.0
_MAX_SECTION_DELAY = 60.0

# Блоки, которые принято нумеровать: у них голое число в конце — это порядковый
# номер, а не длительность. «Куплет 2» — второй куплет, а не две секунды.
_NUMBERED_SECTIONS = re.compile(
    r"^\s*(?:куплет|припев|verse|chorus|part|часть)\b", re.IGNORECASE
)

# Разметка Ultimate Guitar
_UG_TAB_RE = re.compile(r"\[/?tab\]")
_UG_CH_RE = re.compile(r"\[ch\](.*?)\[/ch\]", re.DOTALL)
_UG_STORE_RE = re.compile(r'<div[^>]*\bclass="js-store"[^>]*\bdata-content="([^"]*)"', re.DOTALL)

# ChordPro
_CHORDPRO_DIRECTIVE_RE = re.compile(r"^\s*\{\s*([a-zA-Z_]+)\s*:?\s*([^}]*)\}\s*$", re.MULTILINE)
_CHORDPRO_BRACKET_RE = re.compile(r"\[([^\[\]]{1,20})\]")

_MAX_CHORD_LEN = 14

# Символы, из которых состоит строка ASCII-табулатуры: номера ладов, тактовые
# черты и обозначения приёмов (h — hammer-on, p — pull-off, b — bend, ~ — вибрато)
_TAB_CHARS = frozenset("-|:0123456789hpbrsvxX~/\\()^*. ")
_MIN_TAB_DASHES = 3
_MIN_TAB_SHARE = 0.85
_MIN_TAB_BARS = 2  # столько тактовых черт достаточно, чтобы счесть строку схемой


def is_chord_token(token: str) -> bool:
    """Похож ли токен на аккорд (``Am``, ``F#m7/C#``, ``Csus4``)."""
    if not token or len(token) > _MAX_CHORD_LEN:
        return False
    return bool(_CHORD_RE.match(token))


def _is_service_token(token: str) -> bool:
    """Служебная пометка в строке аккордов: тактовая черта, «x2» и подобное."""
    return token in _SERVICE_TOKENS or bool(_REPEAT_RE.match(token))


def is_chord_line(line: str) -> bool:
    """Является ли строка строкой аккордов, а не текстом песни.

    Считаем аккордовой, если все токены — аккорды или служебные пометки,
    и хотя бы один настоящий аккорд есть.

    Отдельный случай — строка из одного односимвольного токена. Строчная «a»
    в английском тексте почти всегда артикль, и приняв её за аккорд, мы бы съели
    целую строку слов. А вот заглавные «C», «G», «F» на отдельной строке — это
    почти наверняка аккорд: артикль с заглавной буквы отдельной строкой не пишут.
    """
    tokens = line.split()
    if not tokens:
        return False
    if len(tokens) == 1 and len(tokens[0]) == 1 and tokens[0].islower():
        return False

    chords = 0
    for token in tokens:
        if is_chord_token(token):
            chords += 1
        elif not _is_service_token(token):
            return False
    return chords > 0


def is_tab_line(line: str) -> bool:
    """Строка табулатуры или схемы боя вроде ``e|---3---5---|`` или ``| v ^ v |``.

    Опознаём по двум признакам. Первый — тактовые черты: две и больше
    вертикальных черты в строке текст песни не даёт практически никогда, зато
    ими размечены и табы, и схемы боя. Этот признак работает даже когда рядом
    приписан комментарий вроде «(x2)», из-за которого доля букв в строке
    становится слишком большой для второго признака.

    Второй — состав символов: дефисы и номера ладов у табулатуры без подписей.
    """
    stripped = line.strip()
    if len(stripped) < 4:
        return False

    # Тактовые черты: надёжный признак и для табов, и для схем боя
    if stripped.count("|") >= _MIN_TAB_BARS and re.search(r"[-–—↓↑vV^x0-9]", stripped):
        return True

    if stripped.count("-") < _MIN_TAB_DASHES:
        return False

    # У строки табулатуры может быть подпись струны в начале — она не мешает
    body = stripped
    if len(body) > 1 and body[0] in "eEbBgGdDaA" and body[1] in "|:-":
        body = body[1:]

    tab_chars = sum(1 for char in body if char in _TAB_CHARS)
    return tab_chars / len(body) >= _MIN_TAB_SHARE


# Короткая строка, заканчивающаяся двоеточием, — почти наверняка заголовок
# блока: «Вступление:», «Соло гитары:», «Кода:». Строки песни двоеточием
# заканчиваются крайне редко, а вот названий блоков в подборах не счесть.
_MAX_COLON_HEADER_CHARS = 32
_MAX_COLON_HEADER_WORDS = 4


def _looks_like_colon_header(line: str) -> str | None:
    """Заголовок, опознанный по двоеточию в конце.

    Ограничения по длине нужны, чтобы не съесть строку песни: короткая фраза
    с двоеточием — это подпись к блоку, а длинная — уже часть текста.
    """
    stripped = line.strip()
    if not stripped.endswith((":", "：")):
        return None

    name = stripped[:-1].strip()
    if not name or len(name) > _MAX_COLON_HEADER_CHARS:
        return None
    if len(name.split()) > _MAX_COLON_HEADER_WORDS:
        return None
    # Строка аккордов с двоеточием — это не заголовок
    if is_chord_line(name):
        return None
    return name


def parse_section_header(line: str) -> str | None:
    """Вернуть название блока, если строка — заголовок вида «Припев:» или «[Chorus]».

    ``[Am]`` заголовком не считается: это аккорд в формате ChordPro.
    """
    match = _SECTION_RE.match(line)
    if match:
        name = match.group("bracket") or match.group("plain") or match.group("bare") or ""
        name = name.strip()
        if name and not is_chord_token(name):
            return name

    # Заголовок может быть каким угодно — важно двоеточие в конце
    return _looks_like_colon_header(line)


def parse_section_timing(text: str) -> float | None:
    """Сколько секунд держать служебную строку, если это указано в её конце.

    В подборах длительность вступлений и проигрышей пишут по-разному:
    «Вступление 8 сек», «Проигрыш (x2)», просто «Вступление 4». Число без
    единицы считаем секундами, а повторы переводим в секунды по такту.
    """
    stripped = text.strip()
    match = _TIMING_RE.search(stripped)
    if not match:
        return None

    number = int(match.group("number"))
    after = (match.group("after") or "").lower()
    явная_единица = bool(after)
    повторы = bool(match.group("before")) or after.startswith(("раз", "повтор", "x", "х"))

    # У нумеруемых блоков голое число — порядковый номер, а не длительность
    if not явная_единица and not match.group("before") and _NUMBERED_SECTIONS.match(stripped):
        return None

    seconds = number * _SECONDS_PER_REPEAT if повторы else float(number)

    return min(seconds, _MAX_SECTION_DELAY) if seconds > 0 else None


def extract_chords(line: str) -> list[ChordMark]:
    """Достать аккорды из строки аккордов вместе с их позициями в символах."""
    marks: list[ChordMark] = []
    for match in re.finditer(r"\S+", line):
        token = match.group()
        if is_chord_token(token) or _is_service_token(token):
            marks.append(ChordMark(name=token, position=match.start()))
    return marks


# --- Классический формат «аккорды над словами» ------------------------------


def parse_chord_sheet(text: str) -> list[SongLine]:
    """Разобрать аккордовый лист в формате «строка аккордов над строкой слов»."""
    raw_lines = _normalize_newlines(text).split("\n")
    result: list[SongLine] = []
    index = 0

    while index < len(raw_lines):
        line = raw_lines[index].rstrip()

        if not line.strip():
            _append_blank(result)
            index += 1
            continue

        # Заголовок блока — отдельная строка потока: на ней суфлёр
        # ненадолго останавливается и уходит дальше по таймеру
        section = parse_section_header(line)
        if section is not None:
            result.append(SongLine(section=section))
            index += 1
            continue

        # Табулатуру и схемы боя собираем целым блоком:
        # по отдельности их строки бессмысленны
        if is_tab_line(line):
            tab_block: list[str] = []
            while index < len(raw_lines) and is_tab_line(raw_lines[index]):
                tab_block.append(raw_lines[index].rstrip())
                index += 1
            result.append(SongLine(tab_lines=tab_block))
            continue

        if is_chord_line(line):
            chords = extract_chords(line)
            following = raw_lines[index + 1] if index + 1 < len(raw_lines) else None
            if _is_lyric_line(following):
                result.append(SongLine(text=following.rstrip(), chords=chords))
                index += 2
            else:
                # Аккорды без слов — проигрыш
                result.append(SongLine(text="", chords=chords))
                index += 1
            continue

        result.append(SongLine(text=line))
        index += 1

    return _dedent(_trim_blanks(result))


def _is_lyric_line(line: str | None) -> bool:
    """Годится ли строка на роль текста под аккордами."""
    if line is None or not line.strip():
        return False
    return not is_chord_line(line) and parse_section_header(line) is None


def _append_blank(lines: list[SongLine]) -> None:
    """Добавить пустую строку, схлопывая подряд идущие пустые в одну."""
    if lines and lines[-1].is_blank:
        return
    lines.append(SongLine())


def _trim_blanks(lines: list[SongLine]) -> list[SongLine]:
    """Убрать пустые строки в начале и в конце песни."""
    start, end = 0, len(lines)
    while start < end and lines[start].is_blank:
        start += 1
    while end > start and lines[end - 1].is_blank:
        end -= 1
    return lines[start:end]


def _dedent(lines: list[SongLine]) -> list[SongLine]:
    """Убрать общий левый отступ, пересчитав позиции аккордов.

    Сайты часто отдают текст с отступом в несколько пробелов. Сдвигаем всё
    влево на общий минимум, иначе на экране слева останется пустая полоса.
    """
    indents = [
        len(line.text) - len(line.text.lstrip())
        for line in lines
        if line.text.strip()
    ]
    indents += [
        min(chord.position for chord in line.chords)
        for line in lines
        if line.chords and not line.text.strip()
    ]
    shift = min(indents) if indents else 0
    if shift <= 0:
        return lines

    for line in lines:
        if line.text.strip():
            line.text = line.text[shift:]
        line.chords = [
            ChordMark(name=chord.name, position=max(0, chord.position - shift))
            for chord in line.chords
        ]
    return lines


def _normalize_newlines(text: str) -> str:
    """Привести переводы строк к ``\\n``.

    Ultimate Guitar отдаёт исключительно ``\\r\\n``; если этого не сделать,
    хвостовой ``\\r`` попадёт в текст и сломает выравнивание аккордов.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --- ChordPro ---------------------------------------------------------------


def parse_chordpro(text: str) -> tuple[list[SongLine], dict[str, str]]:
    """Разобрать формат ChordPro. Возвращает строки и найденные метаданные."""
    meta: dict[str, str] = {}
    result: list[SongLine] = []
    pending_section: str | None = None

    for raw_line in _normalize_newlines(text).split("\n"):
        directive = _CHORDPRO_DIRECTIVE_RE.match(raw_line)
        if directive:
            key, value = directive.group(1).lower(), directive.group(2).strip()
            if key in ("title", "t"):
                meta["title"] = value
            elif key in ("artist", "subtitle", "st", "a"):
                meta.setdefault("artist", value)
            elif key in ("comment", "c", "ci", "comment_italic"):
                pending_section = value or pending_section
            elif key in ("start_of_chorus", "soc"):
                pending_section = value or "Припев"
            elif key in ("start_of_verse", "sov"):
                pending_section = value or "Куплет"
            elif key in ("start_of_bridge", "sob"):
                pending_section = value or "Бридж"
            elif key in ("capo",):
                meta["capo"] = value
            continue

        if not raw_line.strip():
            _append_blank(result)
            continue

        text_part, chords = _split_chordpro_line(raw_line)
        result.append(SongLine(text=text_part, chords=chords, section=pending_section))
        pending_section = None

    return _dedent(_trim_blanks(result)), meta


def _split_chordpro_line(line: str) -> tuple[str, list[ChordMark]]:
    """Разделить строку ChordPro на чистый текст и аккорды с позициями."""
    chords: list[ChordMark] = []
    plain: list[str] = []
    cursor = 0

    for match in _CHORDPRO_BRACKET_RE.finditer(line):
        plain.append(line[cursor : match.start()])
        position = sum(len(part) for part in plain)
        chords.append(ChordMark(name=match.group(1).strip(), position=position))
        cursor = match.end()

    plain.append(line[cursor:])
    return "".join(plain).rstrip(), chords


# --- Ultimate Guitar --------------------------------------------------------


def ultimate_guitar_to_chord_sheet(content: str) -> str:
    """Превратить контент таба Ultimate Guitar в классический аккордовый лист.

    Маркеры ``[ch]``/``[/ch]`` и ``[tab]``/``[/tab]`` просто удаляются: они лежат
    в той же строке, что и аккорды, поэтому после удаления строка аккордов
    сокращается ровно настолько, чтобы снова встать над нужными слогами.

    Заголовки блоков (``[Verse 1]``, ``[Chorus]``) — обычный текст в тех же
    квадратных скобках, поэтому вырезать теги можно только поимённо.
    """
    text = _normalize_newlines(content)
    text = _UG_TAB_RE.sub("", text)
    text = _UG_CH_RE.sub(lambda m: m.group(1), text)
    return text


def extract_ultimate_guitar_store(page_html: str) -> dict | None:
    """Достать JSON из ``<div class="js-store" data-content="…">`` страницы UG.

    Порядок операций критичен: сначала регулярка, потом ``html.unescape``,
    и только потом ``json.loads``. В обратном порядке разбор падает —
    в сыром атрибуте тысячи ``&quot;``.
    """
    match = _UG_STORE_RE.search(page_html)
    if not match:
        return None
    try:
        return json.loads(html.unescape(match.group(1)))
    except (json.JSONDecodeError, ValueError):
        return None


# --- Точка входа ------------------------------------------------------------


def detect_format(text: str) -> str:
    """Определить формат текста: ``ug``, ``chordpro`` или ``sheet``."""
    if "[ch]" in text or "[tab]" in text:
        return "ug"

    if _CHORDPRO_DIRECTIVE_RE.search(text):
        return "chordpro"

    # ChordPro без директив: аккорды в скобках внутри строк с текстом.
    bracketed = _CHORDPRO_BRACKET_RE.findall(text)
    chord_like = [token for token in bracketed if is_chord_token(token.strip())]
    if len(chord_like) >= 2:
        return "chordpro"

    return "sheet"


def parse_song_text(
    text: str,
    title: str = "",
    artist: str = "",
    source: str = "",
    source_url: str = "",
) -> Song:
    """Разобрать текст с аккордами в любом из поддерживаемых форматов.

    Формат определяется автоматически. Метаданные из ChordPro используются
    только там, где название и исполнитель не заданы явно.
    """
    fmt = detect_format(text)

    if fmt == "ug":
        lines = parse_chord_sheet(ultimate_guitar_to_chord_sheet(text))
        meta: dict[str, str] = {}
    elif fmt == "chordpro":
        lines, meta = parse_chordpro(text)
    else:
        lines = parse_chord_sheet(text)
        meta = {}

    capo: int | None = None
    if meta.get("capo", "").strip().isdigit():
        capo = int(meta["capo"].strip())

    return Song(
        title=title or meta.get("title", "") or "Без названия",
        artist=artist or meta.get("artist", ""),
        lines=lines,
        source=source,
        source_url=source_url,
        capo=capo,
    )
