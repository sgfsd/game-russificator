"""Подписи клавиш и подсказки управления: «SPACE», «ESC», «TAB Cancel    SPACE Confirm».

Переводчик принимает название клавиши за слово: «SPACE» → «КОСМОС», «SHIFT» → «СМЕНА», «ESC» →
«ЭКУ», а строку-подсказку из нескольких колонок склеивает в одну фразу («Подтвердить отмену
вкладок»). На клавиатуре эти подписи латиницей, по-русски пишется только «Пробел» — так и
переводится, без переводчика. Подсказка из колонок переводится по колонкам, клавиша в колонке
остаётся как есть.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .glossary import match_case
from .markup import has_words

#: клавиши, которые подписаны латиницей и так и пишутся по-русски (в любом регистре)
_KEEP = {"esc", "tab", "shift", "ctrl", "alt", "altgr", "backspace", "del", "ins", "pgup", "pgdn", "pgdown",
         "capslock", "caps lock", "numlock", "num lock", "prtsc", "prtscn", "prntscrn", "lmb", "rmb", "mmb",
         "lctrl", "rctrl", "lshift", "rshift", "lalt", "ralt", "leftctrl", "rightctrl", "leftshift",
         "rightshift", "leftalt", "rightalt", "left ctrl", "right ctrl", "left shift", "right shift", "left alt",
         "right alt", "l ctrl", "r ctrl", "l shift", "r shift", "l alt", "r alt", "ctrl+z", "alt+f4", "wasd"}
#: слова, которые капсом — клавиша («ENTER», «DELETE»), а с заглавной могут быть и командой («Enter» —
#: «Войти», «Delete» — «Удалить»): только капсом остаются как есть
_KEEP_CAPS = {"enter", "return", "escape", "delete", "insert", "home", "end", "pause", "space"}
#: клавиши, у которых есть русская подпись
_RUSSIAN = {"space": "пробел", "spacebar": "пробел", "space bar": "пробел"}
_F_KEY_RE = re.compile(r"^f(?:[1-9]|1[0-9]|2[0-4])$")
#: колонки подсказки: «TAB Cancel    SPACE Confirm» — между ними три пробела и больше или табуляция
_GAP_RE = re.compile(r"(\t+| {3,})")


def _norm(text: str) -> str:
    return re.sub(r"[\s_-]+", " ", text.strip().lower())


def key_name(text: str) -> Optional[str]:
    """Перевод строки, которая целиком — название клавиши (как есть или «Пробел»), иначе None."""
    core = text.strip()
    if not core or len(core) > 24:
        return None
    low = _norm(core)
    lead, trail = text[: len(text) - len(text.lstrip())], text[len(text.rstrip()):]
    if low in _RUSSIAN and (core.isupper() or core[:1].isupper()):
        return lead + match_case(_RUSSIAN[low], core) + trail
    if low in _KEEP or _F_KEY_RE.match(low) or (low in _KEEP_CAPS and core.isupper() and len(core) > 1):
        return text
    if "+" in core and _combo(core):
        return text                      # «Ctrl + G», «Alt+F4», «Ctrl +» — сочетание клавиш
    return None


def _combo(core: str) -> bool:
    parts = [p.strip() for p in core.split("+")]
    keys = [p for p in parts if p]
    if not keys or len(keys) > 4:
        return False
    named = [p for p in keys if len(p) > 1]
    return bool(named) and all(
        len(p) == 1 and p.isalnum() or _norm(p) in _KEEP or _norm(p) in _KEEP_CAPS or _F_KEY_RE.match(_norm(p))
        or _norm(p) in ("left alt", "left shift", "left ctrl", "cmd", "win", "mouse", "lmb", "rmb", "click")
        for p in keys) and any(_norm(p) in _KEEP or _norm(p) in _KEEP_CAPS for p in named)


def _is_key_token(token: str, columns: bool) -> bool:
    """Клавиша в подсказке: «TAB», «LEFTCTRL», «F5» — капсом (или F-клавиша). «SPACE», «END», «HOME»
    перед словом — клавиша только в подсказке из колонок: иначе это «SPACE STATION», «END OF CHAPTER»."""
    if not (token.isupper() or _F_KEY_RE.match(token.lower())) or key_name(token) is None:
        return False
    low = _norm(token)
    return columns or low in _KEEP or bool(_F_KEY_RE.match(low))


def plan(text: str) -> Optional[List[Tuple[bool, str]]]:
    """Разбор подсказки управления на куски (переводить ли, текст) или None, если это обычная строка.

    «TAB Cancel    SPACE Confirm» → TAB␣ | Cancel | ␣␣␣␣ | SPACE␣ | Confirm; «SPACE» → «ПРОБЕЛ»."""
    fixed = key_name(text)
    if fixed is not None:
        return [(False, fixed)]
    pieces = _GAP_RE.split(text)
    columns = [p for p in pieces[0::2] if has_words(p)]
    out: List[Tuple[bool, str]] = []
    special = len(columns) >= 2
    for n, piece in enumerate(pieces):
        if n % 2 or not piece.strip():
            out.append((False, piece))
            continue
        lead = piece[: len(piece) - len(piece.lstrip())]
        trail = piece[len(piece.rstrip()):]
        core = piece.strip()
        fixed = key_name(core)
        if fixed is not None:
            out.append((False, piece if fixed == core else lead + fixed + trail))
            special = True
            continue
        m = re.match(r"^(\S+)(\s+)(\S[\s\S]*)$", core)
        if m and _is_key_token(m.group(1), len(columns) >= 2) and has_words(m.group(3)):
            out += [(False, lead + (key_name(m.group(1)) or m.group(1)) + m.group(2)), (True, m.group(3) + trail)]
            special = True
            continue
        m = re.match(r"^([\s\S]*\S)(\s+)(\S+)$", core)
        if m and _is_key_token(m.group(3), len(columns) >= 2) and has_words(m.group(1)):
            out += [(True, lead + m.group(1)), (False, m.group(2) + (key_name(m.group(3)) or m.group(3)) + trail)]
            special = True
            continue
        out.append((True, piece))
    return out if special else None


def assemble(pieces: List[Tuple[bool, str]], done: Dict[str, str]) -> Optional[str]:
    """Собрать строку из переведённых кусков (``done``: текст куска без краёв -> перевод)."""
    out = []
    for translate, text in pieces:
        if not translate:
            out.append(text)
            continue
        core = text.strip()
        tr = done.get(core)
        if not tr or not tr.strip():
            return None
        out.append(text[: len(text) - len(text.lstrip())] + tr.strip() + text[len(text.rstrip()):])
    return "".join(out)


def to_translate(pieces: List[Tuple[bool, str]]) -> List[str]:
    return [t.strip() for translate, t in pieces if translate and t.strip()]


def stale(text: str, translation: str) -> bool:
    """Прошлый перевод строки с клавишей расходится с правилом («SPACE» → «КОСМОС», клавиша в подсказке
    переведена словом) — его надо сделать заново. Подсказки из колонок без клавиш и уже верные переводы
    не трогаются: иначе они переводились бы заново при каждой русификации."""
    pieces = plan(text)
    if pieces is None or not translation:
        return False
    fixed = [p for translate, p in pieces if not translate and p.strip()]
    if not fixed:
        return False
    if not to_translate(pieces):
        return assemble(pieces, {}) != translation
    return not all(p.strip() in translation for p in fixed)
