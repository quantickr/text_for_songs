"""Распознавание речи средствами macOS (Speech.framework).

Зачем он рядом с vosk. На замерах одного и того же аудио системный движок
ошибался заметно реже: на русском с громкой гитарой 4 % против 21 % у vosk,
на английском с акцентом — вовсе без ошибок. Плюс ничего не надо скачивать:
модели уже в системе.

Чем приходится платить: приложение обязано быть упаковано в ``.app`` со своим
``Info.plist``, где объявлено ``NSSpeechRecognitionUsageDescription``. Без этого
macOS убивает процесс при первом же обращении к распознаванию — не показывая
даже диалога. Поэтому запуск через ``python main.py`` с этим движком невозможен
в принципе, и при недоступности мы честно откатываемся на vosk.
"""

from __future__ import annotations

import math
import threading
from typing import Sequence

from PyQt6.QtCore import QObject, pyqtSignal

try:
    import AVFoundation
    import Foundation
    import Speech

    ДОСТУПЕН = True
except ImportError:  # не macOS или не установлен pyobjc
    ДОСТУПЕН = False

# Задача распознавания живёт ограниченное время, поэтому её приходится
# периодически перезапускать. Минута с запасом укладывается в лимит системы.
ПЕРЕЗАПУСК_ЧЕРЕЗ_СЕК = 55.0

# Сколько слов песни отдавать как подсказку контекстом
МАКС_КОНТЕКСТ = 200


class AppleSpeechError(Exception):
    """Системное распознавание недоступно или отказало."""


def доступно() -> bool:
    """Можно ли вообще пользоваться системным распознаванием."""
    if not ДОСТУПЕН:
        return False
    try:
        return Speech.SFSpeechRecognizer.authorizationStatus() in (0, 3)
    except Exception:
        return False


def разрешение_выдано() -> bool:
    """Дал ли пользователь доступ к распознаванию."""
    if not ДОСТУПЕН:
        return False
    return Speech.SFSpeechRecognizer.authorizationStatus() == 3


def поддерживает_язык(язык: str) -> bool:
    """Есть ли в системе распознавание для этого языка."""
    if not ДОСТУПЕН:
        return False
    код = _локаль(язык)
    try:
        доступные = {
            str(l.localeIdentifier()) for l in Speech.SFSpeechRecognizer.supportedLocales()
        }
    except Exception:
        return False
    return код in доступные


def _локаль(язык: str) -> str:
    return {"ru": "ru-RU", "en": "en-US"}.get(язык, "en-US")


class AppleSpeechRecognizer(QObject):
    """Непрерывное распознавание с микрофона средствами системы.

    Набор сигналов совпадает с vosk-распознавателем, поэтому интерфейсу
    безразлично, какой движок работает.
    """

    partial_ready = pyqtSignal(str)
    final_ready = pyqtSignal(str)
    level_changed = pyqtSignal(float)
    listening_changed = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)
    warning_occurred = pyqtSignal(str)
    vocabulary_applied = pyqtSignal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._engine = None
        self._request = None
        self._task = None
        self._recognizer = None
        self._listening = False
        self._таймер_перезапуска: threading.Timer | None = None
        self._словарь: list[str] = []
        self._язык = "ru"
        self._последний_финал = ""

    @property
    def is_listening(self) -> bool:
        return self._listening

    # --- Управление --------------------------------------------------------

    def start(self, язык: str, vocabulary: Sequence[str] | None = None) -> None:
        """Начать слушать микрофон на нужном языке."""
        if not ДОСТУПЕН:
            self.error_occurred.emit(
                "Системное распознавание недоступно: не установлен pyobjc."
            )
            return
        if not разрешение_выдано():
            self.error_occurred.emit(
                "Нет доступа к распознаванию речи. Разрешите его в Системных "
                "настройках → Конфиденциальность и безопасность → Распознавание речи, "
                "и запускайте приложение как .app — иначе macOS не покажет запрос."
            )
            return

        self.stop()
        self._язык = язык
        self._словарь = list(vocabulary or [])[:МАКС_КОНТЕКСТ]

        try:
            self._запустить_поток()
        except Exception as ошибка:
            self.error_occurred.emit(f"Не удалось запустить распознавание: {ошибка}")
            self.stop()

    def stop(self) -> None:
        """Остановить распознавание и освободить микрофон."""
        self._отменить_таймер()

        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:
                pass
            self._task = None

        if self._request is not None:
            try:
                self._request.endAudio()
            except Exception:
                pass
            self._request = None

        if self._engine is not None:
            try:
                if self._engine.isRunning():
                    self._engine.stop()
                self._engine.inputNode().removeTapOnBus_(0)
            except Exception:
                pass
            self._engine = None

        self._установить_слушает(False)

    # --- Внутреннее --------------------------------------------------------

    def _установить_слушает(self, значение: bool) -> None:
        if self._listening != значение:
            self._listening = значение
            self.listening_changed.emit(значение)

    def _отменить_таймер(self) -> None:
        if self._таймер_перезапуска is not None:
            self._таймер_перезапуска.cancel()
            self._таймер_перезапуска = None

    def _запустить_поток(self) -> None:
        """Поднять аудиодвижок и задачу распознавания."""
        локаль = Foundation.NSLocale.alloc().initWithLocaleIdentifier_(_локаль(self._язык))
        self._recognizer = Speech.SFSpeechRecognizer.alloc().initWithLocale_(локаль)
        if self._recognizer is None or not self._recognizer.isAvailable():
            raise AppleSpeechError(f"Нет распознавания для языка «{self._язык}»")

        запрос = Speech.SFSpeechAudioBufferRecognitionRequest.alloc().init()
        запрос.setShouldReportPartialResults_(True)
        # Работаем офлайн: на сцене интернета может не быть, а отправлять
        # звук наружу ради суфлёра ни к чему
        if self._recognizer.supportsOnDeviceRecognition():
            запрос.setRequiresOnDeviceRecognition_(True)
        if self._словарь:
            запрос.setContextualStrings_(self._словарь)
            self.vocabulary_applied.emit(len(self._словарь))
        self._request = запрос

        self._engine = AVFoundation.AVAudioEngine.alloc().init()
        вход = self._engine.inputNode()
        формат = вход.outputFormatForBus_(0)

        вход.installTapOnBus_bufferSize_format_block_(0, 4096, формат, self._на_буфер)
        self._engine.prepare()
        ok, ошибка = self._engine.startAndReturnError_(None)
        if not ok:
            описание = str(ошибка.localizedDescription()) if ошибка else "неизвестная причина"
            raise AppleSpeechError(f"микрофон не открылся: {описание}")

        self._task = self._recognizer.recognitionTaskWithRequest_resultHandler_(
            запрос, self._на_результат
        )
        self._установить_слушает(True)
        self._запланировать_перезапуск()

    def _запланировать_перезапуск(self) -> None:
        """Задача распознавания живёт ограниченное время — обновляем её заранее."""
        self._отменить_таймер()
        self._таймер_перезапуска = threading.Timer(ПЕРЕЗАПУСК_ЧЕРЕЗ_СЕК, self._перезапустить)
        self._таймер_перезапуска.daemon = True
        self._таймер_перезапуска.start()

    def _перезапустить(self) -> None:
        if not self._listening:
            return
        try:
            self.stop()
            self._запустить_поток()
        except Exception as ошибка:
            self.error_occurred.emit(f"Распознавание прервалось: {ошибка}")

    def _на_буфер(self, буфер, время) -> None:  # noqa: ANN001 (типы из Objective-C)
        """Колбэк аудиопотока: отдать звук распознавателю и померить уровень."""
        запрос = self._request
        if запрос is None:
            return
        try:
            запрос.appendAudioPCMBuffer_(буфер)
        except Exception:
            return

        уровень = _уровень_буфера(буфер)
        if уровень is not None:
            self.level_changed.emit(уровень)

    def _на_результат(self, результат, ошибка) -> None:  # noqa: ANN001
        """Колбэк распознавания: промежуточные и окончательные гипотезы."""
        if ошибка is not None:
            # Отмена задачи при остановке — не повод пугать пользователя
            код = getattr(ошибка, "code", lambda: 0)()
            if код not in (203, 216, 301, 1110):
                self.error_occurred.emit(f"Сбой распознавания: {ошибка.localizedDescription()}")
            return

        if результат is None:
            return

        текст = str(результат.bestTranscription().formattedString()).strip()
        if not текст:
            return

        if результат.isFinal():
            self.final_ready.emit(текст)
            self._последний_финал = текст
        else:
            self.partial_ready.emit(текст)


def _уровень_буфера(буфер) -> float | None:  # noqa: ANN001
    """Средний уровень блока 0..1 для индикатора микрофона."""
    try:
        каналы = буфер.floatChannelData()
        кадры = int(буфер.frameLength())
        if not каналы or кадры <= 0:
            return None
        данные = каналы[0]
        # Считаем по каждому шестнадцатому отсчёту: для индикатора этого хватает,
        # а колбэк аудиопотока задерживать нельзя
        шаг = 16
        сумма = 0.0
        сколько = 0
        for i in range(0, кадры, шаг):
            значение = данные[i]
            сумма += значение * значение
            сколько += 1
        if not сколько:
            return None
        return min(1.0, math.sqrt(сумма / сколько))
    except Exception:
        return None
