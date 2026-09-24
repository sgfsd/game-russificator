"""Определение игры на Ren'Py по содержимому папки."""

from __future__ import annotations

import re
from pathlib import Path


def find_game_dir(game_dir: Path) -> Path | None:
    """Найти папку game/ внутри дистрибутива игры."""
    if (game_dir / "game").is_dir():
        return game_dir / "game"
    # некоторые сборки кладут всё в корень
    if any(game_dir.glob("*.rpy")) or any(game_dir.glob("*.rpyc")):
        return game_dir
    return None


def detect(game_dir: Path) -> tuple[float, str, list[str]]:
    """Вернуть (уверенность 0..1, описание, примечания)."""
    game = find_game_dir(game_dir)
    if game is None:
        return 0.0, "", []

    notes = []
    confidence = 0.5
    reasons = []

    rpyc = list(game.glob("*.rpyc"))
    rpy = list(game.glob("*.rpy"))
    # скрипты могут лежать и в подпапках
    rpyc += [p for p in game.rglob("*.rpyc")][:20]
    rpy += [p for p in game.rglob("*.rpy")][:20]

    if rpyc or rpy:
        confidence += 0.3
        reasons.append("скрипты .rpy/.rpyc в game/")
    if any(game.glob("*.rpa")):
        confidence += 0.05
        reasons.append("архивы .rpa")
    # renpy-дистрибутив: lib/ и renpy/ рядом с game/
    if (game.parent / "renpy").is_dir() or any(game.parent.glob("lib/py*-windows")) \
            or any(game.parent.glob("*.exe")):
        confidence += 0.1
        reasons.append("дистрибутив Ren'Py (lib/renpy)")

    # есть ли уже официальные переводы на другие языки
    if (game / "tl").is_dir():
        notes.append("В игре есть официальные переводы (game/tl). Русский показывается поверх языка "
                     "по умолчанию — если в настройках игры выбран другой язык, верните основной.")

    if confidence >= 0.5:
        return min(confidence, 0.98), "; ".join(reasons), notes
    return 0.0, "", notes


def count_translatable_lines(game_dir: Path) -> int:
    """Грубая оценка объёма переводимого текста (для отчёта)."""
    game = find_game_dir(game_dir)
    if game is None:
        return 0
    total = 0
    for f in game.rglob("*.rpy"):
        if "tl" in f.relative_to(game).parts:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        total += len(re.findall(r'"(?:[^"\\]|\\.)*"', text))
    return total
