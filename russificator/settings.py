"""Настройки программы (JSON рядом с данными) и хранение API-ключей.

API-ключи на Windows шифруются DPAPI (привязаны к учётной записи
пользователя — скопированный на другой ПК файл настроек ключ не раскроет).
На других ОС ключ хранится как есть, но только если пользователь сам
попросил его запомнить.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Tuple

from . import paths

DEFAULTS: Dict[str, Any] = {
    "game_dir": "",
    "mode": "machine",               # machine | local | cloud
    "local_model": "",               # id пресета локальной нейросети
    "local_gpu": True,               # использовать видеокарту (Vulkan)
    "local_custom_model": "",        # свой .gguf вместо пресета
    "cloud_provider": "deepseek",
    "cloud_base_url": "",
    "cloud_model": "",
    "cloud_keys": {},                # провайдер -> зашифрованный ключ
    "cloud_parallel": 4,             # одновременных запросов к API
    "font_path": "",
    "data_dir": "",                  # своя папка для моделей и кэша
    "threads": 0,                    # 0 = авто
    "export_dir": "",                # куда сохранять архивы-русификаторы (пусто — рабочий стол)
    "export_credit": True,           # надпись о программе в игре (в архивах «для друзей»)
    "library_folders": [],           # «Мои игры»: свои папки для поиска (например, D:\\Games)
    "library_manual": [],            # «Мои игры»: игры, добавленные вручную
    "library_hidden": [],            # «Мои игры»: скрытые из списка
    "live_enabled": False,           # живой перевод (оверлей) включён — запускать вместе с программой
    "live_autostart": False,         # запускать живой перевод вместе с Windows
    "live_mode": "auto",             # способ перевода в оверлее: auto | machine | cloud | local
    "live_font_scale": 1.0,
    "live_opacity": 0.86,
    "live_interval": 0.6,
    "live_always": [],               # exe, которые переводить всегда
    "live_never": [],                # exe, которые не переводить никогда
    "live_profiles": {},             # exe -> {"region": [x, y, w, h] доли окна}
    "live_deep": True,               # глубокий проход: надписи на картинках и текстурах, особые шрифты
    "check_updates": True,           # раз в 12 часов проверять новую версию на GitHub
    "update_checked_at": 0,
    "update_latest": {},
}


# Файл настроек пишут два процесса: окно программы и живой перевод (трей).
# Чтобы один не затирал изменения другого, save() сливает: ключи, которые этот
# словарь не менял с момента чтения, берутся из файла (их мог поменять другой).
# Снимок «как было при чтении» хранится для каждого словаря, полученного из load().
_base: Dict[int, Tuple[Dict[str, Any], Dict[str, Any]]] = {}


def _read(f: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember(data: Dict[str, Any], snapshot: Dict[str, Any]) -> None:
    _base[id(data)] = (data, snapshot)
    while len(_base) > 16:                      # временные словари не копятся
        _base.pop(next(iter(_base)))


def read() -> Dict[str, Any]:
    """Свежие настройки только для чтения (сохранять их через save() не нужно)."""
    data = dict(DEFAULTS)
    data.update(_read(paths.settings_file()))
    return data


def load() -> Dict[str, Any]:
    data = read()
    _remember(data, json.loads(json.dumps(data)))
    return data


def save(data: Dict[str, Any]) -> None:
    """Сохранить настройки; ``data`` обновляется свежими значениями, изменёнными другим процессом."""
    f = paths.settings_file()
    entry = _base.get(id(data))
    base = entry[1] if entry is not None and entry[0] is data else None
    if base is not None:
        disk = _read(f)
        for k, v in disk.items():
            if k in base and data.get(k) == base[k] and v != base[k]:
                data[k] = v                     # изменено другим процессом, а здесь — нет
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = f.with_name(f"{f.stem}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(8):
        try:
            tmp.replace(f)
            break
        except PermissionError:                 # Windows: файл как раз читает другой процесс
            if attempt == 7:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05 * (attempt + 1))
    _base.pop(id(data), None)
    _remember(data, json.loads(text))


def apply_data_dir(data: Dict[str, Any]) -> None:
    """Применить пользовательскую папку данных (если задана и доступна)."""
    d = (data.get("data_dir") or "").strip()
    paths.set_home(Path(d) if d else None)


# ---------- секреты ----------

def protect(secret: str) -> str:
    if not secret:
        return ""
    raw = secret.encode("utf-8")
    if sys.platform == "win32":
        try:
            return "dpapi:" + base64.b64encode(_dpapi(raw, encrypt=True)).decode("ascii")
        except OSError:
            pass
    return "plain:" + base64.b64encode(raw).decode("ascii")


def unprotect(stored: str) -> str:
    if not stored:
        return ""
    try:
        kind, _, payload = stored.partition(":")
        raw = base64.b64decode(payload)
        if kind == "dpapi":
            return _dpapi(raw, encrypt=False).decode("utf-8")
        if kind == "plain":
            return raw.decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI недоступен")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)
