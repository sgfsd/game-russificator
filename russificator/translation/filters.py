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


def alphabet_run(token: str, min_len: int = 3) -> bool:
    """Буквы алфавита подряд («ABCD», «qrst», «XYZ») — строка экрана ввода имени или раскладки, не слово."""
    t = token.strip()
    return (len(t) >= min_len and t.isalpha() and t.isascii() and (t.isupper() or t.islower())
            and all(ord(b) - ord(a) == 1 for a, b in zip(t, t[1:])))


def looks_translatable(text: str, strict: bool = False) -> bool:
    """Похоже ли на текст для игрока."""
    if not text:
        return False
    s = text.strip()
    if not s or not has_words(s) or is_cyrillic(s):
        return False
    lines = [ln.strip() for ln in plain_text(s).splitlines() if ln.strip()]
    compact = [ln.replace(" ", "") for ln in lines]
    if (any(alphabet_run(ln) for ln in compact)
            and all(alphabet_run(ln, 2) or not re.search(r"[A-Za-z]{2}", ln) for ln in compact)):
        return False  # «ABCD↵EFGH↵…» — сетка букв для ввода имени
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
        # вставки String.Format («Level {0}») — не фигурные скобки кода
        if _CODE_RE.search(re.sub(r"\{\d+(?:,-?\d+)?(?::[^{}]*)?\}", "0", s)):
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


#: заставки движков и технологий — названия, их не переводим
_BRANDS = re.compile(r"(?i)^(?:made with|powered by|created with|built with|a game by|presented by)?\s*"
                     r"(?:unity(?: technologies)?|unreal(?: engine)?|godot(?: engine)?|gamemaker(?: studio)?|"
                     r"rpg maker(?: m[vz]| vx(?: ace)?| xp)?|ren'?py|fmod(?: studio)?|wwise|havok|nvidia|amd|"
                     r"intel|directx|vulkan|opengl|steam(?:works)?|epic games|xbox|playstation|nintendo(?: switch)?|"
                     r"physx|bink(?: video)?|speedtree|dolby(?: atmos)?|"
                     # площадки и соцсети — ссылки в меню игр («Patreon», «Discord»): это названия
                     r"patreon|discord|twitter|youtube|twitch|itch(?:\.io)?|kickstarter|reddit|tiktok|instagram|"
                     r"facebook|bluesky|ko-?fi|tumblr|github|gog(?:\.com)?|boosty|telegram|vk|x\.com)?\s*[™®©]?$")


def is_brand(text: str) -> bool:
    """Строка — название движка, площадки или соцсети («Made with Unity», «Discord», «Patreon»)."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    return bool(t) and bool(_BRANDS.match(t)) and bool(re.search(r"[A-Za-z]{3,}", t))


#: как буквы латиницы передаются кириллицей при транслитерации и «на вид» (B → В, P → Р)
_TRANSLIT = {
    "a": ("а", "э", "ей", "эй", "я"), "b": ("б", "би", "в"), "c": ("к", "с", "ц", "си", "ч"),
    "d": ("д", "ди"), "e": ("е", "э", "и", "ё"), "f": ("ф", "эф"), "g": ("г", "дж", "ж", "джи"),
    "h": ("х", "г", "", "эйч", "ш", "н"), "i": ("и", "ай", "й", "ы"), "j": ("дж", "й", "ж", "джей"),
    "k": ("к", "кей"), "l": ("л", "эл", "ль"), "m": ("м", "эм"), "n": ("н", "эн"), "o": ("о", "оу", "а"),
    "p": ("п", "пи", "р"), "q": ("к", "кью"), "r": ("р", "эр"), "s": ("с", "з", "эс", "ш"),
    "t": ("т", "ти"), "u": ("у", "ю", "а"), "v": ("в", "ви"), "w": ("в", "у"), "x": ("кс", "х", "икс"),
    "y": ("й", "и", "ы", "уай", "у"), "z": ("з", "зет", "зи"),
}


def is_transliteration(source: str, translation: str) -> bool:
    """«Перевод» короткого не-слова — те же буквы кириллицей: инициалы игрока («DAP» → «ДАП»),
    окончания порядковых («ST», «ND» → «СТ», «НД»), буквы («B» → «Б»). Перевода тут нет — понятнее
    оставить как в игре. Только для одного «слова» до четырёх букв."""
    s = (source or "").strip()
    if not s or any(ch.isspace() for ch in s):
        return False
    src = [c for c in s.lower() if c.isalpha()]
    if not 1 <= len(src) <= 4 or not all("a" <= c <= "z" for c in src):
        return False
    dst = "".join(c for c in (translation or "").lower() if c.isalpha())
    if not dst or any("a" <= c <= "z" for c in dst):
        return False
    from functools import lru_cache

    @lru_cache(maxsize=None)
    def fits(i: int, j: int) -> bool:
        if i == len(src):
            return j == len(dst) or dst[j:] == "ь"
        return any(dst.startswith(o, j) and fits(i + 1, j + len(o)) for o in _TRANSLIT[src[i]])
    return fits(0, 0)
