"""Реестр плагинов движков и диспетчер определения движка."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Type

from .plugin_api import Detection, EngineNotDetectedError, EnginePlugin, run_detection

_PLUGINS: List[Type[EnginePlugin]] = []


def register(plugin_cls: Type[EnginePlugin]) -> Type[EnginePlugin]:
    """Декоратор регистрации плагина движка."""
    _PLUGINS.append(plugin_cls)
    return plugin_cls


def all_plugins() -> List[EnginePlugin]:
    """Создать экземпляры всех зарегистрированных плагинов.

    При первом обращении подгружает встроенные плагины движков
    (ленивый импорт, чтобы избежать циклической зависимости).
    """
    _load_builtin_plugins()
    return [cls() for cls in _PLUGINS]


_loaded = False


def _load_builtin_plugins() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        from ..engines.renpy import plugin as _renpy  # noqa: F401
        from ..engines.rpgmaker import plugin as _rpgmaker  # noqa: F401
        from ..engines.unity import plugin as _unity  # noqa: F401
    except ImportError:
        pass


def detect_engine(game_dir: Path) -> tuple[EnginePlugin, Detection]:
    """Определить движок игры по содержимому папки.

    Бросает EngineNotDetectedError, если ни один плагин не подошёл.
    """
    return run_detection(Path(game_dir), all_plugins())
