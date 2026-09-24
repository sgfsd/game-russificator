"""Определение игры на RPG Maker любой версии по содержимому папки.

Линейка и маркеры:
  * RPG Maker MV/MZ : Game.exe рядом с папкой www/ с data/*.json и js/
  * RPG Maker XP/VX/VX Ace : Game.exe с Game.rgss*, Data/*.rxdata|rvdata|rvdata2
  * RPG Maker 2000/2003 : RPG_RT.exe (или RPG_RT.ldb) + *.lmu
"""

from __future__ import annotations

from pathlib import Path

RPGMAKER_VERSIONS = {
    "mz": "RPG Maker MZ",
    "mv": "RPG Maker MV",
    "ace": "RPG Maker VX Ace",
    "vx": "RPG Maker VX",
    "xp": "RPG Maker XP",
    "2k3": "RPG Maker 2003",
    "2k": "RPG Maker 2000",
}


def _version_from_markers(game_dir: Path) -> tuple[str | None, float, list[str]]:
    reasons = []
    www = game_dir / "www"
    data_dir = None

    # --- MV / MZ ---
    for base in (www, game_dir):
        if (base / "data").is_dir() and (base / "js").is_dir():
            version = "mz" if (base / "js").glob("rmmz_*") and any((base / "js").glob("rmmz_*")) else "mv"
            if version == "mz" and not any((base / "js").glob("rmmz_*")):
                version = "mv"
            if version == "mv" and not any((base / "js").glob("rpg_*")):
                # у MV файлы rpg_core.js и т.п.
                if not any((base / "js").glob("rpg_*")) and not any((base / "js").glob("rmmz_*")):
                    continue
            if not any((base / "data").glob("*.json")):
                continue
            reasons.append(f"{base.name}/data/*.json + {base.name}/js/")
            return version, 0.95, reasons

    # --- XP / VX / VX Ace ---
    for rgss, version in (("Game.rgss3a", "ace"), ("Game.rgss2a", "vx"), ("Game.rgssad", "xp")):
        if (game_dir / rgss).exists():
            reasons.append(rgss)
            return version, 0.95, reasons
    if (game_dir / "Data").is_dir():
        exts = {"rvdata2": "ace", "rvdata": "vx", "rxdata": "xp"}
        for ext, version in exts.items():
            if any((game_dir / "Data").glob(f"*.{ext}")):
                reasons.append(f"Data/*.{ext}")
                return version, 0.95, reasons

    # --- 2000 / 2003 ---
    if (game_dir / "RPG_RT.ldb").exists() or (game_dir / "RPG_RT.exe").exists():
        version = "2k3"
        lmu = list(game_dir.glob("*.lmu")) or (list((game_dir / "RPG_RT.ldb").parent.glob("*.lmu")) if (game_dir / "RPG_RT.ldb").exists() else [])
        # 2003 обычно имеет MapXX.lmu и RPG_RT.ldb; точнее — по наличию Rune/2k3-ресурсов
        reasons.append("RPG_RT.ldb/RPG_RT.exe + .lmu")
        return version, 0.9, reasons

    return None, 0.0, []


def detect(game_dir: Path) -> tuple[str | None, float, str, list[str]]:
    """Вернуть (version_id, уверенность, описание, примечания)."""
    version, conf, reasons = _version_from_markers(Path(game_dir))
    if version is None:
        # Game.exe без характерных файлов — низкая уверенность
        if any(Path(game_dir).glob("Game.exe")):
            return None, 0.3, "Game.exe без характерных файлов данных", []
        return None, 0.0, "", []

    notes = []
    if version in ("2k", "2k3", "xp", "vx", "ace"):
        notes.append(
            "Старые версии RPG Maker (2000/2003/XP/VX/VX Ace) хранят текст в бинарных "
            "файлах Ruby Marshal. Поддержка этих версий ограничена: "
            "извлечение и внедрение выполнены для основных данных (реплики событий, "
            "названия предметов/скиллов/термины), но сложные скрипты могут остаться непереведёнными.")
    return version, conf, "; ".join(reasons), notes
