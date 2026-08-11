"""Микрофон и офлайн-распознавание речи через vosk.

Устройство работы:

* аудиоколбэк PortAudio живёт в высокоприоритетном потоке и не имеет права
  делать ничего тяжёлого — он только копирует байты в очередь;
* отдельный рабочий поток забирает байты из очереди и кормит ими vosk;
* результаты уезжают в интерфейс сигналами Qt.

Эмитить сигналы из обычного ``threading.Thread`` безопасно: получатель создан
в потоке интерфейса, поэтому Qt автоматически ставит вызов в его очередь.

Отдельно решается неприятная особенность macOS: если приложению не выдали право
на микрофон, поток может открыться без единой ошибки, но приходить будет
тишина. Поэтому есть сторож, который следит за уровнем сигнала и подсказывает,
куда идти за разрешением.
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Sequence

import sounddevice as sd
from PyQt6.QtCore import QObject, pyqtSignal

from .audio_filter import AudioPreprocessor

# vosk печатает в консоль много отладки — гасим до создания моделей
try:
    import vosk

    vosk.SetLogLevel(-1)
except Exception:  # библиотека может быть не установлена или битая
    vosk = None  # type: ignore[assignment]

TARGET_SAMPLE_RATE = 16000
"""Малые модели vosk обучены на 16 кГц; PortAudio при необходимости ресемплит сам."""

BLOCK_SIZE = 4000
"""Размер блока в кадрах: четверть секунды — компромисс между откликом и нагрузкой."""

QUEUE_MAX_BLOCKS = 32
"""Ограничение очереди: если распознавание отстанет, лучше потерять звук, чем память."""

SILENCE_WARNING_SEC = 6.0
"""Сколько секунд полной тишины считать поводом заподозрить отсутствие прав."""


class SpeechError(Exception):
    """Проблема со звуком или моделью, которую нужно показать пользователю."""


# Модели тяжёлые (десятки мегабайт) и потокобезопасны для совместного чтения,
# поэтому держим по одной на путь и переиспользуем при смене песни.
_model_cache: dict[str, "vosk.Model"] = {}
_model_cache_lock = threading.Lock()


def load_model(path: Path) -> "vosk.Model":
    """Загрузить модель vosk с кэшированием."""
    if vosk is None:
        raise SpeechError(
            "Библиотека vosk не установлена. Выполните: pip install -r requirements.txt"
        )

    key = str(path)
    with _model_cache_lock:
        cached = _model_cache.get(key)
        if cached is not None:
            return cached

    if not path.is_dir():
        raise SpeechError(f"Папка модели не найдена: {path}")

    try:
        model = vosk.Model(str(path))
    except Exception as error:
        raise SpeechError(
            f"Не удалось загрузить модель из {path}: {error}. "
            "Возможно, архив распакован не полностью."
        ) from error

    with _model_cache_lock:
        _model_cache[key] = model
    return model


def list_input_devices() -> list[tuple[int, str]]:
    """Список доступных микрофонов: пары «индекс, название»."""
    try:
        devices = sd.query_devices()
    except Exception:
        return []

    result: list[tuple[int, str]] = []
    for index, device in enumerate(devices):
        if device.get("max_input_channels", 0) > 0:
            result.append((index, str(device.get("name", f"Устройство {index}"))))
    return result


def default_input_device() -> int | None:
    """Индекс микрофона по умолчанию, если он есть."""
    try:
        device = sd.default.device[0]
    except Exception:
        return None
    return device if isinstance(device, int) and device >= 0 else None


class SpeechRecognizer(QObject):
    """Непрерывное распознавание с микрофона в фоновом потоке."""

    partial_ready = pyqtSignal(str)
    """Промежуточная гипотеза: приходит часто, каждый раз с начала фразы."""

    final_ready = pyqtSignal(str)
    """Окончательный результат распознанной фразы."""

    level_changed = pyqtSignal(float)
    """Уровень сигнала 0..1 — для индикатора «микрофон слышит»."""

    listening_changed = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)
    warning_occurred = pyqtSignal(str)

    vocabulary_applied = pyqtSignal(int)
    """Словарь ограничен словами песни; передаётся их количество."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._audio: queue.Queue[bytes | None] = queue.Queue(maxsize=QUEUE_MAX_BLOCKS)
        self._listening = False
        self._preprocessor: AudioPreprocessor | None = None

    @property
    def is_listening(self) -> bool:
        return self._listening

    # --- Управление --------------------------------------------------------

    def start(
        self,
        model_path: Path,
        device: int | None = None,
        vocabulary: Sequence[str] | None = None,
        denoise: bool = True,
    ) -> None:
        """Начать слушать микрофон. Повторный вызов перезапускает поток.

        ``vocabulary`` — слова текущей песни. Если их передать, распознаватель
        будет выбирать только из них, а не из десятков тысяч слов языка. Для
        суфлёра это главный источник точности: нам заранее известно, что человек
        собирается петь, и глупо этим не воспользоваться.
        """
        self.stop()

        self._stop_event.clear()
        self._drain_queue()
        self._thread = threading.Thread(
            target=self._run,
            args=(model_path, device, list(vocabulary or []), denoise),
            name="SpeechRecognizer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Остановить распознавание и дождаться завершения потока."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            # Разбудить рабочий поток, если он ждёт данные
            try:
                self._audio.put_nowait(None)
            except queue.Full:
                pass
            # Ждём с таймаутом: держать интерфейс заблокированным нельзя
            thread.join(timeout=3.0)
        self._thread = None
        self._set_listening(False)

    def _drain_queue(self) -> None:
        while True:
            try:
                self._audio.get_nowait()
            except queue.Empty:
                return

    def _set_listening(self, value: bool) -> None:
        if self._listening != value:
            self._listening = value
            self.listening_changed.emit(value)

    # --- Рабочий поток -----------------------------------------------------

    def _run(
        self, model_path: Path, device: int | None, vocabulary: list[str], denoise: bool
    ) -> None:
        """Тело фонового потока: модель, аудиопоток, цикл распознавания."""
        try:
            model = load_model(model_path)
        except SpeechError as error:
            self.error_occurred.emit(str(error))
            return

        try:
            sample_rate = self._choose_sample_rate(device)
        except SpeechError as error:
            self.error_occurred.emit(str(error))
            return

        recognizer = self._build_recognizer(model, sample_rate, vocabulary)
        recognizer.SetWords(False)
        self._preprocessor = AudioPreprocessor(sample_rate, enabled=denoise)

        try:
            with sd.RawInputStream(
                samplerate=sample_rate,
                blocksize=BLOCK_SIZE,
                device=device,
                dtype="int16",
                channels=1,
                callback=self._audio_callback,
            ):
                self._set_listening(True)
                self._recognize_loop(recognizer)
        except sd.PortAudioError as error:
            self.error_occurred.emit(
                f"Не удалось открыть микрофон: {error}. "
                "Проверьте, что устройство подключено и не занято другой программой."
            )
        except ValueError as error:
            # Так падает sounddevice, если устройство задано, но не найдено
            self.error_occurred.emit(f"Микрофон не найден: {error}")
        except Exception as error:
            self.error_occurred.emit(f"Ошибка захвата звука: {error}")
        finally:
            self._set_listening(False)

    def _build_recognizer(
        self, model: "vosk.Model", sample_rate: int, vocabulary: list[str]
    ) -> "vosk.KaldiRecognizer":
        """Создать распознаватель, по возможности с ограниченным словарём.

        Словарь передаётся моделью-грамматикой: список разрешённых слов плюс
        ``[unk]`` для всего остального. Слова, которых нет в словаре самой
        модели, она принять не может, поэтому при отказе спокойно возвращаемся
        к обычному распознаванию — лучше менее точно, чем никак.
        """
        if vocabulary:
            grammar = json.dumps(sorted(set(vocabulary)) + ["[unk]"], ensure_ascii=False)
            try:
                recognizer = vosk.KaldiRecognizer(model, sample_rate, grammar)
                self.vocabulary_applied.emit(len(set(vocabulary)))
                return recognizer
            except Exception as error:
                self.warning_occurred.emit(
                    f"Не удалось ограничить словарь песней ({error}). "
                    "Распознавание идёт по общему словарю."
                )

        return vosk.KaldiRecognizer(model, sample_rate)

    @staticmethod
    def _choose_sample_rate(device: int | None) -> int:
        """Выбрать частоту дискретизации.

        Сначала пробуем 16 кГц: это родная частота малых моделей vosk, а
        PortAudio на большинстве систем ресемплит сам. Если устройство наотрез
        отказывается, берём его собственную частоту — vosk умеет понижать её сам.
        """
        try:
            sd.check_input_settings(device=device, channels=1, dtype="int16",
                                    samplerate=TARGET_SAMPLE_RATE)
            return TARGET_SAMPLE_RATE
        except Exception:
            pass

        try:
            info = sd.query_devices(device if device is not None else None, kind="input")
            native = int(info["default_samplerate"])
            return native if native > 0 else TARGET_SAMPLE_RATE
        except Exception as error:
            raise SpeechError(
                f"Не удалось определить параметры микрофона: {error}"
            ) from error

    def _audio_callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        """Колбэк PortAudio. Работает в аудиопотоке — только копирование байтов.

        ``indata`` указывает прямо в память PortAudio и живёт лишь до конца
        вызова, поэтому копия через ``bytes()`` обязательна.
        """
        if self._stop_event.is_set():
            return
        try:
            self._audio.put_nowait(bytes(indata))
        except queue.Full:
            pass  # распознавание не успевает — блок теряем осознанно

    def _recognize_loop(self, recognizer: "vosk.KaldiRecognizer") -> None:
        """Цикл распознавания: забирает звук из очереди и кормит им vosk."""
        silence_blocks = 0
        blocks_per_warning = int(SILENCE_WARNING_SEC * TARGET_SAMPLE_RATE / BLOCK_SIZE)
        warned_about_silence = False

        while not self._stop_event.is_set():
            try:
                data = self._audio.get(timeout=0.3)
            except queue.Empty:
                continue
            if data is None:
                break

            # Уровень показываем по сырому звуку: индикатор должен отражать
            # то, что реально слышит микрофон, а не то, что осталось после фильтра
            level = _rms_level(data)
            self.level_changed.emit(level)

            if self._preprocessor is not None:
                data = self._preprocessor.process(data)

            # Сторож тишины: на macOS отсутствие прав на микрофон выглядит
            # не как ошибка, а как бесконечный поток нулей
            if level <= 0.0005:
                silence_blocks += 1
                if silence_blocks >= blocks_per_warning and not warned_about_silence:
                    warned_about_silence = True
                    self.warning_occurred.emit(
                        "Микрофон не слышит звука. Проверьте разрешение: "
                        "Системные настройки → Конфиденциальность и безопасность → Микрофон."
                    )
            else:
                silence_blocks = 0
                warned_about_silence = False

            try:
                if recognizer.AcceptWaveform(data):
                    text = _extract_text(recognizer.Result(), "text")
                    if text:
                        self.final_ready.emit(text)
                else:
                    partial = _extract_text(recognizer.PartialResult(), "partial")
                    if partial:
                        self.partial_ready.emit(partial)
            except Exception as error:
                self.error_occurred.emit(f"Сбой распознавания: {error}")
                return


def _extract_text(raw_json: str, key: str) -> str:
    """Достать текст из JSON-ответа vosk, не падая на неожиданном формате."""
    try:
        return str(json.loads(raw_json).get(key, "")).strip()
    except (ValueError, AttributeError):
        return ""


def _rms_level(data: bytes) -> float:
    """Средний уровень блока 0..1 для индикатора микрофона.

    Считается без numpy: блок маленький, а лишняя зависимость не нужна.
    """
    if len(data) < 2:
        return 0.0

    total = 0
    count = len(data) // 2
    # Считаем по каждому четвёртому отсчёту: для индикатора точности хватает,
    # а нагрузка на поток распознавания вчетверо меньше
    step = 4
    sampled = 0
    for offset in range(0, count, step):
        sample = int.from_bytes(data[offset * 2 : offset * 2 + 2], "little", signed=True)
        total += sample * sample
        sampled += 1

    if not sampled:
        return 0.0
    return min(1.0, (total / sampled) ** 0.5 / 32768.0)
