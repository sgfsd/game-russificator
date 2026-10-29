"""Контроль длины переведённого текста.

UI-элементы и варианты выбора обычно имеют ограниченную ширину. Если
перевод длиннее лимита (``entry.max_length``, задаёт плагин движка), просим
переводчик сократить его; если не вышло — мягко обрезаем по словам, а
строка получает статус TRANSLATED (не APPROVED) и попадает в отчёт.

Лимит никогда не меньше места, которое занимал оригинал: оригинал туда
влезал, значит, и перевод чуть длиннее влезет. Абсолютный лимит движка
(«выбор — 30 знаков») не может резать перевод длинного оригинала.
Если движок сам подгоняет текст при показе (``notes["fits_at_runtime"]``,
Ren'Py), перевод не обрезается вовсе — потерянный смысл хуже мелкого шрифта.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from ..core.universal import Entry
from . import markup
from .base import Translator


#: во сколько раз русский текст может быть длиннее английского, не выходя за место оригинала
_ROOM = 1.2


def _visible_len(text: str) -> int:
    return len(markup.plain_text(text).strip())


#: короткому слову запас нужен больше, чем длинной фразе: «About» → «О программе», «Load» → «Загрузить»
_MIN_EXTRA = 8
#: обрубок слова с точкой: «О пр.», «Наст. игры», «Сохр.»
_ABBREV_RE = re.compile(r"(?<![\w.])[A-Za-zА-Яа-яЁё]{1,4}\.(?=\s|$|[,;:)»])")


def relative_limit(source: str, slack: float = 1.4, extra: int = 8) -> int:
    """Лимит от длины оригинала — для движков, где ширина места заранее неизвестна."""
    n = _visible_len(source)
    return max(int(n * slack) + extra, n + _MIN_EXTRA)


def abbreviated(source: str, translation: str) -> bool:
    """В подписи обрубки слов с точкой («О пр.», «Сохр. игру»), которых нет в оригинале: так сокращать
    нельзя — длинная надпись читается, обрубок нет. Фразы с точкой в конце не проверяются: в них
    «не так.» — обычное слово, а не обрубок."""
    if markup.plain_text(source).strip().endswith(('.', '!', '?', '…')):
        return False
    return len(_ABBREV_RE.findall(markup.plain_text(translation))) > len(_ABBREV_RE.findall(markup.plain_text(source)))


def effective_limit(entry: Entry) -> Optional[int]:
    if not entry.max_length:
        return None
    return max(entry.max_length, int(_visible_len(entry.source) * _ROOM) + 2)


def over_limit(entry: Entry, translation: str) -> bool:
    limit = effective_limit(entry)
    return bool(limit) and len(markup.plain_text(translation)) > limit


def was_trimmed(source: str, translation: str) -> bool:
    """Перевод, обрезанный прошлыми версиями (жёстким лимитом): кончается «…», которого нет
    в оригинале, и заметно короче него — русский текст обычно длиннее английского."""
    tr = markup.plain_text(translation).strip()
    src = markup.plain_text(source).strip()
    return ((tr.endswith("…") and not src.endswith(("…", "...", "..")) and len(tr) < len(src) * 0.9)
            or abbreviated(source, translation))


def validate_length(entry: Entry, translation: str, translator: Translator) -> Tuple[bool, str]:
    """Вернуть (влезает_ли, итоговый_текст)."""
    limit = effective_limit(entry)
    if not limit or len(markup.plain_text(translation)) <= limit:
        return True, translation
    shorter = translator.shorten(translation, limit, entry.source)
    if (shorter and len(markup.plain_text(shorter)) <= limit and markup.same_markup(entry.source, shorter)
            and not abbreviated(entry.source, shorter)):
        return True, shorter
    if markup.tokens(translation) or entry.notes.get("fits_at_runtime"):
        return False, translation  # с разметкой не режем — сломаем теги; движок подгонит сам
    return False, _hard_trim(translation, limit)


def _hard_trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    trimmed = text[: limit - 1].rstrip()
    space = trimmed.rfind(" ")
    if space > limit * 0.5:
        trimmed = trimmed[:space]
    return trimmed.rstrip(" ,.;:-") + "…"
