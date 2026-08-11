"""Поиск, проверка и загрузка моделей vosk.

Модели не кладутся в репозиторий — они весят десятки мегабайт. Программа сама
находит уже скачанную модель, а если её нет, предлагает загрузить с сайта vosk.
"""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import requests

# Куда складываем модели: папка models рядом с проектом
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = PROJECT_ROOT / "models"

# Альтернативные места, где модель могла быть распакована вручную
_EXTRA_SEARCH_DIRS = (
    Path.home() / ".cache" / "vosk",
    Path.home() / "Downloads",
)


@dataclass(frozen=True)
class ModelSpec:
    """Описание модели vosk для конкретного языка."""

    language: str
    name: str
    url: str
    size_mb: int
    description: str


# Маленькие модели: их хватает для суфлёра и они работают в реальном времени
# на обычном ноутбуке. Большие (1.8 ГБ) для этой задачи избыточны.
MODEL_CATALOG: dict[str, ModelSpec] = {
    "ru": ModelSpec(
        language="ru",
        name="vosk-model-small-ru-0.22",
        url="https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",
        size_mb=45,
        description="Русская модель",
    ),
    "en": ModelSpec(
        language="en",
        name="vosk-model-small-en-us-0.15",
        url="https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
        size_mb=40,
        description="Английская модель",
    ),
}

# Модели покрупнее. Нужны прежде всего для английского: маленькая модель
# рассчитана на чистое произношение носителя и заметно спотыкается на акценте.
# Важно, что lgraph-вариант поддерживает динамическую грамматику — без этого
# не работало бы ограничение словаря словами песни.
ACCURATE_CATALOG: dict[str, ModelSpec] = {
    "en": ModelSpec(
        language="en",
        name="vosk-model-en-us-0.22-lgraph",
        url="https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip",
        size_mb=128,
        description="Английская модель (точнее, для акцента)",
    ),
}


def model_spec(language: str, accurate: bool = False) -> ModelSpec | None:
    """Описание модели для языка: обычной или точной."""
    if accurate and language in ACCURATE_CATALOG:
        return ACCURATE_CATALOG[language]
    return MODEL_CATALOG.get(language)


def has_accurate_model(language: str) -> bool:
    return language in ACCURATE_CATALOG


class ModelError(Exception):
    """Проблема с моделью: не найдена, битая, не скачалась."""


def is_valid_model(path: Path) -> bool:
    """Похожа ли папка на распакованную модель vosk.

    Проверяем ключевые файлы акустической модели и конфигурации — именно их
    отсутствие означает, что архив распакован не полностью или выбрана не та папка.
    """
    if not path.is_dir():
        return False
    has_am = (path / "am" / "final.mdl").exists() or (path / "am-onnx").is_dir()
    has_conf = (path / "conf" / "mfcc.conf").exists() or (path / "conf" / "model.conf").exists()
    return has_am and has_conf


def _candidate_dirs(spec: ModelSpec) -> Iterable[Path]:
    """Где искать уже распакованную модель."""
    yield MODELS_DIR / spec.name
    for directory in _EXTRA_SEARCH_DIRS:
        yield directory / spec.name
    # Иногда архив распаковывают во вложенную папку с тем же именем
    yield MODELS_DIR / spec.name / spec.name


def find_model(language: str, prefer_accurate: bool = False) -> Path | None:
    """Найти уже скачанную модель для языка. Вернуть ``None``, если её нет.

    При ``prefer_accurate`` сначала ищем модель покрупнее, а если её не
    скачали — спокойно откатываемся к обычной, чтобы суфлёр не остался
    вообще без распознавания.
    """
    specs = []
    if prefer_accurate and language in ACCURATE_CATALOG:
        specs.append(ACCURATE_CATALOG[language])
    if language in MODEL_CATALOG:
        specs.append(MODEL_CATALOG[language])
    if not specs:
        return None

    for spec in specs:
        for candidate in _candidate_dirs(spec):
            if is_valid_model(candidate):
                return candidate

    # Запасной путь: любая папка в models/, подходящая по языку
    if MODELS_DIR.is_dir():
        for child in sorted(MODELS_DIR.iterdir()):
            if child.is_dir() and f"-{language}-" in child.name and is_valid_model(child):
                return child
    return None


def missing_languages(languages: Iterable[str]) -> list[str]:
    """Из перечисленных языков вернуть те, для которых модели нет."""
    return [lang for lang in languages if lang in MODEL_CATALOG and find_model(lang) is None]


def download_model(
    language: str,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    accurate: bool = False,
) -> Path:
    """Скачать и распаковать модель. Вернуть путь к готовой папке.

    Аргументы:
        progress: вызывается как ``progress(скачано_байт, всего_байт)``;
            ``всего_байт`` равно нулю, если сервер не сообщил размер.
        should_cancel: если возвращает истину, загрузка прерывается.
    """
    spec = model_spec(language, accurate)
    if spec is None:
        raise ModelError(f"Нет описания модели для языка «{language}»")

    for candidate in _candidate_dirs(spec):
        if is_valid_model(candidate):
            return candidate

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    archive = MODELS_DIR / f"{spec.name}.zip.part"

    try:
        _download_file(spec.url, archive, progress, should_cancel)
        _extract_archive(archive, MODELS_DIR)
    except ModelError:
        raise
    except requests.RequestException as error:
        raise ModelError(
            f"Не удалось скачать модель: {error}.\n"
            f"Можно скачать вручную: {spec.url}\n"
            f"и распаковать в {MODELS_DIR}"
        ) from error
    finally:
        archive.unlink(missing_ok=True)

    model_path = find_model(language)
    if model_path is None:
        raise ModelError(
            f"Архив распакован, но модель не найдена в {MODELS_DIR}. "
            "Проверьте содержимое папки."
        )
    return model_path


def _download_file(
    url: str,
    target: Path,
    progress: Callable[[int, int], None] | None,
    should_cancel: Callable[[], bool] | None,
) -> None:
    """Скачать файл потоком, сообщая о прогрессе."""
    with requests.get(url, stream=True, timeout=30) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0

        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 16):
                if should_cancel is not None and should_cancel():
                    raise ModelError("Загрузка отменена")
                if not chunk:
                    continue
                handle.write(chunk)
                downloaded += len(chunk)
                if progress is not None:
                    progress(downloaded, total)


def _extract_archive(archive: Path, destination: Path) -> None:
    """Распаковать zip, не позволяя ему писать за пределы папки назначения."""
    destination = destination.resolve()
    try:
        with zipfile.ZipFile(archive) as zip_file:
            for member in zip_file.namelist():
                target = (destination / member).resolve()
                if not target.is_relative_to(destination):
                    raise ModelError(f"Архив содержит небезопасный путь: {member}")
            zip_file.extractall(destination)
    except zipfile.BadZipFile as error:
        raise ModelError(f"Скачанный архив повреждён: {error}") from error


def remove_model(language: str) -> None:
    """Удалить скачанную модель (нужно, если она побилась)."""
    path = find_model(language)
    if path is not None and path.is_relative_to(MODELS_DIR):
        shutil.rmtree(path, ignore_errors=True)


def describe_model_state(language: str) -> str:
    """Человеческое описание состояния модели для интерфейса."""
    spec = MODEL_CATALOG.get(language)
    if spec is None:
        return f"Язык «{language}» не поддерживается"
    path = find_model(language)
    if path is None:
        return f"{spec.description} не скачана (~{spec.size_mb} МБ)"
    return f"{spec.description}: {path.name}"
