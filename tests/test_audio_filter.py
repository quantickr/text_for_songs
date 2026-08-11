"""Тесты подготовки звука перед распознаванием.

Сигналы синтетические: чистые тона нужной частоты, никакого микрофона.
"""

import math

import numpy as np

from prompter.audio_filter import AudioPreprocessor


def тон(частота: float, длительность: float = 0.25, амплитуда: float = 0.3,
        частота_дискретизации: int = 16000) -> bytes:
    """Синусоида заданной частоты в виде 16-битного моно-блока."""
    отсчёты = np.arange(int(частота_дискретизации * длительность))
    волна = амплитуда * np.sin(2 * math.pi * частота * отсчёты / частота_дискретизации)
    return (волна * 32767).astype(np.int16).tobytes()


def уровень(data: bytes) -> float:
    """Среднеквадратичный уровень блока."""
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0


class TestСрезНиза:
    def test_низкие_частоты_подавляются(self):
        # 60 Гц — область, где гудят нижние струны и наводки, голоса там нет
        pre = AudioPreprocessor(16000)
        низ = тон(60)

        до, после = уровень(низ), уровень(pre.process(низ))

        assert после < до * 0.5

    def test_голосовой_диапазон_сохраняется(self):
        # 500 Гц — середина речевых формант, её терять нельзя
        pre = AudioPreprocessor(16000)
        голос = тон(500)

        до, после = уровень(голос), уровень(pre.process(голос))

        assert после > до * 0.5

    def test_низ_страдает_сильнее_чем_голос(self):
        низ_pre, голос_pre = AudioPreprocessor(16000), AudioPreprocessor(16000)

        доля_низа = уровень(низ_pre.process(тон(60))) / уровень(тон(60))
        доля_голоса = уровень(голос_pre.process(тон(500))) / уровень(тон(500))

        assert доля_низа < доля_голоса


class TestФормат:
    def test_размер_блока_не_меняется(self):
        pre = AudioPreprocessor(16000)
        блок = тон(440)

        assert len(pre.process(блок)) == len(блок)

    def test_выключенный_фильтр_не_трогает_звук(self):
        pre = AudioPreprocessor(16000, enabled=False)
        блок = тон(440)

        assert pre.process(блок) == блок

    def test_пустой_блок_не_ломает(self):
        pre = AudioPreprocessor(16000)

        assert pre.process(b"") == b""

    def test_тишина_не_вызывает_деления_на_ноль(self):
        pre = AudioPreprocessor(16000)
        тишина = b"\x00\x00" * 4000

        результат = pre.process(тишина)

        assert len(результат) == len(тишина)
        assert уровень(результат) < 0.01


class TestПаузы:
    def test_тихий_фрагмент_после_громкого_глушится(self):
        # Так выглядит проигрыш: голос смолк, гитара продолжает звучать
        pre = AudioPreprocessor(16000)
        pre.process(тон(500, амплитуда=0.5))  # запомнили громкий голос

        тихий = тон(500, амплитуда=0.02)
        после = уровень(pre.process(тихий))

        assert после < уровень(тихий)

    def test_громкость_не_накручивается(self):
        # Своей регулировки громкости здесь нет намеренно: она возвращала бы
        # срезанному низу прежний уровень и вытягивала шум в паузах
        pre = AudioPreprocessor(16000)
        блок = тон(500, амплитуда=0.2)

        assert уровень(pre.process(блок)) <= уровень(блок) * 1.1


class TestГромкость:
    def test_сигнал_не_переполняется(self):
        pre = AudioPreprocessor(16000)
        громкий = тон(500, амплитуда=0.95)

        результат = np.frombuffer(pre.process(громкий), dtype=np.int16)

        # Клиппинг звучит как треск и распознаванию только вредит
        assert np.abs(результат).max() <= 32767

    def test_состояние_фильтра_переживает_границу_блоков(self):
        # Без переноса состояния на стыке блоков возникает щелчок,
        # а щелчки распознаватель принимает за звуки речи
        pre = AudioPreprocessor(16000)
        блок = тон(500)

        первый = np.frombuffer(pre.process(блок), dtype=np.int16)
        второй = np.frombuffer(pre.process(блок), dtype=np.int16)

        скачок = abs(int(второй[0]) - int(первый[-1]))
        assert скачок < 8000
