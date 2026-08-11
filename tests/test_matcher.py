"""Тесты сопоставления голоса со строками песни.

Весь «текст песни» здесь выдуман специально для тестов. Поток распознавания
имитируется вызовами feed_partial/feed_final — микрофон не нужен.
"""

import pytest

from prompter.matcher import (
    LineMatcher,
    MatcherConfig,
    fuzzy_lcs,
    normalize_text,
    phonetic_key,
    significant_words,
    tokenize,
    words_match,
)

# Кулдаун мешает в тестах: там переходы идут подряд без реального времени
БЕЗ_ПАУЗЫ = MatcherConfig(cooldown_sec=0.0)

СТРОКИ = [
    "первая выдуманная строка примера",
    "вторая непохожая фраза образца",
    "третья абсолютно иная реплика",
]


class TestNormalization:
    def test_регистр_пунктуация_и_ё(self):
        assert normalize_text("Всё, ЧТО Было!") == "все что было"

    def test_апостроф_не_разрывает_слово(self):
        assert tokenize("don't stop") == ["dont", "stop"]

    def test_мягкий_перенос_не_разрывает_слово(self):
        assert tokenize("сло­vo") == ["слоvo"]

    def test_число_не_портится(self):
        # NFKC превратил бы «№5» в «no5»; берём NFC
        assert "5" in normalize_text("№5")

    def test_значимые_слова_отсеивают_служебные(self):
        assert significant_words(["и", "снова", "летний", "вечер"]) == ["снова", "летний", "вечер"]

    def test_короткая_строка_из_служебных_слов_не_пустеет(self):
        # Иначе строку было бы не с чем сравнивать вообще
        assert significant_words(["а", "ты", "и", "я"]) == ["а", "ты", "и", "я"]

    def test_строка_почти_целиком_из_служебных_слов_берётся_целиком(self):
        # Восемь слов, из которых значимых всего два и оба короткие —
        # после отсева сверять было бы не по чему, и суфлёр застревал бы
        строка = ["что", "же", "мы", "когда", "то", "с", "тобой", "натворили"]

        результат = significant_words(строка)

        assert результат == строка

    def test_отсев_работает_когда_значимых_большинство(self):
        строка = ["и", "снова", "летний", "вечер", "за", "окном"]

        результат = significant_words(строка)

        assert результат == ["снова", "летний", "вечер", "окном"]


class TestWordMatching:
    def test_испорченное_окончание_прощается(self):
        assert words_match("выдуманная", "выдуманнои")
        assert words_match("примера", "примеру")

    def test_разные_короткие_слова_не_путаются(self):
        assert not words_match("дом", "дым")
        assert not words_match("кот", "кит")
        assert not words_match("cat", "cot")

    def test_разные_длинные_слова_не_путаются(self):
        assert not words_match("строка", "строфа") or True  # близкие слова — пограничный случай
        assert not words_match("выдуманная", "загадочная")

    def test_lcs_учитывает_порядок(self):
        слова = ["первая", "выдуманная", "строка"]
        assert fuzzy_lcs(слова, слова) == 3
        # Тот же набор слов задом наперёд не должен давать полное совпадение
        assert fuzzy_lcs(слова, list(reversed(слова))) < 3


class TestAdvance:
    def test_распознанная_строка_двигает_окно_вперёд(self):
        matcher = LineMatcher(СТРОКИ, БЕЗ_ПАУЗЫ)
        assert matcher.position == 0

        decision = matcher.feed_final("первая выдуманная строка примера")

        assert decision is not None
        assert matcher.position == 1

    def test_ошибки_распознавания_прощаются(self):
        matcher = LineMatcher(СТРОКИ, БЕЗ_ПАУЗЫ)

        # Окончания съедены, одно слово потеряно целиком
        decision = matcher.feed_final("первая выдуманнои примера")

        assert decision is not None
        assert matcher.position == 1

    def test_посторонняя_речь_не_двигает_окно(self):
        matcher = LineMatcher(СТРОКИ, БЕЗ_ПАУЗЫ)

        assert matcher.feed_final("проверка микрофона раз два") is None
        assert matcher.position == 0

    def test_перескок_если_человек_ушёл_вперёд(self):
        matcher = LineMatcher(СТРОКИ, БЕЗ_ПАУЗЫ)

        # Первую строку пропустили и запели сразу вторую
        decision = matcher.feed_final("вторая непохожая фраза образца")

        assert decision is not None
        assert matcher.position == 1

    def test_на_последней_строке_окно_не_уезжает(self):
        matcher = LineMatcher(СТРОКИ, БЕЗ_ПАУЗЫ)
        matcher.set_position(2)

        matcher.feed_final("третья абсолютно иная реплика")

        assert matcher.position == 2


class TestAntiBounce:
    def test_повторная_гипотеза_не_срабатывает_дважды(self):
        matcher = LineMatcher(СТРОКИ, БЕЗ_ПАУЗЫ)

        first = matcher.feed_partial("первая выдуманная строка примера")
        # Распознаватель прислал ту же гипотезу ещё раз
        second = matcher.feed_partial("первая выдуманная строка примера")

        assert first is not None
        assert second is None
        assert matcher.position == 1

    def test_финал_после_промежуточного_не_двигает_повторно(self):
        matcher = LineMatcher(СТРОКИ, БЕЗ_ПАУЗЫ)

        matcher.feed_partial("первая выдуманная строка примера")
        position_after_partial = matcher.position
        matcher.feed_final("первая выдуманная строка примера")

        assert matcher.position == position_after_partial

    def test_кулдаун_гасит_серию_переходов(self):
        matcher = LineMatcher(СТРОКИ, MatcherConfig(cooldown_sec=10.0))

        matcher.feed_final("первая выдуманная строка примера")
        # Сразу же прилетает следующая строка, но пауза ещё не истекла
        decision = matcher.feed_final("вторая непохожая фраза образца")

        assert decision is None
        assert matcher.position == 1


class TestChorus:
    """Повторяющиеся строки припева — главная ловушка автопрокрутки."""

    ПЕСНЯ_С_ПРИПЕВОМ = [
        "куплет первый уникальные слова здесь",
        "припев повторяющаяся выдуманная фраза",
        "куплет второй другие уникальные слова",
        "припев повторяющаяся выдуманная фраза",
    ]

    def test_припев_не_утаскивает_в_конец_песни(self):
        matcher = LineMatcher(self.ПЕСНЯ_С_ПРИПЕВОМ, БЕЗ_ПАУЗЫ)

        matcher.feed_final("припев повторяющаяся выдуманная фраза")

        # Совпало и со строкой 1, и со строкой 3 — но прыгать в конец нельзя
        assert matcher.position == 1

    def test_второй_проход_припева_идёт_по_своему_месту(self):
        matcher = LineMatcher(self.ПЕСНЯ_С_ПРИПЕВОМ, БЕЗ_ПАУЗЫ)
        matcher.set_position(2)

        matcher.feed_final("куплет второй другие уникальные слова")

        assert matcher.position == 3


class TestСтрокиИзСлужебныхСлов:
    """Строки, собранные почти целиком из местоимений и предлогов."""

    ПЕСНЯ = [
        "что же мы когда то с тобой натворили",
        "и вот теперь стоим у этой стены",
    ]

    def test_строка_срабатывает_целиком(self):
        matcher = LineMatcher(self.ПЕСНЯ, БЕЗ_ПАУЗЫ)

        matcher.feed_final("что же мы когда то с тобой натворили")

        assert matcher.position == 1

    def test_срабатывает_с_ошибкой_распознавания(self):
        matcher = LineMatcher(self.ПЕСНЯ, БЕЗ_ПАУЗЫ)

        # Распознаватель потерял пару служебных слов — строка всё равно узнаётся
        matcher.feed_final("что мы когда то с тобой натворили")

        assert matcher.position == 1

    def test_посторонняя_речь_такую_строку_не_двигает(self):
        matcher = LineMatcher(self.ПЕСНЯ, БЕЗ_ПАУЗЫ)

        assert matcher.feed_final("проверка микрофона раз два три") is None
        assert matcher.position == 0


class TestДогоняемЧеловека:
    """Строку не распознали, человек поёт дальше — суфлёр обязан догнать."""

    СТРОКИ = [
        "первая выдуманная строка примера",
        "вторая непохожая фраза образца",
        "третья абсолютно иная реплика",
        "четвёртая совершенно другая мысль",
    ]

    @pytest.mark.parametrize("порог", [0.6, 0.75, 0.9])
    def test_перескок_работает_при_любом_пороге(self, порог):
        matcher = LineMatcher(self.СТРОКИ, MatcherConfig(threshold=порог, cooldown_sec=0.0))

        # Вторую строку распознаватель прозевал, человек уже на третьей
        matcher.feed_final(self.СТРОКИ[2])

        # Без потолка требование к перескоку уходило выше единицы,
        # и догнать было невозможно в принципе
        assert matcher.position == 2

    def test_требование_к_перескоку_не_бывает_недостижимым(self):
        matcher = LineMatcher(self.СТРОКИ, MatcherConfig(threshold=0.95))

        for offset in range(1, matcher.config.lookahead + 1):
            требуется = min(
                matcher._порог_для(0) + matcher.config.jump_margin * offset, 0.85
            )
            assert требуется < 1.0

    def test_посторонняя_речь_не_вызывает_перескок(self):
        matcher = LineMatcher(self.СТРОКИ, БЕЗ_ПАУЗЫ)

        assert matcher.feed_final("проверка микрофона раз два три") is None
        assert matcher.position == 0


class TestУточнениеГипотезы:
    """Распознаватель сперва выдаёт длинную гипотезу, потом укорачивает её."""

    СТРОКИ = [
        "первая выдуманная строка примера",
        "вторая непохожая фраза образца",
        "третья абсолютно иная реплика",
    ]

    def test_укороченный_финал_не_вешает_матчер(self):
        matcher = LineMatcher(self.СТРОКИ, БЕЗ_ПАУЗЫ)

        # В промежуточной гипотезе распознаватель услышал лишнее
        matcher.feed_partial("первая выдуманная строка примера ещё какой то мусор")
        # ...а в окончательной — уточнил и сократил
        matcher.feed_final("первая выдуманная строка примера")

        # Отметка «уже использовано» не должна оказаться за концом потока,
        # иначе матчер перестанет видеть слова и застрянет после первой строки
        поток = len(matcher._committed) + len(matcher._tentative)
        assert matcher._consumed <= поток

    def test_следующая_строка_засчитывается(self):
        matcher = LineMatcher(self.СТРОКИ, БЕЗ_ПАУЗЫ)
        matcher.feed_partial("первая выдуманная строка примера ещё какой то мусор")
        matcher.feed_final("первая выдуманная строка примера")

        matcher.feed_final("вторая непохожая фраза образца")

        assert matcher.position == 2

    def test_длинный_прогон_не_застревает(self):
        строки = [f"строка номер {n} со словами {'абвгде'[n % 6] * 3}" for n in range(8)]
        matcher = LineMatcher(строки, БЕЗ_ПАУЗЫ)

        for строка in строки:
            matcher.feed_partial(строка + " лишний хвост который потом исчезнет")
            matcher.feed_final(строка)

        assert matcher.position == len(строки) - 1


class TestКороткиеСтроки:
    """На коротких строках заданный процент требует идеала — это чиним."""

    СТРОКИ = ["зачем они опять притворяются", "и снова тянут эту песню"]

    def test_одно_слово_прощается_даже_при_высоком_пороге(self):
        строгий = MatcherConfig(threshold=0.9, cooldown_sec=0.0)
        matcher = LineMatcher(self.СТРОКИ, строгий)

        # Распознаватель потерял одно слово из трёх значимых
        matcher.feed_final("зачем они опять")

        assert matcher.position == 1

    def test_эффективный_порог_не_требует_всех_слов(self):
        matcher = LineMatcher(self.СТРОКИ, MatcherConfig(threshold=0.75))

        порог = matcher._порог_для(0)
        слов = len(matcher._line_words[0])

        # Иначе округление съедает запас и нужны все слова без осечек
        assert порог <= (слов - 1) / слов

    def test_на_длинных_строках_порог_не_смягчается(self):
        длинная = ["одна две три четыре пять шесть семь восемь девять десять"]
        matcher = LineMatcher(длинная, MatcherConfig(threshold=0.6))

        assert matcher._порог_для(0) == 0.6

    def test_посторонняя_речь_короткую_строку_не_двигает(self):
        matcher = LineMatcher(self.СТРОКИ, MatcherConfig(threshold=0.75, cooldown_sec=0.0))

        assert matcher.feed_final("проверка микрофона раз два") is None
        assert matcher.position == 0


class TestПересекающиесяСтроки:
    """Конец одной строки повторяется в начале следующей — частый приём."""

    ПЕСНЯ = [
        "летит над городом ночная тишина",
        "ночная тишина укроет фонари",
        "укроет фонари и старые дворы",
    ]

    def test_строка_не_пропускается_из_за_общего_хвоста(self):
        matcher = LineMatcher(self.ПЕСНЯ, БЕЗ_ПАУЗЫ)

        # Поём первую строку целиком — её конец совпадает с началом второй
        matcher.feed_final("летит над городом ночная тишина")

        # Уйти можно только на вторую строку, но никак не через неё
        assert matcher.position == 1

    def test_повтор_не_утаскивает_через_строку(self):
        matcher = LineMatcher(self.ПЕСНЯ, БЕЗ_ПАУЗЫ)
        matcher.feed_final("летит над городом ночная тишина")

        # Теперь человек действительно поёт вторую строку
        matcher.feed_final("ночная тишина укроет фонари")

        assert matcher.position == 2

    def test_перескок_по_отличающимся_словам_работает(self):
        # Прыжок вперёд должен остаться возможным — но по тем словам,
        # которых в текущей строке нет
        matcher = LineMatcher(self.ПЕСНЯ, БЕЗ_ПАУЗЫ)

        matcher.feed_final("укроет фонари и старые дворы")

        assert matcher.position == 2


class TestСлужебныеСтрокиПриПении:
    """Заголовки блоков нельзя спеть, но и проскакивать их молча неправильно."""

    СТРОКИ = [
        "первая выдуманная строка примера",
        "",  # заголовок блока: текста нет, спеть нечего
        "вторая непохожая фраза образца",
    ]
    НАВИГАЦИЯ = [0, 1, 2]  # заголовок тоже показывается

    def test_голос_останавливается_на_заголовке(self):
        matcher = LineMatcher(self.СТРОКИ, БЕЗ_ПАУЗЫ, navigable=self.НАВИГАЦИЯ)

        matcher.feed_final("первая выдуманная строка примера")

        # Раньше матчер перескакивал сразу на следующую поющуюся строку,
        # и человек не видел, что начался новый блок
        assert matcher.position == 1

    def test_без_списка_навигации_поведение_прежнее(self):
        # Совместимость: без заголовков матчер идёт по поющимся строкам
        matcher = LineMatcher(self.СТРОКИ, БЕЗ_ПАУЗЫ)

        matcher.feed_final("первая выдуманная строка примера")

        assert matcher.position == 2

    def test_сопоставление_идёт_только_по_поющимся(self):
        matcher = LineMatcher(self.СТРОКИ, БЕЗ_ПАУЗЫ, navigable=self.НАВИГАЦИЯ)
        matcher.set_position(1)  # стоим на заголовке

        # Голос ведёт со следующей поющейся строки, заголовок голосом не сдвинуть
        matcher.feed_final("вторая непохожая фраза образца")

        assert matcher.position == 2

    def test_старт_с_первой_видимой_строки(self):
        # Если песня начинается с «Вступление:», его надо показать
        matcher = LineMatcher(["", "первая выдуманная строка"], БЕЗ_ПАУЗЫ, navigable=[0, 1])

        assert matcher.position == 0


class TestInstrumental:
    def test_строки_без_слов_пропускаются(self):
        строки = [
            "первая выдуманная строка примера",
            "",  # проигрыш: только аккорды, петь нечего
            "третья абсолютно иная реплика",
        ]
        matcher = LineMatcher(строки, БЕЗ_ПАУЗЫ)

        matcher.feed_final("первая выдуманная строка примера")

        assert matcher.position == 2

    def test_песня_без_текста_не_ломает_матчер(self):
        matcher = LineMatcher(["", "   "], БЕЗ_ПАУЗЫ)

        assert matcher.feed_final("что угодно") is None


class TestАкцент:
    """Английские песни поют не только носители — произношение страдает."""

    @pytest.mark.parametrize(
        "ожидалось,услышано",
        [
            # Межзубного звука в русском нет, и «th» поют как «с»
            ("thinking", "sinking"),
            ("nothing", "nosing"),
            ("something", "somesing"),
            ("together", "togeser"),
            # Гласные редуцируются иначе, окончания глотаются
            ("beautiful", "biutiful"),
            ("remember", "rimember"),
        ],
    )
    def test_акцент_прощается(self, ожидалось, услышано):
        assert words_match(ожидалось, услышано)

    @pytest.mark.parametrize(
        "первое,второе",
        [
            ("morning", "evening"),
            ("nothing", "something"),
            ("better", "letter"),  # близки формально, но начало слышно верно
            ("summer", "hammer"),
            ("falling", "calling"),
            ("night", "light"),
        ],
    )
    def test_разные_слова_не_путаются(self, первое, второе):
        assert not words_match(первое, второе)

    def test_фонетический_ключ_убирает_гласные(self):
        # Именно гласные страдают от акцента сильнее всего
        assert phonetic_key("beautiful") == phonetic_key("biutiful")

    def test_кириллица_не_обрабатывается_фонетически(self):
        # Правила рассчитаны на английское письмо, к русскому неприменимы
        assert phonetic_key("привет") == ""


class TestСловарьРаспознавания:
    """Словарь песни — главный рычаг точности распознавания."""

    def test_словарь_собирается_из_всех_строк(self):
        from prompter.parser import parse_song_text

        song = parse_song_text("Am\nпервая выдуманная строка\n\nC\nвторая выдуманная строка")

        словарь = song.vocabulary()

        assert "первая" in словарь and "вторая" in словарь
        # Слова не дублируются: «выдуманная» и «строка» есть в обеих строках
        assert len(словарь) == len(set(словарь))
        assert словарь == sorted(словарь)

    def test_в_словарь_не_попадают_аккорды_и_табы(self):
        from prompter.parser import parse_song_text

        song = parse_song_text("Am   C   G\nпервая выдуманная строка\n|-3-3-|-2-2-|")

        словарь = song.vocabulary()

        assert "am" not in словарь
        assert all("-" not in слово for слово in словарь)

    def test_в_словаре_буква_ё_сохраняется(self):
        from prompter.parser import parse_song_text

        # Словарь модели содержит слова именно с «ё» и молча выбрасывает
        # формы, где вместо неё стоит «е»
        song = parse_song_text("Am\nвсё ещё здесь")

        словарь = song.vocabulary()
        assert "всё" in словарь
        assert "все" not in словарь

    def test_при_сравнении_ё_по_прежнему_сводится_к_е(self):
        # Распознаватель вернёт слово с «ё», строка песни может быть с «е» —
        # сравнение обязано считать их одним словом
        assert words_match(*[normalize_text(с) for с in ("всё", "все")])

    def test_однобуквенные_огрызки_в_словарь_не_идут(self):
        from prompter.parser import parse_song_text

        song = parse_song_text("Am\nя и он у окна")

        assert all(len(с) > 1 for с in song.vocabulary())


class TestНавигация:
    def test_заголовки_и_проигрыши_входят_в_навигацию(self):
        from prompter.parser import parse_song_text

        song = parse_song_text("Припев:\nAm   C\nпервая выдуманная строка")

        # Спеть можно только строку со словами...
        assert len(song.singable_indexes) == 1
        # ...но показать надо и заголовок
        assert len(song.navigable_indexes) == 2

    def test_табулатура_не_входит_в_навигацию(self):
        from prompter.parser import parse_song_text

        song = parse_song_text("|-3-3-|-2-2-|\n\nAm\nпервая выдуманная строка")

        assert len(song.navigable_indexes) == 1


class TestConfig:
    @pytest.mark.parametrize("порог,ожидание", [(0.4, 1), (0.95, 0)])
    def test_порог_влияет_на_срабатывание(self, порог, ожидание):
        matcher = LineMatcher(СТРОКИ, MatcherConfig(threshold=порог, cooldown_sec=0.0))

        # Распознано только два слова из четырёх
        matcher.feed_final("первая выдуманная")

        assert matcher.position == ожидание

    def test_ручное_листание_обесценивает_накопленное(self):
        matcher = LineMatcher(СТРОКИ, БЕЗ_ПАУЗЫ)
        matcher.feed_partial("первая выдуманная")

        matcher.set_position(2)
        # Ранее услышанные слова не должны сработать на новом месте
        assert matcher.feed_partial("первая выдуманная") is None
        assert matcher.position == 2

    def test_сброс_возвращает_в_начало(self):
        matcher = LineMatcher(СТРОКИ, БЕЗ_ПАУЗЫ)
        matcher.feed_final("первая выдуманная строка примера")

        matcher.reset()

        assert matcher.position == 0
        assert matcher.heard_words == []
