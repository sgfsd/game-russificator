"""Защита разметки при переводе: теги, плейсхолдеры, управляющие коды.

Игровой текст пестрит служебными вставками, которые нельзя ни переводить,
ни терять: ``{w}``/``[player]`` (Ren'Py), ``\\C[2]``/``\\N[1]`` (RPG Maker),
``<color=#f00>``/``{0}`` (Unity), ``%s``/``%1`` и т.д. Потеря хотя бы одной
такой вставки ломает строку в игре, а иногда и саму игру.

Два механизма:
  * :func:`mask` / :func:`unmask` — для машинного переводчика: вставки
    заменяются короткими маркерами ``@0``, ``@1``..., которые модель
    гарантированно переносит в перевод как есть;
  * :func:`tokens` / :func:`same_markup` — проверка результата любого
    переводчика: набор вставок в переводе обязан совпасть с оригиналом.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import List, Tuple

_PARTS = [
    r"\{\{|\}\}|\[\[",                               # экранированные скобки Ren'Py
    r"\\[A-Za-z]{1,4}\[[^\]\n]{0,40}\]",              # \C[2] \V[1] \N[1] \I[64] \FS[20]
    r"\\[A-Za-z]{1,4}<[^>\n]{0,40}>",                 # \n<Имя> (плагины RPG Maker)
    r"\\[{}$.|!><^\\]",                               # \{ \. \| \! (RPG Maker)
    r"\\[nrt]",                                       # литеральные \n \t внутри строки
    r"\\[A-Za-z](?![A-Za-z])",                        # \G и прочие однобуквенные коды
    r"\{[^{}\n]{0,80}\}",                             # {w} {0} {color=#fff} {/b} {player}
    r"\[[A-Za-z_][\w.!:]*(?:\[[\w\"']*\])?\]",        # [player] [p.name!c] [items[0]]
    r"</?[A-Za-z][\w\-]*(?:[= ][^<>\n]{0,300})?/?>",  # <b> </color> <size=20> <a class="x" href="…">
    r"<<[^<>\n]{0,80}>>",                             # <<макросы>>
    r"%\([A-Za-z_]\w*\)[-+0#]*\d*(?:\.\d+)?[sdifr]",  # %(name)s
    r"%[-+0#]*\d*(?:\.\d+)?[sdifxXeEgGc]",            # %s %d %.2f
    r"%%",
    r"%\d+",                                          # %1 %2 (термины RPG Maker MV)
    r"\$\{[^}\n]{0,60}\}",                            # ${var}
]

MARKUP_RE = re.compile("|".join(f"(?:{p})" for p in _PARTS))
_LETTERS_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")


def tokens(text: str) -> List[str]:
    """Все служебные вставки строки (в порядке появления)."""
    return MARKUP_RE.findall(text or "")


def same_markup(source: str, translation: str) -> bool:
    """Совпадает ли набор вставок (порядок может меняться — это нормально)."""
    return Counter(tokens(source)) == Counter(tokens(translation))


def missing_markup(source: str, translation: str) -> List[str]:
    """Какие вставки потерялись/лишние — для понятного сообщения в отчёте."""
    a, b = Counter(tokens(source)), Counter(tokens(translation))
    return list(((a - b) + (b - a)).elements())


def plain_text(text: str) -> str:
    """Текст без служебных вставок."""
    return MARKUP_RE.sub(" ", text or "")


def has_words(text: str) -> bool:
    """Есть ли что переводить помимо разметки (хотя бы две буквы подряд)."""
    return bool(re.search(r"[A-Za-z]{2}", plain_text(text)))


def mask(text: str) -> Tuple[str, List[str], str]:
    """Заменить вставки маркерами ``@N`` (или ``#N``, если ``@цифра`` уже в тексте).

    Возвращает (текст_с_маркерами, исходные_вставки, символ_маркера).
    """
    found: List[str] = []
    sign = "#" if re.search(r"@\d", text) else "@"

    def repl(m: re.Match) -> str:
        found.append(m.group(0))
        return f"{sign}{len(found) - 1}"

    masked = MARKUP_RE.sub(repl, text)
    return masked, found, sign


def unmask(translated: str, originals: List[str], sign: str = "@",
           masked_source: str = "") -> Tuple[str, bool]:
    """Вернуть вставки на место. Второй элемент — все ли маркеры нашлись ровно по разу.

    ``masked_source`` (текст с маркерами до перевода) нужен, чтобы убрать
    пробелы, которые модель вставляет вокруг «приклеенных» к слову тегов:
    ``{i}Где я?{/i}``, а не ``{i} Где я? {/i}``.
    """
    if not originals:
        return translated, True
    if masked_source:
        translated = _fix_spacing(masked_source, translated, len(originals), sign)
    ok = True
    for i in range(len(originals)):
        if len(re.findall(rf"{re.escape(sign)}{i}(?!\d)", translated)) != 1:
            ok = False
    # подставляем с конца, чтобы @1 не задел @10
    for i in sorted(range(len(originals)), reverse=True):
        translated = re.sub(rf"{re.escape(sign)}{i}(?!\d)", lambda _m, s=originals[i]: s, translated)
    return translated, ok


def _fix_spacing(masked_source: str, translated: str, count: int, sign: str) -> str:
    for i in range(count):
        tok = re.escape(f"{sign}{i}")
        m = re.search(rf"{tok}(?!\d)", masked_source)
        if not m:
            continue
        glued_before = m.start() > 0 and not masked_source[m.start() - 1].isspace()
        glued_after = m.end() < len(masked_source) and not masked_source[m.end()].isspace()
        if glued_after:
            translated = re.sub(rf"({tok})(?!\d)[ \t]+", r"\1", translated)
        if glued_before:
            translated = re.sub(rf"[ \t]+({tok})(?!\d)", r"\1", translated)
    return translated


def normalize_layout(source: str, translation: str) -> str:
    """Убрать «самодеятельность» переводчика в вёрстке строки.

    * переносы строк, которых не было в оригинале, заменяются пробелами;
    * вставки, стоявшие в оригинале вплотную (``{/i}{w}``), снова склеиваются.
    """
    if "\n" not in source and "\n" in translation:
        translation = re.sub(r"[ \t]*\n[ \t]*", " ", translation).strip()
    if "\\n" not in source and "\\n" in translation:
        translation = re.sub(r"[ \t]*\\n[ \t]*", " ", translation).strip()
    # «Go to the forest» -> «Иди в лес.»: лишняя точка в конце кнопки/пункта меню
    src_end = source.rstrip()[-1:] if source.strip() else ""
    if src_end and src_end not in ".!?…:;" and re.search(r"(?<!\.)\.\s*$", translation):
        translation = re.sub(r"\.(\s*)$", r"\1", translation)
    found = list(MARKUP_RE.finditer(source))
    for a, b in zip(found, found[1:]):
        if a.end() == b.start():
            translation = re.sub(re.escape(a.group(0)) + r"\s+" + re.escape(b.group(0)),
                                 lambda _m, s=a.group(0) + b.group(0): s, translation)
    return translation


def split_by_markup(text: str) -> List[Tuple[bool, str]]:
    """Разбить строку на куски [(это_вставка, текст)] — для пофрагментного перевода."""
    out: List[Tuple[bool, str]] = []
    pos = 0
    for m in MARKUP_RE.finditer(text):
        if m.start() > pos:
            out.append((False, text[pos:m.start()]))
        out.append((True, m.group(0)))
        pos = m.end()
    if pos < len(text):
        out.append((False, text[pos:]))
    return out


def is_cyrillic(text: str) -> bool:
    """Строка уже на русском (больше половины букв — кириллица)."""
    letters = _LETTERS_RE.findall(plain_text(text))
    if not letters:
        return False
    cyr = sum(1 for ch in letters if "А" <= ch <= "я" or ch in "Ёё")
    return cyr * 2 > len(letters)
