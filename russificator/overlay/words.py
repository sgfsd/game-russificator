"""Английские слова для живого перевода: словарь и исправление ошибок распознавания.

Словарь берётся из модели машинного переводчика (``sentencepiece.model`` пакета
Argos en→ru, он же запасной переводчик оверлея): целые английские слова из её
словаря — это ~10 тысяч самых частых слов, упорядоченных по частоте. Отдельный
файл со словарём программе не нужен.

Распознавание путает похожие буквы в пиксельных и стилизованных шрифтах
(«graphic» → «sraphic», «highly» → «hiShlg», «children» → «chi Idren»).
:func:`fix` исправляет такие слова по таблице типичных замен, если в словаре
находится ровно подходящее слово, — переводчик получает нормальный английский.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("russificator.overlay.words")

#: типичные ошибки распознавания: (что прочитано, что было на самом деле)
CONFUSIONS = [
    ("rn", "m"), ("m", "rn"), ("cl", "d"), ("vv", "w"), ("ii", "ll"), ("li", "h"), ("ri", "n"),
    ("0", "o"), ("1", "l"), ("5", "s"), ("8", "b"), ("6", "b"), ("I", "l"), ("l", "i"), ("i", "l"),
    ("S", "g"), ("s", "g"), ("g", "y"), ("q", "g"), ("h", "b"), ("b", "h"), ("u", "v"), ("v", "u"),
    ("e", "c"), ("c", "e"), ("o", "a"), ("a", "o"), ("n", "h"), ("t", "f"), ("f", "t"),
    # заглавные (надписи капсом)
    ("I", "L"), ("0", "O"), ("5", "S"), ("8", "B"), ("O", "V"), ("VV", "W"), ("CL", "D"), ("RN", "M"),
    ("O", "D"), ("D", "O"), ("U", "V"), ("V", "U"), ("E", "F"), ("F", "E"), ("H", "N"), ("N", "H"),
    ("E", "t"), ("t", "l"), ("l", "t"),
]

_TOKEN = re.compile(r"[A-Za-z0-9']+|[^A-Za-z0-9']+")

#: похожие буквы (после приведения к нижнему регистру) — их замена «дешевле» в расстоянии между словами
_LOOK_ALIKE = {frozenset(p) for p in (
    "tl", "et", "sg", "gy", "il", "1l", "1i", "0o", "5s", "8b", "6b", "hb", "uv", "ec", "oa", "nh", "tf", "qg",
    "od", "vy", "co", "uw", "vw", "rn", "ij", "jl", "ft", "ao", "ce", "bd", "pb", "mn", "ae")}
_MULTI = {("rn", "m"), ("cl", "d"), ("vv", "w"), ("ii", "ll"), ("li", "h"), ("ri", "n"), ("in", "m"), ("ln", "h")}


#: классы похожих букв — для быстрого отсева непохожих слов перед точным сравнением
_CLASS = {}
for _i, _group in enumerate(("tlij1f", "ec", "sg5qy", "oad0", "bh86", "uvwy", "nmh", "r", "k", "p", "x", "z")):
    for _ch in _group:
        _CLASS.setdefault(_ch, _i)


def _mask(word: str) -> int:
    m = 0
    for ch in word:
        m |= 1 << _CLASS.get(ch, 12)
    return m


def _sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    return 0.3 if frozenset((a, b)) in _LOOK_ALIKE else 1.0


def weighted_distance(src: str, dst: str, limit: float = 99.0) -> float:
    """Расстояние между прочитанным и словарным словом: похожие буквы (t↔l, s↔g, rn↔m…) почти
    ничего не стоят, остальные замены и пропуски — дорого."""
    n, m = len(src), len(dst)
    prev = [j * 0.8 for j in range(m + 1)]
    prev2 = prev
    for i in range(1, n + 1):
        cur = [i * 0.8] + [0.0] * m
        best = cur[0]
        for j in range(1, m + 1):
            v = min(prev[j] + 0.8, cur[j - 1] + 0.8, prev[j - 1] + _sub_cost(src[i - 1], dst[j - 1]))
            if i >= 2 and (src[i - 2:i], dst[j - 1]) in _MULTI:
                v = min(v, prev2[j - 1] + 0.3)          # «rn» прочитано вместо «m»
            if j >= 2 and (dst[j - 2:j], src[i - 1]) in _MULTI:
                v = min(v, prev[j - 2] + 0.3)           # «m» прочитано вместо «rn»
            cur[j] = v
            best = min(best, v)
        if best > limit:
            return best
        prev2, prev = prev, cur
    return prev[m]


class Words:
    def __init__(self, ranks: Dict[str, int]):
        self.ranks = ranks                   # слово (нижний регистр) -> место по частоте (меньше — чаще)
        self._by_len: Dict[int, List[tuple]] = {}
        for w in ranks:
            self._by_len.setdefault(len(w), []).append((w, _mask(w)))
        self._memo: Dict[str, Optional[str]] = {}

    def __contains__(self, word: str) -> bool:
        return self.known(word)

    def __len__(self) -> int:
        return len(self.ranks)

    def known(self, word: str) -> bool:
        w = word.lower().strip("'")
        if w in self.ranks:
            return True
        if w.endswith("'s") and w[:-2] in self.ranks:
            return True
        for suf in ("s", "es", "ed", "d", "ing", "er", "ly"):
            if w.endswith(suf) and len(w) - len(suf) >= 3 and w[:-len(suf)] in self.ranks:
                return True
        return False

    def rank(self, word: str) -> int:
        w = word.lower()
        if w in self.ranks:
            return self.ranks[w]
        for suf in ("s", "es", "ed", "d", "ing", "er", "ly", "'s"):
            if w.endswith(suf) and w[:-len(suf)] in self.ranks:
                return self.ranks[w[:-len(suf)]] + 1
        return 1 << 30

    # ---------------------------------------------------------- исправление

    def correct(self, token: str) -> Optional[str]:
        """Словарное слово, которое распознавание прочитало как ``token`` (или None)."""
        if len(token) < 2 or self.known(token) or (len(token) == 2 and token.lower() in self.ranks):
            return None
        if token in self._memo:
            return self._memo[token]
        best = self._by_substitution(token) or (self._by_distance(token) if len(token) >= 3 else None)
        fixed = _same_case(token, best) if best else None
        if len(self._memo) > 5000:
            self._memo.clear()
        self._memo[token] = fixed
        return fixed

    def _by_substitution(self, token: str) -> Optional[str]:
        """Одна-две типичные замены букв дают словарное слово."""
        cands = {}
        for one in _variants(token):
            if one != token and self.known(one):
                cands[one] = self.rank(one)
        if not cands:
            for one in list(_variants(token)):
                for two in _variants(one):
                    if two != token and self.known(two):
                        cands[two] = self.rank(two) + 50000       # две замены — хуже одной
        return min(cands, key=cands.get) if cands else None

    def _by_distance(self, token: str) -> Optional[str]:
        """Ближайшее словарное слово по «взвешенному» расстоянию (похожие буквы — дёшево):
        «dottars» → «dollars», «Eotat» → «total». Берётся, только если оно заметно ближе остальных."""
        low = token.lower()
        if not re.fullmatch(r"[a-z0-9']{4,}", low):
            return None
        limit = max(0.61, 0.16 * len(low))
        mask = _mask(low)
        scored = []
        for n in (len(low) - 1, len(low), len(low) + 1):
            for w, wm in self._by_len.get(n, ()):
                if (mask ^ wm).bit_count() > 2:
                    continue                             # другие буквы — точно не это слово
                d = weighted_distance(low, w, limit)
                if d <= limit:
                    scored.append((d, self.ranks[w], w))
        if not scored:
            return None
        scored.sort()
        if len(scored) > 1 and scored[1][0] - scored[0][0] < 0.25:
            return None                                  # два одинаково похожих слова — не угадываем
        return scored[0][2]

    def fix(self, text: str) -> str:
        """Исправить в строке слова с типичными ошибками распознавания (остальное — как есть)."""
        parts = _TOKEN.findall(text or "")
        out: List[str] = []
        i = 0
        while i < len(parts):
            tok = parts[i]
            if not re.match(r"[A-Za-z0-9']", tok) or not re.search(r"[A-Za-z]", tok):
                out.append(tok)
                i += 1
                continue
            # «sui table», «chi Idren»: слово, разорванное распознаванием пробелом
            if i + 2 < len(parts) and parts[i + 1] == " " and re.search(r"[A-Za-z]", parts[i + 2]):
                nxt = parts[i + 2]
                if not (self.known(tok) and self.known(nxt)):
                    glued = tok + nxt
                    fixed = glued if self.known(glued) else self.correct(glued)
                    if fixed and len(glued) >= 5:
                        out.append(_same_case(glued, fixed))
                        i += 3
                        continue
            fixed = self.correct(tok) if len(tok) >= 2 else None
            out.append(fixed or tok)
            i += 1
        return "".join(out)


def _variants(token: str):
    for bad, good in CONFUSIONS:
        start = token.find(bad)
        while start >= 0:
            yield token[:start] + good + token[start + len(bad):]
            start = token.find(bad, start + 1)
    if not token.isupper() and not token.islower() and not (token[:1].isupper() and token[1:].islower()):
        yield token.lower()                     # «hiShlg»: регистр посреди слова — тоже ошибка распознавания


def _same_case(orig: str, word: str) -> str:
    if orig.isupper() and len(orig) > 1:
        return word.upper()
    if orig[:1].isupper() and orig[:1].lower() == word[:1].lower():
        return word[:1].upper() + word[1:].lower()
    if orig[1:2].isupper() and orig[1:].isupper():
        return word.upper()
    return word.lower()                  # заглавная была ошибкой распознавания («Eotat» → «total»)


# ---------------------------------------------------------------- загрузка

_lock = threading.Lock()
_cached: Optional[Words] = None


def load(model: Optional[Path] = None) -> Optional[Words]:
    """Словарь из sentencepiece-модели машинного переводчика (None — модели нет)."""
    global _cached
    with _lock:
        default = model is None
        if default and _cached is not None:
            return _cached
        if default:
            from ..translation.machine import model_dir
            model = model_dir() / "sentencepiece.model"
        if not Path(model).is_file():
            return None
        try:
            import sentencepiece as spm
            sp = spm.SentencePieceProcessor(model_file=str(model))
        except Exception as exc:  # noqa: BLE001
            log.warning("словарь английских слов не загружен: %s", exc)
            return None
        ranks: Dict[str, int] = {}
        for i in range(sp.get_piece_size()):
            piece = sp.id_to_piece(i)
            if piece.startswith("▁") and re.fullmatch(r"[A-Za-z]{2,}", piece[1:]):
                ranks.setdefault(piece[1:].lower(), i)
        for w in ("a", "i"):
            ranks.setdefault(w, 0)
        words = Words(ranks)
        log.info("словарь английских слов: %d", len(words))
        if default:
            _cached = words
        return words
