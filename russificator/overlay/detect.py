"""Игра ли сейчас на экране: решение для окна переднего плана.

Порядок:
  1. «Никогда не переводить» — нет;
  2. «Всегда переводить» — да;
  3. браузеры, мессенджеры, плееры, лаунчеры, офис, сама программа — нет, никогда
     (в том числе ради приватности: их экран не распознаётся вовсе);
  4. exe лежит в папке игры из «Моих игр» — да;
  5. Windows сама считает это игрой (Game Bar, реестр GameConfigStore) — да;
  6. окно занимает весь монитор (полный экран или «окно без рамки») — да.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

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


_OWN = _norm(sys.executable)      # сама программа (в том числе запущенная из исходников через pythonw)


class Decider:
    def __init__(self, game_dirs: Iterable[str] = (), always: Iterable[str] = (), never: Iterable[str] = (),
                 gamebar: Optional[Set[str]] = None):
        self.game_dirs: List[str] = sorted({_norm(d).rstrip("\\/") for d in game_dirs if d}, key=len, reverse=True)
        self.always = {_norm(p) for p in always}
        self.never = {_norm(p) for p in never}
        self.gamebar = gamebar if gamebar is not None else set()

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
        if name in DENY or n == _OWN:
            return False, "не игра (браузер, мессенджер, система)"
        for d in self.game_dirs:
            if n.startswith(d + os.sep) or n.startswith(d + "/"):
                return True, "игра из «Моих игр»"
        if n in self.gamebar:
            return True, "Windows считает это игрой"
        if fullscreen:
            return True, "полноэкранное окно"
        return False, REASON_WINDOW

    def game_dir(self, exe: str) -> Optional[str]:
        n = _norm(exe)
        for d in self.game_dirs:
            if n.startswith(d + os.sep) or n.startswith(d + "/"):
                return d
        return None
