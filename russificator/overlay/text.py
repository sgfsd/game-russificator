"""Текст с экрана: строки распознавания → абзацы, предложения, фильтр иностранного.

Распознавание возвращает строки с рамками. Соседние строки одного абзаца
склеиваются в блок, и перевод ложится поверх всего абзаца. Переводится абзац по
предложениям (:func:`sentences`): у допечатывающейся реплики готовые предложения
не переводятся заново. Распознавание «шумит» (одна и та же строка может прийти с
разницей в букву) — похожие варианты одного текста узнаются нечётким сравнением,
а значок «дальше» в конце реплики отбрасывается (:func:`strip_marker`).

Английское распознавание читает русский текст как латинскую абракадабру
(«Начать» → «HaqaTb», «Налоговые» → «Hanor0Bble»). Такие строки отсеиваются:
по русскому распознаванию той же области (:func:`cyrillic_share`) или, если его
нет, по приметам (:func:`misread_cyrillic`).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


@dataclass
class Line:
    text: str
    x: int
    y: int
    w: int
    h: int
    conf: float = 1.0                # уверенность распознавания
    ru: bool = False                 # распознавание прочитало тут русский текст
    #: наклонная строка (табличка, бирка, надпись на предмете): центр x, y, длина, высота, угол
    #: в радианах; x/y/w/h тогда — описанный вокруг неё прямоугольник
    rot: Optional[Tuple[float, float, float, float, float]] = None
    alt: str = ""                    # английское прочтение строки, которую распознали как кириллицу

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def right(self) -> int:
        return self.x + self.w


@dataclass
class Block:
    """Абзац: строки, склеенные :func:`group_lines`."""
    lines: List[Line]
    key: str = ""

    @property
    def text(self) -> str:
        return join_lines([ln.text for ln in self.lines])

    @property
    def rect(self) -> Tuple[int, int, int, int]:
        x0 = min(ln.x for ln in self.lines)
        y0 = min(ln.y for ln in self.lines)
        x1 = max(ln.right for ln in self.lines)
        y1 = max(ln.bottom for ln in self.lines)
        return x0, y0, x1 - x0, y1 - y0

    @property
    def line_height(self) -> float:
        return sum(ln.h for ln in self.lines) / max(1, len(self.lines))


_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
_CYR = re.compile(r"[А-Яа-яЁё]")
_LETTER = re.compile(r"[^\W\d_]", re.U)


def cyrillic_share(text: str) -> float:
    """Доля кириллических букв среди всех букв (0, если букв нет)."""
    letters = len(_LETTER.findall(text or ""))
    return len(_CYR.findall(text or "")) / letters if letters else 0.0


#: кириллица, у которой нет латинского двойника: русское распознавание не выдаст её за английский текст
#: (а «EXIT» оно может прочитать как «ЕХIТ» — одними буквами-двойниками)
_CYR_DISTINCT = re.compile(r"[БГДЖЗИЙЛПФЦЧШЩЪЫЬЭЮЯбвгджзийлмнптфцчшщъыьэюя]")


def looks_russian(text: str) -> bool:
    """Строка русского распознавания — действительно русский текст (а не английский,
    прочитанный буквами-двойниками)."""
    distinct = len(_CYR_DISTINCT.findall(text or ""))
    share = cyrillic_share(text)
    if distinct >= 2 and share >= 0.5:
        return True
    return distinct >= 1 and share >= 0.75 and len(_LETTER.findall(text or "")) <= 4


# Приметы кириллицы, прочитанной английским распознаванием: цифры вместо букв в слове
# (З→3, О→0, б→6), значки вместо букв (Ж→>K, Ф→$), «ы»→Bbl/lbl, «ь»→b в конце слова,
# «и»→h между заглавными, смена регистра посреди слова («paK0BhHa», «HAnor»).
_DIGIT_AND_LETTER = re.compile(r"^(?=.*[0-9])(?=.*[A-Za-z])[0-9A-Za-z]{3,}$")
#: обычные английские сочетания цифр и букв: 2nd, 10x, x2, v1, F12, HP100, MP3, 3D, 4K
_DIGIT_WORD_OK = re.compile(r"(?i)^(?:\d+(?:st|nd|rd|th|x|k|m|s|p|fps|hp|mp|px|am|pm|d)|[a-z]\d+|\d+[a-z]"
                            r"|[a-z]{1,3}\d+|\d+[a-z]{1,2})$")
_SYMBOL_IN_WORD = re.compile(r"[A-Za-z][>$<}{|\\][A-Za-z]")
_SOFT_SIGN = re.compile(r"[A-Za-z]{2,}b$")
_YERY = re.compile(r"Bbl|lbl|[A-Z]bl[A-Z]")
_UPPER_H = re.compile(r"[A-Z]h[A-Z]|[A-Z]{2}h$")
_CASE_ZIGZAG = re.compile(r"[a-z][A-Z]+[a-z]")
_CAPS_THEN_LOWER = re.compile(r"^[A-Z]{2,}[a-z]{2,}$")
_CAPS_LOWER_CAPS = re.compile(r"[A-Z]{2,}[a-z]+[A-Z]")


def _odd_token(t: str) -> bool:
    if _DIGIT_AND_LETTER.match(t) and not _DIGIT_WORD_OK.match(t):
        return True
    if _SYMBOL_IN_WORD.search(t) or _YERY.search(t) or _UPPER_H.search(t) or _CASE_ZIGZAG.search(t):
        return True
    if _CAPS_THEN_LOWER.match(t) or _CAPS_LOWER_CAPS.search(t):
        return True
    return bool(_SOFT_SIGN.search(t) and not t.islower() and not t.isupper())


def _tokens(text: str) -> List[str]:
    return [t for t in re.split(r"[\s.,!?:;\"'«»()\[\]…–—_-]+", text or "") if t]


def junk_tokens(text: str) -> List[str]:
    """Слова строки, похожие на мусор распознавания («)OOmG», «K0H$nr»)."""
    return [t for t in _tokens(text) if len(re.findall(r"[A-Za-z]", t)) >= 2 and _odd_token(t)]


def known_words(text: str, words: Optional[Set[str]]) -> int:
    """Сколько в строке словарных английских слов (от трёх букв)."""
    if not words:
        return 0
    return sum(1 for t in _tokens(text) if len(re.findall(r"[A-Za-z]", t)) >= 3 and _in_words(t.lower(), words))


def gibberish(text: str, words: Set[str]) -> bool:
    """Несколько незнакомых слов строчными и почти ни одного словарного — абракадабра распознавания
    (узор, стилизованная надпись), а не английский текст. Имена (с заглавной) сюда не попадают."""
    long_tokens = [t for t in _tokens(text) if len(re.findall(r"[A-Za-z]", t)) >= 3]
    if len(long_tokens) < 3:
        return False
    unknown = [t for t in long_tokens if not _in_words(t.lower(), words)]
    if (len(long_tokens) - len(unknown)) / len(long_tokens) >= 0.34:
        return False
    return sum(1 for t in unknown if t.islower()) >= 2


def without_junk(text: str) -> str:
    """Строка без мусорных слов (для переводчика): «ABOUT )OOmG» → «ABOUT»."""
    junk = set(junk_tokens(text))
    if not junk:
        return text
    out = []
    for part in re.split(r"(\s+)", text):
        core = part.strip("()[]{}|.,!?:;\"'«»")
        if core in junk or (core and core.strip("()[]{}|") in junk):
            continue
        out.append(part)
    return re.sub(r"\s{2,}", " ", "".join(out)).strip()


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


def misread_cyrillic(text: str, words: Optional[Set[str]] = None) -> bool:
    """Похоже ли на русский текст, прочитанный английским распознаванием (абракадабра латиницей).

    ``words`` — словарь английских слов (нижний регистр), если есть: настоящий английский
    текст почти весь из словарных слов, а «прочитанная» кириллица — нет."""
    tokens = [t for t in re.split(r"[\s.,!?:;\"'«»()\[\]…–—_-]+", text or "") if t]
    tokens = [t for t in tokens if len(re.findall(r"[A-Za-z]", t)) >= 2]
    if not tokens:
        return False
    odd = sum(1 for t in tokens if _odd_token(t))
    if words is not None:
        long_tokens = [t for t in tokens if len(re.findall(r"[A-Za-z]", t)) >= 3]
        known = sum(1 for t in long_tokens if _in_words(t.lower(), words))
        if long_tokens and known / len(long_tokens) < 0.34 and odd >= 1:
            return True                  # почти нет английских слов, и есть приметы — абракадабра
        if long_tokens and known / len(long_tokens) >= 0.5:
            return False                 # в основном английские слова — шум распознавания не страшен
    return odd >= max(1, (len(tokens) + 1) // 2)


def _in_words(low: str, words: Set[str]) -> bool:
    w = re.sub(r"[^a-z']", "", low).strip("'")
    if not w:
        return False
    if w in words:
        return True
    if w.endswith("'s") and w[:-2] in words:
        return True
    for suf in ("s", "es", "ed", "d", "ing", "er", "ly"):
        if w.endswith(suf) and len(w) - len(suf) >= 3 and w[:-len(suf)] in words:
            return True
    return False


def is_foreign(text: str) -> bool:
    """Есть что переводить: слова латиницей, и это не русский текст (наш перевод, русская игра)."""
    t = (text or "").strip()
    if len(t) < 2:
        return False
    words = _LATIN_WORD.findall(t)
    if not words:
        return False
    letters = len(_LETTER.findall(t))
    cyr = len(_CYR.findall(t))
    if letters and cyr / letters > 0.3:
        return False
    # «HP 100/100», «x2», «FPS 60» — одно короткое слово с цифрами и значками не переводим
    latin = sum(len(w) for w in words)
    if len(words) == 1 and latin <= 3 and len(t) > latin * 2:
        return False
    return True


def join_lines(lines: Iterable[str]) -> str:
    """Строки абзаца в одну: перенос «exam-» + «ple» склеивается, остальное — через пробел."""
    out = ""
    for s in lines:
        s = s.strip()
        if not s:
            continue
        if out.endswith("-") and len(out) > 1 and out[-2].isalpha() and s[:1].islower():
            out = out[:-1] + s
        else:
            out = (out + " " + s) if out else s
    return out


#: значок «дальше» в конце реплики (▼, ▶, мигающий треугольник): распознавание читает его как «v»/«y»
#: или символ — он то есть, то нет, и один и тот же текст выглядел бы разным
_MARKER = re.compile(r"(?:(?<=[.!?…,;:\-–—~\"»”’)\]])\s*[vVyY▼▽▾▶►▸◆◇■□●○>»]"
                     r"|\s+[▼▽▾▶►▸◆◇■□●○])\s*$")


def strip_marker(text: str) -> str:
    """Строка без значка продолжения в конце («Well—v» → «Well—», «Hi. ▼» → «Hi.»)."""
    t = (text or "").rstrip()
    stripped = _MARKER.sub("", t)
    return stripped.rstrip() if stripped.strip() else t


#: сокращения с точкой, после которых предложение не кончается
_ABBR = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "jr", "sr", "no", "vol", "fig", "lt", "sgt",
         "capt", "prof", "mt", "ft", "approx", "dept", "est", "inc", "ltd", "co", "gen", "gov", "sen", "rep"}
_SENT_END = re.compile(r"([.!?…]+[\"»”’)\]]*)(\s+)(?=[\"«“‘(\[]?[A-ZА-ЯЁ0-9¡¿])")
_COMPLETE = re.compile(r"[.!?…][\"»”’)\]]*$")


def sentences(text: str) -> List[str]:
    """Предложения абзаца. Перевод идёт по предложениям: у допечатывающейся реплики уже готовые
    предложения переведены и больше не меняются — переводится только новое."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    out: List[str] = []
    start = 0
    for m in _SENT_END.finditer(t):
        end = m.end(1)
        chunk = t[start:end].strip()
        if m.group(1) == ".":
            word = re.search(r"([A-Za-z][A-Za-z.]*)\.$", chunk)
            if word and (word.group(1).lower() in _ABBR or len(word.group(1)) == 1):
                continue                    # «Mr. Smith», «J. Smith»
        if chunk:
            out.append(chunk)
        start = m.end()
    tail = t[start:].strip()
    if tail:
        out.append(tail)
    return out


def complete(sentence: str) -> bool:
    """Предложение закончено (точка, !, ?, многоточие в конце)."""
    return bool(_COMPLETE.search((sentence or "").strip()))


def normalize(text: str) -> str:
    """Ключ текста: регистр, пробелы и знаки распознавания не важны."""
    t = (text or "").lower()
    t = re.sub(r"[\s ]+", " ", t)
    t = re.sub(r"[^\w ]+", "", t, flags=re.U)
    return t.strip()


def group_lines(lines: List[Line], apart=None) -> List[Block]:
    """Склеить строки в абзацы: близко по вертикали, похожая высота, общий край или центр.

    Промежуток между абзацами заметно больше, чем между строками одного абзаца, — граница
    ищется по промежуткам уже собранного абзаца. Центрированный абзац (заголовок, надпись) не
    принимает строку, выровненную по левому краю (кнопка под ним), и наоборот."""
    lines = sorted((ln for ln in lines if ln.text.strip() and ln.h > 0), key=lambda ln: (ln.y, ln.x))
    blocks: List[dict] = []
    for ln in lines:
        target = None
        for b in reversed(blocks[-6:]):
            last = b["lines"][-1]
            if ln.rot is not None or last.rot is not None:
                if tilted_next(last, ln):   # следующая строка той же наклонной надписи
                    target = b
                    break
                continue
            h = (last.h + ln.h) / 2
            gap = ln.y - last.bottom
            if gap < -h * 0.5:
                continue
            gaps = b["gaps"]
            limit = min(0.9 * h, 2 * sorted(gaps)[len(gaps) // 2] + 0.1 * h) if gaps else 0.6 * h
            if gap > limit:
                continue
            if max(last.h, ln.h) / max(1, min(last.h, ln.h)) > 1.35:
                continue
            if list_break(last, ln):
                continue                    # пункты меню или списка, а не перенос строки абзаца
            if apart is not None and apart(last, ln):
                continue                    # между строками черта рамки или другой цвет — разные надписи
            left_ok = abs(ln.x - last.x) <= h * 1.0
            center_ok = abs((ln.x + ln.w / 2) - (last.x + last.w / 2)) <= h * 1.2
            if b["align"] == "center" and not center_ok:
                continue
            if b["align"] == "left" and not left_ok:
                continue
            if not (left_ok or center_ok):
                continue
            target = b
            if b["align"] is None and left_ok != center_ok:
                b["align"] = "left" if left_ok else "center"     # подходят оба — решит следующая строка
            b["gaps"].append(max(0, gap))
            break
        if target is None:
            blocks.append({"lines": [ln], "gaps": [], "align": None})
        else:
            target["lines"].append(ln)
    out = []
    for b in blocks:
        blk = Block(lines=b["lines"])
        blk.key = normalize(blk.text)
        out.append(blk)
    return out


def list_break(last: Line, ln: Line) -> bool:
    """``ln`` — следующий пункт меню или списка («Patreon» / «Discord» / «Our Website»), а не
    продолжение абзаца: обе строки короткие, следующая — с большой буквы, а предыдущая не
    оборвана на запятой или дефисе. Склеенные пункты перевелись бы одной бессмысленной фразой, а
    ошибочно разделённые короткие фразы переводятся по отдельности без вреда."""
    a, b = last.text.strip(), ln.text.strip()
    if not a or not b or len(a.split()) > 3 or len(b.split()) > 3:
        return False
    if not (b[0].isupper() or b[0].isdigit()):
        return False
    return not re.search(r"[,;:\-–—&+/]$", a)


def _words_of(text: str) -> List[str]:
    """Слова строки без значков по краям: распознавание приклеивает к слову мусор («^Mohmtan», «"Metiman»)."""
    out = []
    for t in _tokens(text):
        core = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", t)
        if core:
            out.append(core)
    return out


def unknown_words(text: str, words: Set[str]) -> List[str]:
    """Слова строки (от трёх букв), которых нет в английском словаре."""
    return [t for t in _words_of(text) if len(re.findall(r"[A-Za-z]", t)) >= 3 and not _in_words(t.lower(), words)]


def caps_junk(text: str, words: Set[str]) -> bool:
    """Строка только из незнакомых слов ЗАГЛАВНЫМИ («DMSL», «DYDD»): так распознавание читает
    логотип или узор. Настоящие надписи заглавными (EXIT, PLAY) — словарные слова, а
    аббревиатуры (HP, FPS) переводить и не нужно."""
    tokens = [t for t in _words_of(text) if len(re.findall(r"[A-Za-z]", t)) >= 2]
    if not tokens or not all(t.isupper() for t in tokens):
        return False
    return not any(_in_words(t.lower(), words) for t in tokens)


def is_name(text: str, words: Set[str]) -> bool:
    """Надпись — имя (героя, места): только слова с большой буквы, и ни одного из словаря
    («Mothman», «Lucy Vane»). Машинный перевод превращает имена в бессмыслицу («Человек-молот»), а
    имя на табличке понятно и так; внутри фразы имена переводит переводчик вместе с ней."""
    tokens = [t for t in _words_of(text) if len(re.findall(r"[A-Za-z]", t)) >= 2]
    if not tokens or len(tokens) > 3:
        return False
    if not all(t[0].isupper() and not t.isupper() for t in tokens):
        return False
    return not any(_in_words(t.lower(), words) for t in tokens)


def merge_rows(lines: List[Line], apart=None) -> List[Line]:
    """Куски одной строки — в одну строку. Распознавание режет строку на слове другого шрифта или
    цвета, со «спецэффектом» (дрожь, глитч), на широком пробеле — и кусок фразы переводился бы
    отдельно, а неуверенно прочитанное слово оставалось бы по-английски. Цвет внутри строки не
    разделяет (выделенное слово — часть фразы); разделяет черта рамки между кусками (``apart``:
    соседние кнопки, вкладки, ячейки).

    Кусок, прочитанный как кириллица посреди английской строки, — английское слово с эффектом:
    берётся его английское прочтение (``Line.alt``)."""
    lines = sorted((ln for ln in lines if ln.text.strip()), key=lambda ln: (ln.y, ln.x))
    used = [False] * len(lines)
    out: List[Line] = []
    for i, a in enumerate(lines):
        if used[i]:
            continue
        used[i] = True
        row = [a]
        grown = a.rot is None
        while grown:
            grown = False
            gx0, gx1 = min(r.x for r in row), max(r.right for r in row)
            gy0, gy1 = min(r.y for r in row), max(r.bottom for r in row)
            gh = sum(r.h for r in row) / len(row)
            for j, b in enumerate(lines):
                if used[j] or b.rot is not None:
                    continue
                hmin, hmax = min(gh, b.h), max(gh, b.h)
                overlap = min(gy1, b.bottom) - max(gy0, b.y)
                gap = max(b.x - gx1, gx0 - b.right)
                if overlap < 0.6 * hmin or hmax > 1.5 * hmin or gap > 0.8 * hmin:
                    continue
                near = min(row, key=lambda r: max(b.x - r.right, r.x - b.right))
                if apart is not None and apart(near, b):
                    continue
                row.append(b)
                used[j] = True
                grown = True
        if len(row) == 1:
            out.append(a)
            continue
        row.sort(key=lambda r: r.x)
        latin = sum(len(r.text) for r in row if not r.ru) >= sum(len(r.text) for r in row if r.ru)
        parts = [(r.alt if latin and r.ru and r.alt else r.text) for r in row]
        x0, y0 = min(r.x for r in row), min(r.y for r in row)
        x1, y1 = max(r.right for r in row), max(r.bottom for r in row)
        merged = Line(" ".join(p.strip() for p in parts if p.strip()), x0, y0, x1 - x0, y1 - y0)
        size = sum(max(1, len(p)) for p in parts)
        merged.conf = sum(r.conf * max(1, len(p)) for r, p in zip(row, parts)) / size
        merged.ru = not latin
        out.append(merged)
    return sorted(out, key=lambda ln: (ln.y, ln.x))


def tilted_next(last: Line, ln: Line) -> bool:
    """``ln`` — следующая строка той же наклонной надписи, что и ``last``: тот же угол, похожая
    высота, лежит сразу под ней (в повёрнутых осях) и перекрывается с ней вдоль строки."""
    import math
    if last.rot is None or ln.rot is None:
        return False
    cx, cy, ll, lt, la = last.rot
    nx, ny, nl, nt, na = ln.rot
    if abs(la - na) > math.radians(3) or max(lt, nt) / max(1.0, min(lt, nt)) > 1.35:
        return False
    c, s = math.cos(la), math.sin(la)
    along, across = (nx - cx) * c + (ny - cy) * s, -(nx - cx) * s + (ny - cy) * c
    t = (lt + nt) / 2
    return 0.4 * t <= across <= 1.3 * t and abs(along) <= (ll + nl) / 2


def block_rot(lines: Sequence[Line]) -> Optional[Tuple[float, float, float, float, float]]:
    """Общая повёрнутая рамка наклонной надписи из нескольких строк (None — строки не наклонные)."""
    import math
    rots = [ln.rot for ln in lines]
    if not rots or any(r is None for r in rots):
        return None
    if len(rots) == 1:
        return rots[0]
    total = sum(r[2] for r in rots)
    ang = sum(r[4] * r[2] for r in rots) / total
    cx = sum(r[0] for r in rots) / len(rots)
    cy = sum(r[1] for r in rots) / len(rots)
    c, s = math.cos(ang), math.sin(ang)
    us, vs = [], []
    for x, y, length, thick, _ in rots:
        u, v = (x - cx) * c + (y - cy) * s, -(x - cx) * s + (y - cy) * c
        us += [u - length / 2, u + length / 2]
        vs += [v - thick / 2, v + thick / 2]
    du, dv = (min(us) + max(us)) / 2, (min(vs) + max(vs)) / 2
    return (cx + du * c - dv * s, cy + du * s + dv * c, max(us) - min(us), max(vs) - min(vs), ang)


Rect = Tuple[int, int, int, int]


def area(r: Rect) -> int:
    return max(0, r[2]) * max(0, r[3])


def intersect_area(a: Rect, b: Rect) -> int:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return max(0, x1 - x0) * max(0, y1 - y0)


def iou(a: Rect, b: Rect) -> float:
    inter = intersect_area(a, b)
    union = area(a) + area(b) - inter
    return inter / union if union > 0 else 0.0


def line_rect(ln: Line) -> Rect:
    return ln.x, ln.y, ln.w, ln.h


def letters(text: str) -> int:
    return len(_LETTER.findall(text or ""))


def merge_deep(base: List[Line], variants: List[List[Line]], min_votes: int = 2) -> List[Line]:
    """Строки глубокого прохода, которых нет в быстром (``base``).

    Каждый вариант картинки (контраст, отдельный цвет, увеличение…) распознаётся
    отдельно; строка берётся, только если её подтвердили хотя бы ``min_votes``
    вариантов (одна и та же область, похожий текст) — так отсекается шум
    распознавания на узорах и картинках. Из подтверждений берётся самое полное
    прочтение. Строка быстрого прохода заменяется, если глубокий прочитал в ней
    заметно больше букв (быстрый увидел только часть надписи)."""
    groups: List[List[Line]] = []
    for lines in variants:
        seen_here: Set[int] = set()
        for ln in lines:
            if letters(ln.text) < 2:
                continue
            r = line_rect(ln)
            best, best_i = 0.0, -1
            for i, g in enumerate(groups):
                if i in seen_here:
                    continue
                ov = max(iou(r, line_rect(m)) for m in g)
                if ov > best:
                    best, best_i = ov, i
            if best >= 0.3 and _text_agrees(groups[best_i], ln.text):
                groups[best_i].append(ln)
                seen_here.add(best_i)
            else:
                groups.append([ln])
                seen_here.add(len(groups) - 1)
    out: List[Line] = []
    for g in groups:
        if len(g) < min_votes:
            continue
        pick = max(g, key=lambda m: (letters(m.text), -m.text.count(" ")))
        r = line_rect(pick)
        overlapping = [b for b in base if intersect_area(r, line_rect(b)) > 0.3 * min(area(r), area(line_rect(b)))]
        if overlapping and letters(pick.text) < 1.5 * max(letters(b.text) for b in overlapping):
            continue                        # быстрый проход уже прочитал эту надпись
        out.append(pick)
    return out


def _text_agrees(group: List[Line], s: str) -> bool:
    """Та же надпись: похожий текст или один вариант прочитал часть другого."""
    k = normalize(s)
    for m in group:
        km = normalize(m.text)
        if not k or not km:
            continue
        if similar(k, km, 0.6) or (len(k) >= 3 and k in km) or (len(km) >= 3 and km in k):
            return True
        if {w for w in k.split() if len(w) >= 3} & {w for w in km.split() if len(w) >= 3}:
            return True
    return False


def similar(a: str, b: str, cutoff: float = 0.88) -> bool:
    if a == b:
        return True
    if not a or not b or abs(len(a) - len(b)) > max(len(a), len(b)) * (1 - cutoff) + 2:
        return False
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= cutoff


class TranslationCache:
    """Переводы этой сессии: точный ключ или очень похожий (шум распознавания).

    Спрашивают часто (на каждом кадре — про каждую надпись), поэтому нечёткий поиск делается один
    раз на ключ: найденное запоминается, как и «не найдено» (до следующего нового перевода)."""

    def __init__(self, limit: int = 3000):
        self.limit = limit
        self.items: Dict[str, str] = {}
        self._alias: Dict[str, str] = {}
        self._miss: Set[str] = set()

    def get(self, key: str) -> Optional[str]:
        if key in self.items:
            return self.items[key]
        alias = self._alias.get(key)
        if alias is not None and alias in self.items:
            return self.items[alias]
        if len(key) < 6 or key in self._miss:
            return None
        cands = [k for k in self.items if abs(len(k) - len(key)) <= max(3, len(key) // 10)]
        best = difflib.get_close_matches(key, cands, n=1, cutoff=0.92)
        if best:
            self._alias[key] = best[0]
            return self.items[best[0]]
        self._miss.add(key)
        return None

    def put(self, key: str, value: str) -> None:
        if len(self.items) >= self.limit:
            for k in list(self.items)[: self.limit // 4]:
                del self.items[k]
            self._alias.clear()
        self.items[key] = value
        self._miss.clear()
