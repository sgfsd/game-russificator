"""Русские буквы пиксельного шрифта — из его же латинских букв, клетка в клетку.

Мелкий пиксельный шрифт (заглавная в 4–8 клеток) нельзя «перерисовать» с обычного шрифта: в 5 клеток
не влезает ни одна буква PT Sans — Ж, Ф, Ш, Ы сливаются в кляксу, тонкие штрихи пропадают. Поэтому
здесь буквы собираются так, как их собирает сам шрифтовик пиксельного шрифта, — из готовых латинских
букв и цифр того же шрифта: И — зеркальная N, Я — зеркальная R, Ь — перевёрнутая P, Г — перевёрнутая L,
З — цифра 3, П/Ч/Ш/Ц/Л/Д/У — из штрихов H, Ж — из K, Ф и Ю — из O. Толщина штрихов, скругления,
пропорции и межбуквенный интервал выходят те же, что у латиницы. Строчные — те же формы высотой
строчных (так рисуется русский строчный шрифт), кроме б и ф с выносными.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np

#: латинские буквы и цифры, из которых собираются русские
SOURCES = "ABCEHIKLMNOPRTWXYËëbenopqxyg036dhkl"
#: буквы с надстрочными знаками: по ним видно, сколько места над буквой шрифт отводит знакам
ACCENTED_UPPER = "ÀÁÂÄÅÈÉÊËÌÍÎÏÒÓÔÖÙÚÛÜ"
ACCENTED_LOWER = "àáâäåèéêëìíîïòóôöùúûü"


@dataclass
class Bm:
    """Пиксельная буква: клетки строками сверху вниз; ``top`` — сколько строк над базовой линией."""
    a: np.ndarray
    top: int
    left: int = 0

    @property
    def w(self) -> int:
        return int(self.a.shape[1])

    @property
    def h(self) -> int:
        return int(self.a.shape[0])

    @property
    def bottom(self) -> int:
        return self.top - self.h


def rasterize(contours, g: float, origin: Tuple[float, float]) -> Optional[Bm]:
    """Контуры пиксельной буквы (прямые отрезки по сетке ``g``) -> клетки (правило ненулевой обмотки)."""
    pts = [p for c in contours for p in c]
    if not pts:
        return None
    ox, oy = origin
    xs = [(x - ox) / g for x, _, _ in pts]
    ys = [(y - oy) / g for _, y, _ in pts]
    i0, i1 = int(math.floor(min(xs) + 0.25)), int(math.ceil(max(xs) - 0.25))
    j0, j1 = int(math.floor(min(ys) + 0.25)), int(math.ceil(max(ys) - 0.25))
    if i1 <= i0 or j1 <= j0:
        return None
    cx = np.arange(i0, i1) + 0.5
    cy = np.arange(j1 - 1, j0 - 1, -1) + 0.5            # строки сверху вниз
    px, py = np.meshgrid(cx, cy)
    wind = np.zeros(px.shape, dtype=int)
    for c in contours:
        n = len(c)
        for k in range(n):
            x0, y0 = (c[k - 1][0] - ox) / g, (c[k - 1][1] - oy) / g
            x1, y1 = (c[k][0] - ox) / g, (c[k][1] - oy) / g
            if y0 == y1:
                continue
            up = y0 < y1
            lo, hi = (y0, y1) if up else (y1, y0)
            hit = (py >= lo) & (py < hi)
            xi = x0 + (py - y0) * (x1 - x0) / (y1 - y0)
            hit &= px < xi
            wind += np.where(hit, 1 if up else -1, 0)
    a = wind != 0
    if not a.any():
        return None
    return _trim(Bm(a, j1, i0))


def _trim(b: Bm) -> Bm:
    ys, xs = np.nonzero(b.a)
    if len(xs) == 0:
        return b
    return Bm(b.a[ys.min():ys.max() + 1, xs.min():xs.max() + 1].copy(), b.top - int(ys.min()), b.left + int(xs.min()))


def _runs(row) -> list:
    out, x, w = [], 0, len(row)
    while x < w:
        if row[x]:
            x0 = x
            while x < w and row[x]:
                x += 1
            out.append((x0, x))
        else:
            x += 1
    return out


def topology(a: np.ndarray) -> Tuple[int, int]:
    """Число кусков «чернил» (связность по 8 соседям) и замкнутых просветов (по 4) пиксельной буквы."""
    h, w = a.shape

    def count(mask, diag, skip_border):
        seen = np.zeros_like(mask)
        n = 0
        steps = [(-1, 0), (1, 0), (0, -1), (0, 1)] + ([(-1, -1), (-1, 1), (1, -1), (1, 1)] if diag else [])
        for y in range(h):
            for x in range(w):
                if not mask[y, x] or seen[y, x]:
                    continue
                stack, border = [(y, x)], False
                seen[y, x] = True
                while stack:
                    cy, cx = stack.pop()
                    border |= cy in (0, h - 1) or cx in (0, w - 1)
                    for dy, dx in steps:
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
                n += not (skip_border and border)
        return n
    return count(a, True, False), count(~a, False, True)


def column_strokes(a: np.ndarray) -> int:
    """Сколько горизонтальных штрихов пересекают столбцы буквы (сумма отрезков по столбцам): у Е — три
    на каждый столбец правее стойки; слились две перекладины — число уменьшилось."""
    pad = np.pad(a, ((1, 0), (0, 0)))
    return int((pad[1:] & ~pad[:-1]).sum())


def fit_rows(a: np.ndarray, rows: int) -> np.ndarray:
    """Изменить высоту пиксельной буквы до ``rows`` строк, не ломая её.

    Ниже: убирается строка, а если так буква рвётся на куски — строка сливается с соседней (у К так
    сохраняется стык диагоналей со стойкой). Из всех вариантов берётся тот, что не рвёт букву и не
    теряет просветов; при равенстве — убирается самая «пустая» строка (вертикальные штрихи, а не
    перекладина), ближе к середине, а из двух равных — верхняя (у в, ь, ы крупнее остаётся нижняя
    часть). Крайняя строка убирается, только если она повторяет соседнюю: иначе укорачивается штрих.
    Выше: повторяется строка, совпадающая с соседней (прямой участок штриха), или самая «пустая»."""
    guard = 0
    while a.shape[0] != rows and guard < 64:
        guard += 1
        h = a.shape[0]
        mid = (h - 1) / 2.0
        if h > rows:
            if h <= 1:
                break
            comp0, holes0 = topology(a)
            best = None
            for i in range(h):
                edge = i in (0, h - 1)
                same = np.array_equal(a[i], a[1 if i == 0 else h - 2]) if edge else False
                variants = [np.delete(a, i, axis=0)] if (not edge or same) else [None]
                if i > 0:
                    merged = a.copy()
                    merged[i - 1] |= merged[i]
                    variants.append(np.delete(merged, i, axis=0))
                for kind, v in enumerate(variants):
                    if v is None:
                        continue
                    comp, holes = topology(v)
                    penalty = 10 * abs(comp - comp0) + 5 * abs(holes - holes0)
                    key = (penalty, kind, int(a[i].sum()), abs(i - mid - 0.5 * kind), i)
                    if best is None or key < best[0]:
                        best = (key, v)
            a = best[1]
        else:
            same = [i for i in range(1, h) if np.array_equal(a[i], a[i - 1])]
            if same:
                i = min(same, key=lambda k: (int(a[k].sum()), abs(k - 0.5 - mid)))
            else:                                    # повтор самой «пустой» строки — не утолщает штрихи
                i = min(range(h), key=lambda k: (int(a[k].sum()), abs(k - mid)))
            a = np.insert(a, i, a[i], axis=0)
    return a


def fit_cols(a: np.ndarray, cols: int) -> np.ndarray:
    """То же по ширине: повторяется (убирается) колонка, совпадающая с соседней, ближе к середине."""
    return fit_rows(a.T, cols).T


class Kit:
    """Латинские буквы шрифта в клетках и его мерки в клетках."""

    def __init__(self, src: Dict[str, Bm], adv: Dict[str, int], cap: int, xh: int, true_lower: bool,
                 s: int, bar: Tuple[int, int]):
        self.src = src
        self.adv = adv
        self.cap = cap
        self.xh = xh
        self.true_lower = true_lower
        H = src["H"]
        self.H = H
        self.s = s                                   # толщина вертикального штриха
        self.bar = bar                               # строки перекладины H
        self.b = bar[1] - bar[0]                     # толщина горизонтального штриха
        self.gap = H.w - 2 * s                       # просвет между стойками
        self.stem = H.a[:, :s].copy()                # стойка шрифта (со срезами концов, если они есть)
        # докуда могут подниматься надстрочные знаки: как у своих букв с диакритикой; нет таких
        # букв — у заглавных знак остаётся в высоте заглавной (строки меню бывают вплотную),
        # у строчных — в высоте выносных (b, d, h, l)
        acc_u = [b.top for c, b in src.items() if c in ACCENTED_UPPER]
        acc_l = [b.top for c, b in src.items() if c in ACCENTED_LOWER]
        asc = [b.top for c, b in src.items() if c in "bdhkl"]
        self.accent_top_upper = max(acc_u) if acc_u else H.top
        self.accent_top_lower = max(acc_l) if acc_l else max(asc + [H.top]) if true_lower else self.accent_top_upper
        base = H.bottom
        desc = 0
        for c in "pqgy":
            if c in src:
                desc = max(desc, base - src[c].bottom)
        self.desc = max(1, min(desc or 1, max(1, int(round(cap / 6.0)))))

    @staticmethod
    def analyse_h(H: Bm) -> Optional[Tuple[int, Tuple[int, int]]]:
        """Толщина стоек и строки перекладины H — или None, если H не «две стойки и перекладина».

        Стойки меряются по самой частой строке из двух отрезков: у стилизованных шрифтов концы стоек
        бывают срезаны или заострены, а середина — ровная."""
        from collections import Counter
        two = [tuple(r) for r in (_runs(row) for row in H.a) if len(r) == 2]
        if not two:
            return None
        (left, right), n = Counter(two).most_common(1)[0]
        if n < H.h * 0.4:
            return None
        s = left[1] - left[0]
        if left[0] != 0 or right[1] != H.w or right[1] - right[0] != s or H.w - 2 * s < 1:
            return None
        inner = H.a[:, s:H.w - s]
        bar = [r for r in range(H.h) if inner[r].any()]
        if not bar or bar[-1] - bar[0] + 1 > H.h // 2 or not inner[bar[0]:bar[-1] + 1].all():
            return None
        if H.a[:, :s].mean() < 0.7 or H.a[:, H.w - s:].mean() < 0.7:
            return None
        return s, (bar[0], bar[-1] + 1)

    @classmethod
    def build(cls, cells: Dict[str, Bm], adv: Dict[str, int], cap: int, xh: int, true_lower: bool) -> Optional["Kit"]:
        """None — шрифт не годится для сборки (нет H или она не «две стойки и перекладина»)."""
        H = cells.get("H")
        if H is None or H.h != cap or H.w < 3:
            return None
        got = cls.analyse_h(H)
        if got is None:
            return None
        return cls(cells, adv, cap, xh, true_lower, *got)

    # --- вспомогательное ---

    def _cap_src(self, ch: str) -> Optional[Bm]:
        b = self.src.get(ch)
        if b is None or b.h != self.cap or b.top != self.H.top:
            return None
        return b

    def _new(self, w: int, h: Optional[int] = None) -> np.ndarray:
        return np.zeros((h or self.cap, w), dtype=bool)

    def _bm(self, a: np.ndarray, top: Optional[int] = None) -> Bm:
        return Bm(a, self.H.top if top is None else top)

    def _stems(self) -> np.ndarray:
        """H без перекладины."""
        a = self.H.a.copy()
        a[self.bar[0]:self.bar[1], self.s:self.H.w - self.s] = False
        return a

    def _tail(self, a: np.ndarray) -> np.ndarray:
        """Хвостик Ц/Щ: нижняя перекладина продлевается вправо, от её конца — выносной вниз."""
        t = max(1, self.s // 2)
        h, w = a.shape
        out = np.zeros((h + self.desc, w + t), dtype=bool)
        out[:h, :w] = a
        out[h - self.b:h, w:w + t] = True
        out[h:, w:w + t] = True
        return out

    # --- заглавные ---

    def upper(self, ch: str) -> Optional[Bm]:
        f = getattr(self, "_u_" + ch, None)
        try:
            return f() if f else None
        except Exception:  # noqa: BLE001 — не собралась буква: её нарисует запасной путь
            return None

    def _u_И(self):
        n = self._cap_src("N")
        if n is not None and self._diagonal(n):
            return self._bm(n.a[:, ::-1].copy())
        # у N нет диагонали (в некоторых шрифтах она нарисована как «П» со скосом) — И строится из
        # стоек H и диагонали снизу слева вверх направо; внутри — не меньше двух клеток
        s, h = self.s, self.cap
        gi = max(2, self.gap)
        w = 2 * s + gi
        a = self._new(w)
        a[:, :s] = True
        a[:, w - s:] = True
        y0 = max(1, int(round(h * 0.2)))
        y1 = max(y0 + min(gi, max(1, h - y0 - 1)), int(round(h * 0.6)))
        span = y1 - y0
        for c in range(gi):
            r0 = y0 + (gi - 1 - c) * span // gi
            r1 = max(r0 + 1, y0 + (gi - c) * span // gi) + s - 1
            a[r0:min(h, r1), s + c] = True
        return self._bm(a)

    def _diagonal(self, b: Bm) -> bool:
        """Между стойками буквы — диагональ (клетки в нескольких строках и столбцах), а не перекладина."""
        s = self.s
        inner = b.a[:, s:b.w - s]
        if inner.shape[1] < 1:
            return False
        rows = np.nonzero(inner.any(axis=1))[0]
        cols = np.nonzero(inner.any(axis=0))[0]
        return len(rows) >= 2 and len(cols) >= min(2, inner.shape[1]) and not inner[0].all()

    def _u_Я(self):
        r = self._cap_src("R")
        return self._bm(r.a[:, ::-1].copy()) if r is not None else None

    def _u_Ь(self):
        # чаша P переносится вниз: её верх — на уровне перекладины H, низ — на базовой линии
        p = self._cap_src("P")
        if p is None or p.a[:, :self.s].mean() < 0.7:
            return None
        bowl_rows = [r for r in range(p.h) if p.a[r, self.s:].any()]
        if not bowl_rows or bowl_rows[0] != 0:
            return None
        bowl = p.a[:bowl_rows[-1] + 1].copy()
        rows = self.cap - self.bar[0]
        if rows < 3:
            rows = min(self.cap, 3)
        a = self._new(p.w)
        a[:, :self.s] = p.a[:, :self.s]
        a[self.cap - rows:, :] |= fit_rows(bowl, rows)
        return self._bm(a)

    def _u_Б(self):
        soft = self._u_Ь()
        if soft is None:
            return None
        a = soft.a.copy()
        a[:self.b, :] = True
        return self._bm(a)

    def _u_Ъ(self):
        soft = self._u_Ь()
        if soft is None:
            return None
        p = max(1, (self.s + 1) // 2)
        a = self._new(soft.w + p)
        a[:, p:] = soft.a
        a[:self.b, :p + self.s] = True
        return self._bm(a)

    def _u_Ы(self):
        soft = self._u_Ь()
        if soft is None:
            return None
        gap = max(1, min(self.gap, self.s // 2))
        a = self._new(soft.w + gap + self.s)
        a[:, :soft.w] = soft.a
        a[:, soft.w + gap:] = self.stem
        return self._bm(a)

    def _u_Г(self):
        l = self._cap_src("L")
        return self._bm(l.a[::-1].copy()) if l is not None else None

    def _u_П(self):
        a = self._stems()
        a[:self.b, :] = True
        return self._bm(a)

    def _u_Ц(self):
        a = self._stems()
        a[self.cap - self.b:, :] = True
        return self._bm(self._tail(a))

    def _sh(self) -> np.ndarray:
        s = self.s
        m = self._cap_src("M") or self._cap_src("W")
        width = m.w if m is not None else 3 * s + 2 * max(1, self.gap // 2)
        gap = max(1, (width - 3 * s) // 2)
        w = 3 * s + 2 * gap
        a = self._new(w)
        for x0 in (0, s + gap, w - s):
            a[:, x0:x0 + s] = self.stem
        a[self.cap - self.b:, :] = True
        return a

    def _u_Ш(self):
        return self._bm(self._sh())

    def _u_Щ(self):
        return self._bm(self._tail(self._sh()))

    def _u_Ч(self):
        a = self.H.a.copy()
        a[self.bar[1]:, :self.s] = False
        return self._bm(a)

    def _u_У(self):
        s, b, (r0, r1) = self.s, self.b, self.bar
        w = self.H.w
        a = self._new(w)
        a[:r0, :s] = True
        a[r0:r1, s:] = True
        a[:self.cap - b, w - s:] = True
        a[self.cap - b:, :w - s] = True
        return self._bm(a)

    def _u_Д(self):
        body = self._u_П().a
        if self.gap >= 2:
            body = fit_cols(body, body.shape[1] - 1)
        o = 1
        w = body.shape[1] + 2 * o
        a = np.zeros((self.cap + self.desc, w), dtype=bool)
        a[:self.cap, o:o + body.shape[1]] = body
        a[self.cap - self.b:self.cap, :] = True
        foot = max(1, (self.s + 1) // 2)
        a[self.cap:, :foot] = True
        a[self.cap:, w - foot:] = True
        return self._bm(a)

    def _u_Л(self):
        s, b = self.s, self.b
        k = 1 if s <= 2 else s // 2
        w = self.H.w + (1 if self.gap - k < 1 else 0)
        foot = max(1, b)
        a = self._new(w)
        a[:b, k:] = True
        a[:self.cap - foot, k:k + s] = True
        a[self.cap - foot:, :s] = True
        a[:, w - s:] = True
        return self._bm(a)

    def _u_Ж(self):
        k = self._cap_src("K")
        if k is None:
            return None
        ks = self.s
        if k.w <= ks + 1 or k.a[:, :ks].mean() < 0.7:
            return None
        arms = k.a[:, ks:]
        return self._bm(np.concatenate([arms[:, ::-1], k.a[:, :ks], arms], axis=1))

    def _ring(self, src: Optional[Bm], rows: int) -> np.ndarray:
        """Кольцо Ф/ф высотой ``rows``: O шрифта, а если она несимметрична (стилизованные срезы углов)
        или узка для стойки посередине — ровное кольцо со срезанными углами штрихами шрифта."""
        s, b = self.s, self.b
        need = 3 * s + 2
        if src is not None:
            a = fit_rows(src.a.copy(), rows)
            if np.array_equal(a, a[:, ::-1]) and a.shape[1] >= need:
                if (a.shape[1] - s) % 2:
                    a = fit_cols(a, a.shape[1] + 1)
                return a
            need = max(need, src.w)
        if (need - s) % 2:
            need += 1
        a = np.zeros((rows, need), dtype=bool)
        bb = min(b, max(1, rows // 3))
        a[:bb, 1:need - 1] = True
        a[rows - bb:, 1:need - 1] = True
        a[bb:rows - bb, :s] = True
        a[bb:rows - bb, need - s:] = True
        if rows <= 2 * bb:
            a[:, :] = True
        return a

    def _u_Ф(self):
        e = max(1, self.b)
        rows = self.cap - 2 * e
        if rows < 3:                                 # мелкий шрифт: стойка выходит только вверх
            rows = max(3, self.cap - e)
        ring = self._ring(self._cap_src("O"), rows)
        w = ring.shape[1]
        a = self._new(w)
        top = max(0, min(e, self.cap - rows))
        a[top:top + rows, :] = ring
        x0 = (w - self.s) // 2
        a[:, x0:x0 + self.s] = True
        return self._bm(a)

    def _u_Ю(self):
        o = self._cap_src("O")
        if o is None:
            return None
        c = 1 if self.s <= 2 else self.s // 2
        a = self._new(self.s + c + o.w)
        a[:, :self.s] = self.stem
        a[self.bar[0]:self.bar[1], self.s:self.s + c] = True
        a[:, self.s + c:] = o.a
        return self._bm(a)

    def _u_Э(self):
        c = self._cap_src("C")
        if c is None:
            return None
        a = c.a[:, ::-1].copy()
        if a.shape[1] < self.s + 2:                  # узкая C: перекладине Э нужно место
            a = fit_cols(a, self.s + 2)
        mid = a[(self.bar[0] + self.bar[1]) // 2]
        runs = _runs(mid)
        if not runs:
            return None
        right = runs[-1][0]                      # внутренний край правой дуги
        x0 = max(1, int(round(a.shape[1] / 3.0)))
        if right - x0 < 1:
            return None
        a[self.bar[0]:self.bar[1], x0:right] = True
        return self._bm(a)

    def _u_З(self):
        three = self._cap_src("3")
        return self._bm(three.a.copy()) if three is not None else None

    def _accent(self, base: Bm, dots: bool, limit: int) -> Bm:
        """Бревис над Й или точки над Ё — толщиной как перекладина, с зазором в клетку, но не выше
        ``limit`` (строк над базовой линией). Не помещается — знак встаёт вплотную к букве (если там
        ему ничего не мешает), а буква становится ниже на недостающие строки."""
        w = base.w
        hb = max(1, min(self.b, 2))
        if dots:
            d = max(1, self.s)
            if w - 2 * d < 1:                        # точки не должны слиться в черту
                d = max(1, (w - 1) // 2)
            cols = [(0, d), (w - d, w)]
        else:
            cols = [(self.s, w - self.s) if w - 2 * self.s >= 2 else (0, w)]
        gap = 1
        room = limit - base.top
        if room < hb + gap:
            free = all(not base.a[0, x0:x1].any() for x0, x1 in cols)
            gap = 0 if free else 1
            cut = hb + gap - max(0, room)
            if cut > 0:
                body = base.top - self.H.bottom      # строки над базовой линией
                if body - cut < 3:
                    cut = max(0, body - 3)
                above, below = base.a[:body], base.a[body:]
                low = fit_rows(above, body - cut)
                if dots and column_strokes(low) != column_strokes(above):
                    # Е ниже не сжать без слияния перекладин: Ё остаётся без точек (как часто пишут
                    # и в книгах) — лучше, чем неузнаваемая буква
                    return base
                base = Bm(np.concatenate([low, below], axis=0), base.top - cut)
        a = np.zeros((hb + gap + base.h, w), dtype=bool)
        a[hb + gap:] = base.a
        for x0, x1 in cols:
            a[:hb, x0:x1] = True
        return Bm(a, base.top + hb + gap)

    def _u_Й(self):
        i = self._u_И()
        return self._accent(i, False, self.accent_top_upper) if i is not None else None

    def _u_Ё(self):
        e = self._cap_src("E")
        return self._accent(e, True, self.accent_top_upper) if e is not None else None

    # --- строчные ---

    def lower(self, ch: str) -> Optional[Bm]:
        try:
            return self._lower(ch)
        except Exception:  # noqa: BLE001
            return None

    def _small(self, up: Bm) -> Bm:
        """Заглавная высотой строчных: меняется только часть над базовой линией."""
        if self.xh >= self.cap or self.xh < 3:
            return up
        body = up.top - self.H.bottom                       # строк от верха до базовой линии шрифта
        above, below = up.a[:body], up.a[body:]
        a = np.concatenate([fit_rows(above, self.xh), below], axis=0)
        return Bm(a, self.H.bottom + self.xh)

    def _lower(self, ch: str) -> Optional[Bm]:
        if ch == "б" and self.true_lower:
            six = self._cap_src("6")
            if six is not None:
                a = six.a.copy()
                xs = np.nonzero(a[0])[0]
                if len(xs):
                    a[:self.b, xs[0]:] = True
                    return self._bm(a)
        if ch == "ф" and self.true_lower:
            o = self.src.get("o")
            if o is not None and o.h == self.xh:
                oval = self._ring(o, o.h)
                w = oval.shape[1]
                bottom = self.H.bottom - self.desc
                a = np.zeros((self.H.top - bottom, w), dtype=bool)
                r0 = self.H.top - o.top
                a[r0:r0 + o.h, :] = oval
                x0 = (w - self.s) // 2
                a[:, x0:x0 + self.s] = True
                return self._bm(a)
        if ch == "ё" and self.true_lower:
            e = self.src.get("e")
            if e is not None and e.h == self.xh and e.bottom == self.H.bottom:
                return self._accent(e, True, self.accent_top_lower)
        if ch in "йё":                               # маленькая И/Е и знак над ней
            base = self._cap_src("E") if ch == "ё" else self._u_И()
            if base is None:
                return None
            return self._accent(self._small(base), ch == "ё", self.accent_top_lower)
        up_ch = ch.upper()
        latin = {"в": "B", "к": "K", "м": "M", "н": "H", "т": "T"}.get(ch)
        up = self._cap_src(latin) if latin else self.upper(up_ch)
        if up is None:
            return None
        return self._small(up)

    # --- интервал ---

    def spacing(self, upper: bool) -> Tuple[int, int]:
        """Отступ слева и справа от «чернил» (в клетках) — как у латинских букв того же регистра."""
        for c in ("HNIE" if upper or not self.true_lower else "nxuo"):
            b = self.src.get(c)
            if b is not None and c in self.adv:
                return b.left, max(0, self.adv[c] - b.left - b.w)
        return 0, 1


def kit_from_font(font, style) -> Optional[Kit]:
    """Набор для сборки русских букв из латиницы пиксельного шрифта ``font`` (sfnt.Font) или None."""
    g = style.pixel
    if not g or style.slant:
        return None
    cells: Dict[str, Bm] = {}
    adv: Dict[str, int] = {}
    for ch in SOURCES:
        gid = font.gid(ch)
        if gid is None or gid == 0:
            continue
        try:
            b = rasterize(font.contours(gid), g, style.origin)
        except Exception:  # noqa: BLE001
            b = None
        if b is None:
            continue
        cells[ch] = b
        adv[ch] = int(round(font.advances[gid] / g))
    cap = style.cap_px
    xh = style.xh_px if style.has_lower else cap
    true_lower = style.has_lower and not style.smallcaps
    return Kit.build(cells, adv, cap, xh, true_lower)


def kit_from_boxes(boxes, style) -> Optional[Kit]:
    """То же по картинкам букв атласа (TextMeshPro, растровый шрифт Unity): «чернила» каждой
    латинской буквы делятся на клетки размером ``style.pixel`` пикселей атласа, клетка берётся
    по её середине."""
    g = style.pixel
    if not g or style.slant:
        return None
    cells: Dict[str, Bm] = {}
    adv: Dict[str, int] = {}
    for ch in SOURCES:
        box = boxes.get(ch)
        mask = getattr(box, "mask", None)
        if box is None or mask is None or not mask.any():
            continue
        h, w = mask.shape
        rows, cols = max(1, int(round(h / g))), max(1, int(round(w / g)))
        ys = np.minimum(h - 1, ((np.arange(rows) + 0.5) * h / rows).astype(int))
        xs = np.minimum(w - 1, ((np.arange(cols) + 0.5) * w / cols).astype(int))
        a = mask[np.ix_(ys, xs)]
        if not a.any():
            continue
        cells[ch] = _trim(Bm(a.astype(bool), int(round(box.top / g)), int(round(box.bearing_x / g))))
        adv[ch] = int(round(box.advance / g))
    cap = style.cap_px
    xh = style.xh_px if style.has_lower else cap
    true_lower = style.has_lower and not style.smallcaps
    return Kit.build(cells, adv, cap, xh, true_lower)
