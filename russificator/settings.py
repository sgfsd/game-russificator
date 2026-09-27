"""Настройки программы (JSON рядом с данными) и хранение API-ключей.

API-ключи на Windows шифруются DPAPI (привязаны к учётной записи
пользователя — скопированный на другой ПК файл настроек ключ не раскроет).
На других ОС ключ хранится как есть, но только если пользователь сам
попросил его запомнить.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any, Dict

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
}


def load() -> Dict[str, Any]:
    data = dict(DEFAULTS)
    try:
        raw = json.loads(paths.settings_file().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data.update(raw)
    except (OSError, ValueError):
        pass
    return data


def save(data: Dict[str, Any]) -> None:
    f = paths.settings_file()
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)


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
