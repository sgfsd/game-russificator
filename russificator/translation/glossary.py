"""Глоссарии: единые переводы имён/терминов и готовые переводы UI-строк.

  1. Пользовательский глоссарий (``project.forced_glossary``) — высший
     приоритет, принудительно подставляется в готовый перевод.
  2. Автоглоссарий: имена персонажей и названия предметов, найденные
     плагином движка, передаются нейросети в каждом батче — чтобы «Mary» не
     стала в одной сцене «Мэри», а в другой «Мария».
  3. UI-фразы (``resources/glossary/*.json``) — точные переводы кнопок и
     пунктов меню для машинного переводчика, который на коротких строках
     без контекста ошибается («Load Game» → «Игра в джойстик»).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

from ..core.universal import TextKind

log = logging.getLogger("russificator.glossary")

RESOURCES = Path(__file__).resolve().parent.parent / "resources" / "glossary"


class GlossaryBuilder:
    """Собирает автоглоссарий по извлечённым строкам."""

    MAX_AUTO_TERMS = 150

    def __init__(self):
        self._names: Counter = Counter()

    def scan(self, project) -> None:
        for e in project.entries:
            if e.speaker:
                self._names[e.speaker] += 1
            if e.kind == TextKind.CHARACTER_NAME:
                self._names[e.source] += 3

    def result(self) -> Dict[str, str]:
        """{имя: ""} — пустое значение: перевод выберет нейросеть, но единый."""
        terms: Dict[str, str] = {}
        for name, _ in self._names.most_common(self.MAX_AUTO_TERMS):
            if self._looks_like_name(name):
                terms[name] = ""
        return terms

    @staticmethod
    def _looks_like_name(text: str) -> bool:
        t = text.strip()
        if not t or len(t) > 30 or len(t.split()) > 3:
            return False
        if re.search(r"[.!?…,:;\"{}\[\]<>\\]", t) or not t[:1].isupper():
            return False
        return True


def apply_glossary(text: str, forced: Dict[str, str]) -> str:
    """Принудительно заменить термины в готовом переводе (глоссарий пользователя и термины игры, оставшиеся
    в переводе по-английски). Только вне разметки — термин «Line» не трогает тег ``<line-indent>``; регистр —
    как у заменяемого слова («Это workbench, который» → «Это верстак, который»)."""
    from .markup import split_by_markup
    pairs = [(src, dst) for src, dst in forced.items() if src and dst]
    if not pairs:
        return text

    def fix(part: str) -> str:
        for src, dst in pairs:
            def sub(m: "re.Match", src=src, dst=dst) -> str:
                # термин внутри оставшейся английской фразы («Proficiency in Poison Arts»): одно русское
                # слово посреди английских дало бы мешанину «Proficiency in Яд Arts»
                before = re.search(r"([A-Za-z]+)[^\w\n]{0,3}$", m.string[:m.start()])
                after = re.match(r"[^\w\n]{0,3}([A-Za-z]+)", m.string[m.end():])
                if before and after:
                    return m.group(0)
                return dst if m.group(0) == src else match_case(dst, m.group(0))
            part = re.sub(rf"(?<!\w){re.escape(src)}(?!\w)", sub, part, flags=re.IGNORECASE)
        return part
    return "".join(c if is_tag else fix(c) for is_tag, c in split_by_markup(text))


@lru_cache(maxsize=1)
def ui_phrases() -> Dict[str, str]:
    phrases: Dict[str, str] = {}
    if RESOURCES.is_dir():
        for f in sorted(RESOURCES.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                phrases.update({k.lower(): v for k, v in data.get("phrases", {}).items()})
            except (OSError, ValueError) as exc:
                log.warning("Не удалось прочитать глоссарий %s: %s", f.name, exc)
    return phrases


def match_case(translation: str, original: str) -> str:
    """Перенести регистр оригинала на перевод (КАПС, С заглавной, строчные)."""
    letters = [c for c in original if c.isalpha()]
    if translation.isupper() and len(translation) > 1:
        return translation  # аббревиатуры (HP, MP) не трогаем
    if letters and all(c.isupper() for c in letters) and len(letters) > 1:
        return translation.upper()
    if original[:1].isupper():
        return translation[:1].upper() + translation[1:]
    if original[:1].islower():
        return translation[:1].lower() + translation[1:]
    return translation


def ui_phrase(text: str) -> Optional[str]:
    """Готовый перевод UI-строки по точному совпадению (или None)."""
    core = text.strip()
    tr = ui_phrases().get(core.lower())
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    if tr is None and "\n" in core:
        return _stacked(core, lead, trail)
    if tr is None:
        # слово в рамке из знаков: «-NEW-», «[ OK ]», «~Start~» — в словаре ищется само слово; без рамки
        # слева — только с «!», «:» или многоточием справа («Done!», «Score:»): вопрос меняет смысл
        # («Quit?» — это «Выйти?», а не «Выход?»)
        m = (re.fullmatch(r"([^\w\s]+\s*)(\w[\w' ]*\w|\w)(\s*[^\w\s]+)", core)
             or re.fullmatch(r"()(\w[\w' ]*\w|\w)(\s*(?:!+|:|\.\.\.|…))", core))
        tr = ui_phrases().get(m.group(2).lower()) if m else None
        if tr is None:
            return None
        return lead + m.group(1) + match_case(tr, m.group(2)) + m.group(3) + trail
    return lead + match_case(tr, core) + trail


def _stacked(core: str, lead: str, trail: str) -> Optional[str]:
    """Подпись кнопки, разложенная по строкам («LEVEL↵SELECT», «Main↵Menu»): строки по отдельности
    переводятся бессмыслицей («УРОВЕНЬ↵ВЫБРАТЬ») — переводится вся подпись по словарю и раскладывается
    на столько же строк."""
    from .markup import rewrap
    lines = [ln.strip() for ln in core.split("\n")]
    if not 2 <= len(lines) <= 3 or any(not ln or len(ln.split()) > 3 for ln in lines):
        return None
    tr = ui_phrase(" ".join(lines))
    if tr is None:
        return None
    return lead + rewrap(tr, [("\n", [len(ln) for ln in lines])]) + trail
