"""Текст с экрана: строки распознавания → блоки, фильтр иностранного, устойчивость.

Распознавание возвращает строки с рамками. Соседние строки одного абзаца
склеиваются в блок — переводится блок целиком (контекст, падежи), и плашка
перевода ложится поверх всего абзаца.

Распознавание «шумит»: одна и та же строка в соседних кадрах может прийти с
разницей в букву, а текст «печатной машинки» растёт по буквам. Поэтому блок
считается готовым к переводу, когда он почти не менялся два прохода подряд, а
похожие варианты одного текста узнаются нечётким сравнением.

Английское распознавание читает русский текст как латинскую абракадабру
(«Начать» → «HaqaTb», «Налоговые» → «Hanor0Bble»). Такие строки отсеиваются:
по русскому распознаванию той же области (:func:`cyrillic_share`) или, если его
нет, по приметам (:func:`misread_cyrillic`).
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


@dataclass
class Line:
    text: str
    x: int
    y: int
    w: int
    h: int

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def right(self) -> int:
        return self.x + self.w


@dataclass
class Block:
    lines: List[Line]
    key: str = ""
    translation: Optional[str] = None
    first_seen: float = 0.0
    last_seen: float = 0.0
    stable_hits: int = 0
    skip: bool = False               # переводить нечего (перевод совпал с оригиналом) — плашку не рисуем
    deep: bool = False               # найден только глубоким проходом (надпись на картинке, особый шрифт)
    hold_until: float = 0.0          # до какого момента держать блок, даже если быстрый проход его не видит
    refined: Optional[str] = None    # текст, перечитанный крупнее (None — ещё не перечитывали, "" — не лучше)

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
                     r"physx|bink(?: video)?|speedtree|dolby(?: atmos)?)?\s*[™®©]?$")


def is_brand(text: str) -> bool:
    """Строка — надпись заставки движка («Made with Unity», «Powered by Unreal Engine»)."""
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


def normalize(text: str) -> str:
    """Ключ текста: регистр, пробелы и знаки распознавания не важны."""
    t = (text or "").lower()
    t = re.sub(r"[\s ]+", " ", t)
    t = re.sub(r"[^\w ]+", "", t, flags=re.U)
    return t.strip()


def group_lines(lines: List[Line]) -> List[Block]:
    """Склеить строки в абзацы: близко по вертикали, похожая высота, общий край или центр."""
    lines = sorted((ln for ln in lines if ln.text.strip() and ln.h > 0), key=lambda ln: (ln.y, ln.x))
    blocks: List[List[Line]] = []
    for ln in lines:
        target = None
        for b in reversed(blocks[-6:]):
            last = b[-1]
            h = (last.h + ln.h) / 2
            gap = ln.y - last.bottom
            if gap < -h * 0.5 or gap > h * 0.9:
                continue
            if max(last.h, ln.h) / max(1, min(last.h, ln.h)) > 1.6:
                continue
            left_ok = abs(ln.x - last.x) <= h * 1.5
            center_ok = abs((ln.x + ln.w / 2) - (last.x + last.w / 2)) <= h * 2
            overlap = min(ln.right, last.right) - max(ln.x, last.x) > 0
            if left_ok or center_ok or (overlap and abs(ln.x - last.x) <= h * 4):
                target = b
                break
        if target is None:
            blocks.append([ln])
        else:
            target.append(ln)
    out = []
    for b in blocks:
        blk = Block(lines=b)
        blk.key = normalize(blk.text)
        out.append(blk)
    return out


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


class Tracker:
    """Какие блоки на экране сейчас и какие из них уже можно переводить.

    ``update`` сопоставляет новые блоки с прошлыми (по близкому тексту и месту),
    копит «попадания без изменений»; готов к переводу блок, который продержался
    ``need`` проходов подряд почти без изменений текста.
    """

    def __init__(self, need: int = 2, forget_after: float = 1.2):
        self.need = need
        self.forget_after = forget_after
        self.blocks: List[Block] = []

    def update(self, fresh: List[Block], now: Optional[float] = None, covered: Sequence[Rect] = (),
               scene_cut: bool = False) -> List[Block]:
        """``covered`` — области, которые в этом кадре не видны (закрыты нашими плашками при захвате
        экрана): блоки под ними не пропадают. ``scene_cut`` — картинка сменилась целиком: пропавшие
        блоки убираются сразу, без передержки."""
        now = time.monotonic() if now is None else now
        kept: List[Block] = []
        used = set()
        for nb in fresh:
            match = None
            for i, ob in enumerate(self.blocks):
                if i in used:
                    continue
                if _near(ob.rect, nb.rect) and (similar(ob.key, nb.key) or _grows(ob.key, nb.key)):
                    match = i
                    break
            if match is None:
                nb.first_seen = nb.last_seen = now
                nb.stable_hits = 1 if nb.key else 0
                kept.append(nb)
                continue
            used.add(match)
            ob = self.blocks[match]
            if _same(ob.key, nb.key):
                nb.stable_hits = ob.stable_hits + 1
                if similar(ob.key, nb.key, 0.93):
                    nb.refined = ob.refined
                if (ob.translation or ob.skip) and similar(ob.key, nb.key, 0.93):
                    nb.translation = ob.translation     # тот же текст (с поправкой на шум) — перевод остаётся
                    nb.skip = ob.skip
                    nb.key = ob.key
            else:
                nb.stable_hits = 1                      # текст ещё печатается — ждём
            nb.first_seen = ob.first_seen
            nb.last_seen = now
            nb.deep = nb.deep and ob.deep
            nb.hold_until = max(nb.hold_until, ob.hold_until)
            kept.append(nb)
        for i, ob in enumerate(self.blocks):
            if i in used:
                continue
            if any(intersect_area(ob.rect, c) >= 0.5 * area(ob.rect) for c in covered):
                ob.last_seen = now                      # под нашей плашкой — не видно, но текст там есть
                kept.append(ob)
            elif scene_cut:
                continue
            elif now < ob.hold_until:
                kept.append(ob)                         # надпись с картинки: быстрый проход её не видит
            elif now - ob.last_seen < self.forget_after and (ob.translation or ob.skip):
                kept.append(ob)                         # мигнул курсор, сменился кадр — держим недолго
        self.blocks = kept
        return kept

    def add(self, fresh: List[Block], now: Optional[float] = None, hold: float = 0.0) -> List[Block]:
        """Добавить блоки глубокого прохода к тем, что уже на экране (без пропажи остальных)."""
        now = time.monotonic() if now is None else now
        added: List[Block] = []
        for nb in fresh:
            match = next((ob for ob in self.blocks if _near(ob.rect, nb.rect) and
                          (similar(ob.key, nb.key) or intersect_area(ob.rect, nb.rect) >= 0.5 * area(nb.rect))),
                         None)
            if match is not None:
                match.hold_until = max(match.hold_until, now + hold)
                match.last_seen = now
                continue
            nb.first_seen = nb.last_seen = now
            nb.stable_hits = self.need if nb.key else 0     # статичный кадр — устойчивость уже проверена
            nb.deep = True
            nb.hold_until = now + hold
            self.blocks.append(nb)
            added.append(nb)
        return added

    def ready(self) -> List[Block]:
        return [b for b in self.blocks if not b.translation and not b.skip and b.key and b.stable_hits >= self.need]


def _same(old: str, new: str) -> bool:
    """Тот же текст с поправкой на шум распознавания (но не дописанный — печатная машинка)."""
    if old == new:
        return True
    if abs(len(old) - len(new)) > max(2, len(new) // 20):
        return False
    return similar(old, new, 0.9)


def _near(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    tol = max(ah, bh, 12)
    return abs(ay - by) <= tol * 1.5 and abs(ax - bx) <= max(aw, bw, 40) * 0.6


def _grows(old: str, new: str) -> bool:
    """Печатная машинка: новый текст продолжает старый."""
    return bool(old) and len(new) > len(old) and new.startswith(old[: max(1, len(old) - 2)])


class TranslationCache:
    """Переводы этой сессии: точный ключ или очень похожий (шум распознавания)."""

    def __init__(self, limit: int = 3000):
        self.limit = limit
        self.items: Dict[str, str] = {}

    def get(self, key: str) -> Optional[str]:
        if key in self.items:
            return self.items[key]
        if len(key) < 6:
            return None
        cands = [k for k in self.items if abs(len(k) - len(key)) <= max(3, len(key) // 10)]
        best = difflib.get_close_matches(key, cands, n=1, cutoff=0.92)
        return self.items[best[0]] if best else None

    def put(self, key: str, value: str) -> None:
        if len(self.items) >= self.limit:
            for k in list(self.items)[: self.limit // 4]:
                del self.items[k]
        self.items[key] = value
