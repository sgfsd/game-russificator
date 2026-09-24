"""Раскладка текста в окнах RPG Maker: перенос по ширине в пикселях.

Окно сообщения и шрифт у каждой версии свои (значения по умолчанию из
стандартных скриптов движка):

  версия  окно сообщения, px   лицо, px   шрифт
  MV      816 − 2·18 = 780     168        28     (разрешение меняют плагины — читаем их)
  MZ      808 − 2·12 − 4 = 780  164       26     (uiAreaWidth и fontSize — из System.json)
  VX Ace  544 − 2·12 = 520     112        24
  VX      544 − 2·16 = 512     112        20
  XP      480 − 32 − 4 = 444   —          22

Ширина строки считается по метрикам PT Sans — им игра рисует русские буквы
(см. ``install_font`` плагина). Управляющие коды занимают свою ширину:
иконка \\I[n] — клетку иконки, имя героя \\N[n] — примерно 8 букв, переменная
\\V[n] — 4 цифры, цвет и прочие коды — ноль. Запас 4% — на кернинг и отличия
шрифтов; остальное подстрахует перенос прямо в игре (``runtime.py``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ...fonts import metrics


@dataclass(frozen=True)
class Box:
    width: float          # ширина строки текста без лица, px
    face: float           # сколько ширины забирает лицо персонажа
    size: float           # размер шрифта сообщений
    icon: float           # ширина иконки \I[n]
    scroll: float         # ширина строки прокручиваемого текста

    def limit(self, face: bool = False, scroll: bool = False) -> float:
        width = self.scroll if scroll else self.width - (self.face if face else 0)
        return width * 0.96


DEFAULTS = {
    "mv": Box(780, 168, 28, 36, 768),
    "mz": Box(780, 160, 26, 36, 768),
    "ace": Box(520, 112, 24, 26, 496),
    "vx": Box(512, 112, 20, 26, 480),
    "xp": Box(444, 0, 22, 26, 444),
}

_CODE_RE = re.compile(r"\\([A-Za-z]+)(\[[^\]\n]*\]|<[^>\n]*>)?|\\[{}$.|!><^\\]")


def box_for(version: str, game_root: Optional[Path] = None) -> Box:
    """Размеры окна сообщения игры: стандартные, с поправкой на её разрешение и шрифт."""
    box = DEFAULTS.get(version, DEFAULTS["mv"])
    if game_root is None:
        return box
    try:
        if version == "mz":
            return _mz_box(box, Path(game_root))
        if version == "mv":
            return _mv_box(box, Path(game_root))
    except Exception:  # noqa: BLE001 - нестандартные файлы — остаёмся на значениях по умолчанию
        pass
    return box


def _data_dir(game_root: Path) -> Path:
    for base in (game_root / "www", game_root):
        if (base / "data").is_dir():
            return base / "data"
    return game_root / "data"


def _mz_box(box: Box, game_root: Path) -> Box:
    sysdata = json.loads((_data_dir(game_root) / "System.json").read_text(encoding="utf-8-sig"))
    adv = sysdata.get("advanced") or {}
    ui_width = float(adv.get("uiAreaWidth") or 816)
    size = float(adv.get("fontSize") or box.size)
    inner = ui_width - 8 - 24 - 4
    return Box(inner, box.face, size, box.icon, inner - 12)


def _mv_box(box: Box, game_root: Path) -> Box:
    """MV: ширину экрана меняют плагины (YEP_CoreEngine, Community_Basic и др.)."""
    base = game_root / "www" if (game_root / "www").is_dir() else game_root
    plugins = base / "js" / "plugins.js"
    if not plugins.is_file():
        return box
    width = None
    for p in plugin_list(plugins.read_text(encoding="utf-8-sig")):
        if not isinstance(p, dict) or not p.get("status"):
            continue
        for key, value in (p.get("parameters") or {}).items():
            if re.fullmatch(r"(?i)screen\s*width|screenwidth|box\s*width|boxwidth", str(key).strip()):
                try:
                    width = max(width or 0, float(str(value).strip()))
                except ValueError:
                    pass
    if not width or width <= 816:
        return box
    inner = width - 36
    return Box(inner, box.face, box.size, box.icon, inner - 12)


def plugin_list(js: str) -> list:
    """Массив ``$plugins`` из js/plugins.js (JSON внутри JS-присваивания)."""
    start = js.find("[")
    end = js.rfind("]")
    if start < 0 or end <= start:
        return []
    return json.loads(js[start:end + 1])


def text_width(text: str, box: Box) -> float:
    """Ширина строки с управляющими кодами RPG Maker в пикселях."""
    m = metrics.default_metrics()
    width = 0.0
    pos = 0
    for match in _CODE_RE.finditer(text):
        width += m.width(text[pos:match.start()], box.size)
        code = (match.group(1) or "").upper()
        if code == "I":
            width += box.icon
        elif code in ("N", "P"):
            width += m.width("Персонаж", box.size)
        elif code == "V":
            width += m.width("0000", box.size)
        elif code == "G":
            width += m.width("зол", box.size)
        pos = match.end()
    return width + m.width(text[pos:], box.size)


def wrap(text: str, box: Box, face: bool = False, scroll: bool = False,
         min_width: float = 0.0) -> List[str]:
    """Разбить перевод на строки окна по ширине в пикселях (слово длиннее строки — отдельной строкой)."""
    limit = max(box.limit(face, scroll), min_width)
    lines: List[str] = []
    for paragraph in text.split("\n"):
        cur = ""
        for word in paragraph.split(" "):
            cand = f"{cur} {word}" if cur else word
            if cur and text_width(cand, box) > limit:
                lines.append(cur)
                cur = word
            else:
                cur = cand
        lines.append(cur)
    while len(lines) > 1 and not lines[-1].strip():
        lines.pop()
    return lines or [""]
