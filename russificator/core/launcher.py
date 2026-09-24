"""Ярлык «Играть на русском»: запуск игры вместе с живым переводом.

Unity-игры часть текста собирают на лету — его переводит сервер русификатора
(см. ``translation/live_server.py``). Чтобы игроку не нужно было помнить
«сначала открой русификатор», в папке игры создаётся ярлык: он запускает
русификатор без окна (``--play``), тот поднимает перевод, запускает игру и
сам закрывается, когда игра завершится.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from .. import paths

log = logging.getLogger("russificator.launcher")

SHORTCUT_NAME = "Играть на русском.lnk"
CREATE_NO_WINDOW = 0x08000000


def _program() -> Tuple[str, List[str], str]:
    """(программа, аргументы до режима, рабочая папка) — exe сборки или pythonw с модулем окна."""
    if paths.is_frozen():
        exe = Path(sys.executable).resolve()
        return str(exe), [], str(exe.parent)
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    return str(pyw if pyw.is_file() else py), ["-m", "russificator.ui.app"], str(paths.bundle_dir())


def play_command() -> Tuple[str, List[str], str]:
    """(программа, аргументы перед папкой игры, рабочая папка) для режима ``--play``."""
    exe, args, workdir = _program()
    return exe, args + ["--play"], workdir


def serve_command(game_dir: Path) -> Tuple[str, str, str]:
    """(программа, строка аргументов с ``{pid}``, рабочая папка) — для плагина внутри игры."""
    exe, args, workdir = _program()
    line = " ".join(_quote(a) for a in args + ["--serve", str(game_dir), "--pid"]) + " {pid}"
    return exe, line, workdir


def _quote(arg: str) -> str:
    return '"' + arg.replace('"', '\\"') + '"' if (" " in arg or not arg) else arg


def _ps_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def create_shortcut(lnk: Path, game_dir: Path, game_exe: Optional[Path], description: str) -> bool:
    """Создать .lnk через WScript.Shell (PowerShell, команда передаётся в UTF-16 — пути с кириллицей целы)."""
    if sys.platform != "win32" or os.environ.get("RUSSIFICATOR_NO_SHORTCUT"):
        return False
    target, args, workdir = play_command()
    arguments = " ".join(_quote(a) for a in args + [str(game_dir)])
    icon = f"{game_exe},0" if game_exe else f"{target},0"
    script = (
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({_ps_str(str(lnk))});"
        f"$s.TargetPath={_ps_str(target)};$s.Arguments={_ps_str(arguments)};"
        f"$s.WorkingDirectory={_ps_str(workdir)};$s.IconLocation={_ps_str(icon)};"
        f"$s.Description={_ps_str(description)};$s.Save()"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-EncodedCommand", encoded], check=True, timeout=60, capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ярлык не создан: %s", exc)
        return False
    return lnk.is_file()


def desktop_dir() -> Optional[Path]:
    """Рабочий стол пользователя (с учётом перенаправления в OneDrive)."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0:  # CSIDL_DESKTOPDIRECTORY
                return Path(buf.value)
        except Exception:  # noqa: BLE001
            pass
    d = Path.home() / "Desktop"
    return d if d.is_dir() else None


def desktop_shortcut(game_dir: Path, game_exe: Optional[Path], title: str) -> Optional[Path]:
    desk = desktop_dir()
    if desk is None:
        return None
    safe = "".join(ch for ch in title if ch not in '\\/:*?"<>|').strip() or "Игра"
    lnk = desk / f"{safe} (на русском).lnk"
    ok = create_shortcut(lnk, game_dir, game_exe, f"{title} — запуск с русификатором")
    return lnk if ok else None
