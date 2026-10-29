"""Словарь терминов игры: один термин — один перевод во всей игре.

Машинный перевод и нейросеть переводят строки по отдельности, поэтому термин игры выходит в разных
строках по-разному («Workbench» — «верстак», «рабочее место», а то и остаётся английским: «Это
workbench, который…»), а многозначное слово — не в том значении («Deck» в карточной игре — «палуба»).
Здесь до перевода:

  * собираются термины игры — выделенные в тексте названия (``<color>Workbench</color>``,
    ``{b}Mana{/b}``, ``\\C[2]Potion\\C[0]``) и обычные слова, записанные с заглавной в середине
    («WorkBench»);
  * узнаётся жанр игры (``resources/glossary/en-ru-genres.json``): карточная — значит, deck — колода;
  * термины переводятся один раз тем же переводчиком и кладутся в глоссарий проекта: нейросеть получает
    их в подсказке, а оставшееся английским слово в любом переводе заменяется переводом термина
    (``glossary.apply_glossary``);

после перевода :func:`polish` доводит и переводы из памяти и прошлых запусков: заменяет английские остатки
терминов и жанровые основы («палуб» → «колод» — склонения совпадают, падеж сохраняется).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Tuple

from ..core.universal import Entry, EntryStatus, TextKind
from . import markup
from .filters import is_cyrillic, looks_translatable
from .glossary import RESOURCES, apply_glossary, match_case

log = logging.getLogger("russificator.terms")

#: больше терминов не набираем: глоссарий уходит нейросети в каждом батче
MAX_TERMS = 300

#: выделение названия в тексте: Unity/TMP, Ren'Py, RPG Maker
_HIGHLIGHT_RES = [
    re.compile(r"<(color|b|u)(?:=[^<>\n]*)?>([^<>{}\n]{2,40})</\1>"),
    re.compile(r"\{(color|b|u)(?:=[^{}\n]*)?\}([^<>{}\n]{2,40})\{/\1\}"),
    re.compile(r"(\\C\[\d+\])([^\\<>{}\n]{2,40})\\C\[0\]"),
]
_COMPOUND_RE = re.compile(r"(?<![\w$])[A-Z][a-z]+[A-Z][a-z]+(?![\w$])")


@lru_cache(maxsize=1)
def genre_rules() -> Dict[str, dict]:
    try:
        data = json.loads((RESOURCES / "en-ru-genres.json").read_text(encoding="utf-8"))
        return data.get("genres", {})
    except (OSError, ValueError) as exc:
        log.warning("Жанровые правила не прочитаны: %s", exc)
        return {}


def genres(sources: List[str]) -> List[str]:
    """Жанры игры по доле строк с признаком (карточная: «card» хотя бы в 1% строк и не меньше 15 строк)."""
    out = []
    total = max(1, len(sources))
    for name, rule in genre_rules().items():
        rx = re.compile(rule.get("detect", r"(?!)"), re.I)
        n = sum(1 for s in sources if rx.search(s))
        if n >= rule.get("min_count", 15) and n >= total * rule.get("min_share", 0.01):
            out.append(name)
    return out


def _term_like(t: str) -> bool:
    """Название, а не фраза и не число: 1–4 слова, первое — с заглавной, без конца предложения."""
    t = t.strip()
    words = t.split()
    return (1 <= len(words) <= 4 and t[:1].isalpha() and t[:1].isupper() and not re.search(r"[.!?…:;,\"]$", t)
            and not is_cyrillic(t) and sum(c.isalpha() for c in t) >= 0.6 * len(t)
            and looks_translatable(t) and not re.search(r"[{}<>\[\]\\=_@#$%]", t))


def collect(sources: Iterable[str]) -> Counter:
    """Термины игры с числом строк, где они встречаются выделенными (или словом вида «WorkBench»)."""
    found: Counter = Counter()
    for s in sources:
        seen = set()
        for rx in _HIGHLIGHT_RES:
            for m in rx.finditer(s):
                t = m.group(2).strip()
                if _term_like(t):
                    seen.add(t)
        for m in _COMPOUND_RE.finditer(s):
            if markup.plain_compound(m.group(0)):
                seen.add(m.group(0))
        found.update(seen)
    return found


def _good(term: str, tr: Optional[str]) -> bool:
    """Перевод термина годится: русский, без английских слов термина, не пустой."""
    if not tr or not tr.strip() or tr.strip() == term.strip() or not re.search("[А-Яа-яЁё]", tr):
        return False
    words = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", term)}
    return not any(w.lower() in words for w in re.findall(r"[A-Za-z]{3,}", tr))


def build(entries: List[Entry], translator, glossary: Dict[str, str]) -> Dict[str, str]:
    """Термины игры с переводами, которых ещё нет в ``glossary`` (глоссарий проекта: прошлые запуски и
    правка пользователя важнее). Жанровые термины — из правил жанра, остальные — переводчиком, одним
    проходом; переводчик, который упал или не справился, просто не даёт термина."""
    sources = [e.source for e in entries]
    out: Dict[str, str] = {}
    for g in genres(sources):
        for k, v in genre_rules()[g].get("terms", {}).items():
            if not glossary.get(k):
                out[k] = v
    found = collect(sources)
    todo = [t for t, _ in found.most_common(MAX_TERMS) if not glossary.get(t) and t not in out]
    if not todo:
        return out
    probes = [Entry(id=f"term:{i}", source=t, kind=TextKind.UI, target_key=f"term:{i}") for i, t in enumerate(todo)]
    done: Dict[str, str] = {}
    try:
        for i in range(0, len(probes), 40):
            done.update(translator.translate(probes[i:i + 40], {}) or {})
    except Exception as exc:  # noqa: BLE001 — без терминов перевод идёт как раньше
        log.warning("Термины игры не переведены: %s", exc)
    for p in probes:
        tr = done.get(p.id)
        if _good(p.source, tr):
            out[p.source] = tr.strip()
    log.info("Термины игры: %d (жанр: %s)", len(out), ", ".join(genres(sources)) or "—")
    return out


def stem_fix(source: str, translation: str, rules: List[dict]) -> str:
    """Жанровая основа слова в переводе: «Боевая палуба» → «Боевая колода» (там, где в оригинале deck)."""
    for r in rules:
        if not re.search(r["when"], source) or (r.get("unless") and re.search(r["unless"], source, re.I)):
            continue
        frm, to = r["from"], r["to"]

        def sub(m: "re.Match", to=to) -> str:
            w = m.group(0)
            return to.upper() if w.isupper() and len(w) > 1 else match_case(to, w)
        translation = _outside_markup(translation, lambda part, frm=frm, sub=sub: re.sub(
            rf"(?<![А-Яа-яЁё]){frm}", sub, part, flags=re.I))
    return translation


def _outside_markup(text: str, fn) -> str:
    return "".join(c if is_tag else fn(c) for is_tag, c in markup.split_by_markup(text))


def polish(project, terms: Dict[str, str]) -> int:
    """Довести готовые переводы проекта: английские остатки терминов — переводом термина, жанровые основы
    — по правилам жанра. Возвращает, сколько переводов изменилось."""
    entries = [e for e in project.entries
               if e.translation and e.status in (EntryStatus.TRANSLATED, EntryStatus.APPROVED)]
    rules = [r for g in genres([e.source for e in project.entries]) for r in genre_rules()[g].get("stems", [])]
    forced = {k: v for k, v in terms.items() if v}
    changed = 0
    for e in entries:
        tr = e.translation
        if forced:
            tr = apply_glossary(tr, {k: v for k, v in forced.items() if k.lower() in e.source.lower()})
        if rules:
            tr = stem_fix(e.source, tr, rules)
        if tr != e.translation and markup.same_markup(e.source, tr):
            e.translation = tr
            changed += 1
    return changed


def leftovers(source: str, translation: str, terms: Dict[str, str]) -> List[Tuple[str, str]]:
    """Термины, оставшиеся в переводе по-английски (для отчёта и тестов)."""
    out = []
    for k, v in terms.items():
        if v and k.lower() in source.lower() and re.search(rf"(?<!\w){re.escape(k)}(?!\w)", translation, re.I):
            out.append((k, v))
    return out
