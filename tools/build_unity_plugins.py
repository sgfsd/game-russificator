"""Сборка плагинов Unity (resources/unity/*.dll) из исходников.

    python tools/build_unity_plugins.py

Нужен компилятор C#: mcs (Mono — Linux/macOS) или csc из .NET Framework 4
(есть в Windows 10/11: C:\\Windows\\Microsoft.NET\\Framework64\\v4.0.30319\\csc.exe).
Ссылки на BepInEx и HarmonyX берутся из официальных архивов BepInEx (скачиваются
один раз в .cache/unity-refs), UnityEngine — из заглушки tools/unity_stubs: в игре
плагин работает с настоящими сборками игры.

Russificator.Unity.dll — BepInEx 5 (Mono), ссылается на mscorlib 2.0, поэтому
работает и в старых играх на .NET 3.5 (Unity 5), и в новых.

Russificator.Unity.IL2CPP.dll — тот же исходник с символом IL2CPP, BepInEx 6 (.NET 6):
ссылки — эталонные сборки .NET 6 (NuGet Microsoft.NETCore.App.Ref), BepInEx 6 и
Il2CppInterop из официального архива, обёртки Unity — заглушки tools/unity_stubs/il2cpp.
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
BEPINEX6 = ("https://github.com/BepInEx/BepInEx/releases/download/v6.0.0-pre.2/"
            "BepInEx-Unity.IL2CPP-win-x64-6.0.0-pre.2.zip")
NET6_REF = "https://api.nuget.org/v3-flatcontainer/microsoft.netcore.app.ref/6.0.36/microsoft.netcore.app.ref.6.0.36.nupkg"
BEPINEX6_CORE = ["BepInEx.Core.dll", "BepInEx.Unity.IL2CPP.dll", "BepInEx.Unity.Common.dll",
                 "Il2CppInterop.Runtime.dll", "Il2CppInterop.Common.dll", "0Harmony.dll"]


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


def _net6_refs() -> list:
    d = CACHE / "net6"
    if not (d / "System.Runtime.dll").is_file():
        pkg = _download(NET6_REF, CACHE / "net6ref.nupkg")
        d.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(pkg) as z:
            for n in z.namelist():
                if n.startswith("ref/net6.0/") and n.endswith(".dll"):
                    (d / Path(n).name).write_bytes(z.read(n))
    return [f"-r:{p}" for p in sorted(d.glob("*.dll"))]


def build_il2cpp() -> Path:
    refs = CACHE / "bepinex6"
    if not all((refs / n).is_file() for n in BEPINEX6_CORE):
        _extract(_download(BEPINEX6, CACHE / "BepInEx6-IL2CPP.zip"),
                 {f"BepInEx/core/{n}": n for n in BEPINEX6_CORE}, refs)
    cc = _compiler()
    base = ["-nologo", "-nostdlib", "-noconfig", *_net6_refs()]
    interop = [f"-r:{refs / 'Il2CppInterop.Runtime.dll'}", f"-r:{refs / 'Il2CppInterop.Common.dll'}"]
    stubs = CACHE / "stub-il2cpp"
    stubs.mkdir(parents=True, exist_ok=True)
    src = ROOT / "tools" / "unity_stubs" / "il2cpp"

    def stub(name: str, extra: list) -> Path:
        out = stubs / f"{name}.dll"
        subprocess.run(cc + base + ["-target:library", f"-out:{out}", *interop, *extra, str(src / f"{name}.cs")],
                       check=True)
        return out

    mscorlib = stub("Il2Cppmscorlib", [])
    core = stub("UnityEngine.CoreModule", [f"-r:{mscorlib}"])
    text = stub("UnityEngine.TextRenderingModule", [])
    imgui = stub("UnityEngine.IMGUIModule", [f"-r:{mscorlib}", f"-r:{core}", f"-r:{text}"])
    out = RES / "Russificator.Unity.IL2CPP.dll"
    subprocess.run(cc + base + ["-target:library", "-optimize", "-define:IL2CPP", f"-out:{out}", *interop,
                                *[f"-r:{refs / n}" for n in BEPINEX6_CORE if not n.startswith("Il2CppInterop")],
                                f"-r:{mscorlib}", f"-r:{core}", f"-r:{text}", f"-r:{imgui}",
                                str(RES / "RussificatorUnity.cs")], check=True)
    print("built", out, out.stat().st_size)
    return out


if __name__ == "__main__":
    build_mono()
    build_il2cpp()
