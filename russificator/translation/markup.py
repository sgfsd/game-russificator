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


def mask(text: str, signs: str = "@#") -> Tuple[str, List[str], str]:
    """Заменить вставки маркерами ``@N`` (или следующим знаком из ``signs``, если ``@цифра`` уже в
    тексте). Знак маркера должен быть в словаре модели перевода — иначе она его потеряет.

    Возвращает (текст_с_маркерами, исходные_вставки, символ_маркера).
    """
    found: List[str] = []
    sign = next((c for c in signs if not re.search(re.escape(c) + r"\d", text)), signs[-1])

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


#: конец строки, после которого фраза закончена (перенос — не посреди фразы)
_PHRASE_END = tuple(".!?:;…»\"')]>}|*-–—")


#: теги разметки и подстановки ({0}, [name]) по краям строки — не текст
_EDGE_TAGS = r"(?:<[^<>]*>|\{[^{}]*\}|\[[^\[\]]*\])"


def _soft_break(prev: str, nxt: str) -> bool:
    """Перенос посреди фразы: строка не кончается знаком конца фразы, а следующая начинается со
    строчной буквы («insert↵coin», «you have found↵a rusty key»); теги по краям не в счёт."""
    p = re.sub(_EDGE_TAGS + r"+\s*$", "", prev.rstrip()).rstrip()
    # в начале строки пропускаются только теги оформления: строка с подстановки («{2} challenges
    # passed») или числа — новый пункт, а не продолжение фразы
    n = re.sub(r"^\s*(?:<[^<>]*>)+", "", nxt).lstrip()
    return bool(p) and bool(n) and not p.endswith(_PHRASE_END) and n[0].isalpha() and n[0].islower()


def join_soft_wraps(text: str):
    """Склеить строки, разбитые посреди фразы, в одну (через пробел). Возвращает (текст, раскладка)
    — раскладка None, если склеивать нечего; иначе по ней :func:`rewrap` вернёт переводу те же
    переносы."""
    if "\n" not in text:
        return text, None
    lines = text.split("\n")
    groups: List[List[str]] = []
    for line in lines:
        if groups and _soft_break(groups[-1][-1], line):
            groups[-1].append(line)
        else:
            groups.append([line])
    if all(len(g) == 1 for g in groups):
        return text, None
    layout = []
    out = []
    for g in groups:
        if len(g) == 1:
            out.append(g[0])
            layout.append(None)
            continue
        lead = g[0][: len(g[0]) - len(g[0].lstrip())]
        trail = g[-1][len(g[-1].rstrip()):]
        eol = "\r\n" if g[0].endswith("\r") else "\n"
        out.append(lead + " ".join(x.strip() for x in g) + trail)
        layout.append((eol, [len(x.strip()) for x in g]))
    return "\n".join(out), layout


#: короткие служебные слова: строка ими не кончается («нажмите, чтобы↵присоединиться» — плохо)
_HANGING = {"и", "а", "но", "или", "в", "во", "на", "с", "со", "к", "ко", "по", "из", "от", "до", "для", "за",
            "не", "ни", "о", "об", "у", "что", "чтобы", "как", "если", "это", "при", "без", "под", "над"}


def _cut_cost(word: str, at: int, target: float, total: int) -> float:
    """Насколько плох перенос после слова ``word`` (на ``at`` символе при желаемом ``target``):
    после знака препинания — лучше, после предлога или союза — плохо."""
    cost = abs(at - target)
    if word[-1:] in ",;:—–":
        cost *= 0.5
    if word.lower().strip(",.;:!?") in _HANGING:
        cost += total * 0.3
    return cost


def rewrap(text: str, layout) -> str:
    """Разложить перевод склеенных строк обратно на столько же строк, сколько было в оригинале,
    — по словам, в той же пропорции длин."""
    lines = text.split("\n")
    if len(lines) != len(layout):
        return text
    out = []
    for line, lay in zip(lines, layout):
        if lay is None:
            out.append(line)
            continue
        eol, lengths = lay
        lead = line[: len(line) - len(line.lstrip())]
        trail = line[len(line.rstrip()):]
        words = re.split(r" (?![^<>]*>)", line.strip())     # пробел внутри тега — не граница слов
        k = min(len(lengths), len(words))
        if k <= 1:
            out.append(line)
            continue
        total = len(line.strip())
        whole = float(sum(lengths)) or 1.0
        cum = [0]                                    # длина текста до конца i-го слова
        for w in words:
            cum.append(cum[-1] + len(w) + (1 if cum[-1] else 0))
        cuts: List[int] = []                         # после какого слова перенос
        acc = 0.0
        for j in range(1, k):
            acc += lengths[j - 1]
            target = total * acc / whole
            lo = (cuts[-1] if cuts else 0) + 1
            hi = len(words) - (k - j)
            cuts.append(min(range(lo, hi + 1), key=lambda i: _cut_cost(words[i - 1], cum[i], target, total)))
        pieces, start = [], 0
        for c in cuts + [len(words)]:
            pieces.append(" ".join(words[start:c]))
            start = c
        out.append(lead + eol.join(pieces) + trail)
    return "\n".join(out)


def normalize_layout(source: str, translation: str) -> str:
    """Убрать «самодеятельность» переводчика в вёрстке строки.

    * переносы строк, которых не было в оригинале, заменяются пробелами;
    * вставки, стоявшие в оригинале вплотную (``{/i}{w}``), снова склеиваются.
    """
    if "\n" not in source and "\n" in translation:
        translation = re.sub(r"[ \t]*\n[ \t]*", " ", translation).strip()
    # переводчик склеил строки, разбитые посреди фразы (нейросеть так делает почти всегда): перевод
    # раскладывается на столько же строк, сколько было в оригинале, — игра переносила их по ширине
    joined, layout = join_soft_wraps(source)
    if layout and translation.count("\n") == joined.count("\n") < source.count("\n"):
        translation = rewrap(translation, layout)
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
