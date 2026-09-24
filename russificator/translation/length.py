"""Контроль длины переведённого текста.

UI-элементы и варианты выбора обычно имеют ограниченную ширину. Если
перевод длиннее лимита (``entry.max_length``, задаёт плагин движка), просим
переводчик сократить его; если не вышло — мягко обрезаем по словам, а
строка получает статус TRANSLATED (не APPROVED) и попадает в отчёт.
"""

from __future__ import annotations

from typing import Tuple

from ..core.universal import Entry
from . import markup
from .base import Translator


def validate_length(entry: Entry, translation: str, translator: Translator) -> Tuple[bool, str]:
    """Вернуть (влезает_ли, итоговый_текст)."""
    limit = entry.max_length
    if not limit or len(markup.plain_text(translation)) <= limit:
        return True, translation
    shorter = translator.shorten(translation, limit, entry.source)
    if shorter and len(markup.plain_text(shorter)) <= limit and markup.same_markup(entry.source, shorter):
        return True, shorter
    if markup.tokens(translation):
        return False, translation  # с разметкой не режем — сломаем теги
    return False, _hard_trim(translation, limit)


def _hard_trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    trimmed = text[: limit - 1].rstrip()
    space = trimmed.rfind(" ")
    if space > limit * 0.5:
        trimmed = trimmed[:space]
    return trimmed.rstrip(" ,.;:-") + "…"
