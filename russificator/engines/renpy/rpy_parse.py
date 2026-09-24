"""Разбор исходников Ren'Py (.rpy) — запасной путь, когда нет .rpyc.

Строки обрабатываются в точности как лексер Ren'Py (renpy/lexer.py,
``Lexer.string``): пробелы и переводы строк схлопываются в один пробел,
затем раскрываются экранирования (``\\n`` → перенос, ``\\{`` → ``{{``,
``\\[`` → ``[[``, ``\\%`` → ``%%``, ``\\uXXXX`` → символ). Благодаря этому
ключи словаря совпадают с тем, что движок передаёт в
``config.say_menu_text_filter``.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

from .rpyc import FoundText, _CHAR_RE, _LITERAL_RE, _UNDERSCORE_RE, _literal, display_strings

_STRING = r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\''

# реплика: [персонаж [атрибуты]] "текст" [with …] / рассказчик: "текст"
_SAY_RE = re.compile(rf'^(?P<who>[A-Za-z_]\w*(?:\s+[\w\-]+)*?)?\s*(?P<str>{_STRING})(?P<tail>\s.*)?$')
_MENU_ITEM_RE = re.compile(rf'^(?P<str>{_STRING})\s*(?:if\s+.+)?:\s*$')
_SCREEN_TEXT_RE = re.compile(rf'^(?:text|textbutton|label|tooltip)\s+(?P<arg>(?:__?\(\s*)?(?:{_STRING})\s*\)?)')

_NOT_SPEAKERS = {"scene", "show", "hide", "play", "queue", "stop", "voice", "jump", "call", "image", "define",
                 "default", "style", "transform", "window", "with", "pause", "label", "screen", "use", "add",
                 "text", "textbutton", "key", "imagebutton", "hotspot", "font", "background", "idle", "hover",
                 "action", "at", "sound", "music", "audio", "nvl", "camera", "layer", "renpy", "return"}


def renpy_string(literal: str) -> str:
    """Значение строкового литерала так, как его видит Ren'Py (без кавычек в начале/конце)."""
    raw = literal.startswith("r")
    s = literal[1:] if raw else literal
    s = s[1:-1]
    if raw:
        return s

    def dequote(m: re.Match) -> str:
        c = m.group(1)
        if c == "{":
            return "{{"
        if c == "[":
            return "[["
        if c == "%":
            return "%%"
        if c == "n":
            return "\n"
        if c[0] == "u" and m.group(2):
            return chr(int(m.group(2), 16))
        return c

    s = re.sub(r"[ \n]+", " ", s)
    return re.sub(r"\\(u([0-9a-fA-F]{1,4})|.)", dequote, s)


def extract_texts(source: str, filename: str) -> Tuple[List[FoundText], Dict[str, str]]:
    found: List[FoundText] = []
    characters: Dict[str, str] = {}
    in_menu_indent = None
    in_python_indent = None
    in_screen_indent = None
    for no, raw in enumerate(source.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if in_menu_indent is not None and indent <= in_menu_indent:
            in_menu_indent = None
        if in_python_indent is not None and indent <= in_python_indent:
            in_python_indent = None
        if in_screen_indent is not None and indent <= in_screen_indent:
            in_screen_indent = None

        # define e = Character("Имя")
        m = re.match(r"^define\s+([\w.]+)\s*=\s*(.+)$", stripped)
        if m:
            cm = _CHAR_RE.match(m.group(2))
            if cm:
                name = _literal(cm.group(2))
                if name:
                    characters[m.group(1)] = name
                    found.append(FoundText(name, "name", None, filename, no))
            continue
        for um in _UNDERSCORE_RE.finditer(stripped):
            val = _literal(um.group(1))
            if val and val.strip():
                found.append(FoundText(val, "ui", None, filename, no))
        for val in display_strings(stripped):
            found.append(FoundText(val, "ui", None, filename, no))
        if in_python_indent is not None:
            continue
        if re.match(r"^(?:init\s+[-\d]*\s*)?python\b.*:$", stripped):
            in_python_indent = indent
            continue
        if stripped.startswith("$"):
            continue
        if re.match(r"^screen\s+\w+", stripped):
            in_screen_indent = indent
            continue
        if in_screen_indent is not None:
            sm = _SCREEN_TEXT_RE.match(stripped)
            if sm:
                lm = _LITERAL_RE.match(sm.group("arg"))
                val = _literal(lm.group(1)) if lm else None
                if val and val.strip():
                    found.append(FoundText(val, "ui", None, filename, no))
            continue
        if re.match(r"^menu\b.*:$", stripped):
            in_menu_indent = indent
            continue
        if in_menu_indent is not None:
            mm = _MENU_ITEM_RE.match(stripped)
            if mm:
                found.append(FoundText(renpy_string(mm.group("str")), "choice", None, filename, no))
                continue
        sm = _SAY_RE.match(stripped)
        if sm:
            who = sm.group("who")
            if who and who.split()[0] in _NOT_SPEAKERS:
                continue
            text = renpy_string(sm.group("str"))
            if text.strip():
                found.append(FoundText(text, "dialogue" if who else "narration",
                                       who.split()[0] if who else None, filename, no))
    return found, characters
