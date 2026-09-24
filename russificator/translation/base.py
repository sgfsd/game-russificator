"""Интерфейс переводчика — общий для всех четырёх способов перевода.

Слой перевода ничего не знает о движках: получает строки универсального
формата (:class:`Entry`) и возвращает переводы. Подготовка (скачать модель,
запустить локальный сервер, проверить ключ) вынесена в :meth:`prepare`,
чтобы интерфейс мог показать прогресс и ошибку до начала работы.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Optional

from ..core.universal import Entry

#: колбэк статуса подготовки: (сообщение, доля_готовности_0..1 или None)
StatusFn = Callable[[str, Optional[float]], None]


def _noop(message: str, fraction: Optional[float] = None) -> None:
    pass


class TranslatorError(RuntimeError):
    """Перевод невозможен (нет модели, неверный ключ, кончились деньги...).

    ``fatal=True`` — продолжать бессмысленно: пайплайн останавливается
    сразу, а не «проваливает» по одной тысячи строк.
    """

    def __init__(self, message: str, fatal: bool = True):
        super().__init__(message)
        self.fatal = fatal


class Translator(ABC):
    """Переводчик английского игрового текста на русский."""

    #: стабильный идентификатор (с моделью) — ключ кэша переводов
    cache_id: str = ""
    #: название для интерфейса и отчёта
    title: str = ""
    #: понимает контекст (говорящий, соседние реплики) — LLM
    context_aware: bool = False
    #: лимиты одного запроса
    max_batch_items: int = 30
    max_batch_chars: int = 3000
    #: сколько батчей можно переводить одновременно
    parallel: int = 1

    def prepare(self, status: StatusFn = _noop, cancel: Optional[threading.Event] = None) -> None:
        """Подготовить переводчик к работе (скачать, запустить, проверить)."""

    @abstractmethod
    def translate(self, entries: List[Entry], glossary: Dict[str, str]) -> Dict[str, str]:
        """Перевести батч. Вернуть {entry.id: перевод}.

        Строку, которую перевести не удалось, можно не включать в результат —
        сервис перевода повторит её отдельно или пометит как FAILED.
        """

    def shorten(self, text: str, limit: int, source: str) -> Optional[str]:
        """Сократить перевод до ``limit`` символов (если переводчик умеет)."""
        return None

    def close(self) -> None:
        """Освободить ресурсы (остановить локальный сервер и т.п.)."""
