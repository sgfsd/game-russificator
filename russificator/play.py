"""Живой перевод без окна программы: режимы ``--play`` и ``--serve``.

``--play <папка игры>`` — ярлык «Играть на русском» (см. ``core/launcher.py``):
сервер перевода начинает слушать сразу, затем запускается игра, а когда она
закрывается — всё останавливается.

``--serve <папка игры> --pid <процесс игры>`` — так сервер запускает сама игра:
плагин русификатора в BepInEx (``resources/unity/RussificatorUnity.cs``) при
старте игры видит, что перевод не запущен, и запускает эту команду. Поэтому
живой перевод работает, как бы игру ни запустили — из Steam, ярлыком, exe.

Порт открывается за доли секунды (XUnity не получит отказов, пока модель
загружается), переводчик готовится в фоне. Способ перевода берётся из
настроек программы; если он недоступен (нет ключа, модель удалена) —
машинный перевод.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import paths, settings

log = logging.getLogger("russificator.play")


def _setup_logging(name: str = "play.log") -> None:
    handler = logging.FileHandler(paths.logs_dir() / name, mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _options(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Настройки переводчика для живого перевода (с запасным машинным переводом) или None."""
    from .translation import local_llm, machine
    from .translation.cloud import PROVIDERS
    opts = dict(cfg)
    mode = cfg.get("mode") or "machine"
    if mode == "cloud":
        provider = PROVIDERS.get(cfg.get("cloud_provider", ""))
        key = settings.unprotect((cfg.get("cloud_keys") or {}).get(cfg.get("cloud_provider", ""), ""))
        if provider is None or (provider.needs_key and not key):
            log.info("ключ облачного провайдера не сохранён — живой перевод машинный")
            mode = "machine"
        else:
            opts["api_key"] = key
    elif mode == "local":
        preset = (local_llm.PRESET_BY_ID.get(cfg.get("local_model", ""))
                  or local_llm.PRESET_BY_ID[local_llm.recommend_preset()])
        custom = cfg.get("local_custom_model", "")
        if local_llm.server_exe() is None or not ((custom and Path(custom).is_file())
                                                  or (preset and local_llm.model_installed(preset))):
            log.info("локальная нейросеть не установлена — живой перевод машинный")
            mode = "machine"
    if mode == "machine" and not machine.is_installed():
        return None
    opts["mode"] = mode
    return opts


def _game_exe(game_dir: Path) -> Optional[Path]:
    from .engines.unity import detect as unity_detect
    info = unity_detect.inspect(game_dir)
    if info is not None and info.exe is not None:
        return info.exe
    exes = [p for p in game_dir.glob("*.exe")
            if not any(w in p.name.lower() for w in ("unins", "crash", "setup", "redist"))]
    return exes[0] if exes else None


def _running(exe: Path) -> bool:
    """Запущен ли процесс из этого exe (игры со Steam перезапускают себя — ждём по пути exe)."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    pids = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
        return False
    target = str(exe.resolve()).lower()
    buf = ctypes.create_unicode_buffer(1024)
    for pid in pids[:needed.value // ctypes.sizeof(wintypes.DWORD)]:
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        try:
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) and buf.value.lower() == target:
                return True
        finally:
            k32.CloseHandle(h)
    return False


LIVE_PORT = 47631   # = engines.unity.xunity.LIVE_PORT (здесь без импорта движка — быстрее старт)


def _factory(game_title: str):
    """Создать переводчик и память (в фоне, уже после открытия порта)."""
    def make():
        import threading
        from .translation import create_translator
        from .translation.memory import TranslationMemory
        cfg = settings.load()
        settings.apply_data_dir(cfg)
        opts = _options(cfg)
        if opts is None:
            raise RuntimeError("ни один переводчик не установлен — живой перевод недоступен")
        translator = create_translator(opts["mode"], opts, game_title)
        translator.prepare(lambda m, f=None: None, threading.Event())
        log.info("живой перевод: %s", opts["mode"])
        try:
            memory = TranslationMemory(paths.cache_dir() / "translations.db")
        except Exception as exc:  # noqa: BLE001
            log.warning("память переводов недоступна: %s", exc)
            memory = None
        return translator, memory
    return make


def start_server(game_dir: Path):
    """Сервер живого перевода или None (порт уже занят — перевод работает в другой копии)."""
    from .translation.live_server import LiveServer
    server = LiveServer(None, None)
    try:
        server.start(LIVE_PORT, factory=_factory(Path(game_dir).name))
    except OSError:
        log.info("порт %d занят — живой перевод уже запущен", LIVE_PORT)
        return None
    return server


def _process_alive(pid: int) -> bool:
    if sys.platform != "win32":
        import os
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(h, 0) == 0x102   # WAIT_TIMEOUT — процесс жив
    finally:
        k32.CloseHandle(h)


def serve(game_dir: Path, pid: int) -> int:
    """Работать, пока жив процесс игры ``pid`` (его запустила сама игра через плагин)."""
    server = start_server(game_dir)
    if server is None:
        return 0
    log.info("живой перевод для %s (процесс %d)", game_dir, pid)
    try:
        while _process_alive(pid):
            time.sleep(2)
    finally:
        log.info("игра закрыта, переведено на лету: %d", server.count)
        server.stop()
    return 0


def play(game_dir: Path) -> int:
    from .engines.unity import xunity

    game_dir = Path(game_dir)
    exe = _game_exe(game_dir)
    if exe is None:
        log.error("не найден exe игры в %s", game_dir)
        _message(f"Не найден .exe игры в папке:\n{game_dir}")
        return 2

    server = start_server(game_dir) if xunity.is_installed(game_dir) else None

    log.info("запуск %s", exe)
    try:
        proc = subprocess.Popen([str(exe)], cwd=str(exe.parent))
    except OSError as exc:
        _message(f"Не удалось запустить игру:\n{exc}")
        if server is not None:
            server.stop()
        return 1
    proc.wait()
    # игра могла перезапустить себя (Steam) — ждём, пока закроются все её процессы
    idle = 0
    while idle < 2:
        time.sleep(3)
        idle = 0 if _running(exe) else idle + 1
    if server is not None:
        log.info("переведено на лету: %d", server.count)
        server.stop()
    return 0


def _message(text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Русификатор игр", 0x10)
    except Exception:  # noqa: BLE001
        print(text, file=sys.stderr)


def run(game_dir: str, pid: int = 0) -> int:
    """``pid`` задан — режим ``--serve`` (сервер для уже запущенной игры), иначе ``--play``."""
    _setup_logging("serve.log" if pid else "play.log")
    try:
        return serve(Path(game_dir), pid) if pid else play(Path(game_dir))
    except Exception:  # noqa: BLE001
        log.exception("режим %s упал", "--serve" if pid else "--play")
        return 1
