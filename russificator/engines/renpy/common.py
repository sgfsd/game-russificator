"""Строки самого движка Ren'Py, которые видит игрок.

Подтверждения («Are you sure you want to quit?», «перезаписать сохранение?»), меню доступности
(Shift+A), предупреждение о видеокарте, экран ошибки, режим пропуска, дни недели и месяцы в
слотах сохранений — всё это не в скриптах игры, а в ``renpy/common/``. У каждой выпущенной игры
своя копия движка, поэтому строки берутся оттуда — ровно той версии, с которой игра работает.

Движок сам помечает каждую такую строку ``_("…")`` (по этим пометкам Ren'Py строит свои переводы),
поэтому берутся только помеченные строки. Инструменты разработчика (консоль, режиссёр, Shift+D,
обновлятор, покупки в приложении) игроку выпущенной игры не видны — их файлы пропускаются.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List

from . import rpyc
from .rpyc import FoundText, _UNDERSCORE_RE, _literal

log = logging.getLogger("russificator.renpy")

SOURCE_EXT = (".rpy", ".rpym")
COMPILED_EXT = (".rpyc", ".rpymc")
#: файлы движка, которые видит только разработчик
DEV_ONLY_RE = re.compile(r"(?:^|/)_(?:developer|compat)/|(?:^|/)00(?:console|director|updater|iap|speechbubble|"
                         r"build|testing|webaudio|sync)\b", re.I)


def common_dir(game_root: Path) -> Path:
    return Path(game_root).parent / "renpy" / "common"


def engine_texts(game_root: Path) -> List[FoundText]:
    """Помеченные ``_()`` строки движка из ``renpy/common`` игры (исходник, если есть, иначе .rpyc)."""
    root = common_dir(game_root)
    if not root.is_dir():
        return []
    files: Dict[str, Path] = {}
    for p in sorted(root.rglob("*")):
        ext = p.suffix.lower()
        if ext not in SOURCE_EXT + COMPILED_EXT or not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if DEV_ONLY_RE.search(rel):
            continue
        base = rel.rsplit(".", 1)[0]
        cur = files.get(base)
        if cur is None or (cur.suffix.lower() in COMPILED_EXT and ext in SOURCE_EXT):
            files[base] = p
    out: List[FoundText] = []
    for base, p in sorted(files.items()):
        name = f"renpy/common/{base}.rpy"
        try:
            if p.suffix.lower() in SOURCE_EXT:
                for no, line in enumerate(p.read_text("utf-8", "replace").splitlines(), start=1):
                    for m in _UNDERSCORE_RE.finditer(line):
                        val = _literal(m.group(1))
                        if val and val.strip():
                            out.append(FoundText(val, "ui", None, name, no))
            else:
                found, _ = rpyc.extract_texts(rpyc.load_rpyc(p.read_bytes()), name)
                out += [f for f in found if f.kind == "ui"]
        except Exception as exc:  # noqa: BLE001
            log.warning("Файл движка %s не разобран: %s", p.name, exc)
    return out
