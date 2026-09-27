"""Текст с экрана: строки распознавания → блоки, фильтр иностранного, устойчивость.

Распознавание возвращает строки с рамками. Соседние строки одного абзаца
склеиваются в блок — переводится блок целиком (контекст, падежи), и плашка
перевода ложится поверх всего абзаца.

Распознавание «шумит»: одна и та же строка в соседних кадрах может прийти с
разницей в букву, а текст «печатной машинки» растёт по буквам. Поэтому блок
считается готовым к переводу, когда он почти не менялся два прохода подряд, а
похожие варианты одного текста узнаются нечётким сравнением.
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


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

    def update(self, fresh: List[Block], now: Optional[float] = None) -> List[Block]:
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
                if ob.translation and similar(ob.key, nb.key, 0.93):
                    nb.translation = ob.translation     # тот же текст (с поправкой на шум) — перевод остаётся
                    nb.key = ob.key
            else:
                nb.stable_hits = 1                      # текст ещё печатается — ждём
            nb.first_seen = ob.first_seen
            nb.last_seen = now
            kept.append(nb)
        # пропавший на мгновение блок (мигнул курсор, сменился кадр) держим недолго
        for i, ob in enumerate(self.blocks):
            if i not in used and now - ob.last_seen < self.forget_after and ob.translation:
                kept.append(ob)
        self.blocks = kept
        return kept

    def ready(self) -> List[Block]:
        return [b for b in self.blocks if not b.translation and b.key and b.stable_hits >= self.need]


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
