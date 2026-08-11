"""Диалог настроек: порог совпадения, шрифт, микрофон, голосовая прокрутка."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import apple_speech
from ..settings import AppSettings
from ..speech import list_input_devices
from ..vosk_models import MODEL_CATALOG, describe_model_state


class SettingsDialog(QDialog):
    """Правка настроек. Изменения применяются только по кнопке «Сохранить»."""

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.setMinimumWidth(560)

        self.settings = settings

        self.voice_check = QCheckBox("Листать текст по голосу")
        self.voice_check.setChecked(settings.voice_scroll_enabled)

        self.threshold_slider = QSlider(Qt.Orientation.Horizontal)
        self.threshold_slider.setRange(30, 100)
        self.threshold_slider.setValue(int(settings.threshold * 100))
        self.threshold_value = QLabel()
        self.threshold_slider.valueChanged.connect(self._update_threshold_label)
        self._update_threshold_label(self.threshold_slider.value())

        self.device_combo = QComboBox()
        self.device_combo.addItem("Устройство по умолчанию", -1)
        for index, name in list_input_devices():
            self.device_combo.addItem(name, index)
        выбранный = self.device_combo.findData(settings.input_device)
        self.device_combo.setCurrentIndex(max(0, выбранный))

        self.font_spin = QSpinBox()
        self.font_spin.setRange(14, 96)
        self.font_spin.setSuffix(" px")
        self.font_spin.setValue(settings.font_size)

        self.context_spin = QSpinBox()
        self.context_spin.setRange(1, 5)
        self.context_spin.setValue(settings.context_lines)

        self.animation_spin = QSpinBox()
        self.animation_spin.setRange(0, 800)
        self.animation_spin.setSingleStep(20)
        self.animation_spin.setSuffix(" мс")
        self.animation_spin.setSpecialValueText("без анимации")
        self.animation_spin.setValue(settings.scroll_animation_ms)
        self.animation_spin.setToolTip(
            "Сколько длится доезд строки. Меньше — резче и быстрее, "
            "больше — плавнее, но на быстрых песнях начинает отставать."
        )

        self.engine_combo = QComboBox()
        self.engine_combo.addItem("vosk (работает везде)", "vosk")
        системный_готов = apple_speech.разрешение_выдано()
        подпись = (
            "системный macOS (точнее)"
            if системный_готов
            else "системный macOS — нужен запуск через .app"
        )
        self.engine_combo.addItem(подпись, "apple")
        выбран = self.engine_combo.findData(settings.speech_engine)
        self.engine_combo.setCurrentIndex(max(0, выбран))
        self.engine_combo.setToolTip(
            "Системное распознавание macOS на замерах ошибалось заметно реже, "
            "особенно когда рядом звучит инструмент. Работает только если "
            "приложение запущено как .app — соберите его командой python build_app.py."
        )

        self.vocabulary_check = QCheckBox("Слушать только слова текущей песни")
        self.vocabulary_check.setChecked(settings.limit_vocabulary)
        self.vocabulary_check.setToolTip(
            "Распознаватель выбирает не из всего языка, а из слов этой песни. "
            "Самый заметный способ поднять точность."
        )

        self.denoise_check = QCheckBox("Чистить звук перед распознаванием")
        self.denoise_check.setChecked(settings.denoise)
        self.denoise_check.setToolTip(
            "Срезает низ, где громче всего звучит гитара, и глушит паузы. "
            "Полностью отделить голос от инструмента в одном микрофоне нельзя, "
            "но разборчивость заметно растёт."
        )

        self.accurate_check = QCheckBox("Модель распознавания покрупнее")
        self.accurate_check.setChecked(settings.accurate_model)
        self.accurate_check.setToolTip(
            "Для английского: маленькая модель рассчитана на произношение носителя. "
            "Крупная (125 МБ вместо 39) заметно устойчивее к акценту."
        )

        self.autoskip_check = QCheckBox("Пропускать служебные строки по таймеру")
        self.autoskip_check.setChecked(settings.auto_skip_service_lines)
        self.autoskip_check.setToolTip(
            "«Припев», «Проигрыш», схемы боя спеть нельзя, и голос их не сдвинет. "
            "Если в строке указана длительность («Вступление 8 сек»), берётся она."
        )

        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0.0, 10.0)
        self.delay_spin.setSingleStep(0.1)
        self.delay_spin.setDecimals(1)
        self.delay_spin.setSuffix(" с")
        self.delay_spin.setValue(settings.service_line_delay)

        self.debug_check = QCheckBox("Показывать распознанные слова")
        self.debug_check.setChecked(settings.show_debug_log)

        self.robots_check = QCheckBox("Уважать robots.txt сайтов")
        self.robots_check.setChecked(settings.respect_robots)
        self.robots_check.setToolTip(
            "Некоторые сайты запрещают автоматические запросы к страницам поиска. "
            "Если выключить, приложение перестанет спрашивать у сайта разрешения."
        )

        модели = QLabel(
            "Модели распознавания:\n"
            + "\n".join(f"   • {describe_model_state(язык)}" for язык in MODEL_CATALOG)
        )
        модели.setObjectName("Hint")

        порог_подсказка = QLabel(
            "Чем ниже порог, тем охотнее программа листает вперёд — "
            "но и тем чаще ошибается на похожих строках."
        )
        порог_подсказка.setObjectName("Hint")
        порог_подсказка.setWordWrap(True)
        # Без запаса по высоте перенесённая строка обрезается на полуслове
        порог_подсказка.setMinimumHeight(36)

        form = QFormLayout()
        form.setSpacing(12)
        form.addRow(self.voice_check)
        form.addRow("Распознавание:", self.engine_combo)
        form.addRow("Порог совпадения:", self._with_value(self.threshold_slider, self.threshold_value))
        form.addRow("", порог_подсказка)
        form.addRow(self.vocabulary_check)
        form.addRow(self.accurate_check)
        form.addRow(self.denoise_check)
        form.addRow("Микрофон:", self.device_combo)
        form.addRow(self.autoskip_check)
        form.addRow("Держать служебную строку:", self.delay_spin)
        form.addRow("Размер шрифта:", self.font_spin)
        form.addRow("Строк до и после:", self.context_spin)
        form.addRow("Плавность прокрутки:", self.animation_spin)
        form.addRow(self.debug_check)
        form.addRow(self.robots_check)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("Primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        заголовок = QLabel("Настройки")
        заголовок.setObjectName("Title")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 20)
        layout.setSpacing(18)
        layout.addWidget(заголовок)
        layout.addLayout(form)
        layout.addWidget(модели)
        layout.addWidget(buttons)

    @staticmethod
    def _with_value(slider: QSlider, label: QLabel) -> QWidget:
        контейнер = QWidget()
        строка = QVBoxLayout(контейнер)
        строка.setContentsMargins(0, 0, 0, 0)
        строка.addWidget(slider)
        строка.addWidget(label)
        return контейнер

    def _update_threshold_label(self, value: int) -> None:
        self.threshold_value.setText(f"{value} % слов строки")
        self.threshold_value.setObjectName("Hint")

    def _save(self) -> None:
        self.settings.voice_scroll_enabled = self.voice_check.isChecked()
        self.settings.speech_engine = str(self.engine_combo.currentData())
        self.settings.threshold = self.threshold_slider.value() / 100
        self.settings.input_device = int(self.device_combo.currentData())
        self.settings.limit_vocabulary = self.vocabulary_check.isChecked()
        self.settings.accurate_model = self.accurate_check.isChecked()
        self.settings.denoise = self.denoise_check.isChecked()
        self.settings.auto_skip_service_lines = self.autoskip_check.isChecked()
        self.settings.service_line_delay = self.delay_spin.value()
        self.settings.font_size = self.font_spin.value()
        self.settings.context_lines = self.context_spin.value()
        self.settings.scroll_animation_ms = self.animation_spin.value()
        self.settings.show_debug_log = self.debug_check.isChecked()
        self.settings.respect_robots = self.robots_check.isChecked()
        self.settings.save()
        self.accept()
