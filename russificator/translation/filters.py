"""Фильтр «это текст для игрока, а не служебная строка».

Главная защита от поломки игр: переводить можно только то, что игрок
видит на экране. Идентификаторы, пути, ключи конфигов, имена анимаций,
теги и прочее, переведённое по ошибке, ломает логику игры.

Режим ``strict`` — для строк, найденных эвристически (литералы кода Unity,
произвольные поля ассетов): там отсекается всё сомнительное. Для строк из
заведомо текстовых мест (реплики Ren'Py, коды 401 RPG Maker) хватает
мягкой проверки.
"""

from __future__ import annotations

import re

from .markup import has_words, is_cyrillic, plain_text

_URL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|www\.)\S+$", re.I)
_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.]+$")
_PATH_RE = re.compile(r"^[\w\-. ]*[\\/][\w\-. \\/]*$")
_FILE_RE = re.compile(r"^[\w\-. /\\]+\.[A-Za-z0-9]{1,5}$")
_IDENT_RE = re.compile(r"^[A-Za-z_][\w.:\-/]*$")
_HEX_RE = re.compile(r"^(?:0x)?[0-9a-fA-F\-]{6,}$")
_CAMEL_RE = re.compile(r"[a-z][A-Z]")
_CODE_RE = re.compile(r"[;{}]\s*$|^\s*(?:function|var|let|const|return|if|for|while|class|def|import|public|private)\b"
                      r"|==|!=|&&|\|\||=>|::|\(\)|\w+\(.*\)\s*;?$")
_SENTENCE_END = re.compile(r"[.!?…:)\"'»]$")


def looks_translatable(text: str, strict: bool = False) -> bool:
    """Похоже ли на текст для игрока."""
    if not text:
        return False
    s = text.strip()
    if not s or not has_words(s) or is_cyrillic(s):
        return False
    if _URL_RE.match(s) or _EMAIL_RE.match(s) or _HEX_RE.match(s):
        return False
    if " " not in s:
        if _FILE_RE.match(s) or _PATH_RE.match(s):
            return False
        mixed_digits = bool(re.search(r"\d", s)) and not re.fullmatch(r"[A-Za-z]+\d{0,2}[.!?]?", s)
        if "_" in s or _CAMEL_RE.search(s.lstrip("<[{")) or mixed_digits:
            return False  # snake_case, camelCase, Item01x — идентификаторы
        if strict:
            # одно слово: берём только «словарное» с заглавной или капсом (кнопки, пункты меню)
            if not re.fullmatch(r"[A-Z][a-z]+[.!?]?|[A-Z]{2,}[.!?]?|[A-Z][a-z]+-[A-Za-z]+", s):
                return False
            if _IDENT_RE.match(s) and s.isupper() and len(s) > 12:
                return False
    if strict:
        if _CODE_RE.search(s):
            return False
        p = plain_text(s)
        letters = sum(ch.isalpha() for ch in p)
        if letters < max(2, len(p.strip()) * 0.4):
            return False  # в основном цифры/символы
        if "=" in s and " " not in s.split("=", 1)[0]:
            return False  # key=value
    return True


_SYSLOG_RE = re.compile(r"^[\w\-./]+\[\d+\]:")                      # systemd[1]: …, CRON[6043]: …
_UNIX_PATH_RE = re.compile(r"(?:^|[\s(\"'=])/(?:[\w.\-]+/)+[\w.\-]*")  # /opt/psa/admin/…
_SHELL_RE = re.compile(r"&&|>\s*/dev/null|2>&1|\$\(")


def looks_technical(text: str) -> bool:
    """Журналы, пути и команды, которые игра показывает «как есть» (терминал хакера и т.п.)."""
    s = (text or "").strip()
    return bool(_SYSLOG_RE.match(s) or _UNIX_PATH_RE.search(s) or _SHELL_RE.search(s))


def looks_like_sentence(text: str) -> bool:
    """Фраза из нескольких слов — точно текст, даже в strict-режиме."""
    s = plain_text(text).strip()
    words = re.findall(r"[A-Za-z']+", s)
    return len(words) >= 3 or (len(words) >= 2 and bool(_SENTENCE_END.search(s)))
