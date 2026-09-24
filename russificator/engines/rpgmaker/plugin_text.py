"""Текст в параметрах плагинов RPG Maker MV/MZ и в аргументах их команд.

Плагины (меню, окна, подсказки, квесты) держат надписи в параметрах
``js/plugins.js`` и в аргументах команд плагинов (MZ, код 357), часто —
JSON внутри строки (структуры и списки MZ). Переводятся только значения
параметров с «текстовыми» именами (Text, Name, Help, Command, Message…) и
только если значение похоже на текст: имена файлов, переключатели, формулы,
код, цвета и числа не трогаются — иначе сломается логика плагина.

Путь к значению — список ключей (``{json}`` — слой JSON внутри строки,
``[3]`` — элемент списка), при записи структура собирается обратно в том же
компактном виде, в каком её пишет редактор.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator, List, Tuple

from ...translation.filters import looks_technical, looks_translatable

_TEXT_KEY = re.compile(r"text|name|label|title|message|msg|help|desc|command|cmd|vocab|term|caption|header|"
                       r"format|word|string|display|prompt|tip|hint|button|option|question|answer|choice|"
                       r"quest|objective|tutorial|popup|info|menu|window|tooltip|subtitle|dialog|line|content|"
                       r"notif|victory|defeat|escape|confirm|cancel|yes|no\b", re.I)
_NOT_TEXT_KEY = re.compile(r"file|image|img|picture|\bpic|icon|face|sound|\bse\b|bgm|bgs|\bme\b|audio|volume|"
                           r"pitch|\bpan\b|font|switch|variable|\bvar\b|\bid\b|\bkey|symbol|eval|code|script|"
                           r"formula|condition|colou?r|opacity|^x$|^y$|width|height|size|rect|anchor|plugin|tag|"
                           r"url|path|folder|\bdir|speed|rate|count|\bmax|\bmin|offset|padding|margin|align|"
                           r"index|frame|animation|sprite|json|regex|pattern|mode|type|style|position|layer|scale|"
                           r"skin|note\s*tag|notetag|element|state|class|troop|weapon|armor|armour|actor|enemy|"
                           r"map|region|terrain|tile|event|common|param", re.I)
_CODE_VALUE = re.compile(r"[;{}]|=>|\bfunction\b|\bthis\.|\$game|\$data|\bvar\b|\blet\b|\bconst\b|\breturn\b|"
                         r"Math\.|\|\||&&|===?|!==?|\w+\(.*\)$")


def is_text(key: str, value: str) -> bool:
    """Похоже ли значение параметра с именем ``key`` на надпись для игрока."""
    v = value.strip()
    if not v or len(v) > 1000 or v.lower() in ("true", "false", "null", "none", "auto"):
        return False
    if not key or not _TEXT_KEY.search(key) or _NOT_TEXT_KEY.search(key):
        return False
    if _CODE_VALUE.search(v) or looks_technical(v) or re.fullmatch(r"[\d\s.,%+\-*/#()]+", v):
        return False
    return looks_translatable(v)


def walk(value: Any, path: List[str]) -> Iterator[Tuple[List[str], str]]:
    """Все строковые листья значения (с раскрытием JSON внутри строк)."""
    if isinstance(value, str):
        v = value.strip()
        if v[:1] in "[{":
            try:
                parsed = json.loads(v)
            except ValueError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                yield from walk(parsed, path + ["{json}"])
                return
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from walk(v, path + [str(k)])
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk(v, path + [f"[{i}]"])


def key_name(path: List[str]) -> str:
    """Имя параметра для эвристики — последний настоящий ключ пути."""
    for part in reversed(path):
        if part != "{json}" and not (part.startswith("[") and part.endswith("]")):
            return part
    return ""


def texts(value: Any, path: List[str]) -> Iterator[Tuple[List[str], str]]:
    for p, leaf in walk(value, path):
        if is_text(key_name(p), leaf):
            yield p, leaf


def set_at(value: Any, path: List[str], new: str) -> Any:
    """Вернуть значение с заменой листа по пути (JSON внутри строк пересобирается)."""
    if not path:
        return new
    head, rest = path[0], path[1:]
    if head == "{json}":
        parsed = json.loads(value)
        return json.dumps(set_at(parsed, rest, new), ensure_ascii=False, separators=(",", ":"))
    if head.startswith("[") and head.endswith("]") and isinstance(value, list):
        i = int(head[1:-1])
        value[i] = set_at(value[i], rest, new)
        return value
    value[head] = set_at(value[head], rest, new)
    return value


def path_key(path: List[str]) -> str:
    return json.dumps(path, ensure_ascii=False)
