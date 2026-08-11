"""Сборка приложения в ``.app``-бандл для macOS.

Нужна ровно ради одного: системное распознавание речи Apple работает, только
когда процесс принадлежит бандлу с объявленным ``NSSpeechRecognitionUsageDescription``.
Запущенный как ``python main.py`` суфлёр этот движок использовать не сможет —
macOS убьёт процесс при первом обращении, даже не показав диалога.

Запуск::

    python build_app.py

Собранное приложение появится рядом: ``Суфлёр.app``. Внутри него лежит не копия
питона, а запускающий скрипт, который зовёт интерпретатор из ``.venv`` — поэтому
пересобирать бандл после правок кода не нужно.
"""

from __future__ import annotations

import plistlib
import shutil
import stat
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent
ИМЯ = "Суфлёр"
ИДЕНТИФИКАТОР = "local.songprompter.app"

INFO_PLIST = {
    "CFBundleName": "SongPrompter",
    "CFBundleDisplayName": ИМЯ,
    "CFBundleIdentifier": ИДЕНТИФИКАТОР,
    "CFBundleExecutable": "run",
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": "1.0",
    "CFBundleVersion": "1",
    "LSMinimumSystemVersion": "10.15",
    # Без этих двух строк macOS молча убивает процесс при запросе доступа
    "NSSpeechRecognitionUsageDescription": (
        "Суфлёр распознаёт слова песни, чтобы листать текст по голосу."
    ),
    "NSMicrophoneUsageDescription": (
        "Суфлёр слушает микрофон, чтобы понимать, какую строку вы поёте."
    ),
    # Приложение с окном, а не фоновая служба
    "LSUIElement": False,
    "NSHighResolutionCapable": True,
}

ЗАПУСКАТОР = """#!/bin/zsh
# Зовём интерпретатор из виртуального окружения проекта, чтобы бандл
# не приходилось пересобирать после каждой правки кода.
cd "{корень}" || exit 1
exec "{питон}" "{корень}/main.py" "$@"
"""


def собрать(корень: Path = КОРЕНЬ) -> Path:
    """Собрать бандл и вернуть путь к нему."""
    питон = корень / ".venv" / "bin" / "python"
    if not питон.exists():
        raise SystemExit(
            f"Не найден интерпретатор {питон}.\n"
            "Сначала создайте окружение: python3.12 -m venv .venv && "
            ".venv/bin/pip install -r requirements.txt"
        )

    бандл = корень / f"{ИМЯ}.app"
    if бандл.exists():
        shutil.rmtree(бандл)

    macos = бандл / "Contents" / "MacOS"
    macos.mkdir(parents=True)

    (бандл / "Contents" / "Info.plist").write_bytes(plistlib.dumps(INFO_PLIST))

    запуск = macos / "run"
    запуск.write_text(
        ЗАПУСКАТОР.format(корень=корень, питон=питон), encoding="utf-8"
    )
    запуск.chmod(запуск.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return бандл


def main() -> int:
    if sys.platform != "darwin":
        print("Бандл нужен только на macOS — на других системах запускайте python main.py")
        return 1

    бандл = собрать()
    print(f"Собрано: {бандл}")
    print()
    print("Запуск:  open " + str(бандл).replace(" ", r"\ "))
    print()
    print("При первом запуске macOS спросит доступ к микрофону и распознаванию речи.")
    print("После этого в настройках можно выбрать системный движок распознавания —")
    print("он заметно точнее, особенно когда рядом звучит инструмент.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
