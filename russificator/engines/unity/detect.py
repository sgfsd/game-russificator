"""Определение игры на Unity и её параметров (Mono/IL2CPP, разрядность, версия)."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class UnityGame:
    root: Path              # корень игры (где .exe)
    data: Path              # папка <Имя>_Data
    exe: Optional[Path]     # исполняемый файл игры
    backend: str            # "mono" | "il2cpp"
    arch: str               # "x64" | "x86"
    version: str            # "2019.4.40f1" (или "")
    uses_tmp: bool          # есть TextMeshPro

    @property
    def major(self) -> int:
        m = re.match(r"(\d+)", self.version or "")
        return int(m.group(1)) if m else 0


def data_dirs(game_dir: Path) -> List[Path]:
    out = []
    for child in Path(game_dir).iterdir():
        if child.is_dir() and child.name.endswith("_Data") and (
                (child / "globalgamemanagers").exists() or (child / "mainData").exists()
                or (child / "data.unity3d").exists() or (child / "Managed").is_dir()
                or (child / "il2cpp_data").is_dir()):
            out.append(child)
    return sorted(out)


def find_exe(game_dir: Path, data: Path) -> Optional[Path]:
    exe = game_dir / (data.name[: -len("_Data")] + ".exe")
    if exe.is_file():
        return exe
    exes = [p for p in game_dir.glob("*.exe") if "crash" not in p.name.lower() and "unins" not in p.name.lower()]
    return exes[0] if exes else None


def pe_arch(exe: Optional[Path]) -> str:
    """Разрядность exe по PE-заголовку."""
    if exe is None:
        return "x64"
    try:
        with exe.open("rb") as fh:
            head = fh.read(4096)
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        machine = struct.unpack_from("<H", head, pe + 4)[0]
        return "x86" if machine == 0x14C else "x64"
    except (OSError, struct.error):
        return "x64"


def unity_version(data: Path) -> str:
    for name in ("globalgamemanagers", "mainData", "data.unity3d", "level0", "resources.assets"):
        f = data / name
        if f.is_file():
            try:
                with f.open("rb") as fh:
                    head = fh.read(4096)
                m = re.search(rb"(20\d\d\.\d+\.\d+[abfp]\d+|[5-6]\.\d+\.\d+[abfp]\d+|6000\.\d+\.\d+[abfp]\d+)", head)
                if m:
                    return m.group(1).decode()
            except OSError:
                pass
    return ""


def inspect(game_dir: Path) -> Optional[UnityGame]:
    game_dir = Path(game_dir)
    datas = data_dirs(game_dir)
    if not datas:
        return None
    data = datas[0]
    exe = find_exe(game_dir, data)
    il2cpp = (game_dir / "GameAssembly.dll").exists() or (data / "il2cpp_data").is_dir()
    managed = data / "Managed"
    if il2cpp:
        meta = data / "il2cpp_data" / "Metadata" / "global-metadata.dat"
        uses_tmp = meta.exists() and _file_contains(meta, b"TMPro")
    else:
        uses_tmp = any(managed.glob("*TextMeshPro*.dll")) or any(managed.glob("Unity.TextMeshPro*.dll"))
    return UnityGame(root=game_dir, data=data, exe=exe, backend="il2cpp" if il2cpp else "mono",
                     arch=pe_arch(exe), version=unity_version(data), uses_tmp=uses_tmp)


def _file_contains(path: Path, needle: bytes) -> bool:
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 22)
                if not chunk:
                    return False
                if needle in chunk:
                    return True
    except OSError:
        return False


def detect(game_dir: Path) -> tuple[float, str, list]:
    g = inspect(Path(game_dir))
    if g is None:
        return 0.0, "", []
    parts = [f"папка {g.data.name}", "IL2CPP" if g.backend == "il2cpp" else "Mono", g.arch]
    if g.version:
        parts.append(f"Unity {g.version}")
    notes = []
    if g.exe is None:
        notes.append("Не найден .exe игры — русификатор рассчитан на Windows-версии Unity-игр.")
    return 0.95, ", ".join(parts), notes
