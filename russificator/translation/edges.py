"""Проверка перевода по оригиналу: мусор вместо перевода и «края» строки.

Нейросеть, особенно маленькая локальная, иногда возвращает не перевод: номер строки из
пакета («7»), имя говорящего («Duke»), кусок своего JSON-ответа, пересказ длинной фразы в
три слова. Такой «перевод» нельзя пускать в игру — строка уходит на повтор.

Края строки — кавычки реплики (``''…''``, ``"…"``, ``«…»``), маркер варианта выбора (``• ``),
пробелы и переводы строк — у перевода должны быть такими же, как у оригинала: модель их
теряет или приносит из соседних строк пакета.
"""

from __future__ import annotations

import json
import re
from typing import Optional, Tuple

from .markup import markup_matches, plain_text

_LETTER_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")
_CYR_RE = re.compile(r"[А-Яа-яЁё]")
_JSON_RE = re.compile(r'^\s*[\[{]\s*"|"(?:id|type|speaker|text|context|translation|max_chars)"\s*:')
#: открывающая кавычка -> закрывающая
_QUOTES = (("''", "''"), ('"', '"'), ("“", "”"), ("«", "»"), ("„", "“"))
_QUOTE_CHARS = "\"'“”«»„"
_BULLETS = ("•", "·", "*", "-", "–", "—")
_PAREN_RE = re.compile(r"^\(([^()]{1,25})\)\s+")
#: буквы украинского и белорусского алфавитов: в русском переводе это ошибка модели («ДЕЙВІД»)
_FOREIGN_CYR = str.maketrans({"і": "и", "І": "И", "ї": "и", "Ї": "И", "є": "е", "Є": "Е", "ґ": "г",
                              "Ґ": "Г", "ў": "у", "Ў": "У"})
#: латинские буквы, неотличимые от русских: в русском слове («ЧТO» с латинской O) — опечатка модели
_LOOKALIKE = str.maketrans("AaBCcEeHKMOoPpTXxy", "АаВСсЕеНКМОоРрТХху")
#: слово из букв обоих алфавитов («ТINFOILHAT»): модель начала транслитерировать и бросила
_MIXED_WORD_RE = re.compile(r"(?<![\w])(?=[\w]*[А-Яа-яЁё])(?=[\w]*[A-Za-z])[A-Za-zА-Яа-яЁё]+(?![\w])")


def _letters(text: str) -> int:
    return len(_LETTER_RE.findall(plain_text(text)))


def _salvage_json(translation: str) -> Optional[str]:
    """Перевод — кусок JSON-ответа модели: если в нём ровно одна русская строка, она и есть перевод."""
    raw = translation.strip()
    for cand in (raw, raw + "}", raw + '"}'):
        try:
            data = json.loads(cand)
        except ValueError:
            continue
        values = []
        stack = [data]
        while stack:
            v = stack.pop()
            if isinstance(v, str):
                if _CYR_RE.search(v):
                    values.append(v)
            elif isinstance(v, dict):
                stack.extend(v.values())
            elif isinstance(v, list):
                stack.extend(v)
        return values[0] if len(values) == 1 else None
    return None


def check(source: str, translation: str, speaker: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """(годный перевод или None, причина отказа). Перевод-JSON с одной русской строкой — исправляется."""
    tr = translation
    if _JSON_RE.search(tr) and not _JSON_RE.search(source):
        fixed = _salvage_json(tr)
        if fixed is None:
            return None, "кусок JSON-ответа вместо перевода"
        tr = fixed
    src_letters, tr_letters = _letters(source), _letters(tr)
    if src_letters >= 2 and tr_letters == 0:
        return None, "в переводе нет ни одной буквы"
    if src_letters >= 30 and tr_letters < src_letters * 0.35:
        return None, "перевод втрое короче оригинала (пересказ или чужая строка)"
    if speaker and tr.strip() == speaker.strip() and source.strip() != speaker.strip():
        return None, "вместо перевода — имя говорящего"
    if not _CYR_RE.search(source):
        tr = tr.translate(_FOREIGN_CYR)
        # «ЧТO», «пpивет» — латинская буква-двойник в русском слове: исправляется на русскую
        tr = _MIXED_WORD_RE.sub(lambda m: m.group(0) if m.group(0) in source or re.search(
            r"[^AaBCcEeHKMOoPpTXxyА-Яа-яЁё]", m.group(0)) else m.group(0).translate(_LOOKALIKE), tr)
        mixed = [w for w in _MIXED_WORD_RE.findall(plain_text(tr)) if w not in source]
        if mixed:
            return None, f"слово из латиницы и кириллицы вперемешку: {mixed[0]}"
    return tr, None


def _split(text: str) -> Tuple[str, str, str]:
    """(ведущая разметка/пробелы, середина, хвостовая разметка/пробелы)."""
    spans = {m.start(): m.end() for m in markup_matches(text)}
    ends = {e: s for s, e in spans.items()}
    a = 0
    while a < len(text) and (text[a].isspace() or a in spans):
        a = spans.get(a, a + 1) if not text[a].isspace() else a + 1
    b = len(text)
    while b > a and (text[b - 1].isspace() or (b in ends and ends[b] >= a)):
        b = b - 1 if text[b - 1].isspace() else ends[b]
    return text[:a], text[a:b], text[b:]


def _bullet(inner: str) -> str:
    for b in _BULLETS:
        if inner.startswith(b + " "):
            return b + " "
    return ""


def _quote(inner: str) -> Tuple[str, str]:
    for q_open, q_close in _QUOTES:
        if inner.startswith(q_open):
            return q_open, (q_close if len(inner) > len(q_open) and inner.endswith(q_close) else "")
    return "", ""


def fix(source: str, translation: str) -> str:
    """Перевод с краями оригинала: пробелы и переводы строк по краям, маркер варианта выбора,
    кавычки реплики. Разметка по краям ({i}…{/i}) остаётся как в переводе."""
    s_lead_ws = source[: len(source) - len(source.lstrip())]
    s_trail_ws = source[len(source.rstrip()):]
    t_lead, t_inner, t_tail = _split(translation.strip())
    _, s_inner, _ = _split(source.strip())

    s_bul, t_bul = _bullet(s_inner), _bullet(t_inner)
    s_body = s_inner[len(s_bul):]
    t_body = t_inner[len(t_bul):]
    m = _PAREN_RE.match(t_body)
    if m and not s_body.startswith("("):
        t_body = t_body[m.end():]               # «(Наблюдение) …» — приписка из соседней строки
    # приписка в оригинале («• (Keen Eye) ''…''») — кавычки считаются после неё
    s_paren = _PAREN_RE.match(s_body)
    t_paren = _PAREN_RE.match(t_body)
    s_q = s_body[s_paren.end():] if s_paren else s_body
    head = t_body[: t_paren.end()] if (t_paren and s_paren) else ""
    t_q = t_body[len(head):]

    q_open, q_close = _quote(s_q)
    if q_open:
        core = t_q.strip()
        while core[:1] in _QUOTE_CHARS and core:
            core = core[1:]
        while core[-1:] in _QUOTE_CHARS and core:
            core = core[:-1]
        if core:
            t_q = q_open + core.strip() + q_close
    elif s_q[:1] not in _QUOTE_CHARS:
        if t_q[:1] in _QUOTE_CHARS and t_q[-1:] in _QUOTE_CHARS and len(t_q) > 2 \
                and s_q[-1:] not in _QUOTE_CHARS:
            t_q = t_q.strip(_QUOTE_CHARS).strip()   # модель обернула перевод в кавычки
        else:
            t_open = _quote(t_q)[0]
            t_close = dict(_QUOTES).get(t_open, "")
            unpaired = (t_q.count(t_open) % 2 if t_open == t_close
                        else t_q.count(t_open) > t_q.count(t_close))
            if t_open and unpaired:                 # непарная кавычка из соседней реплики
                t_q = t_q[len(t_open):].lstrip()
    inner = s_bul + head + t_q
    return s_lead_ws + t_lead.lstrip() + inner + t_tail.rstrip() + s_trail_ws
