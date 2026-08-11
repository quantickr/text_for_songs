"""Настройки приложения, сохраняемые между запусками.

Обёртка над ``QSettings``. Отдельный слой нужен из-за особенности Qt: значения
возвращаются строками (в INI-файлах и реестре тип не сохраняется), поэтому
читать их «как есть» нельзя — нужно приводить типы и подставлять умолчания.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from PyQt6.QtCore import QSettings

from .matcher import MatcherConfig


@dataclass
class AppSettings:
    """Все пользовательские настройки в одном месте."""

    # Голосовая прокрутка
    voice_scroll_enabled: bool = True
    threshold: float = 0.6
    """Доля значимых слов строки, при которой строка считается спетой."""
    jump_margin: float = 0.15
    cooldown_sec: float = 0.35
    buffer_size: int = 12

    limit_vocabulary: bool = True
    """Ограничивать словарь распознавателя словами текущей песни.

    Главный рычаг точности: распознавателю не приходится выбирать из всего
    языка. Выключать имеет смысл, только если поёте не по тексту.
    """

    auto_skip_service_lines: bool = True
    """Пропускать служебные строки («Припев», «Проигрыш») по таймеру."""

    service_line_delay: float = 0.5
    """Через сколько секунд уходить со служебной строки без указания времени."""

    speech_engine: str = "vosk"
    """Какой движок распознавания использовать: ``vosk`` или ``apple``.

    Системный движок macOS точнее (на замерах вчетверо меньше ошибок с гитарой
    и вовсе без ошибок на английском с акцентом), но работает только когда
    приложение запущено как ``.app``. Значение по умолчанию — vosk, потому что
    он работает всегда и везде.
    """

    accurate_model: bool = False
    """Использовать модель распознавания покрупнее там, где она есть.

    Нужна прежде всего для английского: маленькая модель рассчитана на чистое
    произношение носителя и заметно спотыкается на акценте.
    """

    denoise: bool = True
    """Чистить звук перед распознаванием: срезать низ и глушить паузы."""

    # Звук
    input_device: int = -1  # -1 означает «устройство по умолчанию»

    # Отображение
    font_size: int = 34
    context_lines: int = 2
    """Сколько строк показывать до и после текущей."""
    scroll_animation_ms: int = 280
    """Сколько длится доезд строки. Ноль — переключать мгновенно."""
    show_debug_log: bool = True

    # Сеть
    respect_robots: bool = True

    def matcher_config(self) -> MatcherConfig:
        """Сконструировать настройки матчера из пользовательских значений."""
        return MatcherConfig(
            threshold=self.threshold,
            jump_margin=self.jump_margin,
            cooldown_sec=self.cooldown_sec,
            buffer_size=self.buffer_size,
        )

    @property
    def device_index(self) -> int | None:
        """Индекс устройства ввода или ``None`` для устройства по умолчанию."""
        return None if self.input_device < 0 else self.input_device

    # --- Сохранение и загрузка --------------------------------------------

    def save(self) -> None:
        settings = QSettings()
        for key, value in asdict(self).items():
            settings.setValue(key, value)
        settings.sync()

    @classmethod
    def load(cls) -> AppSettings:
        """Прочитать настройки, устойчиво к мусору в хранилище."""
        settings = QSettings()
        defaults = cls()
        values: dict[str, object] = {}

        for key, default in asdict(defaults).items():
            raw = settings.value(key, default)
            try:
                if isinstance(default, bool):
                    values[key] = _to_bool(raw, default)
                elif isinstance(default, int):
                    values[key] = int(raw)
                elif isinstance(default, float):
                    values[key] = float(raw)
                else:
                    values[key] = raw
            except (TypeError, ValueError):
                values[key] = default

        instance = cls(**values)  # type: ignore[arg-type]
        instance._clamp()
        return instance

    def _clamp(self) -> None:
        """Загнать значения в разумные пределы: в хранилище может быть что угодно."""
        self.threshold = min(max(self.threshold, 0.1), 1.0)
        self.jump_margin = min(max(self.jump_margin, 0.0), 0.5)
        self.cooldown_sec = min(max(self.cooldown_sec, 0.0), 5.0)
        self.buffer_size = min(max(self.buffer_size, 4), 40)
        self.font_size = min(max(self.font_size, 14), 96)
        self.context_lines = min(max(self.context_lines, 1), 5)
        self.service_line_delay = min(max(self.service_line_delay, 0.0), 10.0)
        self.scroll_animation_ms = min(max(self.scroll_animation_ms, 0), 800)
        if self.speech_engine not in ("vosk", "apple"):
            self.speech_engine = "vosk"


def _to_bool(raw: object, default: bool) -> bool:
    """QSettings возвращает булево значение строкой — приводим аккуратно."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(raw, (int, float)):
        return bool(raw)
    return default
