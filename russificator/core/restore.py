"""Откат русификации для любого движка (по манифесту бэкапа)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Tuple

from .backup import BACKUP_DIR, GameBackup


def restore_backups(game_dir: Path) -> Tuple[int, List[str]]:
    """Вернуть игру к исходному состоянию. Возвращает (сколько файлов, заметки)."""
    game_dir = Path(game_dir)
    backup = GameBackup(game_dir)
    if backup.exists:
        rpy_touched = [rel for rel in [*backup.modified, *backup.created] if rel.endswith(".rpy")]
        count, notes = backup.restore()
        _drop_renpy_bytecode(game_dir, rpy_touched)
        notes.append("Готово: игра возвращена к оригиналу.")
        return count, notes
    if (game_dir / BACKUP_DIR).is_dir():
        return _restore_legacy(game_dir)
    return 0, ["Русификация в этой папке не найдена — откатывать нечего."]


def _drop_renpy_bytecode(game_dir: Path, rpy_files: List[str]) -> None:
    """Ren'Py кэширует компиляцию .rpy в .rpyc — после отката кэш надо сбросить."""
    for rel in rpy_files:
        rpyc = (game_dir / rel).with_suffix(".rpyc")
        if rpyc.exists() and not (game_dir / BACKUP_DIR / (rel + "c")).exists():
            rpyc.unlink()
    for cache in game_dir.glob("**/game/cache"):
        shutil.rmtree(cache, ignore_errors=True)


def _restore_legacy(game_dir: Path) -> Tuple[int, List[str]]:
    """Бэкап старой версии программы (без манифеста): просто копируем всё обратно."""
    root = game_dir / BACKUP_DIR
    restored = 0
    for f in sorted(root.rglob("*")):
        if f.is_file():
            dest = game_dir / f.relative_to(root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            restored += 1
    for rel in ("game/zzz_russificator.rpy", "game/zzz_russificator_font.rpy",
                "game/tl/russian/russificator.rpy", "www/fonts/russificator.css"):
        for p in (game_dir / rel, game_dir / (rel + "c")):
            if p.is_file():
                p.unlink()
    shutil.rmtree(game_dir / "game" / "fonts" / "russificator", ignore_errors=True)
    shutil.rmtree(root, ignore_errors=True)
    return restored, [f"Восстановлено файлов из бэкапа старой версии: {restored}."]
