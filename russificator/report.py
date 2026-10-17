"""Отчёт об ошибке — готовый текст для issue на GitHub.

Собирается из того, что программа знает о себе: версия, Windows, железо, способ
перевода, игра и её движок, хвост журнала. Личное в отчёт не попадает: ключи API
вычищаются (и известные программе, и всё, что похоже на ключ), домашняя папка
заменяется на ``%USERPROFILE%``, имя компьютера — на ``%COMPUTERNAME%``.
"""

from __future__ import annotations

import os
import platform
import re
import struct
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

LOG_LINES = 80
HOME_MARK = "%USERPROFILE%"
KEY_MARK = "<ключ удалён>"

# то, что похоже на ключ API или токен, даже если программа его не знает: сам ключ целиком…
_KEY_LIKE = [
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_\-]{16,}"),                     # OpenAI, DeepSeek, OpenRouter, Anthropic
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),                              # Google
    re.compile(r"\b(?:gsk|hf|xai|ghp|gho|github_pat)_[A-Za-z0-9_]{16,}"),  # Groq, Hugging Face, xAI, GitHub
]
# …и значение после «ключевого» слова (слово остаётся — видно, что ключ был)
_KEY_AFTER = [
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)(\b(?:x-)?(?:api[-_ ]?key|authorization|access[-_ ]?token|token|secret|password)"
               r"\"?\s*[:=]\s*\"?)(?!bearer\b|<)[^\s\"',;&]{6,}"),
    re.compile(r"(?i)([?&](?:key|api_key|token|access_token)=)(?!<)[^&\s]+"),
]
_USERS_DIR = re.compile(r"(?i)\b[a-z]:[\\/]+(?:users|documents and settings)[\\/]+[^\\/\s\"'<>|:*?]+")
_EMAIL = re.compile(r"\b[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)*\.[a-z]{2,}\b", re.I)


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """Убрать из текста ключи, домашнюю папку, имя компьютера и почту."""
    for s in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(s, KEY_MARK)
    for rx in _KEY_LIKE:
        text = rx.sub(KEY_MARK, text)
    for rx in _KEY_AFTER:
        text = rx.sub(lambda m: m.group(1) + KEY_MARK, text)
    home = str(Path.home())
    if len(home) > 3:                                          # домашняя папка — в обоих видах слэшей
        for variant in {home, home.replace("\\", "/"), home.replace("/", "\\")}:
            text = re.sub(re.escape(variant), lambda _m: HOME_MARK, text, flags=re.I)
    text = _USERS_DIR.sub(lambda _m: HOME_MARK, text)         # и чужие профили: C:\Users\<кто-то>
    # имя компьютера (DESKTOP-1A2B3C); имя пользователя отдельно не ищем — «User», «Admin» совпали бы
    # с обычными словами журнала, а в путях оно уже скрыто вместе с домашней папкой
    pc = os.environ.get("COMPUTERNAME", "")
    if len(pc) >= 5:
        text = re.sub(rf"(?i)(?<![\w%]){re.escape(pc)}(?![\w%])", lambda _m: "%COMPUTERNAME%", text)
    return _EMAIL.sub("<почта>", text)


def windows_name() -> str:
    """«Windows 10 Pro 22H2 (10.0.19045), 64-бит» — версия системы и разрядность."""
    bits = "64-бит" if platform.machine().endswith("64") else "32-бит"
    if sys.platform != "win32":
        return f"{platform.system()} {platform.release()} ({platform.machine()})"
    name, display = "", ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            def val(n: str) -> str:
                try:
                    return str(winreg.QueryValueEx(k, n)[0])
                except OSError:
                    return ""
            name, display = val("ProductName"), val("DisplayVersion") or val("ReleaseId")
    except OSError:
        pass
    build = sys.getwindowsversion().build
    if name.startswith("Windows 10") and build >= 22000:      # в реестре Windows 11 до сих пор «Windows 10»
        name = "Windows 11" + name[len("Windows 10"):]
    name = name or f"Windows {platform.release()}"
    return f"{name} {display}".strip() + f" ({platform.version()}), {bits}"


def gpu_names() -> List[str]:
    """Видеокарты по данным Windows (без запуска сторонних программ)."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                    ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                    ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]

    names: List[str] = []
    try:
        enum = ctypes.windll.user32.EnumDisplayDevicesW
        for i in range(16):
            dev = DISPLAY_DEVICEW()
            dev.cb = ctypes.sizeof(dev)
            if not enum(None, i, ctypes.byref(dev), 0):
                break
            name = dev.DeviceString.strip()
            if name and not (dev.StateFlags & 0x8) and name not in names:   # 0x8 — псевдоустройство зеркала
                names.append(name)
    except (OSError, AttributeError):
        pass
    return names


def log_tail(path: Path, lines: int = LOG_LINES) -> List[str]:
    """Последние строки журнала (большой файл читается с конца, не целиком)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            data = f.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    out = text.split("\n")
    if size > len(data):
        out = out[1:]                                          # первая строка обрезана посередине
    while out and not out[-1].strip():
        out.pop()
    return out[-lines:]


def _fence(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}text\n{text}\n{fence}"


def build(facts: Dict[str, Any], log_file: Optional[Path] = None, prev_log: Optional[Path] = None,
          secrets: Iterable[str] = ()) -> str:
    """Markdown для issue. ``facts`` — пары «что: значение» по разделам: ``system``, ``translation``,
    ``game`` (значение ``None`` или пустое пропускается)."""
    def rows(section: str) -> List[str]:
        return [f"- **{k}:** {v}" for k, v in (facts.get(section) or {}).items() if v not in (None, "")]

    parts = [
        "### Что случилось",
        "",
        "<!-- Своими словами: что делали, что ожидали и что получилось. Можно приложить скриншот. -->",
        "",
        "### Программа и компьютер",
        "",
        *rows("system"),
        "",
        "### Перевод",
        "",
        *rows("translation"),
        "",
        "### Игра",
        "",
        *(rows("game") or ["- игра не выбрана"]),
        "",
    ]
    tail = log_tail(log_file) if log_file is not None else []
    parts += ["### Журнал (конец russificator.log)", ""]
    parts.append(_fence("\n".join(tail)) if tail else "Журнал пуст или не найден.")
    if prev_log is not None:
        try:
            kb = max(1, prev_log.stat().st_size // 1024) if prev_log.is_file() else 0
        except OSError:
            kb = 0
        parts += ["", f"Журнал прошлого запуска (russificator.prev.log): "
                      + (f"есть, {kb} КБ — приложу, если нужно." if kb else "нет.")]
    return scrub("\n".join(parts).rstrip() + "\n", secrets)


def program_build() -> str:
    """Как запущена программа: собранный exe или исходники, и разрядность."""
    from . import paths
    bits = f"{struct.calcsize('P') * 8}-бит"
    if paths.is_frozen():
        return f"exe, {bits}"
    return f"из исходников, Python {platform.python_version()} {bits}"
