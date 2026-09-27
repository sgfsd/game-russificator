"""Сборка плагинов Unity (resources/unity/*.dll) из исходников.

    python tools/build_unity_plugins.py

Нужен компилятор C#: mcs (Mono — Linux/macOS) или csc из .NET Framework 4
(есть в Windows 10/11: C:\\Windows\\Microsoft.NET\\Framework64\\v4.0.30319\\csc.exe).
Ссылки на BepInEx и HarmonyX берутся из официальных архивов BepInEx (скачиваются
один раз в .cache/unity-refs), UnityEngine — из заглушки tools/unity_stubs: в игре
плагин работает с настоящими сборками игры.

Russificator.Unity.dll — BepInEx 5 (Mono), ссылается на mscorlib 2.0, поэтому
работает и в старых играх на .NET 3.5 (Unity 5), и в новых.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "russificator" / "resources" / "unity"
CACHE = ROOT / ".cache" / "unity-refs"
BEPINEX5 = "https://github.com/BepInEx/BepInEx/releases/download/v5.4.23.5/BepInEx_win_x64_5.4.23.5.zip"


def _download(url: str, dest: Path) -> Path:
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        print("download", url)
        with urllib.request.urlopen(url, timeout=120) as r, dest.open("wb") as out:
            shutil.copyfileobj(r, out)
    return dest


def _extract(archive: Path, members: dict, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for member, name in members.items():
            (dest / name).write_bytes(z.read(member))


def _compiler() -> list:
    mcs = shutil.which("mcs")
    if mcs:
        return [mcs]
    csc = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    if csc.is_file():
        return [str(csc)]
    sys.exit("Нужен компилятор C#: mcs (Mono) или csc (.NET Framework 4).")


def _mscorlib2() -> list:
    """Ссылки на mscorlib/System 2.0 (профиль .NET 3.5 — как у Unity 5)."""
    for base in (Path("/usr/lib/mono/2.0-api"), Path("/usr/local/lib/mono/2.0-api"),
                 Path("/Library/Frameworks/Mono.framework/Versions/Current/lib/mono/2.0-api")):
        if (base / "mscorlib.dll").is_file():
            return [f"-r:{base / 'mscorlib.dll'}", f"-r:{base / 'System.dll'}"]
    win = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework" / "v2.0.50727"
    if (win / "mscorlib.dll").is_file():
        return [f"-r:{win / 'mscorlib.dll'}", f"-r:{win / 'System.dll'}"]
    sys.exit("Не найдены сборки .NET 2.0 (mono-devel: /usr/lib/mono/2.0-api).")


def build_mono() -> Path:
    refs = CACHE / "bepinex5"
    if not (refs / "BepInEx.dll").is_file():
        _extract(_download(BEPINEX5, CACHE / "BepInEx5.zip"),
                 {"BepInEx/core/BepInEx.dll": "BepInEx.dll", "BepInEx/core/0Harmony.dll": "0Harmony.dll"}, refs)
    cc = _compiler()
    stub = CACHE / "stub" / "UnityEngine.dll"
    stub.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cc + ["-nologo", "-target:library", "-nostdlib", *_mscorlib2(), f"-out:{stub}",
                         str(ROOT / "tools" / "unity_stubs" / "UnityEngine.cs")], check=True)
    out = RES / "Russificator.Unity.dll"
    subprocess.run(cc + ["-nologo", "-target:library", "-optimize", "-nostdlib", *_mscorlib2(),
                         f"-r:{stub}", f"-r:{refs / 'BepInEx.dll'}", f"-r:{refs / '0Harmony.dll'}",
                         f"-out:{out}", str(RES / "RussificatorUnity.cs")], check=True)
    print("built", out, out.stat().st_size)
    return out


if __name__ == "__main__":
    build_mono()
