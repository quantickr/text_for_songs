"""Тесты системного распознавания macOS.

Микрофон не используется: проверяется доступность, выбор языка и то, что при
недоступности движка суфлёр честно откатывается на vosk, а не падает.
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="только для macOS")


class TestДоступность:
    def test_модуль_импортируется(self):
        from prompter import apple_speech

        assert isinstance(apple_speech.ДОСТУПЕН, bool)

    def test_языки_переводятся_в_локали(self):
        from prompter.apple_speech import _локаль

        assert _локаль("ru") == "ru-RU"
        assert _локаль("en") == "en-US"
        # Незнакомый язык не должен ронять — берём английский
        assert _локаль("xx") == "en-US"

    def test_проверка_языка_не_падает_без_системы(self):
        from prompter import apple_speech

        # Функция обязана вернуть булево в любом случае, даже если
        # pyobjc отсутствует или система отказала
        assert isinstance(apple_speech.поддерживает_язык("ru"), bool)
        assert isinstance(apple_speech.поддерживает_язык("несуществующий"), bool)


class TestРаспознаватель:
    def test_создаётся_и_не_слушает(self, qt_app):
        from prompter.apple_speech import AppleSpeechRecognizer

        r = AppleSpeechRecognizer()

        assert not r.is_listening

    def test_остановка_без_запуска_безопасна(self, qt_app):
        from prompter.apple_speech import AppleSpeechRecognizer

        r = AppleSpeechRecognizer()
        r.stop()  # не должно бросать

        assert not r.is_listening

    def test_набор_сигналов_совпадает_с_vosk(self, qt_app):
        """Интерфейсу должно быть безразлично, какой движок работает."""
        from prompter.apple_speech import AppleSpeechRecognizer
        from prompter.speech import SpeechRecognizer

        нужные = {
            "partial_ready", "final_ready", "level_changed",
            "listening_changed", "error_occurred", "warning_occurred",
            "vocabulary_applied",
        }

        for класс in (SpeechRecognizer, AppleSpeechRecognizer):
            assert нужные <= set(dir(класс)), класс.__name__

    def test_без_разрешения_сообщает_а_не_падает(self, qt_app):
        from prompter import apple_speech
        from prompter.apple_speech import AppleSpeechRecognizer

        if apple_speech.разрешение_выдано():
            pytest.skip("доступ выдан — этот путь не воспроизвести")

        r = AppleSpeechRecognizer()
        ошибки = []
        r.error_occurred.connect(ошибки.append)

        r.start("ru")

        # Суфлёр должен объяснить, что делать, а не молча замолчать
        assert len(ошибки) == 1
        assert ".app" in ошибки[0]
        assert not r.is_listening


class TestОткатНаVosk:
    def test_при_выключенной_настройке_системный_не_берётся(self, qt_app):
        from prompter.ui.main_window import MainWindow

        w = MainWindow()
        w.settings.speech_engine = "vosk"

        assert not w._системный_движок_готов("ru")

    def test_остановка_глушит_оба_движка(self, qt_app):
        from prompter.ui.main_window import MainWindow

        w = MainWindow()
        w._stop_listening()

        assert not w.recognizer.is_listening
        assert not w.apple_recognizer.is_listening


class TestНастройка:
    def test_значение_по_умолчанию_безопасное(self):
        from prompter.settings import AppSettings

        # vosk работает всегда, системный — только в .app
        assert AppSettings().speech_engine == "vosk"

    def test_мусор_в_настройке_чинится(self):
        from prompter.settings import AppSettings

        s = AppSettings(speech_engine="что-то не то")
        s._clamp()

        assert s.speech_engine == "vosk"
