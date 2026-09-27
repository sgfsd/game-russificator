"""«Мои игры»: поиск установленных игр на компьютере.

Источники (всё только читается, ничего не меняется):
  * Steam — все библиотеки из ``libraryfolders.vdf`` (на любых дисках);
  * GOG — реестр ``GOG.com\\Games``;
  * Epic Games — манифесты ``ProgramData\\Epic\\EpicGamesLauncher\\Data\\Manifests``;
  * Ubisoft Connect — реестр ``Ubisoft\\Launcher\\Installs``;
  * itch.io — папка приложения ``%APPDATA%\\itch\\apps``;
  * Game Pass / Microsoft Store — папки ``XboxGames`` на дисках (только оверлей:
    файлы таких игр защищены);
  * общий список установленных программ Windows (Uninstall) — берутся только
    папки, где найден игровой движок;
  * папки, которые пользователь добавил сам (и папки для поиска из настроек);
  * игры, которые уже русифицировались (проекты программы).

Движок здесь определяется быстро, по характерным файлам — без чтения
ассетов; точное определение — при выборе игры (``russificator.detect_engine``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

log = logging.getLogger("russificator.library")

#: движки, которые русификатор переводит файлами
SUPPORTED = {"unity", "renpy", "rpgmaker"}

ENGINE_TITLES = {
    "unity": "Unity", "renpy": "Ren'Py", "rpgmaker": "RPG Maker", "unreal": "Unreal Engine",
    "godot": "Godot", "gamemaker": "GameMaker", "source": "Source", "rpgmaker2k": "RPG Maker 2000/2003",
    "nwjs": "HTML5 (NW.js)", "kirikiri": "KiriKiri", "wolf": "WOLF RPG", "tyrano": "TyranoScript",
    "": "Движок не определён",
}

SOURCE_TITLES = {
    "steam": "Steam", "gog": "GOG", "epic": "Epic Games", "ubisoft": "Ubisoft Connect", "itch": "itch.io",
    "xbox": "Game Pass", "registry": "Установлена", "folder": "Папка поиска", "manual": "Добавлена вручную",
    "russified": "Русифицирована",
}

#: Steam: не игры (инструменты, среды выполнения)
STEAM_SKIP_APPIDS = {"228980", "250820", "1070560", "1391110", "1628350", "1493710", "2180100", "1826330",
                     "961940", "1113280", "1245040", "1420170", "1580130", "1887720", "2348590", "2805730"}
_SKIP_NAME = re.compile(r"redistributable|steamworks|proton|steam linux runtime|steamvr|soundtrack|"
                        r"\bost\b|artbook|dedicated server|\bsdk\b|benchmark", re.I)
_SKIP_EXE = re.compile(r"unins|setup|install|crash|redist|vc_?redist|dxsetup|dotnet|directx|"
                       r"launcher_?helper|report|updater?\b|patcher|UnityCrashHandler", re.I)


@dataclass
class GameEntry:
    path: str
    title: str
    source: str = "folder"
    appid: str = ""                 # Steam AppID — для обложки и запуска через Steam
    exe: str = ""
    engine: str = ""                # быстрый результат: unity | renpy | rpgmaker | unreal | …
    sources: List[str] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        """Можно русифицировать файлами (Game Pass/Store — защищённые папки, только оверлей)."""
        return self.engine in SUPPORTED and "xbox" not in (self.sources or [self.source])

    def to_dict(self) -> dict:
        d = asdict(self)
        d["supported"] = self.supported
        d["engine_title"] = ENGINE_TITLES.get(self.engine, self.engine or ENGINE_TITLES[""])
        d["source_title"] = SOURCE_TITLES.get(self.source, self.source)
        d["cover"] = steam_cover(self.appid) if self.appid else ""
        return d


def steam_cover(appid: str) -> str:
    return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"


# ---------------------------------------------------------------- движок по файлам

def quick_engine(path: Path) -> str:
    """Движок по характерным файлам (несколько проверок наличия файлов, без чтения)."""
    p = Path(path)
    try:
        names = {c.name.lower(): c for c in p.iterdir()}
    except OSError:
        return ""
    # Unity: <Имя>_Data рядом с UnityPlayer.dll / exe
    if "unityplayer.dll" in names or "gameassembly.dll" in names or \
            any(n.endswith("_data") and (c / "globalgamemanagers").exists() for n, c in names.items()
                if c.is_dir()) or \
            any(n.endswith("_data") and (c / "Managed").is_dir() for n, c in names.items() if c.is_dir()):
        return "unity"
    # Ren'Py: game/ со скриптами или архивами, renpy/ рядом
    game = names.get("game")
    if game is not None and game.is_dir() and ("renpy" in names or _any(game, ("*.rpa", "*.rpyc", "*.rpy"))):
        return "renpy"
    # RPG Maker
    www = names.get("www")
    for base in ([www] if www is not None and www.is_dir() else []) + [p]:
        js = base / "js"
        if (base / "data").is_dir() and js.is_dir() and (_any(js, ("rpg_*.js", "rmmz_*.js"))):
            return "rpgmaker"
    if any(n in names for n in ("game.rgss3a", "game.rgss2a", "game.rgssad")) or \
            ("data" in names and _any(names["data"], ("*.rvdata2", "*.rvdata", "*.rxdata"))):
        return "rpgmaker"
    if "rpg_rt.ldb" in names or "rpg_rt.exe" in names:
        return "rpgmaker2k"
    # Unreal: Engine/Binaries или <Имя>/Binaries/Win64
    if "engine" in names and (names["engine"] / "Binaries").is_dir():
        return "unreal"
    if any((c / "Binaries" / "Win64").is_dir() and (c / "Content" / "Paks").is_dir()
           for c in names.values() if c.is_dir()):
        return "unreal"
    # Godot: .pck рядом с exe с тем же именем
    for n in names:
        if n.endswith(".pck") and (n[:-4] + ".exe") in names:
            return "godot"
    # GameMaker: data.win
    if "data.win" in names:
        return "gamemaker"
    if any(n.endswith(".xp3") for n in names):
        return "kirikiri"
    if "data.wolf" in names or ("data" in names and _any(names["data"], ("*.wolf",))):
        return "wolf"
    if "package.nw" in names or "nw.pak" in names:
        return "nwjs"
    return ""


def _any(d: Path, patterns: Iterable[str]) -> bool:
    try:
        return any(next(d.glob(pat), None) is not None for pat in patterns)
    except OSError:
        return False


def main_exe(path: Path) -> Optional[Path]:
    """Самый вероятный exe игры в папке (или в Binaries/Win64 у Unreal)."""
    p = Path(path)
    cands: List[Path] = []
    try:
        cands = [c for c in p.glob("*.exe") if not _SKIP_EXE.search(c.stem)]
    except OSError:
        pass
    if not cands:
        for sub in ("bin", "Bin", "x64", "Binaries/Win64", "bin/x64", "Game"):
            try:
                cands = [c for c in (p / sub).glob("*.exe") if not _SKIP_EXE.search(c.stem)]
            except OSError:
                cands = []
            if cands:
                break
    if not cands:
        return None
    # Unity: exe с тем же именем, что и папка _Data
    for c in cands:
        if (c.parent / f"{c.stem}_Data").is_dir():
            return c
    if len(cands) > 1:
        folder = re.sub(r"[^a-z0-9]", "", p.name.lower())
        cands.sort(key=lambda c: (re.sub(r"[^a-z0-9]", "", c.stem.lower()) != folder, -_size(c)))
    return cands[0]


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def has_exe(path: Path, depth: int = 2) -> bool:
    p = Path(path)
    try:
        if any(True for _ in p.glob("*.exe")):
            return True
        if depth > 1:
            return any(has_exe(c, depth - 1) for c in p.iterdir() if c.is_dir() and not c.name.startswith("."))
    except OSError:
        return False
    return False


# ---------------------------------------------------------------- Steam

def _vdf(text: str) -> dict:
    """Минимальный разбор текстового VDF Valve (libraryfolders.vdf, appmanifest_*.acf)."""
    tokens = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text)
    stack: List[dict] = [{}]
    key: Optional[str] = None
    for s, brace in tokens:
        if brace == "{":
            d: dict = {}
            if key is not None:
                stack[-1][key] = d
            stack.append(d)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        else:
            s = s.replace("\\\\", "\\").replace('\\"', '"')
            if key is None:
                key = s
            else:
                stack[-1][key] = s
                key = None
    return stack[0]


def steam_root() -> Optional[Path]:
    for hive, sub, name in (("HKCU", r"Software\Valve\Steam", "SteamPath"),
                            ("HKLM", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                            ("HKLM", r"SOFTWARE\Valve\Steam", "InstallPath")):
        v = _reg_value(hive, sub, name)
        if v and Path(v).is_dir():
            return Path(v)
    for guess in (Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam",
                  Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Steam"):
        if guess.is_dir():
            return guess
    return None


def steam_libraries(root: Optional[Path] = None) -> List[Path]:
    root = root or steam_root()
    if root is None:
        return []
    libs: List[Path] = [root]
    for vdf in (root / "steamapps" / "libraryfolders.vdf", root / "config" / "libraryfolders.vdf"):
        try:
            data = _vdf(vdf.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
        for k, v in folders.items():
            path = v.get("path") if isinstance(v, dict) else (v if k.isdigit() else None)
            if path:
                libs.append(Path(path))
    out, seen = [], set()
    for lib in libs:
        key = _norm(lib)
        if key not in seen and (lib / "steamapps").is_dir():
            seen.add(key)
            out.append(lib)
    return out


def steam_games(libraries: Optional[List[Path]] = None) -> Iterator[GameEntry]:
    for lib in libraries if libraries is not None else steam_libraries():
        apps = lib / "steamapps"
        try:
            manifests = sorted(apps.glob("appmanifest_*.acf"))
        except OSError:
            continue
        for acf in manifests:
            try:
                st = _vdf(acf.read_text(encoding="utf-8", errors="replace")).get("AppState") or {}
            except OSError:
                continue
            appid, name, folder = str(st.get("appid", "")), st.get("name", ""), st.get("installdir", "")
            if not folder or appid in STEAM_SKIP_APPIDS or _SKIP_NAME.search(name or ""):
                continue
            path = apps / "common" / folder
            if path.is_dir():
                yield GameEntry(path=str(path), title=name or folder, source="steam", appid=appid)


# ---------------------------------------------------------------- другие магазины

def gog_games() -> Iterator[GameEntry]:
    for sub in (r"SOFTWARE\WOW6432Node\GOG.com\Games", r"SOFTWARE\GOG.com\Games"):
        for key in _reg_subkeys("HKLM", sub):
            vals = _reg_values("HKLM", sub + "\\" + key)
            path = vals.get("path") or vals.get("PATH") or vals.get("workingDir")
            name = vals.get("gameName") or vals.get("GAMENAME") or key
            if path and Path(path).is_dir():
                exe = vals.get("exe") or vals.get("EXE") or ""
                yield GameEntry(path=str(Path(path)), title=name, source="gog", exe=exe)


def epic_games() -> Iterator[GameEntry]:
    base = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    try:
        items = sorted(base.glob("*.item"))
    except OSError:
        return
    for item in items:
        try:
            d = json.loads(item.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        cats = [str(c).lower() for c in d.get("AppCategories") or []]
        if cats and "games" not in cats:
            continue
        path = d.get("InstallLocation")
        if path and Path(path).is_dir():
            exe = d.get("LaunchExecutable") or ""
            yield GameEntry(path=str(Path(path)), title=d.get("DisplayName") or Path(path).name, source="epic",
                            exe=str(Path(path) / exe) if exe else "")


def ubisoft_games() -> Iterator[GameEntry]:
    sub = r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs"
    for key in _reg_subkeys("HKLM", sub):
        path = _reg_value("HKLM", sub + "\\" + key, "InstallDir")
        if path and Path(path).is_dir():
            yield GameEntry(path=str(Path(path)), title=Path(path).name, source="ubisoft")


def itch_games() -> Iterator[GameEntry]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return
    roots = [Path(appdata) / "itch" / "apps"]
    db = Path(appdata) / "itch" / "db" / "butler.db"
    titles: Dict[str, str] = {}
    if db.is_file():
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            try:
                for (p,) in con.execute("SELECT path FROM install_locations"):
                    if p:
                        roots.append(Path(p))
                for folder, title in con.execute(
                        "SELECT caves.install_folder_name, games.title FROM caves JOIN games ON caves.game_id = games.id"):
                    if folder and title:
                        titles[str(folder).lower()] = str(title)
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("itch: база не прочитана: %s", exc)
    seen = set()
    for root in roots:
        try:
            children = sorted(c for c in root.iterdir() if c.is_dir())
        except OSError:
            continue
        for c in children:
            if _norm(c) in seen or c.name.startswith((".", "downloads")):
                continue
            seen.add(_norm(c))
            yield GameEntry(path=str(c), title=titles.get(c.name.lower(), c.name), source="itch")


def xbox_games() -> Iterator[GameEntry]:
    """Game Pass / Microsoft Store: папки XboxGames на всех дисках."""
    for drive in _drives():
        root = drive / "XboxGames"
        try:
            children = sorted(c for c in root.iterdir() if c.is_dir())
        except OSError:
            continue
        for c in children:
            if c.name.lower() in ("gamesave", "gamingroot"):
                continue
            content = c / "Content"
            yield GameEntry(path=str(content if content.is_dir() else c), title=c.name, source="xbox")


def registry_games() -> Iterator[GameEntry]:
    """Установленные программы Windows — только папки с игровым движком."""
    keys = [("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Uninstall")]
    for hive, sub in keys:
        for key in _reg_subkeys(hive, sub):
            vals = _reg_values(hive, sub + "\\" + key)
            path = vals.get("InstallLocation") or ""
            if not path or len(path) < 4:
                continue
            p = Path(path.strip('"'))
            if not p.is_dir() or _is_system(p):
                continue
            if quick_engine(p):
                yield GameEntry(path=str(p), title=vals.get("DisplayName") or p.name, source="registry")


def folder_games(folders: Iterable[str], depth: int = 2) -> Iterator[GameEntry]:
    """Игры в папках, которые указал пользователь (например, D:\\Games)."""
    for f in folders:
        root = Path(f)
        if not root.is_dir():
            continue
        yield from _scan_folder(root, depth)


def _scan_folder(root: Path, depth: int) -> Iterator[GameEntry]:
    engine = quick_engine(root)
    if engine:
        yield GameEntry(path=str(root), title=root.name, source="folder", engine=engine)
        return
    if depth <= 0:
        return
    try:
        children = sorted(c for c in root.iterdir() if c.is_dir() and not c.name.startswith((".", "$")))
    except OSError:
        return
    for c in children:
        e = quick_engine(c)
        if e or (depth == 1 and has_exe(c, 1)):
            yield GameEntry(path=str(c), title=c.name, source="folder", engine=e)
        elif depth > 1:
            yield from _scan_folder(c, depth - 1)


# ---------------------------------------------------------------- сборка списка

_PRIORITY = ["steam", "gog", "epic", "ubisoft", "itch", "xbox", "registry", "folder", "manual", "russified"]

SOURCES: List[Tuple[str, Callable[[], Iterable[GameEntry]]]] = [
    ("steam", steam_games), ("gog", gog_games), ("epic", epic_games), ("ubisoft", ubisoft_games),
    ("itch", itch_games), ("xbox", xbox_games), ("registry", registry_games),
]


def discover(extra_folders: Iterable[str] = (), manual: Iterable[str] = (), russified: Iterable[str] = (),
             status: Optional[Callable[[str], None]] = None) -> List[GameEntry]:
    """Все найденные игры без повторов (одна папка — одна запись, источники склеиваются)."""
    found: Dict[str, GameEntry] = {}

    def add(e: GameEntry) -> None:
        key = _norm(Path(e.path))
        prev = found.get(key)
        if prev is None:
            e.sources = [e.source]
            found[key] = e
            return
        if e.source not in prev.sources:
            prev.sources.append(e.source)
        # название и источник — от самого «говорящего» (Steam лучше имени папки)
        if _PRIORITY.index(e.source) < _PRIORITY.index(prev.source):
            prev.source = e.source
            prev.title = e.title or prev.title
        prev.appid = prev.appid or e.appid
        prev.exe = prev.exe or e.exe
        prev.engine = prev.engine or e.engine

    for p in russified:
        if Path(p).is_dir():
            add(GameEntry(path=str(Path(p)), title=Path(p).name, source="russified"))
    for p in manual:
        if Path(p).is_dir():
            add(GameEntry(path=str(Path(p)), title=Path(p).name, source="manual"))
    for name, fn in SOURCES:
        if status:
            status(SOURCE_TITLES.get(name, name))
        started = time.monotonic()
        try:
            for e in fn():
                add(e)
        except Exception as exc:  # noqa: BLE001
            log.warning("источник %s не прочитан: %s", name, exc)
        log.info("источник %s: %.2f с", name, time.monotonic() - started)
    if status:
        status("Папки поиска")
    for e in folder_games(extra_folders):
        add(e)

    out: List[GameEntry] = []
    for e in found.values():
        p = Path(e.path)
        if not e.engine:
            e.engine = quick_engine(p)
        if e.source != "xbox" and not e.engine and not has_exe(p):
            continue            # саундтреки, инструменты, пустые папки
        if not e.exe:
            exe = main_exe(p)
            e.exe = str(exe) if exe else ""
        elif not Path(e.exe).is_absolute():
            e.exe = str(p / e.exe)
        e.title = _clean_title(e.title)
        out.append(e)
    out.sort(key=lambda g: g.title.lower())
    return out


def _clean_title(t: str) -> str:
    t = re.sub(r"[™®©]", "", t or "").strip()
    return re.sub(r"\s{2,}", " ", t)


def find_game(folder_name: str = "", exe_name: str = "", title: str = "",
              games: Optional[List[GameEntry]] = None) -> List[Path]:
    """Папки игры по имени папки / exe / названию (для установщика русификатора)."""
    games = games if games is not None else discover()
    want_folder = _simple(folder_name)
    want_exe = (exe_name or "").lower()
    want_title = _simple(title)
    scored: List[Tuple[int, Path]] = []
    for g in games:
        p = Path(g.path)
        score = 0
        if want_exe and (p / exe_name).is_file():
            score += 4
        if want_folder and _simple(p.name) == want_folder:
            score += 3
        if want_title and _simple(g.title) == want_title:
            score += 2
        if score:
            scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored]


def _simple(s: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]", "", (s or "").lower())


# ---------------------------------------------------------------- Windows

def _norm(p: Path) -> str:
    try:
        s = str(Path(p).resolve())
    except OSError:
        s = str(p)
    return os.path.normcase(s).rstrip("\\/")


def _is_system(p: Path) -> bool:
    s = str(p).lower()
    return any(w in s for w in ("\\windows\\", "\\microsoft\\", "\\common files\\", "\\windowsapps\\",
                                "\\nvidia", "\\amd\\", "\\intel\\", "\\drivers\\"))


def _drives() -> List[Path]:
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:  # noqa: BLE001
        return [Path("C:\\")]
    out = []
    for i in range(26):
        if mask & (1 << i):
            d = Path(f"{chr(65 + i)}:\\")
            try:
                if ctypes.windll.kernel32.GetDriveTypeW(str(d)) in (2, 3):   # съёмный или жёсткий
                    out.append(d)
            except Exception:  # noqa: BLE001
                out.append(d)
    return out


def _hive(name: str):
    import winreg
    return winreg.HKEY_LOCAL_MACHINE if name == "HKLM" else winreg.HKEY_CURRENT_USER


def _reg_value(hive: str, sub: str, name: str) -> Optional[str]:
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(_hive(hive), sub) as k:
            v, _ = winreg.QueryValueEx(k, name)
            return str(v) if v else None
    except OSError:
        return None


def _reg_values(hive: str, sub: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if sys.platform != "win32":
        return out
    try:
        import winreg
        with winreg.OpenKey(_hive(hive), sub) as k:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                if isinstance(value, str):
                    out[name] = value
                i += 1
    except OSError:
        pass
    return out


def _reg_subkeys(hive: str, sub: str) -> List[str]:
    if sys.platform != "win32":
        return []
    out = []
    try:
        import winreg
        with winreg.OpenKey(_hive(hive), sub) as k:
            i = 0
            while True:
                try:
                    out.append(winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
    except OSError:
        pass
    return out
