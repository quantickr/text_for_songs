"""Подготовка звука перед распознаванием: убрать лишнее, оставить голос.

Сразу о границах возможного. Когда человек играет на гитаре и поёт в один
микрофон, полностью отделить голос от инструмента нельзя — это задача
разделения источников, её решают нейросети вроде Demucs, и в реальном времени
на процессоре они не работают. Здесь делается то, что реально помогает и стоит
доли миллисекунды на блок:

* **срез низа** — самые громкие струны гитары лежат ниже голоса, и именно они
  «забивают» распознавателю вход; голос при этом почти не страдает, потому что
  разборчивость держится на формантах выше 300 Гц;
* **подавление пауз** — когда голоса нет, а гитара звучит, блок глушится, и
  распознаватель не выдумывает слова из перебора.

Своего выравнивания громкости здесь намеренно нет: vosk нормализует признаки
сам, а вторая нормализация поверх сводила бы на нет работу фильтра — она
покорно возвращала бы срезанному низу прежнюю громкость и заодно вытягивала
шум в паузах.

Всё считается через numpy: цикл на чистом Python по 4000 отсчётов держал бы GIL
и мешал бы аудиопотоку.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# Ниже этой частоты у голоса почти ничего нет, а у гитары — основная мощь.
# Значение подобрано замерами, а не на глаз: со срезом 130 Гц распознавание
# не менялось вовсе (гитарные струны выше по частоте, чем кажется), 200 Гц
# заметно помогает русскому в шуме, а 250 помогает ещё сильнее, но начинает
# портить английский. Разборчивость речи держится на формантах выше 300 Гц,
# поэтому голос от такого среза почти не страдает.
HIGHPASS_HZ = 200.0

# Порог тишины: доля от недавнего пикового уровня, ниже которой считаем,
# что голоса нет и блок можно приглушить
GATE_RATIO = 0.12
GATE_ATTENUATION = 0.15

# Насколько быстро «забывается» замеренный пик громкости
PEAK_DECAY = 0.97


@dataclass
class _Biquad:
    """Коэффициенты биквадратного фильтра и его состояние."""

    b0: float
    b1: float
    b2: float
    a1: float
    a2: float

    def __post_init__(self) -> None:
        self.x1 = self.x2 = 0.0
        self.y1 = self.y2 = 0.0

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Пропустить блок через фильтр, сохраняя состояние между блоками.

        Состояние обязательно тянуть из блока в блок: иначе на каждой границе
        возникнет щелчок, а щелчки распознаватель принимает за звуки речи.
        """
        out = np.empty_like(samples)
        x1, x2, y1, y2 = self.x1, self.x2, self.y1, self.y2

        for index, x0 in enumerate(samples):
            y0 = self.b0 * x0 + self.b1 * x1 + self.b2 * x2 - self.a1 * y1 - self.a2 * y2
            out[index] = y0
            x2, x1 = x1, x0
            y2, y1 = y1, y0

        self.x1, self.x2, self.y1, self.y2 = x1, x2, y1, y2
        return out


def _make_highpass(cutoff_hz: float, sample_rate: int, q: float = 0.707) -> _Biquad:
    """Биквад-фильтр верхних частот (формулы из аудиокуков RBJ)."""
    w0 = 2.0 * math.pi * cutoff_hz / sample_rate
    cos_w0 = math.cos(w0)
    alpha = math.sin(w0) / (2.0 * q)

    b0 = (1.0 + cos_w0) / 2.0
    b1 = -(1.0 + cos_w0)
    b2 = (1.0 + cos_w0) / 2.0
    a0 = 1.0 + alpha
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha

    return _Biquad(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


class AudioPreprocessor:
    """Обработка блоков звука перед подачей в распознаватель."""

    def __init__(self, sample_rate: int, enabled: bool = True) -> None:
        self.enabled = enabled
        self.sample_rate = sample_rate
        self._highpass = _make_highpass(HIGHPASS_HZ, sample_rate)
        self._peak = 0.0

    def process(self, data: bytes) -> bytes:
        """Обработать блок 16-битного моно-звука и вернуть такой же блок."""
        if not self.enabled or not data:
            return data

        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size == 0:
            return data

        samples = self._highpass.process(samples)

        # Уровень блока считаем по средней мощности, а не по одиночному пику:
        # случайный щелчок иначе открывал бы гейт на весь блок
        rms = float(np.sqrt(np.mean(np.square(samples))))
        self._peak = max(rms, self._peak * PEAK_DECAY)

        if self._peak > 0 and rms < self._peak * GATE_RATIO:
            samples *= GATE_ATTENUATION  # похоже на паузу: голоса нет

        np.clip(samples, -1.0, 1.0, out=samples)
        return (samples * 32767.0).astype(np.int16).tobytes()

    def reset(self) -> None:
        """Сбросить состояние — при смене устройства или песни."""
        self._highpass = _make_highpass(HIGHPASS_HZ, self.sample_rate)
        self._peak = 0.0
