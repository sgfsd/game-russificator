"""Перевод «на месте»: английский текст стирается, на его место встаёт русский в том же стиле.

Вместо плашки поверх оригинала:
  1. фон вокруг надписи оценивается по краю её рамки, пиксели букв (всё, что заметно отличается
     от фона) закрашиваются фоном — «заливка из краёв» по пирамиде уменьшенных копий, поэтому
     и однотонная рамка диалога, и бумага, и дерево стираются без пятен;
  2. у оригинала берутся цвет букв, высота строки, число строк, выравнивание, регистр (всё
     заглавными — перевод тоже), жирность (по толщине штриха) и «пиксельность» (жёсткие края
     блоками — перевод рисуется мелким шрифтом без сглаживания и увеличивается блоками);
  3. русский текст подбирается под рамку оригинала: те же строки, при нехватке места — меньше
     шрифт или чуть уже буквы; если цвет букв близок к фону — тонкая обводка для читаемости.

Картинка каждой надписи считается один раз (при появлении перевода) и дальше только
выкладывается на своё место.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

Rect = Tuple[int, int, int, int]


@dataclass
class Item:
    rect: Rect                              # рамка всего абзаца оригинала
    lines: List[Rect]                       # рамки строк оригинала
    text: str                               # перевод
    source: str = ""                        # оригинал (регистр, число слов)
    #: наклонная надпись: центр x, y, длина, высота (всей надписи), угол (радианы)
    rot: Optional[Tuple[float, float, float, float, float]] = None
    #: повёрнутые рамки её строк (у надписи из нескольких строк)
    line_rots: Optional[List[Tuple[float, float, float, float, float]]] = None


@dataclass
class Patch:
    x: int
    y: int
    image: object                           # PIL RGBA, непрозрачная заплатка с русским текстом
    style: dict = field(default_factory=dict)


# ------------------------------------------------------------------ шрифты

def _font_file(bold: bool) -> Optional[str]:
    cands = []
    if sys.platform == "win32":
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        cands += [fonts / ("segoeuib.ttf" if bold else "segoeui.ttf"),
                  fonts / ("arialbd.ttf" if bold else "arial.ttf")]
    res = Path(__file__).resolve().parents[1] / "resources" / "fonts"
    cands += [res / ("PT_Sans-Web-Bold.ttf" if bold else "PT_Sans-Web-Regular.ttf")]
    for p in cands:
        if p.is_file():
            return str(p)
    return None


def _pixel_font_file() -> Optional[str]:
    """Для пиксельных надписей — шрифт, который без сглаживания на мелком размере остаётся чётким."""
    if sys.platform == "win32":
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        for name in ("tahomabd.ttf", "tahoma.ttf", "arialbd.ttf", "arial.ttf"):
            if (fonts / name).is_file():
                return str(fonts / name)
    return _font_file(True)


@lru_cache(maxsize=128)
def font(path: Optional[str], px: int):
    from PIL import ImageFont
    if path:
        try:
            return ImageFont.truetype(path, max(4, px))
        except OSError:
            pass
    return ImageFont.load_default()


# ------------------------------------------------------------------ анализ оригинала

def analyze(crop, inner) -> Optional[dict]:
    """Цвет фона и букв, маска букв, жирность и «пиксельность» надписи.

    ``crop`` — картинка вокруг надписи (h, w, 3, uint8), ``inner`` — маска, где сами строки."""
    import numpy as np
    h, w = crop.shape[:2]
    if h < 4 or w < 4:
        return None
    c = crop.astype(np.int32)
    ring = np.concatenate([c[:2].reshape(-1, 3), c[-2:].reshape(-1, 3), c[:, :2].reshape(-1, 3),
                           c[:, -2:].reshape(-1, 3)])
    bg = np.median(ring, axis=0)
    # надпись на бирке, кнопке, табличке размером с неё: кольцо вокруг рамки уже за краем подложки —
    # тогда фон — самый частый цвет у внутреннего края рамки строк: там поля вокруг букв (жирные
    # буквы могут занимать больше половины рамки, а к её краю не доходят)
    px = c[inner & ~_erode(inner, 2)]
    if px.size:
        q = px // 16
        code = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
        values, counts = np.unique(code, return_counts=True)
        top = int(np.argmax(counts))
        own = px[code == values[top]].mean(axis=0)
        if counts[top] >= 0.3 * len(px) and float(np.sqrt(((own - bg) ** 2).sum())) > 40:
            bg = own
            ring = px[np.sqrt(((px - own) ** 2).sum(axis=1)) < 40]
    dist = np.sqrt(((c - bg) ** 2).sum(axis=2))
    inside = dist[inner]
    if inside.size == 0:
        return None
    thr = max(38.0, _otsu(inside))
    mask = (dist > thr) & inner
    cover = float(mask.sum()) / max(1, int(inner.sum()))
    if cover < 0.02:
        return None
    core = dist >= np.percentile(dist[mask], 60)
    fg = np.median(c[mask & core], axis=0)
    # «пиксельность»: у пиксельного шрифта почти нет промежуточных оттенков между буквами и фоном
    span = max(1.0, float(np.sqrt(((fg - bg) ** 2).sum())))
    mid = ((dist > 0.25 * span) & (dist < 0.75 * span) & inner).sum() / max(1, mask.sum())
    runs = _run_lengths(mask)
    block = _block_size(runs) if mid < 0.12 else 1
    stroke = float(np.median(runs)) if runs.size else 1.0
    # стирать нужно и полупрозрачные края букв: порог — от шума самого фона, а не от яркости букв
    noise = float(np.sqrt(((ring - bg) ** 2).sum(axis=1)).std()) if len(ring) > 8 else 10.0
    noise = min(noise, 20.0)
    erase = (dist > max(16.0, min(thr * 0.45, 3.0 * noise + 14.0))) & inner
    erase &= ~_rules(erase, inner)
    fill, edge = _outline(c, mask)
    if fill is not None:
        fg = fill
    return {"bg": tuple(int(v) for v in bg), "fg": tuple(int(v) for v in fg), "mask": mask, "erase": erase,
            "cover": cover, "block": block, "stroke": stroke, "mid": float(mid), "span": span,
            "edge": tuple(int(v) for v in edge) if edge is not None else None}


def _erode(mask, r: int = 1):
    import numpy as np
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out &= np.roll(np.roll(mask, dy, axis=0), dx, axis=1)
    return out


def _outline(c, mask):
    """Буквы с обводкой (светлые с тёмным контуром и наоборот): цвет заливки и цвет контура.
    Контур — тот из двух цветов букв, что лежит по краю штрихов, заливка — внутри."""
    import numpy as np
    inner = _erode(mask, 1)
    edge = mask & ~inner
    if inner.sum() < 20 or edge.sum() < 20:
        return None, None
    lum = c[..., 0] * 0.299 + c[..., 1] * 0.587 + c[..., 2] * 0.114
    split = float(np.median(lum[mask]))
    light = lum > split
    if not (mask & light).any() or not (mask & ~light).any():
        return None, None
    a, b = np.median(c[mask & light], axis=0), np.median(c[mask & ~light], axis=0)
    if float(np.sqrt(((a - b) ** 2).sum())) < 80:
        return None, None
    edge_light = float((edge & light).sum()) / max(1, int(edge.sum()))
    in_light = float((inner & light).sum()) / max(1, int(inner.sum()))
    # обводка — только если заливка занимает заметную долю букв: у тонких штрихов со светлой
    # серединкой глаз видит цвет краёв, это и есть цвет букв
    if edge_light <= 0.35 and in_light >= 0.6 and (inner & light).sum() >= 0.35 * mask.sum():
        return a, b                     # светлые буквы, тёмный контур
    if edge_light >= 0.65 and in_light <= 0.4 and (inner & ~light).sum() >= 0.35 * mask.sum():
        return b, a                     # тёмные буквы, светлый контур
    return None, None


def _inside_frame(mask, inner):
    """Внутренность рамки вокруг надписи (кнопка, табличка) или None, если рамки нет.

    Рамка — сплошные линии у самых краёв рамки строки: сверху и снизу (строки пикселей, отмеченные
    почти на всю ширину) и по бокам (столбцы почти на всю высоту); у двойной рамки — обе линии.
    Стирается всё внутри неё (буквы подходят к рамке вплотную), новый текст — с отступом."""
    import numpy as np
    ys, xs = np.nonzero(inner)
    if not len(ys):
        return None
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
    sub = mask[y0:y1, x0:x1]
    hh, ww = sub.shape
    if hh < 12 or ww < 12:
        return None
    rows = sub.mean(axis=1) >= 0.8
    cols = sub.mean(axis=0) >= 0.8
    band_y, band_x = max(2, int(hh * 0.3)), max(2, int(hh * 0.5))

    near = max(3, int(hh * 0.15))

    def outer(flags, order):
        """Внутренний край рамки: самая внешняя сплошная линия от края и вплотную за ней (через
        несколько пикселей) — вторая линия двойной рамки."""
        edge, since = None, 0
        for i in order:
            if flags[i]:
                edge, since = i, 0
            elif edge is not None:
                since += 1
                if since > near:
                    break
        return edge
    t_edge = outer(rows, range(band_y))
    b_edge = outer(rows, range(hh - 1, hh - band_y - 1, -1))
    if t_edge is None or b_edge is None:
        return None
    l_edge = outer(cols, range(min(band_x, ww)))
    r_edge = outer(cols, range(ww - 1, max(-1, ww - band_x - 1), -1))
    t, b = t_edge + 2, b_edge - 1
    lft = (l_edge + 2) if l_edge is not None else 0
    rgt = (r_edge - 1) if r_edge is not None else ww
    if b - t < 6 or rgt - lft < 6:
        return None
    out = np.zeros_like(inner)
    out[y0 + t:y0 + b, x0 + lft:x0 + rgt] = True
    return out


def _rules(mask, inner):
    """Длинные тонкие линии в рамке строки (край кнопки, черта, рамка таблички) — это не буквы,
    их не стираем: строки и столбцы рамки, отмеченные почти на всю её длину."""
    import numpy as np
    out = np.zeros_like(mask)
    ys, xs = np.nonzero(inner)
    if not len(ys):
        return out
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    sub = mask[y0:y1, x0:x1]
    hh, ww = sub.shape
    rows = sub.sum(axis=1) >= 0.85 * ww
    cols = sub.sum(axis=0) >= 0.85 * hh
    part = out[y0:y1, x0:x1]
    part[rows, :] = sub[rows, :]
    part[:, cols] |= sub[:, cols]
    return out


def _otsu(values) -> float:
    import numpy as np
    hist, edges = np.histogram(values, bins=64)
    total = hist.sum()
    if total == 0:
        return 0.0
    centers = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)
    w1 = total - w0
    m0 = np.cumsum(hist * centers) / np.maximum(w0, 1)
    m1 = ((hist * centers).sum() - np.cumsum(hist * centers)) / np.maximum(w1, 1)
    between = w0 * w1 * (m0 - m1) ** 2
    return float(centers[int(np.argmax(between))])


def _run_lengths(mask):
    """Длины горизонтальных отрезков букв (толщина штрихов)."""
    import numpy as np
    m = mask.astype(np.int8)
    d = np.diff(np.concatenate([np.zeros((m.shape[0], 1), np.int8), m, np.zeros((m.shape[0], 1), np.int8)], axis=1),
                axis=1)
    starts = np.argwhere(d == 1)
    ends = np.argwhere(d == -1)
    if not len(starts):
        return np.zeros(0)
    return (ends[:, 1] - starts[:, 1]).astype(np.float32)


def _block_size(runs) -> int:
    """Размер «пикселя» у пиксельного шрифта: длины штрихов кратны ему."""
    import numpy as np
    if runs.size < 20:
        return 1
    best, best_score = 1, 0.0
    for b in range(2, 9):
        score = float((np.mod(runs, b) == 0).mean())
        if score >= 0.8 and score >= best_score - 0.02:
            best, best_score = b, score
    return best


# ------------------------------------------------------------------ стирание оригинала

def inpaint(img, hole):
    """Закрасить ``hole`` цветами окружения: пирамида уменьшенных копий с весами."""
    import numpy as np
    img = img.astype(np.float32)
    known = (~hole).astype(np.float32)
    levels = [(img * known[..., None], known)]
    while True:
        acc, wt = levels[-1]
        if wt.min() > 0 or min(wt.shape) <= 2:
            break
        h, w = wt.shape
        h2, w2 = (h + 1) // 2, (w + 1) // 2
        pa = np.zeros((h2 * 2, w2 * 2, 3), np.float32)
        pw = np.zeros((h2 * 2, w2 * 2), np.float32)
        pa[:h, :w], pw[:h, :w] = acc, wt
        acc2 = pa.reshape(h2, 2, w2, 2, 3).sum(axis=(1, 3))
        wt2 = pw.reshape(h2, 2, w2, 2).sum(axis=(1, 3))
        levels.append((acc2, wt2))
    acc, wt = levels[-1]
    color = acc / np.maximum(wt, 1e-6)[..., None]
    if wt.min() <= 0:                                   # вообще нет известных точек
        color[:] = (acc.sum(axis=(0, 1)) / max(1e-6, float(wt.sum())))
    for acc, wt in reversed(levels[:-1]):
        h, w = wt.shape
        up = np.repeat(np.repeat(color, 2, axis=0), 2, axis=1)[:h, :w]
        own = acc / np.maximum(wt, 1e-6)[..., None]
        blend = np.clip(wt, 0, 1)[..., None]
        color = own * blend + up * (1 - blend)
    out = np.where(hole[..., None], color, img)
    return np.clip(out, 0, 255).astype(np.uint8)


def _soften(img, hole, radius: float):
    """Сгладить залитое место (без «квадратиков» от пирамиды), не трогая остальную картинку."""
    import numpy as np
    from PIL import Image, ImageFilter
    if not hole.any():
        return img
    blurred = np.asarray(Image.fromarray(img, "RGB").filter(ImageFilter.GaussianBlur(radius)))
    return np.where(hole[..., None], blurred, img)


def _dilate(mask, r: int):
    import numpy as np
    if r <= 0:
        return mask
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy * dy + dx * dx > r * r + 1:
                continue
            out |= np.roll(np.roll(mask, dy, axis=0), dx, axis=1)
    return out


# ------------------------------------------------------------------ русский текст в рамке оригинала

def _wrap(text: str, fnt, width: float, squeeze: float) -> List[str]:
    lines: List[str] = []
    cur = ""
    for word in text.split():
        cand = f"{cur} {word}" if cur else word
        if fnt.getlength(cand) * squeeze <= width or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def ink_size(font_path: Optional[str], sample: str, ink_h: float) -> int:
    """Размер шрифта, при котором буквы такой же строки (оригинала) были бы высотой ``ink_h``:
    рамка распознавания у стилизованных шрифтов заметно больше самих букв."""
    probe = font(font_path, 100)
    sample = "".join(ch for ch in sample if not ch.isspace())[:40] or "Hg"
    try:
        box = probe.getbbox(sample)
    except Exception:  # noqa: BLE001
        return max(6, int(ink_h))
    height = max(1, box[3] - box[1])
    return max(6, int(round(ink_h * 100 / height)))


class StyleBook:
    """Размеры шрифта, уже выбранные в этой игре. Надпись того же вида (похожая высота букв,
    жирность, пиксельность) получает тот же размер — перевод соседних реплик и одной реплики на
    разных кадрах не «прыгает» по размеру из-за того, что рамка распознавания дрожит на пиксель."""

    def __init__(self) -> None:
        self.sizes: dict = {}

    def snap(self, px: int, kind: tuple) -> int:
        known = self.sizes.setdefault(kind, [])
        for k in known:
            if abs(k - px) <= max(1, round(0.12 * k)):
                return k
        known.append(px)
        return px


#: ступени уменьшения шрифта, если перевод не помещается и с переносом вниз, и с сужением букв
SHRINK = (1.0, 0.9, 0.8, 0.7, 0.6)


def layout(text: str, font_path: Optional[str], box_w: int, box_h: int, n_lines: int, line_h: float,
           start: Optional[int] = None, extra_px: int = 0):
    """Размер шрифта, сжатие по ширине и строки, чтобы перевод занял место оригинала.

    Размер — как у оригинала (``start``); не помещается — сначала лишние строки ниже, на
    свободном фоне (``extra_px`` пикселей под надписью), затем чуть уже буквы и только потом
    меньше шрифт — ступенями, чтобы у похожих надписей размеры совпадали."""
    start = start or max(6, int(round(line_h * 0.78)))
    for k in SHRINK:
        size = max(5, int(round(start * k)))
        fnt = font(font_path, size)
        step = size * 1.2
        max_lines = max(n_lines, int((box_h + extra_px + 0.25 * step) // step))
        for squeeze in (1.0, 0.92, 0.85):
            lines = _wrap(text, fnt, box_w, squeeze)
            if len(lines) <= max_lines and all(fnt.getlength(ln) * squeeze <= box_w + 1 for ln in lines):
                return size, squeeze, lines
    size = max(5, int(round(start * SHRINK[-1])))
    fnt = font(font_path, size)
    return size, 0.85, _wrap(text, fnt, box_w, 0.85)


def _fit(text: str, font_path: Optional[str], box_w: int, spare_w: int, box_h: int, n_lines: int, line_h: float,
         start: int, extra_px: int):
    """Размер, сжатие и строки перевода; если в ширину оригинала он ложится только мельче или
    сжатым — пробуем с запасом свободного фона по бокам (``spare_w``). Возвращает и ширину, в
    которую уложен текст."""
    size, squeeze, lines = layout(text, font_path, box_w, box_h, n_lines, line_h, start=start, extra_px=extra_px)
    if spare_w > 0 and (size < start or squeeze < 1.0):
        wide = layout(text, font_path, box_w + spare_w, box_h, n_lines, line_h, start=start, extra_px=extra_px)
        if (wide[0], wide[1]) > (size, squeeze):
            return wide[0], wide[1], wide[2], box_w + spare_w
    return size, squeeze, lines, box_w


def free_side(frame, fw: int, fh: int, x: int, y0: int, y1: int, bg, limit: int, step: int) -> int:
    """Сколько столбцов ровного фона (цвета ``bg``) слева (``step`` = -1) или справа (1) от ``x``."""
    import numpy as np
    if limit <= 0:
        return 0
    arr = np.frombuffer(frame, dtype=np.uint8, count=fw * fh * 4).reshape(fh, fw, 4)
    a, b = (max(0, x - limit), x) if step < 0 else (x, min(fw, x + limit))
    if b <= a or y1 <= y0:
        return 0
    cols = arr[y0:y1, a:b, 2::-1].astype(np.int32)
    ok = (np.sqrt(((cols - np.array(bg)) ** 2).sum(axis=2)) < 40).mean(axis=0) >= 0.97
    if step < 0:
        ok = ok[::-1]
    bad = np.flatnonzero(~ok)
    return int(bad[0]) if len(bad) else len(ok)


def free_below(frame, fw: int, fh: int, x0: int, x1: int, y: int, bg, limit: int) -> int:
    """Сколько пикселей ровного фона (цвета ``bg``) под надписью — туда может продолжиться перевод,
    если он длиннее оригинала (окно диалога обычно выше текста)."""
    import numpy as np
    if limit <= 0 or y >= fh:
        return 0
    arr = np.frombuffer(frame, dtype=np.uint8, count=fw * fh * 4).reshape(fh, fw, 4)
    rows = arr[y:min(fh, y + limit), max(0, x0):min(fw, x1), 2::-1].astype(np.int32)
    if rows.size == 0:
        return 0
    dist = np.sqrt(((rows - np.array(bg)) ** 2).sum(axis=2))
    ok = (dist < 40).mean(axis=1) >= 0.97
    bad = np.flatnonzero(~ok)
    return int(bad[0]) if len(bad) else len(ok)


def render_item(frame, fw: int, fh: int, item: Item, book: Optional[StyleBook] = None) -> Optional[Patch]:
    """Заплатка для одной надписи: стёртый оригинал + русский текст в его стиле (или None —
    стиль не определить, тогда рисуется обычная плашка). ``book`` — размеры шрифта, уже
    выбранные в этой игре (похожие надписи — одним размером)."""
    import numpy as np
    from PIL import Image, ImageDraw
    if item.rot is not None:
        return _render_tilted(frame, fw, fh, item, book)
    x, y, w, h = item.rect
    lh = sum(r[3] for r in item.lines) / max(1, len(item.lines)) if item.lines else h
    pad = max(2, int(round(lh * 0.22)))
    x0, y0, x1, y1 = max(0, x - pad), max(0, y - pad), min(fw, x + w + pad), min(fh, y + h + pad)
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None
    arr = np.frombuffer(frame, dtype=np.uint8, count=fw * fh * 4).reshape(fh, fw, 4)
    crop = np.ascontiguousarray(arr[y0:y1, x0:x1, 2::-1])
    inner = np.zeros(crop.shape[:2], bool)
    # рамка распознавания бывает уже букв (срезает край «O» — оставалась «(»): строка стирается с запасом
    ext = max(1, int(round(lh * 0.15)))
    for lx, ly, lw, lhh in item.lines or [item.rect]:
        inner[max(0, ly - y0):max(0, ly - y0 + lhh), max(0, lx - x0 - ext):max(0, lx - x0 + lw + ext)] = True
    st = analyze(crop, inner)
    if st is None:
        return None
    framed = _inside_frame(st["erase"] | st["mask"], inner)
    in_frame = False
    if framed is not None:
        # надпись в рамке (кнопка, табличка): рамку не трогаем, стираем и пишем внутри неё
        st2 = analyze(crop, framed)
        if st2 is not None:
            st, inner = st2, framed
            ys, xs = np.nonzero(framed)
            g = max(2, int(round((ys.max() - ys.min()) * 0.08)))    # поле между рамкой и новым текстом
            x, y = x0 + int(xs.min()) + g, y0 + int(ys.min()) + g
            w, h = max(4, int(xs.max() - xs.min() + 1) - 2 * g), max(4, int(ys.max() - ys.min() + 1) - 2 * g)
            item = Item(rect=(x, y, w, h), lines=[(x, y, w, h)] if len(item.lines) <= 1 else
                        [(max(lx, x), max(ly, y), min(lx + lw, x + w) - max(lx, x), min(ly + lh_, y + h) - max(ly, y))
                         for lx, ly, lw, lh_ in item.lines], text=item.text, source=item.source)
            lh = min(lh, h)
            in_frame = True
    # стираем только внутри рамок строк: рамка кнопки или край таблички рядом остаются целыми
    # (внутри найденной рамки кнопки своих черт нет — там всё, что не фон, — буквы)
    keep = np.zeros_like(inner) if in_frame else _rules(st["erase"] | st["mask"], inner)
    hole = _dilate(st["erase"], max(1, int(round(lh * 0.08))) + (st["block"] // 2 if st["block"] > 1 else 0)) & \
        (inner if in_frame else _dilate(inner, 1)) & ~keep
    clean = inpaint(crop, hole)
    # следы оригинала остались (обводка, тень, рисованная надпись) — ровно закрашиваем рамку строк
    # цветом фона (размытая заливка на пёстром фоне выглядит грязным пятном)
    whole = (inner if in_frame else _dilate(inner, 1)) & ~keep
    left = np.sqrt(((clean.astype(np.int32) - np.array(st["bg"])) ** 2).sum(axis=2))
    if ((left > max(40.0, st["span"] * 0.6)) & inner).sum() > 0.04 * max(1, int(inner.sum())):
        hole = whole
        clean = crop.copy()
        clean[hole] = np.array(st["bg"], dtype=np.uint8)
        clean = _soften(clean, _dilate(hole, 2) & ~_erode(hole, 2), 1.5)
    else:
        clean = _soften(clean, hole, max(1.0, lh * 0.12))
    img = Image.fromarray(clean, "RGB").convert("RGBA")

    source = item.source or ""
    letters = [c for c in source if c.isalpha()]
    text = item.text.strip()
    if letters and all(c.isupper() for c in letters):
        text = text.upper()
    # высота самих букв оригинала (без полей рамки распознавания)
    inks = []
    for lx, ly, lw, lhh in item.lines or [item.rect]:
        sub = st["mask"][max(0, ly - y0):max(0, ly - y0 + lhh), max(0, lx - x0):max(0, lx - x0 + lw)]
        if sub.size == 0:
            continue
        rows = np.flatnonzero(sub.sum(axis=1) >= max(1, 0.02 * sub.shape[1]))
        if len(rows):
            inks.append(rows[-1] - rows[0] + 1)
    ink_h = float(np.median(inks)) if inks else lh * 0.7
    bold = st["stroke"] >= max(2.0, ink_h * 0.13)
    block = st["block"]
    # внутри рамки кнопки — строго её ширина (иначе буквы залезут на рамку)
    box_w = max(8, w - 2) if in_frame else max(8, min(x1 - x0 - 2, int(w * 1.08)))
    box_h = max(8, h)
    n = max(1, len(item.lines))
    fg = st["fg"] + (255,)
    lum = lambda c: 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]  # noqa: E731
    outline = st["edge"] + (255,) if st["edge"] is not None else None
    if outline is None and abs(lum(st["fg"]) - lum(st["bg"])) < 35:
        outline = (0, 0, 0, 200) if lum(st["fg"]) > 110 else (255, 255, 255, 200)

    centered = _centered(item, n)
    # вокруг надписи ровный фон (окно диалога шире и выше текста, кнопка шире слова) — длинный
    # русский перевод займёт его, а не уменьшит шрифт: ниже — новые строки, по бокам — шире строка
    free = 0 if in_frame else free_below(frame, fw, fh, x0, x1, y1, st["bg"], int(lh * 3))
    below = (y1 - (y + h)) + free
    side_l = side_r = 0
    if not in_frame:
        cap = int(max(lh * 2, w * 0.6))
        fl = free_side(frame, fw, fh, x0, y0, y1, st["bg"], cap, -1)
        fr = free_side(frame, fw, fh, x1, y0, y1, st["bg"], cap, 1)
        side_l, side_r = (min(fl, fr), min(fl, fr)) if centered else (0, fr)
    if block > 1:
        # пиксельный шрифт: рисуем мелко без сглаживания и увеличиваем «пикселями» оригинала
        small_h, small_lh = max(4, box_h // block), max(4.0, lh / block)
        fp = _pixel_font_file()
        start = ink_size(fp, source, ink_h / block)
        if book is not None:
            start = book.snap(start, ("pixel", block))
        size, squeeze, lines, used_w = _fit(text, fp, max(4, box_w // block), (side_l + side_r) // block, small_h,
                                            n, small_lh, start, below // block)
        layer = _draw_lines(lines, fp, size, squeeze, used_w, small_h, fg, None, centered, antialias=False)
        layer = layer.resize((layer.width * block, layer.height * block), Image.NEAREST)
    else:
        fp = _font_file(bold)
        start = ink_size(fp, source, ink_h)
        if book is not None:
            start = book.snap(start, ("bold" if bold else "regular",))
        size, squeeze, lines, used_w = _fit(text, fp, box_w, side_l + side_r, box_h, n, lh, start, below)
        layer = _draw_lines(lines, fp, size, squeeze, used_w, box_h, fg, outline, centered, antialias=True)
    tx = (x - x0) + (w - layer.width) // 2 if centered else (x - x0)
    if layer.height <= h:
        ty = (y - y0) + (h - layer.height) // 2         # в строках оригинала, по центру
    else:
        ty = y - y0                                    # длиннее оригинала — от его верхней строки вниз
    # перевод шире или выше стёртого места — заплатка растёт на свободный фон вокруг (как он есть в кадре)
    grow_l = min(side_l, max(0, -tx))
    grow_r = min(side_r, max(0, tx + layer.width - img.width))
    grow_b = min(free, max(0, ty + layer.height - img.height))
    if grow_l or grow_r or grow_b:
        around = arr[y0:y1 + grow_b, x0 - grow_l:x1 + grow_r, 2::-1]
        canvas = Image.fromarray(np.ascontiguousarray(around), "RGB").convert("RGBA")
        canvas.paste(img, (grow_l, 0))
        img, x0, tx = canvas, x0 - grow_l, tx + grow_l
    if layer.width > img.width or layer.height > img.height:
        layer = layer.crop((0, 0, min(layer.width, img.width), min(layer.height, img.height)))
    tx = max(0, min(img.width - layer.width, tx))
    ty = max(0, min(img.height - layer.height, ty))
    img.alpha_composite(layer, (tx, ty))
    return Patch(x0, y0, img, {"block": block, "bold": bold, "fg": st["fg"], "bg": st["bg"], "size": size,
                               "centered": centered, "ink": round(ink_h, 1), "outline": st["edge"]})


def _render_tilted(frame, fw: int, fh: int, item: Item, book: Optional[StyleBook] = None) -> Optional[Patch]:
    """Наклонная надпись (бирка с именем, табличка, надпись на предмете): строка поворачивается
    горизонтально, стирается и переписывается как обычная, а заплатка поворачивается обратно —
    русский текст ложится под тем же углом и только в пределах самой надписи."""
    import math
    import numpy as np
    from PIL import Image, ImageFilter
    cx, cy, length, thick, ang = item.rot
    pad = max(2, int(round(thick * 0.12)))
    sw, sh = int(math.ceil(length)) + 2 * pad, int(math.ceil(thick)) + 2 * pad
    ca, sa = abs(math.cos(ang)), abs(math.sin(ang))
    hx, hy = (sw * ca + sh * sa) / 2 + 2, (sw * sa + sh * ca) / 2 + 2
    x0, y0 = max(0, int(cx - hx)), max(0, int(cy - hy))
    x1, y1 = min(fw, int(math.ceil(cx + hx))), min(fh, int(math.ceil(cy + hy)))
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None
    arr = np.frombuffer(frame, dtype=np.uint8, count=fw * fh * 4).reshape(fh, fw, 4)
    region = Image.fromarray(np.ascontiguousarray(arr[y0:y1, x0:x1, 2::-1]), "RGB")
    c, n = math.cos(ang), math.sin(ang)
    lx, ly = cx - x0, cy - y0
    data = (c, -n, lx - sw / 2 * c + sh / 2 * n, n, c, ly - sw / 2 * n - sh / 2 * c)
    straight = np.asarray(region.transform((sw, sh), Image.AFFINE, data, resample=Image.BICUBIC))
    bgra = np.concatenate([straight[..., ::-1], np.full((sh, sw, 1), 255, np.uint8)], axis=2).tobytes()
    body = (pad, pad, sw - 2 * pad, sh - 2 * pad)
    rows = [body]
    if item.line_rots and len(item.line_rots) > 1:
        rows = []
        for lcx, lcy, ll, lt, _ in item.line_rots:
            du, dv = (lcx - cx) * c + (lcy - cy) * n, -(lcx - cx) * n + (lcy - cy) * c
            rows.append((int(round(sw / 2 + du - ll / 2)), int(round(sh / 2 + dv - lt / 2)), int(ll), int(lt)))
    flat = render_item(bgra, sw, sh, Item(rect=body, lines=rows, text=item.text, source=item.source), book)
    if flat is None:
        return None
    # в заплатку идёт только изменённое (стёртые буквы и новый текст) — остальное остаётся
    # исходной картинкой, а не её повёрнутой туда-обратно копией
    got = np.asarray(flat.image.convert("RGB")).astype(np.int32)
    ph, pw = got.shape[:2]
    was = straight[flat.y:flat.y + ph, flat.x:flat.x + pw].astype(np.int32)
    changed = np.abs(got - was).max(axis=2) > 10
    alpha = Image.fromarray((_dilate(changed, 2) * 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(1))
    piece = flat.image.convert("RGB").convert("RGBA")
    piece.putalpha(alpha)
    canvas = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    canvas.paste(piece, (flat.x, flat.y))
    turned = canvas.convert("RGBa").rotate(-math.degrees(ang), resample=Image.BICUBIC, expand=True).convert("RGBA")
    px, py = int(round(cx - turned.width / 2)), int(round(cy - turned.height / 2))
    # за край кадра заплатка не выходит
    l, t = max(0, -px), max(0, -py)
    r, b = min(turned.width, fw - px), min(turned.height, fh - py)
    if r - l < 2 or b - t < 2:
        return None
    turned = turned.crop((l, t, r, b))
    style = dict(flat.style, angle=round(math.degrees(ang), 1))
    return Patch(px + l, py + t, turned, style)


def _centered(item: Item, n: int) -> bool:
    words = len((item.source or item.text).split())
    if n == 1:
        return words <= 4
    lefts = [r[0] for r in item.lines]
    centers = [r[0] + r[2] / 2 for r in item.lines]
    lh = sum(r[3] for r in item.lines) / n
    return (max(lefts) - min(lefts)) > lh * 0.6 and (max(centers) - min(centers)) < lh * 0.6


def _draw_lines(lines: Sequence[str], font_path: Optional[str], size: int, squeeze: float, box_w: int, box_h: int,
                color, outline, centered: bool, antialias: bool):
    from PIL import Image, ImageDraw
    fnt = font(font_path, size)
    step = int(round(size * 1.2))
    widths = [fnt.getlength(ln) for ln in lines]
    raw_w = int(max(widths) + 4) if widths else 4
    raw_h = max(step * len(lines) + 2, size + 4)
    layer = Image.new("RGBA", (raw_w, raw_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if not antialias:
        d.fontmode = "1"
    for i, ln in enumerate(lines):
        lx = (raw_w - widths[i]) / 2 if centered else 1
        if outline:
            d.text((lx, i * step), ln, font=fnt, fill=color, stroke_width=max(1, size // 14), stroke_fill=outline)
        else:
            d.text((lx, i * step), ln, font=fnt, fill=color)
    bbox = layer.getbbox()
    if bbox:
        top = bbox[1]
        layer = layer.crop((0, top, raw_w, min(raw_h, bbox[3] + 1)))
    if squeeze < 0.999:
        layer = layer.resize((max(1, int(layer.width * squeeze)), layer.height),
                             Image.LANCZOS if antialias else Image.NEAREST)
    return layer


def compose(canvas: Tuple[int, int], patches: Sequence[Patch]):
    """Все заплатки на прозрачной картинке размера кадра."""
    from PIL import Image
    img = Image.new("RGBA", canvas, (0, 0, 0, 0))
    for p in patches:
        img.alpha_composite(p.image, (p.x, p.y))
    return img
