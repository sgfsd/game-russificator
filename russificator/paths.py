"""Где программа хранит свои данные: модели, кэш переводов, настройки, логи.

Программа портативная: по умолчанию всё лежит рядом с ней (папка
``RussificatorData`` возле exe или ``userdata`` в корне исходников), чтобы
гигабайтные модели не забивали системный диск. Если рядом писать нельзя
(например, программа лежит в Program Files) — используется профиль
пользователя. Переменная окружения ``RUSSIFICATOR_HOME`` и настройка
«Папка данных» имеют приоритет.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

_override: Optional[Path] = None


def is_frozen() -> bool:
    """Запущены из собранного exe (PyInstaller)."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Папка с ресурсами программы (web-интерфейс, словари)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _default_home() -> Path:
    env = os.environ.get("RUSSIFICATOR_HOME")
    if env:
        return Path(env)
    if is_frozen():
        portable = Path(sys.executable).resolve().parent / "RussificatorData"
    else:
        portable = Path(__file__).resolve().parent.parent / "userdata"
    if _writable(portable):
        return portable
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "GameRussificator"


def set_home(path: Optional[Path]) -> None:
    """Переопределить папку данных (из настроек пользователя)."""
    global _override
    _override = Path(path) if path else None


def app_home() -> Path:
    home = _override or _default_home()
    home.mkdir(parents=True, exist_ok=True)
    return home


def sub(name: str) -> Path:
    d = app_home() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def models_dir() -> Path:
    return sub("models")


def tools_dir() -> Path:
    return sub("tools")


def cache_dir() -> Path:
    return sub("cache")


def logs_dir() -> Path:
    return sub("logs")


def settings_file() -> Path:
    """Настройки живут в «родной» папке, чтобы смена папки данных их не теряла."""
    home = _default_home()
    home.mkdir(parents=True, exist_ok=True)
    return home / "settings.json"
