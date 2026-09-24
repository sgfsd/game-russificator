"""Текст в Ruby-скриптах RPG Maker XP/VX/VX Ace (Data/Scripts.*).

Часть надписей стандартные скрипты держат прямо в коде, а не в базе:
модуль ``Vocab`` VX Ace (сообщения боя «%s emerges!», магазин, сохранение),
команды меню пользовательских скриптов (``add_command("Quests", :quest)``),
пункты титульного меню XP (``s1 = "New Game"``), константы-надписи скриптов
(``HELP_TEXT = "…"``). Без них половина боя и меню осталась бы английской.

Берутся только строковые литералы в таких местах:
  * константы модуля ``Vocab`` (скрипт с «Vocab» в имени);
  * константы с «текстовым» именем (TEXT, NAME, HELP, MESSAGE, VOCAB…);
  * первый аргумент ``add_command``, последний строковый аргумент ``draw_text``;
  * ``s1 = "…"``… (меню XP).
Строки с подстановкой ``#{…}`` не трогаются. Замена — по точному месту
литерала, с проверкой, что там прежний текст, и с экранированием по правилам
Ruby, поэтому синтаксис скрипта не может сломаться.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ...translation.filters import looks_technical, looks_translatable
from . import ruby_marshal as M

_DQ = r'"(?:[^"\\]|\\.)*"'
_SQ = r"'(?:[^'\\]|\\.)*'"
_LIT = rf"(?:{_DQ}|{_SQ})"
_CONST_RE = re.compile(rf"^[ \t]*([A-Z]\w*)[ \t]*=[ \t]*({_LIT})[ \t]*(?:#.*)?$", re.M)
_ADD_COMMAND_RE = re.compile(rf"\badd_command\(\s*({_LIT})\s*,")
_DRAW_TEXT_RE = re.compile(rf"\bdraw_text\((?:[^()\n]|\([^()\n]*\))*?,\s*({_LIT})\s*(?:,\s*[\w.:]+\s*)?\)")
_XP_MENU_RE = re.compile(rf"^[ \t]*s\d+[ \t]*=[ \t]*({_LIT})[ \t]*(?:#.*)?$", re.M)
_TEXT_CONST = re.compile(r"TEXT|VOCAB|NAME|HELP|MESSAGE|MSG|TITLE|COMMAND|CMD|LABEL|CAPTION|HEADER|DESC|WORD|"
                         r"TERM|PROMPT|STRING|INFO|TIP|HINT|QUESTION|ANSWER|CHOICE|OPTION|BUTTON|MENU", re.I)
_NOT_TEXT_CONST = re.compile(r"FILE|IMAGE|PICTURE|ICON|_SE$|^SE_|BGM|BGS|SOUND|FONT|SWITCH|VARIABLE|_ID$|^ID_|"
                             r"KEY|SYMBOL|REGEX|PATTERN|SKIN|PATH|FOLDER|DIR$", re.I)
_ESCAPES = {"n": "\n", "t": "\t", "e": "\x1b", "s": " ", "\\": "\\", '"': '"', "'": "'", "#": "#"}


@dataclass
class Literal:
    start: int          # позиция литерала (с кавычками) в коде скрипта
    raw: str            # литерал как в коде
    value: str          # его значение


def unquote(raw: str) -> Optional[str]:
    """Значение строкового литерала Ruby (None — есть подстановка #{…} или непонятные escape)."""
    body = raw[1:-1]
    if raw[0] == "'":
        return re.sub(r"\\([\\'])", r"\1", body)
    if "#{" in body.replace("\\#", ""):
        return None
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                i += 2
                continue
            if nxt.isalpha() or nxt.isdigit():
                return None            # \x41, \u… и т.п. — оставляем как есть
            out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def quote(value: str, like: str) -> str:
    """Литерал Ruby для значения — в тех же кавычках, что и оригинал."""
    if like[0] == "'" and "\n" not in value:
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    esc = (value.replace("\\", "\\\\").replace('"', '\\"').replace("#", "\\#")
           .replace("\n", "\\n").replace("\t", "\\t").replace("\x1b", "\\e"))
    return '"' + esc + '"'


def _wanted(value: Optional[str], strict: bool) -> bool:
    if value is None or not value.strip() or looks_technical(value):
        return False
    return looks_translatable(value, strict=strict)


def literals(code: str, script_name: str) -> Iterator[Literal]:
    """Строки скрипта, которые игрок увидит на экране."""
    seen = set()
    vocab = "vocab" in script_name.lower()

    def lit(m: re.Match, group: int) -> Literal:
        raw = m.group(group)
        return Literal(m.start(group), raw, unquote(raw) or "")

    for m in _CONST_RE.finditer(code):
        name = m.group(1)
        if vocab or (_TEXT_CONST.search(name) and not _NOT_TEXT_CONST.search(name)):
            item = lit(m, 2)
            if item.start not in seen and _wanted(unquote(item.raw), strict=not vocab):
                seen.add(item.start)
                yield item
    for regex, strict in ((_ADD_COMMAND_RE, False), (_DRAW_TEXT_RE, True), (_XP_MENU_RE, False)):
        for m in regex.finditer(code):
            item = lit(m, 1)
            if item.start not in seen and _wanted(unquote(item.raw), strict=strict):
                seen.add(item.start)
                yield item


def load(raw: bytes) -> Tuple[List[Any], bool]:
    data, utf8 = M.load_with_info(raw)
    return (data if isinstance(data, list) else []), utf8


def code_of(entry: Any) -> Optional[str]:
    """Исходник скрипта [id, имя, сжатый код] (байты не-UTF-8 сохраняются как есть)."""
    if not (isinstance(entry, list) and len(entry) > 2 and isinstance(entry[2], str)):
        return None
    try:
        data = zlib.decompress(str(entry[2]).encode("utf-8", "surrogateescape"))
    except (zlib.error, UnicodeEncodeError):
        return None
    return data.decode("utf-8", "surrogateescape")


def set_code(entry: List[Any], code: str) -> None:
    packed = zlib.compress(code.encode("utf-8", "surrogateescape"))
    old = entry[2]
    entry[2] = M.RubyString(packed.decode("utf-8", "surrogateescape"), getattr(old, "ivars", None))


def replace(code: str, changes: Dict[int, Tuple[str, str]]) -> Tuple[str, int]:
    """Заменить литералы {позиция: (старый литерал, новое значение)}; чужие места не трогаются."""
    done = 0
    for start in sorted(changes, reverse=True):
        old_raw, value = changes[start]
        if code[start:start + len(old_raw)] != old_raw:
            continue                   # скрипт изменился — пропускаем
        code = code[:start] + quote(value, old_raw) + code[start + len(old_raw):]
        done += 1
    return code, done
