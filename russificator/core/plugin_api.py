"""Плагинная архитектура: каждый поддерживаемый движок — плагин.

Плагин отвечает ровно за три вещи:
  1. detect()  — распознать, что игра сделана на его движке, и сказать, насколько уверен;
  2. extract() — извлечь весь переводимый текст в универсальный формат;
  3. inject()  — записать перевод обратно в игру так, как понимает этот движок.

Диспетчер (core.dispatcher) опрашивает все зарегистрированные плагины и
выбирает лучший по уверенности. Жёсткого if/else по движкам в ядре нет.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .universal import ExtractionResult, TranslationProject


@dataclass
class Detection:
    """Ответ плагина на запрос «это твой движок?»."""

    confidence: float  # 0..1; 0 = точно не этот движок
    engine_name: str = ""
    details: str = ""  # человекочитаемое обоснование, попадает в отчёт
    notes: List[str] = field(default_factory=list)  # ограничения поддержки

    @classmethod
    def no(cls) -> "Detection":
        return cls(confidence=0.0)


class EnginePlugin(ABC):
    """Базовый класс плагина движка."""

    #: машинное имя движка, например "renpy", "unity", "rpgmaker_mv"
    engine_id: str = ""
    #: человекочитаемое название
    title: str = ""
    #: перевод подставляется в рантайме — внедрение имеет смысл даже без заранее найденных строк
    runtime_translation: bool = False
    #: движок умеет доводить перевод «на лету» через сервер русификатора (Unity/XUnity)
    supports_live: bool = False

    def __init__(self) -> None:
        #: колбэк статуса долгих операций (загрузки и т.п.) — задаёт пайплайн
        self.status = lambda message, fraction=None: None
        #: событие отмены (threading.Event) — задаёт пайплайн
        self.cancel = None

    @abstractmethod
    def detect(self, game_dir: Path) -> Detection:
        """Определить по содержимому папки, подходит ли плагин."""

    @abstractmethod
    def extract(self, game_dir: Path, project: TranslationProject) -> ExtractionResult:
        """Извлечь весь переводимый текст в универсальный формат.

        Плагин сам заполняет project.entries. Никаких файлов игры
        модифицировать на этом этапе нельзя.
        """

    def pre_extract(self, game_dir: Path, project: TranslationProject) -> Optional[Path]:
        """Подготовить игру к извлечению (например, распаковать упакованный .exe).

        Возвращает путь к папке, с которой нужно работать дальше (может
        отличаться от исходной, если игра была распакована во временную
        папку). По умолчанию — None (работаем с исходной папкой).
        """
        return None

    @abstractmethod
    def inject(self, game_dir: Path, project: TranslationProject) -> None:
        """Внедрить перевод в игру.

        Должен быть максимально неразрушающим (перевод хранится отдельно
        там, где движок это позволяет). Всё, что не удалось внедрить,
        фиксируется в project.warnings / project.errors, но не должно
        ломать игру целиком.
        """

    # ---------- необязательные возможности ----------

    def install_font(self, game_dir: Path, project: TranslationProject,
                     font_path: Path) -> bool:
        """Подключить кириллический шрифт. По умолчанию — нет.

        Возвращает True, если шрифт удалось подключить.
        """
        return False

    def post_inject_instructions(self) -> List[str]:
        """Что пользователь может понадобиться сделать руками после внедрения."""
        return []


def run_detection(game_dir: Path, plugins: List[EnginePlugin]) -> tuple[EnginePlugin, Detection]:
    """Опросить плагины и выбрать лучший по уверенности.

    Возвращает (плагин, его Detection). Бросает EngineNotDetectedError,
    если ни один плагин не подошёл — вызывающий код обязан явно сообщить
    пользователю, что движок не распознан.
    """
    best: Optional[tuple[EnginePlugin, Detection]] = None
    for plugin in plugins:
        try:
            det = plugin.detect(Path(game_dir))
        except Exception as exc:  # ошибка одного плагина не должна ронять остальные
            det = Detection(confidence=0.0, engine_name=plugin.engine_id,
                            details=f"ошибка при проверке: {exc}")
        if det.confidence <= 0:
            continue
        if best is None or det.confidence > best[1].confidence:
            best = (plugin, det)
    if best is None:
        raise EngineNotDetectedError(Path(game_dir), [p.title or p.engine_id for p in plugins])
    return best


class EngineNotDetectedError(Exception):
    """Движок игры не распознан ни одним плагином."""

    def __init__(self, game_dir: Path, known_engines: List[str]):
        self.game_dir = game_dir
        self.known_engines = known_engines
        super().__init__(
            f"Не удалось определить движок игры в папке {game_dir}. "
            f"Поддерживаемые движки: {', '.join(e for e in known_engines if e)}."
        )
