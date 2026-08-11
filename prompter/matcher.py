"""Сопоставление распознанной речи со строками песни.

Это ядро автопрокрутки. Задача: по потоку слов от распознавателя понять, что
человек допел текущую строку (или уже ушёл вперёд), и сдвинуть окно текста.

Ключевые решения и почему именно так:

* **Порог сравнения слов зависит от длины слова.** Единый порог не работает:
  ``тебя``/``тебе`` и ``день``/``тень`` дают одинаковую близость, но первое — то же
  слово с испорченным окончанием, а второе — совершенно разные слова. Поэтому
  короткие слова сверяются точно, а длинные — нечётко.

* **Jaro-Winkler, а не обычное расстояние.** Он даёт бонус за совпадающий префикс,
  а распознавание пения портит именно окончания, оставляя начало слова целым.

* **LCS вместо «доли общих слов».** Метрики вида ``token_set_ratio`` игнорируют
  порядок и дали бы почти сто процентов на строке, спетой задом наперёд.
  Наибольшая общая подпоследовательность учитывает порядок.

* **Кандидаты берутся только из локального окна вокруг текущей строки.** Это же
  и решение проблемы припевов: одинаковые строки из других куплетов физически
  не попадают в рассмотрение.
"""

from __future__ import annotations

import re
import time
import unicodedata
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Sequence

from rapidfuzz.distance import JaroWinkler, Levenshtein

# Символы, которые надо выбросить до чистки пунктуации: мягкий перенос и
# невидимые разделители. Если оставить, они превратятся в пробел и разорвут слово.
_INVISIBLE = dict.fromkeys(
    map(ord, "­​‌‍⁠﻿"), None
)

# Апострофы удаляются, а не заменяются пробелом: иначе «don't» станет двумя
# токенами и пословное покрытие английской строки развалится.
_APOSTROPHES = dict.fromkeys(map(ord, "'’ʼ`"), None)

_PUNCT_RE = re.compile(r"[^\w\s]|_", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")

# Служебные слова, которые почти не несут смысла при сопоставлении: их
# распознаватель то теряет, то придумывает.
_STOPWORDS = frozenset(
    """
    и а но да же ли бы б то не ни в во на за по до от из с со к ко у о об обо
    для про при над под без через между я ты он она оно мы вы они мне тебе ему
    ей нам вам им меня тебя его ее её нас вас их мой моя мое моё твой твоя это
    эта этот эти тот та те как что чем чтобы кто где когда там тут вот уж ведь
    the a an and or but if of in on at to for from by with as is are was were
    be been am do does did not no nor so yet i you he she it we they me him her
    us them my your his its our their this that these those there here what
    """.split()
)

# Пороги близости для нечёткого сравнения слов, подобранные под ошибки ASR
_SHORT_WORD_LEN = 3  # такие слова сверяются только точно
_MEDIUM_WORD_LEN = 5
_MEDIUM_THRESHOLD = 0.88
_LONG_THRESHOLD = 0.85
_PREFIX_LEN = 3

_MAX_HISTORY = 64  # сколько слов потока держим в памяти

# Когда значимых слов остаётся меньше трёх или меньше сорока процентов строки,
# отсев служебных приносит больше вреда, чем пользы
_MIN_MEANINGFUL_WORDS = 3
_MIN_MEANINGFUL_SHARE = 0.4

# Начиная с трёх слов строке прощается хотя бы одно нераспознанное слово.
# На строках короче прощать нечего: там и так почти нет информации
_MIN_WORDS_FOR_FORGIVENESS = 3

# Выше этого требование к перескоку не поднимается ни при каких настройках.
# Совпадение в единицу означало бы, что распознаватель не ошибся ни разу —
# при пении такого практически не бывает
_MAX_JUMP_THRESHOLD = 0.85


def normalize_text(text: str, fold_yo: bool = True) -> str:
    """Привести текст к виду, пригодному для сравнения.

    Порядок операций важен:

    1. NFC — склеивает «е» с комбинирующим умляутом в настоящую «ё».
       Именно NFC, а не NFKC: тот портит символы, превращая «№5» в «no5».
    2. Удаление невидимых символов — до чистки пунктуации.
    3. ``casefold`` — правильнее ``lower`` для многоязычного текста.
    4. Только после этого «ё» → «е», иначе декомпозированная форма не поймается.

    ``fold_yo=False`` оставляет «ё» на месте. Это нужно для словаря
    распознавателя: модель хранит слова именно с «ё» и молча выбрасывает
    те, где вместо неё стоит «е».
    """
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_INVISIBLE).translate(_APOSTROPHES)
    text = text.casefold()
    if fold_yo:
        text = text.replace("ё", "е")
    text = _PUNCT_RE.sub(" ", text)
    return _SPACES_RE.sub(" ", text).strip()


def tokenize(text: str, fold_yo: bool = True) -> list[str]:
    """Разбить текст на нормализованные слова."""
    normalized = normalize_text(text, fold_yo=fold_yo)
    return normalized.split() if normalized else []


def significant_words(words: Sequence[str]) -> list[str]:
    """Оставить только значимые слова.

    Отсев служебных слов помогает, пока их в строке меньшинство. Но строки,
    целиком собранные из местоимений и предлогов, встречаются сплошь и рядом,
    и там отсев выкашивает почти всё: от восьми слов остаётся два коротких,
    сверять не по чему, и суфлёр на такой строке застревает.

    Поэтому если значимых слов слишком мало — по числу или по доле от строки —
    сверяемся по всем словам. Служебные слова распознаватель обычно слышит
    не хуже прочих, особенно когда словарь ограничен словами песни.
    """
    meaningful = [w for w in words if len(w) > 1 and w not in _STOPWORDS]
    if len(meaningful) < _MIN_MEANINGFUL_WORDS:
        return list(words)
    if words and len(meaningful) / len(words) < _MIN_MEANINGFUL_SHARE:
        return list(words)
    return meaningful


_LATIN_RE = re.compile(r"^[a-z]+$")

# Буквосочетания английского, которые читаются не так, как пишутся.
# Порядок важен: длинные сочетания разбираются раньше коротких.
_EN_DIGRAPHS = (
    ("ough", "u"), ("augh", "af"), ("tion", "sn"), ("sion", "sn"),
    # «th» сводим к «s»: межзубного звука в русском нет, и поющие
    # по-английски произносят его как «с» или «з» — самая частая подмена
    ("ph", "f"), ("ck", "k"), ("ch", "c"), ("sh", "s"), ("th", "s"),
    ("wh", "v"), ("qu", "kv"), ("kn", "n"), ("wr", "r"), ("gh", ""),
    ("mb", "m"), ("ng", "n"), ("x", "ks"), ("ce", "se"), ("ci", "si"),
)

# Согласные, которые у неносителей сливаются: звонкие оглушаются,
# межзубные превращаются в свистящие, «w» читается как «в»
_EN_FOLD = str.maketrans(
    {"w": "v", "z": "s", "j": "d", "y": "i", "q": "k",
     "g": "k", "b": "p", "d": "t", "v": "f", "c": "k"}
)

_VOWELS = "aeiou"
_MIN_PHONETIC_LEN = 5
_MIN_PHONETIC_KEY = 3

# Порог для слов с разным первым звуком: там нужна почти полная уверенность
_DIFFERENT_START_THRESHOLD = 0.93


def phonetic_key(word: str) -> str:
    """Огрубить английское слово до «звукового скелета».

    Нужно для тех, кто поёт по-английски без родного произношения: ``think``
    превращается в ``синк``, ``the`` в ``зэ``, окончания глотаются. Побуквенное
    сравнение такие слова не узнаёт, а скелет из согласных — узнаёт.

    Применяется только к длинным словам: у коротких скелеты слишком часто
    совпадают у совершенно разных слов.
    """
    if not _LATIN_RE.match(word):
        return ""

    result = word
    for source, target in _EN_DIGRAPHS:
        result = result.replace(source, target)
    result = result.translate(_EN_FOLD)

    # Гласные, кроме первой буквы, выбрасываем: именно они страдают от акцента
    if result:
        result = result[0] + "".join(ch for ch in result[1:] if ch not in _VOWELS)

    # Схлопываем повторы: удвоенные согласные на слух не различаются
    сжатый: list[str] = []
    for char in result:
        if not сжатый or сжатый[-1] != char:
            сжатый.append(char)
    return "".join(сжатый)


def words_match(expected: str, heard: str) -> bool:
    """Считать ли распознанное слово совпадающим с ожидаемым.

    Порог зависит от длины ожидаемого слова — в этом весь смысл. Одна замена
    буквы в трёхбуквенном слове даёт ту же близость, что и потерянное окончание
    в длинном, но смысл у этих двух случаев противоположный.
    """
    if expected == heard:
        return True

    length = len(expected)
    if length <= _SHORT_WORD_LEN:
        return False  # короткие слова — только точно

    similarity = JaroWinkler.normalized_similarity(expected, heard)
    if length <= _MEDIUM_WORD_LEN:
        if similarity >= _MEDIUM_THRESHOLD:
            return True
        # Запасной путь: целое начало слова плюс не больше одной правки в хвосте
        return (
            expected[:_PREFIX_LEN] == heard[:_PREFIX_LEN]
            and Levenshtein.distance(expected, heard) <= 1
        )

    if similarity >= _LONG_THRESHOLD:
        # Разное начало слова — повод насторожиться: «better» и «letter»
        # формально близки, но это разные слова, а вот начало распознаватель
        # почти всегда слышит верно
        if expected[0] == heard[0] or similarity >= _DIFFERENT_START_THRESHOLD:
            return True

    # Последняя попытка для английского: сравниваем на слух, а не по буквам.
    # Спасает, когда поют с сильным акцентом и распознаватель пишет мимо.
    return _phonetically_close(expected, heard)


def _phonetically_close(expected: str, heard: str) -> bool:
    """Совпадают ли слова на слух, с точностью до одного звука.

    Точного равенства скелетов мало: акцент искажает не только гласные.
    Поэтому допускаем одну правку — но лишь у достаточно длинных слов
    и достаточно длинных скелетов, иначе начнут совпадать разные слова.
    """
    if len(expected) < _MIN_PHONETIC_LEN or len(heard) < _MIN_PHONETIC_LEN:
        return False

    ключ_ожидаемого = phonetic_key(expected)
    ключ_услышанного = phonetic_key(heard)
    if len(ключ_ожидаемого) < _MIN_PHONETIC_KEY or len(ключ_услышанного) < _MIN_PHONETIC_KEY:
        return False
    if ключ_ожидаемого == ключ_услышанного:
        return True

    # Первый звук должен совпадать — иначе это просто разные слова
    if ключ_ожидаемого[0] != ключ_услышанного[0]:
        return False
    return Levenshtein.distance(ключ_ожидаемого, ключ_услышанного) <= 1


def fuzzy_lcs(expected: Sequence[str], heard: Sequence[str]) -> int:
    """Длина наибольшей общей подпоследовательности при нечётком равенстве слов.

    Обычная динамика по таблице. Строки короткие (единицы слов), поэтому
    квадратичная сложность здесь ничего не стоит.
    """
    if not expected or not heard:
        return 0

    previous = [0] * (len(heard) + 1)
    for exp_word in expected:
        current = [0]
        for index, heard_word in enumerate(heard):
            if words_match(exp_word, heard_word):
                current.append(previous[index] + 1)
            else:
                current.append(max(previous[index + 1], current[index]))
        previous = current
    return previous[-1]


@dataclass
class MatcherConfig:
    """Настройки сопоставления. Всё, что имеет смысл крутить, вынесено сюда."""

    threshold: float = 0.6
    """Доля значимых слов строки, при которой считаем строку спетой."""

    jump_margin: float = 0.15
    """Насколько увереннее должна быть следующая строка, чтобы прыгнуть на неё."""

    buffer_size: int = 12
    """Сколько последних распознанных слов участвуют в сравнении."""

    lookahead: int = 2
    """На сколько строк вперёд разрешено заглядывать.

    Двух хватает, чтобы догнать человека, когда строку не распознали: он поёт
    следующую, а суфлёр видит её в кандидатах. Трёх уже нельзя — на замерах
    повтор припева начинал утаскивать в конец песни.
    """

    cooldown_sec: float = 0.35
    """Пауза после перехода — защита от двойного срабатывания."""


@dataclass
class MatchDecision:
    """Решение матчера о переходе."""

    new_index: int
    score: float
    matched_line: int
    """Строка, по которой сработало совпадение (может отличаться от новой позиции)."""

    song_finished: bool = False
    """Спета последняя строка песни — пора переходить к следующей в очереди."""


@dataclass
class LineMatcher:
    """Следит за потоком распознанных слов и решает, когда листать текст.

    Работает с полным списком строк песни, включая пустые и проигрыши: позиция
    снаружи — это индекс в этом же списке, поэтому интерфейсу не нужно ничего
    пересчитывать. Внутри матчер сам знает, какие строки можно спеть.
    """

    lines: Sequence[str]
    config: MatcherConfig = field(default_factory=MatcherConfig)
    navigable: Sequence[int] | None = None
    """Строки, на которых суфлёр останавливается, включая заголовки блоков.

    Сопоставлять с голосом их нельзя — спеть «Припев» никто не будет, — но и
    проскакивать молча неправильно: человек должен видеть, что начался новый
    блок. Уводит с такой строки таймер, а не голос.
    """

    def __post_init__(self) -> None:
        self._line_words: list[list[str]] = [
            significant_words(tokenize(line)) for line in self.lines
        ]
        # Индексы строк, по которым вообще можно сопоставлять
        self._singable: list[int] = [
            i for i, words in enumerate(self._line_words) if words
        ]
        self._navigable: list[int] = (
            sorted(self.navigable) if self.navigable is not None else list(self._singable)
        )
        self._position: int = self._navigable[0] if self._navigable else 0
        self._committed: list[str] = []  # слова из завершённых фраз
        self._tentative: list[str] = []  # слова из текущей незавершённой гипотезы
        self._consumed: int = 0  # сколько слов потока уже «съедено» переходами
        self._last_advance: float = 0.0

    # --- Состояние ---------------------------------------------------------

    @property
    def position(self) -> int:
        return self._position

    def set_position(self, index: int) -> None:
        """Задать позицию извне (ручное листание, начало песни, смена трека).

        Весь накопленный поток слов при этом обесценивается: он относился
        к другому месту песни.
        """
        self._position = index
        self._consumed = len(self._committed) + len(self._tentative)
        self._last_advance = time.monotonic()

    def reset(self) -> None:
        """Полный сброс — при смене песни."""
        self._committed.clear()
        self._tentative.clear()
        self._consumed = 0
        self._position = self._navigable[0] if self._navigable else 0
        self._last_advance = 0.0

    @property
    def heard_words(self) -> list[str]:
        """Слова, участвующие в сравнении прямо сейчас (для отладочного лога)."""
        stream = self._committed + self._tentative
        return stream[self._consumed :][-self.config.buffer_size :]

    # --- Приём распознавания ----------------------------------------------

    def feed_partial(self, text: str) -> MatchDecision | None:
        """Скормить промежуточную гипотезу распознавателя.

        Промежуточный результат каждый раз приходит целиком с начала фразы,
        поэтому он не добавляется, а заменяет неподтверждённый хвост потока.
        """
        self._tentative = tokenize(text)
        self._подрезать_отметку()
        return self._decide()

    def feed_final(self, text: str) -> MatchDecision | None:
        """Скормить окончательный результат распознавания фразы."""
        self._committed.extend(tokenize(text))
        self._tentative = []
        self._trim_history()
        self._подрезать_отметку()
        return self._decide()

    def _подрезать_отметку(self) -> None:
        """Не дать отметке «уже использовано» уйти за конец потока.

        Распознаватель сперва выдаёт гипотезу подлиннее, а потом уточняет и
        укорачивает её. Отметка ставилась по длине потока в момент перехода,
        и после укорочения оказывалась за его концом — матчер переставал
        видеть хоть что-нибудь и застревал намертво после первой же строки.
        """
        поток = len(self._committed) + len(self._tentative)
        if self._consumed > поток:
            self._consumed = поток

    def _trim_history(self) -> None:
        """Не давать потоку слов расти бесконечно."""
        overflow = len(self._committed) - _MAX_HISTORY
        if overflow > 0:
            self._committed = self._committed[overflow:]
            self._consumed = max(0, self._consumed - overflow)

    # --- Принятие решения --------------------------------------------------

    def _decide(self) -> MatchDecision | None:
        """Решить, надо ли сдвинуть окно, и куда именно."""
        if not self._singable:
            return None
        if time.monotonic() - self._last_advance < self.config.cooldown_sec:
            return None

        heard = self.heard_words
        if not heard:
            return None

        candidates = self._candidates()
        if not candidates:
            return None

        порог = self._порог_для(candidates[0])
        current_score = self._score(candidates[0], heard)
        target: int | None = None
        target_score = 0.0
        matched_line = candidates[0]

        # Текущая строка спета — показываем следующую
        if current_score >= порог:
            if self._position != candidates[0]:
                # Стоим на служебной строке, а поют уже следующую за ней —
                # переходим на неё саму, а не через неё
                self._position = candidates[0]
                self._consumed = len(self._committed) + len(self._tentative)
                self._last_advance = time.monotonic()
                return MatchDecision(
                    new_index=candidates[0], score=current_score, matched_line=candidates[0]
                )

            following = self._next_singable(candidates[0])
            if following == candidates[0]:
                # Дальше петь нечего: это была последняя строка песни
                self._last_advance = time.monotonic()
                self._consumed = len(self._committed) + len(self._tentative)
                return MatchDecision(
                    new_index=self._position,
                    score=current_score,
                    matched_line=candidates[0],
                    song_finished=True,
                )
            target = following
            target_score = current_score

        # Человек ушёл вперёд: слова следующей строки узнаются увереннее.
        # Чем дальше строка, тем выше требование — иначе легко проскочить.
        общие = set(self._line_words[candidates[0]])
        for offset, candidate in enumerate(candidates[1:], start=1):
            # Потолок обязателен: без него надбавка за дальность быстро уводит
            # требование выше единицы, и перескок становится невозможен в
            # принципе — человек поёт дальше, а суфлёр стоит на месте
            required = min(
                self._порог_для(candidate) + self.config.jump_margin * offset,
                _MAX_JUMP_THRESHOLD,
            )
            # Слова, общие с текущей строкой, в пользу прыжка не считаем.
            # Иначе строка, начинающаяся так же, как кончается предыдущая,
            # «узнаётся» ещё до того, как её начали петь, — и пропускается
            score = self._score(candidate, heard, exclude=общие)
            if score >= required and score > current_score:
                target = candidate
                target_score = score
                matched_line = candidate

        if target is None or target == self._position:
            return None

        self._position = target
        self._consumed = len(self._committed) + len(self._tentative)
        self._last_advance = time.monotonic()
        return MatchDecision(new_index=target, score=target_score, matched_line=matched_line)

    def _candidates(self) -> list[int]:
        """Текущая поющаяся строка и несколько следующих."""
        start = bisect_left(self._singable, self._position)
        return self._singable[start : start + 1 + self.config.lookahead]

    def _next_singable(self, index: int) -> int:
        """Следующая строка, на которой надо остановиться.

        Это может быть и заголовок блока: пропускать его молча нельзя, иначе
        человек не увидит, что начался припев. Дальше его уведёт таймер.
        """
        position = bisect_left(self._navigable, index)
        if position < len(self._navigable) and self._navigable[position] == index:
            position += 1
        if position < len(self._navigable):
            return self._navigable[position]
        return index

    def _порог_для(self, line_index: int) -> float:
        """Порог совпадения для конкретной строки.

        Заданный процент на коротких строках превращается в требование
        идеального распознавания: при пороге 75 % строка из трёх слов требует
        все три, потому что округление съедает весь запас. Достаточно одной
        осечки распознавателя — и такая строка не сработает никогда.

        Поэтому хотя бы одно слово прощаем всегда: для строки из ``N`` слов
        порог не выше ``(N-1)/N``. На длинных строках это ничего не меняет.
        """
        слов = len(self._line_words[line_index])
        if слов < _MIN_WORDS_FOR_FORGIVENESS:
            return self.config.threshold
        return min(self.config.threshold, (слов - 1) / слов)

    def _score(
        self, line_index: int, heard: Sequence[str], exclude: set[str] | None = None
    ) -> float:
        """Доля значимых слов строки, найденных в потоке с сохранением порядка.

        ``exclude`` убирает из рассмотрения слова, которые ничего не доказывают —
        например, общие с текущей строкой при оценке прыжка вперёд.
        """
        expected = self._line_words[line_index]
        if exclude:
            expected = [word for word in expected if word not in exclude]
        if not expected:
            return 0.0
        return fuzzy_lcs(expected, heard) / len(expected)

    def debug_scores(self) -> list[tuple[int, float]]:
        """Оценки кандидатов — для отладочной панели интерфейса."""
        heard = self.heard_words
        return [(index, self._score(index, heard)) for index in self._candidates()]
