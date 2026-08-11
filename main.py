"""Точка входа суфлёра.

Запуск::

    python main.py
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QApplication

from prompter import APP_NAME, ORG_DOMAIN, ORG_NAME
from prompter.ui.main_window import MainWindow
from prompter.ui.theme import STYLESHEET


def main() -> int:
    app = QApplication(sys.argv)

    # Эти имена должны быть заданы до первого обращения к QSettings:
    # именно по ним определяется, куда сохранять настройки
    QCoreApplication.setOrganizationName(ORG_NAME)
    QCoreApplication.setOrganizationDomain(ORG_DOMAIN)
    QCoreApplication.setApplicationName(APP_NAME)

    # Fusion даёт одинаковый вид на macOS, Windows и Linux
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
