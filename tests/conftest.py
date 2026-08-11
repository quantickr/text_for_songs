"""Общая настройка тестов.

Qt поднимается в режиме offscreen: тестам не нужен ни экран, ни оконный сервер.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    """Одно приложение Qt на всю сессию тестов."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
