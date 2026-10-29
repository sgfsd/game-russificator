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

from .markup import has_words, is_cyrillic, plain_compound, plain_text

#: полноширинные знаки (U+FF01–FF5E) и пробел/запятая/точка CJK -> обычные
_HALFWIDTH = {c: chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F)}
_HALFWIDTH.update({0x3000: " ", 0x3001: ",", 0x3002: "."})
#: полноширинная пунктуация (не буквы и не цифры)
_FULLWIDTH_PUNCT_RE = re.compile(r"[！-／：-＠［-｀｛-～、。]")


def halfwidth(text: str) -> str:
    """Полноширинные знаки и латиница (китайская раскладка: «：», «，», «！», «Ａ») — обычными."""
    return text.translate(_HALFWIDTH)

_URL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|www\.)\S+$", re.I)
_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.]+$")
_PATH_RE = re.compile(r"^[\w\-. ]*[\\/][\w\-. \\/]*$")
_FILE_RE = re.compile(r"^[\w\-. /\\]+\.[A-Za-z0-9]{1,5}$")
_IDENT_RE = re.compile(r"^[A-Za-z_][\w.:\-/]*$")
_HEX_RE = re.compile(r"^(?:0x)?[0-9a-fA-F\-]{6,}$")
_CAMEL_RE = re.compile(r"[a-z][A-Z]")
_BRACE_TAG_RE = re.compile(r"\{[^{}\n]*\}")
_CODE_RE = re.compile(r"[;{}]\s*$|^\s*(?:function|var|let|const|return|if|for|while|class|def|import|public|private)\b"
                      r"|==|!=|&&|\|\||=>|::|\(\)|\w+\(.*\)\s*;?$")
_SENTENCE_END = re.compile(r"[.!?…:)\"'»]$")
#: кавычки и знаки реплики по краям одного слова: «"Yes?"», «Wait…!», «(sigh)»
_WORD_EDGES_RE = re.compile(r"^[\"'«“‘(\[]+|[\"'»”’)\].!?…,:;~*]+$")


def _ui_pair(token: str) -> bool:
    """«Save/Load», «On/Off», «Yes/No» — два слова интерфейса через косую черту: подпись, а не путь."""
    parts = token.strip().split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z]{2,12}", p) for p in parts):
        return False
    from .glossary import ui_phrases
    phrases = ui_phrases()
    return all(p.lower() in phrases for p in parts)


def alphabet_run(token: str, min_len: int = 3) -> bool:
    """Буквы алфавита подряд («ABCD», «qrst», «XYZ») — строка экрана ввода имени или раскладки, не слово."""
    t = token.strip()
    return (len(t) >= min_len and t.isalpha() and t.isascii() and (t.isupper() or t.islower())
            and all(ord(b) - ord(a) == 1 for a, b in zip(t, t[1:])))


#: единицы и знаки при числах: разрешение, частота, размеры
_UNITS = {"x", "hz", "khz", "mhz", "ghz", "fps", "px", "dpi", "ppi", "ms", "kb", "mb", "gb", "tb", "kbps", "mbps",
          "p", "k", "fov", "db", "khz", "rpm"}


def _measurement(s: str) -> bool:
    """Строка из чисел и единиц измерения («1920 x 1080 @ 50Hz», «144Hz», «4K») — не текст."""
    if not re.search(r"\d", s):
        return False
    words = re.findall(r"[A-Za-z]+", s)
    return bool(words) and all(w.lower() in _UNITS for w in words)


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
    if _measurement(s):
        return False  # «1920 x 1080 @ 50Hz», «60 FPS», «x 1080» — числа и единицы, переводить нечего
    # полноширинные знаки китайской раскладки («WorkBench：Cultivate») — как знак и пробел: в коде их не
    # пишут, это текст, и строка из них — не один «идентификатор»
    spaced = _FULLWIDTH_PUNCT_RE.sub(lambda m: halfwidth(m.group(0)) + " ", s).strip()
    if strict:
        tok, single = spaced, " " not in spaced
    else:
        # в заведомо текстовых местах теги ({#quick_page}Quick, {size=32}Flashing\nImages) и
        # перевод строки не делают строку идентификатором — проверяется видимый текст
        tok = _BRACE_TAG_RE.sub("", spaced).replace("\\n", " ").strip()
        single = not re.search(r"\s", tok)
    if single:
        if _FILE_RE.match(tok) or (_PATH_RE.match(tok) and not _ui_pair(tok)):
            return False
        mixed_digits = bool(re.search(r"\d", tok)) and not re.fullmatch(r"[A-Za-z]+\d{0,2}[.!?]?", tok)
        camel = _CAMEL_RE.search(tok.lstrip("<[{")) and not plain_compound(_WORD_EDGES_RE.sub("", tok))
        if "_" in tok or camel or mixed_digits:
            return False  # snake_case, camelCase, Item01x — идентификаторы
        if strict:
            # одно слово: берём только «словарное» с заглавной или капсом (кнопки, пункты меню), в том
            # числе с пунктуацией реплики («Wait…!», «"Yes?"») и заиканием («Y-You…!», «S-Sorry»)
            word = _WORD_EDGES_RE.sub("", s)
            if not (re.fullmatch(r"[A-Z][a-z]+|[A-Z]{2,}|[A-Z][a-z]+-[A-Za-z]+|[A-Z](?:-[A-Z]?[a-z]+)+", word)
                    or _ui_pair(s)):
                return False
            if _IDENT_RE.match(s) and s.isupper() and len(s) > 12:
                return False
    if strict:
        p = plain_text(s)
        # вставки String.Format («Level {0}») — не фигурные скобки кода
        m = _CODE_RE.search(re.sub(r"\{\d+(?:,-?\d+)?(?::[^{}]*)?\}", "0", s))
        # «;» в конце пункта списка описаний («Lifespan +20 years after cultivation;») — не код
        if m and not (m.group(0).strip() == ";" and len(re.findall(r"[A-Za-z]{2,}", p)) >= 4):
            return False
        letters = sum(ch.isalpha() for ch in p)
        if letters < max(2, len(p.strip()) * 0.4):
            return False  # в основном цифры/символы
        if "=" in p and " " not in p.strip().split("=", 1)[0]:
            return False  # key=value (теги разметки «<line-indent=20%>» — не в счёт)
    return True


_SYSLOG_RE = re.compile(r"^[\w\-./]+\[\d+\]:")                      # systemd[1]: …, CRON[6043]: …
_UNIX_PATH_RE = re.compile(r"(?:^|[\s(\"'=])/(?:[\w.\-]+/)+[\w.\-]*")  # /opt/psa/admin/…
_SHELL_RE = re.compile(r"&&|>\s*/dev/null|2>&1|\$\(")


def looks_technical(text: str) -> bool:
    """Журналы, пути и команды, которые игра показывает «как есть» (терминал хакера и т.п.)."""
    s = (text or "").strip()
    return bool(_SYSLOG_RE.match(s) or _UNIX_PATH_RE.search(s) or _SHELL_RE.search(s))


def looks_spoken(text: str) -> bool:
    """Строка из места, которое игра заведомо показывает игроку (реплика или вариант ответа сценария,
    колонка текста таблицы сценария): там текст и одно слово со строчной («sure», «dang»), и заикание
    («Y-You…!»), и чат («hey sam»). Отсекается только то, что точно не текст: числа, пути, файлы,
    идентификаторы (snake_case, camelCase), журналы и команды."""
    return looks_translatable(text) and not looks_technical(text)


def is_name_word(text: str) -> bool:
    """Одно слово с заглавной и гласной («Tom», «Sam», «Zoe») — скорее всего имя: его перевод — та же
    фамилия кириллицей («Том»), и это правильный перевод, а не «буквы как в игре»."""
    s = (text or "").strip()
    return bool(re.fullmatch(r"[A-Z][a-z]{2,}", s)) and bool(re.search(r"[aeiouy]", s[1:]))


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
    if not s or any(ch.isspace() for ch in s) or is_name_word(s):
        return False                      # «Tom» → «Том» — имя, так его и пишут по-русски
    if not 1 <= sum(c.isalpha() for c in s) <= 4:
        return False
    return spelled_in_cyrillic(s, translation)


def spelled_in_cyrillic(source: str, translation: str) -> bool:
    """Перевод — те же буквы оригинала кириллицей (транслитерация), любой длины: «dang» → «дэнг»."""
    src = [c for c in (source or "").lower() if c.isalpha()]
    if not src or not all("a" <= c <= "z" for c in src):
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
