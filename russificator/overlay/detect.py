"""Игра ли сейчас на экране: решение для окна переднего плана.

Порядок:
  1. «Никогда не переводить» — нет;
  2. «Всегда переводить» — да;
  3. браузеры, мессенджеры, плееры, лаунчеры, офис, сама программа — нет, никогда
     (в том числе ради приватности: их экран не распознаётся вовсе);
  4. exe лежит в папке игры из «Моих игр» — да;
  5. Windows сама считает это игрой (Game Bar, реестр GameConfigStore) — да;
  6. рядом с exe файлы игрового движка (Unity, Ren'Py, RPG Maker, Unreal, Godot…)
     или папка отката русификатора — да (игры в окне находятся сами);
  7. окно занимает весь монитор (полный экран или «окно без рамки») — да.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

#: процессы, которые оверлей не трогает никогда (по имени exe, в нижнем регистре)
DENY = {
    # браузеры
    "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "opera_gx.exe", "browser.exe", "brave.exe",
    "vivaldi.exe", "yandex.exe", "iexplore.exe", "arc.exe", "thorium.exe", "waterfox.exe", "librewolf.exe",
    # мессенджеры, звонки, почта
    "telegram.exe", "discord.exe", "slack.exe", "teams.exe", "ms-teams.exe", "whatsapp.exe", "skype.exe",
    "zoom.exe", "viber.exe", "outlook.exe", "thunderbird.exe", "signal.exe",
    # плееры и медиа
    "vlc.exe", "mpc-hc.exe", "mpc-hc64.exe", "mpc-be.exe", "mpc-be64.exe", "potplayermini.exe",
    "potplayermini64.exe", "potplayer.exe", "wmplayer.exe", "video.ui.exe", "spotify.exe", "yandexmusic.exe",
    "obs64.exe", "obs32.exe", "mpv.exe", "kmplayer.exe", "aimp.exe",
    # лаунчеры и сервисы игр (сами — не игры)
    "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "galaxyclient.exe", "battle.net.exe",
    "eadesktop.exe", "origin.exe", "ubisoftconnect.exe", "upc.exe", "gamebar.exe", "xboxpcapp.exe",
    "gamingservicesui.exe", "itch.exe", "playnite.desktopapp.exe", "playnite.fullscreenapp.exe",
    # система, офис, разработка, пароли, удалённый доступ
    "explorer.exe", "applicationframehost.exe", "searchhost.exe", "searchapp.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "lockapp.exe", "textinputhost.exe", "taskmgr.exe", "systemsettings.exe",
    "mmc.exe", "regedit.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "conhost.exe",
    "notepad.exe", "notepad++.exe", "winword.exe", "excel.exe", "powerpnt.exe", "onenote.exe", "acrobat.exe",
    "acrord32.exe", "sumatrapdf.exe", "code.exe", "devenv.exe", "rider64.exe", "pycharm64.exe", "idea64.exe",
    "unity.exe", "unityhub.exe", "blender.exe", "photoshop.exe", "keepass.exe", "keepassxc.exe",
    "1password.exe", "bitwarden.exe", "anydesk.exe", "teamviewer.exe", "mstsc.exe", "rustdesk.exe",
    # сама программа
    "russificator.exe", "russificatorsetup.exe",
}

#: причина «не игра», при которой окно можно добавить в «Всегда» (обычное окно, не из запретного списка)
REASON_WINDOW = "обычное окно"

#: движки, по файлам которых окно считается игрой (Electron/NW.js-программы сюда не входят)
GAME_ENGINES = {"unity", "renpy", "rpgmaker", "rpgmaker2k", "unreal", "godot", "gamemaker", "kirikiri", "wolf"}
#: выше этих папок движок не ищем (иначе любая программа в Program Files могла бы сойти за игру)
_STOP_DIRS = {"program files", "program files (x86)", "programdata", "windows", "users", "appdata", "local",
              "roaming", "steamapps", "common", "games", "игры"}
_engine_cache: Dict[str, str] = {}


def engine_near(exe: str) -> str:
    """Движок игры по файлам рядом с exe (папка exe и до двух уровней выше), «russified» —
    игра, русифицированная программой; пустая строка — не игра. Результат запоминается."""
    n = _norm(exe)
    if not n:
        return ""
    if n in _engine_cache:
        return _engine_cache[n]
    found = ""
    try:
        from ..core.backup import BACKUP_DIR
        from ..library import quick_engine
        folder = Path(exe).parent
        for cand in [folder, *list(folder.parents)[:2]]:
            if cand.parent == cand or cand.name.lower() in _STOP_DIRS:
                break
            if (cand / BACKUP_DIR).is_dir():
                found = "russified"
                break
            engine = quick_engine(cand)
            if engine in GAME_ENGINES:
                found = engine
                break
    except Exception:  # noqa: BLE001
        found = ""
    if len(_engine_cache) > 512:
        _engine_cache.clear()
    _engine_cache[n] = found
    return found


def gamebar_exes() -> Set[str]:
    """exe, которые Windows (Game Bar) считает играми: HKCU\\System\\GameConfigStore\\Children."""
    out: Set[str] = set()
    if sys.platform != "win32":
        return out
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"System\GameConfigStore\Children") as root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, sub) as k:
                        v, _ = winreg.QueryValueEx(k, "MatchedExeFullPath")
                        if v:
                            out.add(_norm(v))
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(str(p))) if p else ""


def _own() -> Set[str]:
    """Сама программа (в том числе запущенная из исходников: .venv перенаправляет на базовый pythonw)."""
    try:
        from .win32 import own_exes
        return {_norm(p) for p in own_exes()}
    except Exception:  # noqa: BLE001
        return {_norm(sys.executable)}


_OWN = _own()


class Decider:
    def __init__(self, game_dirs: Iterable[str] = (), always: Iterable[str] = (), never: Iterable[str] = (),
                 gamebar: Optional[Set[str]] = None, by_engine: bool = True):
        self.game_dirs: List[str] = sorted({_norm(d).rstrip("\\/") for d in game_dirs if d}, key=len, reverse=True)
        self.always = {_norm(p) for p in always}
        self.never = {_norm(p) for p in never}
        self.gamebar = gamebar if gamebar is not None else set()
        self.by_engine = by_engine

    def decide(self, exe: str, fullscreen: bool) -> Tuple[bool, str]:
        """(игра ли, почему) для процесса окна переднего плана."""
        if not exe:
            return False, "нет процесса"
        n = _norm(exe)
        name = Path(exe).name.lower()
        if n in self.never:
            return False, "в списке «Никогда»"
        if n in self.always:
            return True, "в списке «Всегда»"
        if name in DENY or n in _OWN:
            return False, "не игра (браузер, мессенджер, система)"
        for d in self.game_dirs:
            if n.startswith(d + os.sep) or n.startswith(d + "/"):
                return True, "игра из «Моих игр»"
        if n in self.gamebar:
            return True, "Windows считает это игрой"
        engine = engine_near(exe) if self.by_engine else ""
        if engine == "russified":
            return True, "игра, русифицированная программой"
        if engine:
            from ..library import ENGINE_TITLES
            return True, f"игра на {ENGINE_TITLES.get(engine, engine)}"
        if fullscreen:
            return True, "полноэкранное окно"
        return False, REASON_WINDOW

    def game_dir(self, exe: str) -> Optional[str]:
        n = _norm(exe)
        for d in self.game_dirs:
            if n.startswith(d + os.sep) or n.startswith(d + "/"):
                return d
        return None
