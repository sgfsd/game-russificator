"""Текст из TextAsset'ов Unity: JSON, CSV/TSV, XML, Ink, Yarn и простой текст.

Многие игры держат диалоги и таблицы локализации не в сценах, а в текстовых
ассетах. Игра читает такой файл и выводит строку как есть — значит, XUnity
найдёт её перевод в словаре по точному совпадению. Отсюда берём только то,
что похоже на текст для игрока: фразы и короткие подписи в полях с «текстовыми»
именами (text, title, description…). Файлы игры не меняются.

Форматы:
  * JSON — все строковые значения; Ink (``inkVersion``) — строки ``^текст``;
  * CSV/TSV — колонка английского текста по заголовку (en, english, text…),
    иначе ячейки, похожие на фразы;
  * XML — текст элементов и значения атрибутов, похожие на фразы;
  * Yarn/простой текст — строки целиком и реплики «Имя: текст».
HTML (страницы встроенного браузера) и двоичные ассеты пропускаются.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Callable, Iterator, Optional

from ...translation.filters import looks_like_sentence, looks_technical, looks_translatable

AddFn = Callable[[str], None]

MAX_SIZE = 8 * 1024 * 1024
#: ключи JSON/атрибуты, в которых короткая строка — скорее всего подпись интерфейса
_LABEL_KEY_RE = re.compile(r"text|title|label|caption|header|desc|message|msg|hint|tip|name|button|option|"
                           r"line|dialog|speech|say|content|question|answer|choice|quest|objective|note|"
                           r"subtitle|tooltip|prompt|body|summary|localiz|english|^en(_|-|$)", re.I)
_EN_COLUMN_RE = re.compile(r"^(en|eng|english|en[-_](us|gb)|text|value|source|original|default|string)$", re.I)
_SPEAKER_RE = re.compile(r"^\s*([A-Z][\w .'-]{0,30}):\s+(.+)$")


def _wanted(value: str, key: str = "") -> bool:
    v = value.strip()
    if not v or len(v) > 2500 or looks_technical(v) or not looks_translatable(v, strict=True):
        return False
    if looks_like_sentence(v):
        return True
    words = v.split()
    return bool(key and _LABEL_KEY_RE.search(key)) and 1 <= len(words) <= 5 and v[:1].isupper()


def decode(raw: bytes) -> Optional[str]:
    """Текст ассета или None (двоичные данные, чужая кодировка)."""
    if not raw or len(raw) > MAX_SIZE:
        return None
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encoding = "utf-16"
    elif raw.count(b"\0") > len(raw) // 100:
        return None                                   # двоичные данные
    else:
        encoding = "utf-8-sig"
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError:
        return None


def extract(text: str, add: AddFn) -> None:
    """Передать в ``add`` строки для перевода из текста ассета."""
    head = text.lstrip()[:200].lower()
    if head.startswith(("<!doctype html", "<html")) or "<body" in head:
        return                                        # страница браузера: XUnity её не видит
    if head[:1] in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if data is not None:
            ink = isinstance(data, dict) and "inkVersion" in data
            for key, value in _json_strings(data, ""):
                if ink:
                    if value.startswith("^") and _wanted(value[1:], "text"):
                        add(value[1:])
                elif _wanted(value, key):
                    add(value)
            return
    if head.startswith("<"):
        _xml(text, add)
        return
    if _csv(text, add):
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("//", "#", "<<", "->", "===", "---")):
            continue
        m = _SPEAKER_RE.match(line)
        if m and _wanted(m.group(2), "text"):
            add(m.group(2).strip())                    # Yarn/скрипты: «Имя: реплика»
        elif _wanted(line):
            add(line)


def _json_strings(node, key: str, depth: int = 0) -> Iterator:
    if depth > 40:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                yield str(k), v
            elif isinstance(v, (dict, list)):
                yield from _json_strings(v, str(k), depth + 1)
    elif isinstance(node, list):
        for v in node:
            if isinstance(v, str):
                yield key, v
            elif isinstance(v, (dict, list)):
                yield from _json_strings(v, key, depth + 1)


def _csv(text: str, add: AddFn) -> bool:
    lines = text.splitlines()
    if len(lines) < 2:
        return False
    first = lines[0]
    delim = "\t" if first.count("\t") >= 1 else "," if first.count(",") >= 1 else ";" if first.count(";") >= 1 else None
    if delim is None:
        return False
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    except csv.Error:
        return False
    widths = [len(r) for r in rows[:50] if r]
    if not widths or max(widths) < 2 or widths.count(widths[0]) < len(widths) * 0.8:
        return False                                  # не таблица, а текст с запятыми
    header = [h.strip() for h in rows[0]]
    cols = [i for i, h in enumerate(header) if _EN_COLUMN_RE.match(h)]
    body = rows[1:] if cols else rows
    for row in body:
        for i, cell in enumerate(row):
            if cols and i not in cols:
                continue
            if _wanted(cell, header[i] if cols and i < len(header) else ""):
                add(cell)
    return True


_XML_TEXT_RE = re.compile(r">([^<>]+)<")
_XML_ATTR_RE = re.compile(r"""\s([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def _xml(text: str, add: AddFn) -> None:
    import html
    for m in _XML_TEXT_RE.finditer(text):
        value = html.unescape(m.group(1)).strip()
        if _wanted(value, "text"):
            add(value)
    for m in _XML_ATTR_RE.finditer(text):
        value = html.unescape(m.group(2) if m.group(2) is not None else m.group(3) or "")
        if _wanted(value, m.group(1)):
            add(value)
