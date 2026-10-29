"""Русские буквы в шрифте игры — того же размера, толщины, наклона и вида, что и родные.

Когда в шрифте игры нет кириллицы, Unity (и любой движок на FreeType) берёт недостающие
буквы из системного шрифта того же кегля. Но «кегль» у шрифтов разный: у пиксельного шрифта
заглавная занимает половину кегля, у Arial — 0,72, у узкого Oswald — 0,8. Поэтому русский
текст выходил в 1,5–2 раза крупнее английского и наезжал строками друг на друга.

Здесь русские буквы дописываются прямо в шрифт игры, по меркам, снятым с самого шрифта:

  * высота заглавных (по H, I, E…) и строчных (по x, z, v…), средняя ширина букв —
    русские заглавные и строчные выходят ровно той же высоты и той же «плотности»;
  * толщина основного и поперечного штрихов (по H) — буквы утолщаются или утончаются
    сдвигом контура, как синтетический полужирный FreeType;
  * наклон (курсив) — сдвигом по горизонтали;
  * моноширинность — каждая буква встаёт в ту же клетку;
  * заглавные в роли строчных (шрифты «капителью» и без строчных) — строчные русские
    рисуются заглавными высоты строчных;
  * пиксельный шрифт (контуры из клеток одной сетки) — русские буквы собираются из клеток
    той же сетки: PT Sans рисуется без сглаживания (его хинтинг даёт чёткие буквы от 5 px)
    кеглем, при котором высота букв в пикселях та же, что у шрифта игры.

Буквы, совпадающие по виду с латинскими (А, В, Е, К, М, Н, О, Р, С, Т, Х, а, е, о, р, с, у,
х, Ё = Ë), берутся из самого шрифта игры — они совпадают с ним до пикселя. Остальные
рисуются из PT Sans (SIL OFL, лежит в программе).
"""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import sfnt

log = logging.getLogger("russificator.fonts.cyrillic")

RUSSIAN = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя"
EXTRA = "«»—–…№„“”’"

#: русские буквы, которые в любом шрифте рисуются как латинские
HOMO_UPPER = {"А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C",
              "Т": "T", "Х": "X", "Ё": "Ë"}
HOMO_LOWER = {"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "ё": "ë"}
#: …а в шрифте «капителью» строчные латинские — это маленькие заглавные
HOMO_SMALLCAPS = {"а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c",
                  "т": "t", "х": "x", "ё": "ë"}

LATIN_UPPER = "ABCDEFGHKLMNOPRSTUXYZ"
LATIN_LOWER = "abcdeghknopsuxyz"
CAP_REF = "HIEFTLNZ"
X_REF = "xzvwunmr"

#: в шрифте должно быть не меньше стольких латинских букв, иначе это не текстовый шрифт
#: (значки, кнопки геймпада, другая письменность) — его не трогаем
MIN_LATIN = 40


def _fonts_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "resources" / "fonts"


@lru_cache(maxsize=2)
def donor_bytes(bold: bool = False) -> bytes:
    name = "PT_Sans-Web-Bold.ttf" if bold else "PT_Sans-Web-Regular.ttf"
    return (_fonts_dir() / name).read_bytes()


@lru_cache(maxsize=2)
def _donor_font(bold: bool) -> sfnt.Font:
    return sfnt.Font(donor_bytes(bold))


# ---------- мерки шрифта ----------

@dataclass
class Style:
    """Мерки шрифта в единицах шрифта (upem)."""
    upem: float
    cap: float                      # высота заглавных
    xh: float                       # высота строчных
    smallcaps: bool                 # строчные нарисованы как маленькие заглавные (или их нет)
    stem: float                     # толщина вертикального штриха
    hbar: float                     # толщина горизонтального штриха
    slant: float                    # наклон: сдвиг по x на единицу высоты
    adv_upper: float                # средняя ширина заглавных (по LATIN_UPPER)
    adv_lower: float                # средняя ширина строчных (по LATIN_LOWER)
    mono: Optional[float] = None    # ширина клетки моноширинного шрифта
    pixel: Optional[float] = None   # размер «пикселя» пиксельного шрифта
    origin: Tuple[float, float] = (0.0, 0.0)   # сдвиг сетки пикселей
    has_lower: bool = True
    stem_lower: float = 0.0         # толщина основного штриха строчных (по «l»; 0 — как у заглавных)
    base: float = 0.0               # низ заглавных над базовой линией (у пиксельных шрифтов бывает 1 px)
    xbase: float = 0.0              # то же у строчных

    def scaled(self, k: float) -> "Style":
        """Те же мерки в другом масштабе (``upem * k``)."""
        from dataclasses import replace
        return replace(self, upem=self.upem * k, cap=self.cap * k, xh=self.xh * k, stem=self.stem * k,
                       hbar=self.hbar * k, stem_lower=self.stem_lower * k, adv_upper=self.adv_upper * k, adv_lower=self.adv_lower * k,
                       mono=self.mono * k if self.mono else None, pixel=self.pixel * k if self.pixel else None,
                       origin=(self.origin[0] * k, self.origin[1] * k), base=self.base * k, xbase=self.xbase * k)

    @property
    def cap_px(self) -> int:
        return int(round(self.cap / self.pixel)) if self.pixel else 0

    @property
    def xh_px(self) -> int:
        return int(round(self.xh / self.pixel)) if self.pixel else 0


def _median(vals: Sequence[float]) -> Optional[float]:
    vals = sorted(vals)
    return vals[len(vals) // 2] if vals else None


def _heights(font: sfnt.Font, chars: str) -> Tuple[Optional[float], float]:
    """Верх и низ букв ``chars`` (медианы): у букв без выносных низ — это базовая линия шрифта."""
    tops, bottoms = [], []
    for ch in chars:
        g = font.gid(ch)
        bb = font.bbox(g) if g is not None else None
        if bb and bb[3] > 0:
            tops.append(bb[3])
            bottoms.append(bb[1])
    top = _median(tops)
    base = _median(bottoms) or 0.0
    if top is None or abs(base) > 0.2 * top:
        base = 0.0
    return top, base


def _mean_adv(font: sfnt.Font, chars: str) -> Optional[float]:
    vals = [font.advance(c) for c in chars if font.has(c)]
    vals = [v for v in vals if v]
    return sum(vals) / len(vals) if vals else None


def _raster(data: bytes, ch: str, px: float):
    """Глиф сглаженным растром: (маска bool, x левого края, y верха относительно базовой линии)."""
    import numpy as np
    from PIL import ImageFont
    font = ImageFont.truetype(io.BytesIO(data), max(1, int(round(px))))
    l, t, r, b = font.getbbox(ch, anchor="ls")
    if r <= l or b <= t:
        return None
    from PIL import Image, ImageDraw
    im = Image.new("L", (r - l + 2, b - t + 2), 0)
    ImageDraw.Draw(im).text((1 - l, 1 - t), ch, font=font, fill=255, anchor="ls")
    return np.asarray(im) > 127, l - 1, t - 1


def _strokes(data: bytes, upem: int, cap: float, ref: str) -> Tuple[float, float, float]:
    """Толщина вертикального и горизонтального штриха и наклон — по растру буквы ``ref`` (обычно H)."""
    size_px = 160.0 * upem / max(cap, 1.0)       # кегль, при котором заглавная ≈ 160 px
    r = _raster(data, ref, size_px)
    m = mask_strokes(r[0]) if r is not None else None
    if m is None:
        return cap * 0.12, cap * 0.1, 0.0
    stem_px, hbar_px, slant, cap_px = m
    k = cap / cap_px
    return stem_px * k, hbar_px * k, slant


def _lower_stem(data: bytes, upem: float, cap: float) -> float:
    """Толщина основного штриха строчных — по «l» (у антиквы и гротесков с контрастом она тоньше, чем
    у заглавной H): ширина «чернил» посередине высоты буквы."""
    size_px = 160.0 * upem / max(cap, 1.0)
    for ch in "lij":
        r = _raster(data, ch, size_px)
        if r is None:
            continue
        w = _mid_run(r[0])
        if w:
            return w * upem / size_px
    return 0.0


def _mid_run(mask) -> int:
    """Ширина самого левого отрезка «чернил» посередине высоты картинки (основной штрих l/i)."""
    import numpy as np
    rows = np.nonzero(mask.any(axis=1))[0]
    if len(rows) < 4:
        return 0
    row = mask[int((rows[0] + rows[-1]) * 0.55)]
    xs = np.nonzero(row)[0]
    if len(xs) == 0:
        return 0
    w = 1
    while xs[0] + w < len(row) and row[xs[0] + w]:
        w += 1
    return w


def mask_strokes(mask) -> Optional[Tuple[float, float, float, int]]:
    """По картинке заглавной (H, N, I): толщина вертикального штриха, горизонтального, наклон и
    высота буквы — всё в пикселях картинки."""
    import numpy as np
    h = mask.shape[0]
    rows = np.nonzero(mask.any(axis=1))[0]
    if len(rows) < 4:
        return None
    y_top, y_bot = rows[0], rows[-1]
    cap_px = y_bot - y_top + 1

    def first_run(row):
        xs = np.nonzero(mask[row])[0]
        if len(xs) == 0:
            return None, 0
        start = xs[0]
        end = start
        while end + 1 < mask.shape[1] and mask[row, end + 1]:
            end += 1
        return start, end - start + 1

    lo = int(y_bot - 0.3 * cap_px)
    hi = int(y_bot - 0.75 * cap_px)
    s_lo, w_lo = first_run(max(0, min(h - 1, lo)))
    s_hi, w_hi = first_run(max(0, min(h - 1, hi)))
    stem_px = min(w for w in (w_lo, w_hi) if w) if (w_lo or w_hi) else cap_px * 0.12
    slant = 0.0
    if s_lo is not None and s_hi is not None and lo != hi:
        slant = (s_hi - s_lo) / float(lo - hi)
    # горизонтальный штрих: столбец посередине буквы, самый короткий отрезок «чернил»
    cols = np.nonzero(mask.any(axis=0))[0]
    mid = int((cols[0] + cols[-1]) / 2) if len(cols) else mask.shape[1] // 2
    col = mask[y_top:y_bot + 1, mid]
    runs, cur = [], 0
    for v in col:
        if v:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    hbar_px = min(runs) if runs else stem_px * 0.8
    return float(stem_px), float(hbar_px), slant, int(cap_px)


@dataclass
class GlyphBox:
    """Глиф готового атласа (TextMeshPro, растровый шрифт Unity): метрики в пикселях атласа."""
    width: float
    height: float
    bearing_x: float
    top: float                      # верх «чернил» над базовой линией
    advance: float
    mask: object = None             # bool-картинка «чернил» (сверху вниз) или None


def _norm_mask(mask):
    import numpy as np
    from PIL import Image
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    crop = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    im = Image.fromarray((crop * 255).astype("uint8")).resize((24, 24), Image.Resampling.BILINEAR)
    return np.asarray(im) > 127


def mask_grid(boxes: Dict[str, GlyphBox], cap: float) -> Optional[float]:
    """Размер «пикселя» пиксельного шрифта по картинкам букв атласа (или None).

    У пиксельного шрифта (и в его SDF-атласе после порога 0.5) все границы букв — по строкам и по
    столбцам — лежат на одной сетке; у обычного шрифта кривые дают границы где угодно. Берётся
    самая крупная сетка, которой объясняются почти все границы (95%)."""
    import numpy as np
    data = []
    for ch in "HELTIFNZAMWKXYVkxzlmn":
        b = boxes.get(ch)
        if b is None or b.mask is None:
            continue
        a = b.mask.astype(np.int8)
        xs = np.nonzero(np.diff(np.pad(a, ((0, 0), (1, 1))), axis=1))[1].astype(float)
        ys = np.nonzero(np.diff(np.pad(a, ((1, 1), (0, 0))), axis=0))[0].astype(float)
        data.append((xs, ys))
    if len(data) < 6:
        return None
    best = None
    for g10 in range(20, int(cap * 10 / 4) + 1):
        g = g10 / 10.0
        tol = max(0.75, 0.08 * g)
        hits = total = 0
        for xs, ys in data:
            for v in (xs, ys):
                if not len(v):
                    continue
                off = float(np.median(np.mod(v, g)))         # сдвиг сетки у этой буквы
                e = np.abs(((v - off + g / 2) % g) - g / 2)
                hits += int((e <= tol).sum())
                total += len(v)
        if total and hits >= 0.95 * total:
            best = g
    if best is None or not 4 <= cap / best <= 24:
        return None
    return best


def style_from_glyphs(size: float, boxes: Dict[str, GlyphBox], pixel: Optional[float] = None) -> Optional[Style]:
    """Мерки шрифта по глифам атласа (кегль ``size`` = пиксели атласа на кегль).

    ``pixel`` — размер «пикселя», если известен (растровый шрифт без полутонов); иначе он ищется по
    картинкам букв (SDF-атлас пиксельного шрифта)."""
    if sum(1 for c in LATIN_UPPER if c in boxes) < 12:
        return None

    def tb(chars):
        tops = [boxes[c].top for c in chars if c in boxes and boxes[c].height > 0]
        bots = [boxes[c].top - boxes[c].height for c in chars if c in boxes and boxes[c].height > 0]
        top, bot = _median(tops), (_median(bots) or 0.0)
        if top is None or abs(bot) > 0.2 * top:
            bot = 0.0
        return top, bot
    top, base = tb(CAP_REF)
    if not top or top - base <= 0:
        return None
    cap = top - base
    if pixel is None or pixel <= 1.0:          # растровый шрифт бывает нарисован крупнее, чем 1:1
        pixel = mask_grid(boxes, cap) or pixel
    has_lower = sum(1 for c in range(97, 123) if chr(c) in boxes) >= 20
    xtop, xbase = tb(X_REF) if has_lower else (None, base)
    xh = xtop - xbase if xtop and xtop > xbase else cap
    smallcaps = not has_lower
    if has_lower:
        same = total = 0
        for lo in "aegnrbdq":
            a, b = boxes.get(lo), boxes.get(lo.upper())
            if a is None or b is None or a.mask is None or b.mask is None:
                continue
            ma, mb = _norm_mask(a.mask), _norm_mask(b.mask)
            if ma is None or mb is None:
                continue
            total += 1
            same += (ma & mb).sum() / max(1, (ma | mb).sum()) > 0.72
        smallcaps = total >= 4 and same >= total * 0.75
    stem, hbar, slant = cap * 0.12, cap * 0.1, 0.0
    ref = next((boxes[c] for c in "HNI" if c in boxes and boxes[c].mask is not None), None)
    m = mask_strokes(ref.mask) if ref is not None else None
    if m is not None:
        k = cap / m[3]
        stem, hbar, slant = m[0] * k, m[1] * k, m[2]
    if abs(slant) < 0.04:
        slant = 0.0
    stem_lower = 0.0
    if has_lower and not smallcaps:
        lb = next((boxes[c] for c in "lij" if c in boxes and boxes[c].mask is not None), None)
        stem_lower = float(_mid_run(lb.mask)) if lb is not None else 0.0
    adv_u = [boxes[c].advance for c in LATIN_UPPER if c in boxes]
    adv_l = [boxes[c].advance for c in LATIN_LOWER if c in boxes]
    mean_u = sum(adv_u) / len(adv_u)
    mean_l = sum(adv_l) / len(adv_l) if adv_l else mean_u * 0.8
    all_adv = adv_u + adv_l
    mono = None
    if len(all_adv) >= 20 and max(all_adv) - min(all_adv) <= max(all_adv) * 0.01:
        mono = float(_median(all_adv))
    return Style(upem=size, cap=cap, xh=xh, smallcaps=smallcaps, stem=stem, hbar=hbar, slant=slant,
                 adv_upper=mean_u, adv_lower=mean_l, mono=mono, pixel=pixel, has_lower=has_lower,
                 stem_lower=stem_lower, base=base, xbase=xbase)


def box_twin_ok(boxes: Dict[str, GlyphBox], twin: str, style: Style) -> bool:
    """Латинский двойник в атласе годится (для Ë/ë — это и правда E/e с точками)."""
    b = boxes.get(twin)
    if b is None or b.height < style.cap * 0.3:
        return False
    base = {"Ë": "E", "ë": "e"}.get(twin)
    if base is None:
        return True
    b0 = boxes.get(base)
    if b0 is None:
        return False
    return (abs(b.width - b0.width) <= 0.25 * max(1.0, b0.width)
            and abs((b.top - b.height) - (b0.top - b0.height)) <= 0.08 * style.cap
            and b.top > b0.top + 0.06 * style.cap)


def _shape(data: bytes, ch: str, px: float):
    """Нормированная картинка буквы 24x24 — для сравнения формы строчной и заглавной."""
    import numpy as np
    from PIL import Image
    r = _raster(data, ch, px)
    if r is None:
        return None
    mask = r[0]
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    crop = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    im = Image.fromarray((crop * 255).astype("uint8")).resize((24, 24), Image.Resampling.BILINEAR)
    return np.asarray(im) > 127


def _is_smallcaps(data: bytes, font: sfnt.Font, cap: float) -> bool:
    """Строчные латинские нарисованы как маленькие заглавные (Bangers, VCR и т.п.)?"""
    size_px = 80.0 * font.upem / max(cap, 1.0)
    pairs = [(lo, lo.upper()) for lo in "aegnrbdq" if font.has(lo) and font.has(lo.upper())]
    if len(pairs) < 4:
        return True
    same = 0
    for lo, up in pairs:
        gl, gu = font.gid(lo), font.gid(up)
        if gl == gu:
            same += 1
            continue
        a, b = _shape(data, lo, size_px), _shape(data, up, size_px)
        if a is None or b is None:
            continue
        iou = (a & b).sum() / max(1, (a | b).sum())
        same += iou > 0.72
    return same >= len(pairs) * 0.75


def _pixel_grid(font: sfnt.Font) -> Tuple[Optional[float], Tuple[float, float]]:
    """Размер клетки пиксельного шрифта (или None): контуры латиницы — только из горизонталей и
    вертикалей, все координаты лежат на одной сетке."""
    pts: List[Tuple[float, float]] = []
    lengths: List[float] = []
    total = axis = 0
    for ch in "HEnoxlABkagswM":
        g = font.gid(ch)
        if g is None:
            continue
        for c in font.contours(g):
            if any(not on for _, _, on in c):
                return None, (0.0, 0.0)
            for i, (x, y, _) in enumerate(c):
                px_, py_, _ = c[i - 1]
                total += 1
                if abs(x - px_) < 1e-6 or abs(y - py_) < 1e-6:
                    axis += 1
                    ln = abs(x - px_) + abs(y - py_)
                    if ln > 1e-6:
                        lengths.append(ln)
                pts.append((x, y))
    if total < 20 or axis < total * 0.97 or not lengths:
        return None, (0.0, 0.0)
    lengths.sort()
    smallest = lengths[0]
    near = [v for v in lengths if v < smallest * 1.5]
    grid = sum(near) / len(near)
    if grid < 1:
        return None, (0.0, 0.0)
    ox = _median([x - grid * math.floor(x / grid + 0.5) for x, _ in pts]) or 0.0
    oy = _median([y - grid * math.floor(y / grid + 0.5) for _, y in pts]) or 0.0
    bad = sum(1 for x, y in pts
              if abs((x - ox) / grid - round((x - ox) / grid)) > 0.12
              or abs((y - oy) / grid - round((y - oy) / grid)) > 0.12)
    if bad > len(pts) * 0.03:
        return None, (0.0, 0.0)
    return grid, (ox, oy)


def measure(data: bytes, font: Optional[sfnt.Font] = None) -> Optional[Style]:
    """Снять мерки со шрифта. None — шрифт не текстовый (мало латиницы) или не читается."""
    font = font or sfnt.Font(data)
    latin = sum(1 for c in range(65, 91) if c in font.cmap) + sum(1 for c in range(97, 123) if c in font.cmap)
    if latin < MIN_LATIN // 2 or sum(1 for c in range(65, 91) if c in font.cmap) < 20:
        return None
    top, base = _heights(font, CAP_REF)
    if not top or top - base <= 0:
        return None
    cap = top - base
    has_lower = sum(1 for c in range(97, 123) if c in font.cmap) >= 20
    xtop, xbase = _heights(font, X_REF) if has_lower else (None, base)
    xh = xtop - xbase if xtop and xtop > xbase else None
    smallcaps = not has_lower or not xh or _is_smallcaps(data, font, cap)
    if not xh:
        xh, xbase = cap, base
    pixel, origin = _pixel_grid(font)
    ref = next((c for c in "HNI" if font.has(c)), "H")
    stem, hbar, slant = _strokes(data, font.upem, cap, ref)
    stem_lower = _lower_stem(data, font.upem, cap) if has_lower and not smallcaps else 0.0
    if abs(slant) < 0.04:
        slant = 0.0
    adv_u = _mean_adv(font, LATIN_UPPER) or cap * 0.7
    adv_l = _mean_adv(font, LATIN_LOWER) or adv_u * 0.8
    latin_adv = [font.advance(c) for c in LATIN_UPPER + LATIN_LOWER if font.has(c)]
    mono = None
    if len(latin_adv) >= 20 and max(latin_adv) - min(latin_adv) <= max(latin_adv) * 0.01:
        mono = float(_median(latin_adv))
    return Style(upem=font.upem, cap=cap, xh=xh, smallcaps=smallcaps, stem=stem, hbar=hbar, slant=slant,
                 adv_upper=adv_u, adv_lower=adv_l, mono=mono, pixel=pixel, origin=origin, has_lower=has_lower,
                 stem_lower=stem_lower, base=base, xbase=xbase)


@lru_cache(maxsize=2)
def donor_style(bold: bool) -> Style:
    st = measure(donor_bytes(bold), _donor_font(bold))
    assert st is not None
    return st


# ---------- утолщение контура ----------

def embolden(contours: List[sfnt.Contour], xstr: float, ystr: float) -> List[sfnt.Contour]:
    """Сдвинуть контур наружу на xstr/2 по горизонтали и ystr/2 по вертикали (как FT_Outline_EmboldenXY).

    Контуры TrueType идут по часовой стрелке: закрашено справа от направления обхода, значит
    «наружу» — левая нормаль. Отрицательная сила утончает."""
    out = []
    sx, sy = xstr / 2.0, ystr / 2.0
    for c in contours:
        n = len(c)
        if n < 3:
            out.append(list(c))
            continue
        new = []
        for i in range(n):
            x, y, on = c[i]
            # соседние точки, не совпадающие с текущей
            j = (i - 1) % n
            while j != i and abs(c[j][0] - x) < 1e-9 and abs(c[j][1] - y) < 1e-9:
                j = (j - 1) % n
            k = (i + 1) % n
            while k != i and abs(c[k][0] - x) < 1e-9 and abs(c[k][1] - y) < 1e-9:
                k = (k + 1) % n
            ax, ay = x - c[j][0], y - c[j][1]
            bx, by = c[k][0] - x, c[k][1] - y
            la, lb = math.hypot(ax, ay), math.hypot(bx, by)
            if la < 1e-9 or lb < 1e-9:
                new.append((x, y, on))
                continue
            ax, ay, bx, by = ax / la, ay / la, bx / lb, by / lb
            n1 = (-ay, ax)
            n2 = (-by, bx)
            d = 1.0 + n1[0] * n2[0] + n1[1] * n2[1]
            d = max(d, 0.35)                      # острый угол — не уводим точку слишком далеко
            mx, my = (n1[0] + n2[0]) / d, (n1[1] + n2[1]) / d
            new.append((x + mx * sx, y + my * sy, on))
        out.append(new)
    return out


# ---------- новые глифы ----------

@dataclass
class _Fit:
    """Как превратить букву донора в букву шрифта игры: масштабы, утолщение, наклон."""
    sx: float
    sy: float
    xstr: float
    ystr: float
    slant: float
    dy: float = 0.0                 # подъём над базовой линией (как у букв шрифта игры)


def _fit(target: Style, donor: Style, upper: bool) -> _Fit:
    """Подобрать масштабы и утолщение: высота букв — как у шрифта игры, средняя ширина — тоже,
    толщина штрихов — как у шрифта игры."""
    if upper:
        t_h, d_h, t_adv, d_adv = target.cap, donor.cap, target.adv_upper, donor.adv_upper
    elif target.smallcaps:
        t_h, d_h, t_adv, d_adv = target.xh, donor.cap, target.adv_lower, donor.adv_upper
    else:
        t_h, d_h, t_adv, d_adv = target.xh, donor.xh, target.adv_lower, donor.adv_lower
    t_stem, t_hbar, d_stem, d_hbar = target.stem, target.hbar, donor.stem, donor.hbar
    if not upper and not target.smallcaps and target.stem_lower and donor.stem_lower:
        # строчные — по штриху строчных; поперечные штрихи — в той же пропорции
        t_hbar *= target.stem_lower / target.stem
        d_hbar *= donor.stem_lower / donor.stem
        t_stem, d_stem = target.stem_lower, donor.stem_lower
    xstr = ystr = 0.0
    sy = t_h / d_h
    sx = sy
    for _ in range(3):
        xstr = t_stem - d_stem * sx
        xstr = max(-0.4 * d_stem * sx, min(1.5 * d_stem * sx, xstr))
        ystr = t_hbar - d_hbar * sy
        ystr = max(-0.4 * d_hbar * sy, min(1.5 * d_hbar * sy, ystr))
        sy = (t_h - ystr) / d_h
        sx = (t_adv - xstr) / d_adv
        sx = max(sy * 0.65, min(sy * 1.4, sx))
    dy = target.base if upper else target.xbase
    return _Fit(sx=sx, sy=sy, xstr=xstr, ystr=ystr, slant=target.slant, dy=dy)


def _vector_glyph(ch: str, src: str, donor: sfnt.Font, fit: _Fit, mono: Optional[float]) -> Optional[sfnt.NewGlyph]:
    g = donor.gid(src)
    if g is None:
        return None
    pts = [[(x * fit.sx, y * fit.sy, on) for x, y, on in c] for c in donor.contours(g)]
    if fit.xstr or fit.ystr:
        pts = embolden(pts, fit.xstr, fit.ystr)
        pts = [[(x + fit.xstr / 2, y + fit.ystr / 2, on) for x, y, on in c] for c in pts]
    if fit.slant:
        pts = [[(x + fit.slant * y, y, on) for x, y, on in c] for c in pts]
    if fit.dy:
        pts = [[(x, y + fit.dy, on) for x, y, on in c] for c in pts]
    advance = donor.advances[g] * fit.sx + fit.xstr
    if mono:
        xs = [p[0] for c in pts for p in c]
        if xs:
            x0, x1 = min(xs), max(xs)
            width = x1 - x0
            room = mono * 0.94
            k = room / width if width > room else 1.0
            pts = [[((x - x0) * k + (mono - width * k) / 2, y, on) for x, y, on in c] for c in pts]
        advance = mono
    return sfnt.NewGlyph(codepoint=ord(ch), advance=int(round(advance)),
                         contours=[[(int(round(x)), int(round(y)), on) for x, y, on in c] for c in pts])


# --- пиксельные шрифты ---

def _mono_bitmap(ppem: int, ch: str, bold: bool):
    """Буква PT Sans без сглаживания (с хинтингом): (bool-матрица сверху вниз, левый край, верх над
    базовой линией в пикселях, ширина шага)."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    font = _pil_font(ppem, bold)
    l, t, r, b = font.getbbox(ch, anchor="ls")
    adv = font.getlength(ch)
    if r <= l or b <= t:
        return None, 0, 0, adv
    im = Image.new("1", (r - l + 4, b - t + 4), 0)
    d = ImageDraw.Draw(im)
    d.fontmode = "1"
    d.text((2 - l, 2 - t), ch, font=font, fill=1, anchor="ls")
    arr = np.asarray(im, dtype=bool)
    ys, xs = np.nonzero(arr)
    if len(xs) == 0:
        return None, 0, 0, adv
    arr = arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    left = l - 2 + xs.min()
    top = -(t - 2 + ys.min())          # пикселей над базовой линией до верхней строки
    return arr, left, top, adv


@lru_cache(maxsize=256)
def _pil_font(ppem: int, bold: bool):
    from PIL import ImageFont
    return ImageFont.truetype(io.BytesIO(donor_bytes(bold)), ppem)


@lru_cache(maxsize=128)
def _ppem_for(height_px: int, ref: str, bold: bool) -> int:
    """Кегль PT Sans, при котором буква ``ref`` без сглаживания ровно ``height_px`` пикселей."""
    best, best_d = max(3, height_px), 99
    for ppem in range(3, max(8, height_px * 3)):
        arr, _, top, _ = _mono_bitmap(ppem, ref, bold)
        if arr is None:
            continue
        d = abs(top - height_px)
        if d < best_d or (d == best_d and ppem < best):
            best, best_d = ppem, d
        if top > height_px + 1:
            break
    return best


def _squeeze(arr, width: int):
    """Сузить пиксельную букву до ``width`` колонок: сначала убираются повторяющиеся колонки."""
    import numpy as np
    while arr.shape[1] > width:
        dup = [i for i in range(1, arr.shape[1]) if np.array_equal(arr[:, i], arr[:, i - 1])]
        if dup:
            i = dup[len(dup) // 2]
        else:
            i = arr.shape[1] // 2
        arr = np.delete(arr, i, axis=1)
    return arr


def _stretch_rows(arr, rows: int):
    """Дотянуть пиксельную букву по высоте до ``rows`` строк: повторяются строки, совпадающие с
    соседней (прямые участки штрихов), — тогда форма не ломается."""
    import numpy as np
    guard = 0
    while arr.shape[0] < rows and guard < 64:
        guard += 1
        h = arr.shape[0]
        same = [i for i in range(1, h) if np.array_equal(arr[i], arr[i - 1])]
        if same:
            i = min(same, key=lambda k: abs(k - h / 2))
        else:
            i = h // 2
        arr = np.insert(arr, i, arr[i], axis=0)
    return arr


def _stem_width(arr) -> int:
    """Типичная толщина вертикальных штрихов буквы: медиана длин отрезков строк (без длинных
    горизонталей)."""
    lengths = []
    for row in arr:
        run = 0
        for v in list(row) + [False]:
            if v:
                run += 1
            elif run:
                lengths.append(run)
                run = 0
    short = sorted(v for v in lengths if v <= 3)
    return short[len(short) // 2] if short else 1


def _grow_rows(arr):
    """Расширить каждый отрезок строки на 1 px — вправо, а если там просвет закроется, то влево;
    если некуда — отрезок остаётся (просветы между штрихами не заливаются)."""
    import numpy as np
    h, w = arr.shape
    out = np.zeros((h, w + 1), dtype=bool)
    out[:, :w] = arr
    for r in range(h):
        row = arr[r]
        x = 0
        while x < w:
            if not row[x]:
                x += 1
                continue
            x0 = x
            while x < w and row[x]:
                x += 1
            right_free = x >= w or (x + 1 >= w or not row[x + 1])
            left_free = x0 > 0 and not row[x0 - 1] and (x0 - 2 < 0 or not row[x0 - 2])
            if right_free:
                out[r, x] = True
            elif left_free:
                out[r, x0 - 1] = True
    if not out[:, w].any():
        out = out[:, :w]
    return out


def _thicken(arr, top: int, extra_x: int, extra_y: int):
    """Утолстить штрихи на extra_x/extra_y пикселей, не заливая просветы между ними."""
    for _ in range(extra_x):               # толще вертикальные штрихи
        arr = _grow_rows(arr)
    for _ in range(extra_y):               # толще горизонтальные: растёт вверх, низ остаётся на месте
        grown = _grow_rows(arr[::-1].T).T[::-1]
        if grown.shape[0] > arr.shape[0]:
            top += 1
        arr = grown
    return arr, top


def _fit_cell(src: str, ppem: int, bold: bool, room: int, top: int, below: int, extra_x: int, extra_y: int):
    """Широкая буква в узкой клетке моноширинного шрифта: берётся меньший кегль, при котором она
    влезает по ширине, и дотягивается по высоте (над базовой линией и под ней — отдельно)."""
    import numpy as np
    # сначала — с утолщением штрихов, как у шрифта игры, но не мельче 3/4 нужного кегля;
    # не влезает — без утолщения: тонкая, но читаемая буква лучше чёрной кляксы
    for ex, ey, lowest in ((extra_x, extra_y, int(ppem * 0.75)), (0, 0, 3)):
        for p in range(ppem, max(3, lowest) - 1, -1):
            arr, _, t, _ = _mono_bitmap(p, src, bold)
            if arr is None:
                return None
            arr, t = _thicken(arr, t, ex, ey)
            if arr.shape[1] > room:
                continue
            cut = max(0, min(arr.shape[0], t))
            upper_part, lower_part = arr[:cut], arr[cut:]
            upper_part = _stretch_rows(upper_part, top) if len(upper_part) else upper_part
            if below > 0 and len(lower_part):
                lower_part = _stretch_rows(lower_part, below)
            return np.concatenate([upper_part, lower_part], axis=0), max(t, top)
    return None


def _bitmap_strokes(arr) -> Tuple[int, int]:
    """Толщина вертикального и горизонтального штриха пиксельной «Н»."""
    import numpy as np
    h, w = arr.shape
    row = arr[max(0, min(h - 1, int(h * 0.75)))]
    xs = np.nonzero(row)[0]
    stem = 0
    if len(xs):
        stem = 1
        while xs[0] + stem < w and row[xs[0] + stem]:
            stem += 1
    col = arr[:, w // 2]
    runs = [len(r) for r in "".join("1" if v else "0" for v in col).split("0") if r]
    return stem, (min(runs) if runs else 1)


@lru_cache(maxsize=64)
def _pixel_weight(height: int, upper: bool, stem_px: int, hbar_px: int) -> Tuple[bool, int, int]:
    """Обычный или полужирный PT Sans и сколько пикселей добавить к штрихам, чтобы «Н» была той же
    толщины, что «H» шрифта игры. Полужирный без сглаживания сам держит просветы между штрихами,
    поэтому он лучше искусственного утолщения."""
    ref = "Н" if upper else "н"
    best = None
    for bold in (False, True):
        for ey_try in range(0, max(1, hbar_px)):
            ppem = _ppem_for(max(2, height - ey_try), ref, bold)
            arr = _mono_bitmap(ppem, ref, bold)[0]
            if arr is None:
                continue
            stem, hbar = _bitmap_strokes(arr)
            ex = max(0, stem_px - stem)
            ey = max(0, hbar_px - hbar)
            if ey != ey_try:
                continue
            cost = abs(stem_px - stem - ex) + abs(hbar_px - hbar - ey) + 0.6 * (ex + ey) + (0.1 if bold else 0)
            if best is None or cost < best[0]:
                best = (cost, bold, ex, ey)
    if best is None:
        return False, max(0, stem_px - 1), max(0, hbar_px - 1)
    return best[1], best[2], best[3]


def _pixel_glyph(ch: str, src: str, target: Style, upper: bool, track: int) -> Optional[sfnt.NewGlyph]:
    g = target.pixel
    stem_px = max(1, int(round(target.stem / g)))
    hbar_px = max(1, int(round(target.hbar / g)))
    shape_upper = upper or target.smallcaps
    full = target.cap_px if upper else target.xh_px
    bold, extra_x, extra_y = _pixel_weight(full, shape_upper, stem_px, hbar_px)
    height = full - extra_y
    ref = "Н" if shape_upper else "н"
    ppem = _ppem_for(max(2, height), ref, bold)
    arr, left, top, adv = _mono_bitmap(ppem, src, bold)
    advance_px = int(round(adv)) + track + extra_x
    if arr is not None:
        arr, top = _thicken(arr, top, extra_x, extra_y)
        # хинтинг PT Sans рисует разные буквы одного кегля по-разному (у «н» штрих 2 px, у «и» — 1 px):
        # буква тоньше нужного утолщается ещё
        for _ in range(2):
            if _stem_width(arr) >= stem_px:
                break
            arr = _grow_rows(arr)
    if target.mono:
        cell = int(round(target.mono / g))
        room = cell - 1 if cell > 4 else cell
        if arr is not None:
            if arr.shape[1] > room:
                fitted = _fit_cell(src, ppem, bold, room, top, arr.shape[0] - top, extra_x, extra_y)
                if fitted is not None:
                    arr, top = fitted
                else:
                    arr = _squeeze(arr, room)
            left = (cell - arr.shape[1]) // 2
        advance_px = cell
    ox, oy = target.origin
    lift = target.base if upper else target.xbase
    oy = oy + g * round((lift - oy) / g) if lift else oy
    contours = _cell_contours(arr, left, top, g, ox, oy) if arr is not None else []
    return sfnt.NewGlyph(codepoint=ord(ch), advance=int(round(advance_px * g)), contours=contours)


def _cell_contours(arr, left: int, top: int, g: float, ox: float, oy: float) -> List[List[Tuple[int, int, bool]]]:
    """Клетки буквы -> контуры: граница каждого куска клеток обводится одним контуром (внутренние
    просветы — отдельными, обходом в другую сторону), как у родных букв пиксельного шрифта.

    Набор отдельных квадратиков или прямоугольников рисуется так же, но хинтинг растеризатора
    (Unity — FreeType с выравниванием по пикселям) двигает края разных контуров по-разному: на
    стыках клетки съезжают, а мелкие детали (кратка над Й) пропадают. ``top`` — строк над базовой
    линией, ``left`` — клеток от начала."""
    h, w = arr.shape

    def filled(r, c):
        return 0 <= r < h and 0 <= c < w and bool(arr[r, c])

    # рёбра границы в клетках (x вправо, y вверх от базовой линии); закрашено — справа по ходу
    out_edges: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
    for r in range(h):
        for c in range(w):
            if not arr[r, c]:
                continue
            yt, yb = top - r, top - r - 1
            if not filled(r, c - 1):
                out_edges.setdefault((c, yb), []).append((c, yt))
            if not filled(r - 1, c):
                out_edges.setdefault((c, yt), []).append((c + 1, yt))
            if not filled(r, c + 1):
                out_edges.setdefault((c + 1, yt), []).append((c + 1, yb))
            if not filled(r + 1, c):
                out_edges.setdefault((c + 1, yb), []).append((c, yb))

    def turn(d_in, d_out) -> int:
        # правый поворот предпочтительнее: куски, касающиеся углами, остаются разными контурами
        cross = d_in[0] * d_out[1] - d_in[1] * d_out[0]
        return 0 if cross < 0 else (1 if cross == 0 else 2)

    loops = []
    while out_edges:
        # начало — в обычном углу, не в точке, где куски касаются углами (там два выхода)
        start = next((p for p, v in out_edges.items() if len(v) == 1), next(iter(out_edges)))
        loop = [start]
        cur = start
        prev_dir = None
        while True:
            nexts = out_edges.get(cur)
            if not nexts:
                break
            if prev_dir is not None and len(nexts) > 1:
                nexts.sort(key=lambda p: turn(prev_dir, (p[0] - cur[0], p[1] - cur[1])))
            nxt = nexts.pop(0)
            if not nexts:
                del out_edges[cur]
            prev_dir = (nxt[0] - cur[0], nxt[1] - cur[1])
            cur = nxt
            if cur == start and start not in out_edges:
                break
            loop.append(cur)
        # точки посреди прямых участков не нужны
        pts = []
        n = len(loop)
        for i in range(n):
            a, b, c = loop[i - 1], loop[i], loop[(i + 1) % n]
            if (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) != 0:
                pts.append(b)
        if len(pts) >= 3:
            loops.append(pts)
    return [[(int(round(ox + (left + x) * g)), int(round(oy + y * g)), True) for x, y in loop] for loop in loops]


def _kit_glyph(ch: str, bm, target: Style, spacing: Tuple[int, int]) -> sfnt.NewGlyph:
    """Буква, собранная из латиницы пиксельного шрифта (:mod:`pixel_cyrillic`), — в контуры шрифта."""
    g = target.pixel
    arr = bm.a
    lsb, rsb = spacing
    left, advance_px = lsb, lsb + arr.shape[1] + rsb
    if target.mono:
        cell = int(round(target.mono / g))
        room = cell - 1 if cell > 4 else cell
        if arr.shape[1] > room:                      # широкая буква (Ж, Ш, Ю) в клетке моноширинного
            from .pixel_cyrillic import fit_cols
            arr = fit_cols(arr, room)
        left = (cell - arr.shape[1]) // 2
        advance_px = cell
    ox, oy = target.origin
    return sfnt.NewGlyph(codepoint=ord(ch), advance=int(round(advance_px * g)),
                         contours=_cell_contours(arr, left, bm.top, g, ox, oy))


def _pixel_tracking(target: Style) -> int:
    """На сколько пикселей буквы шрифта игры шире букв PT Sans того же роста (межбуквенный интервал)."""
    g = target.pixel
    ppem = _ppem_for(max(2, target.cap_px), "H", False)
    pt = [_mono_bitmap(ppem, c, False)[3] for c in LATIN_UPPER]
    d = target.adv_upper / g - sum(pt) / len(pt)
    return max(-1, min(2, int(round(d))))


# ---------- главное ----------

def _twin_ok(font: sfnt.Font, style: Style, twin: str) -> bool:
    """Латинская буква-двойник в шрифте настоящая: не пустая, не заглушка-квадрат, а Ë/ë — это и
    правда E/e с точками сверху (в DOS-шрифтах коды Ë и ë ведут на псевдографику «╦» и «δ»)."""
    g = font.gid(twin)
    if g is None:
        return False
    bb = font.bbox(g)
    if bb is None or bb[3] - bb[1] < style.cap * 0.3:
        return False
    if font.num_glyphs > 1 and g != 0:
        notdef = font.raw_glyph(0)
        if notdef and font.raw_glyph(g) == notdef:
            return False
    base = {"Ë": "E", "ë": "e"}.get(twin)
    if base is None:
        return True
    gb = font.gid(base)
    bb0 = font.bbox(gb) if gb is not None else None
    if bb0 is None:
        return False
    w = max(1.0, bb0[2] - bb0[0])
    if not (abs(bb[0] - bb0[0]) <= 0.25 * w and abs(bb[2] - bb0[2]) <= 0.25 * w
            and abs(bb[1] - bb0[1]) <= 0.08 * style.cap and bb[3] > bb0[3] + 0.06 * style.cap):
        return False
    # ниже верха основы — та же буква, что и основа (у «δ» на месте «ë» — другая)
    return _same_below(font, g, gb, bb0[3])


def _same_below(font: sfnt.Font, g_acc: int, g_base: int, top: float) -> bool:
    import numpy as np
    from PIL import Image, ImageDraw
    k = 64.0 / max(1.0, top)
    size = (int(max(font.advances[g_acc], font.advances[g_base]) * k) + 8, 72)

    def draw(gid):
        im = Image.new("L", size, 0)
        d = ImageDraw.Draw(im)
        for c in font.contours(gid):
            poly = [(4 + x * k, 68 - y * k) for x, y, _ in c]
            if len(poly) >= 3:
                layer = Image.new("L", size, 0)
                ImageDraw.Draw(layer).polygon(poly, fill=255)
                im = Image.fromarray(np.asarray(im) ^ np.asarray(layer))
        return np.asarray(im)[int(68 - top * k) + 2:, :] > 127
    a, b = draw(g_acc), draw(g_base)
    union = (a | b).sum()
    return union > 0 and (a & b).sum() / union > 0.7


def fullwidth_letters(latin: Sequence[float], russian: Sequence[float], em: float) -> bool:
    """Русские буквы шрифта — полноширинные: каждая стоит в клетке иероглифа (ширина ≈ кегль, у всех
    одинаковая), а латиница обычная, пропорциональная. Так кириллицу кладут китайские и японские шрифты
    по кодировкам GB2312/GBK/JIS (FangZhengLiBian в CardCultivation: «п» шириной 256 при кегле 256, «n» —
    144). Текст из таких букв выходит вразрядку «Э т о  к а р т а» — их надо рисовать заново по меркам
    латиницы того же шрифта."""
    if len(latin) < 20 or len(russian) < 10 or em <= 0:
        return False
    lat, rus = _median(latin), _median(russian)
    return (rus >= 0.85 * em and rus >= 1.35 * lat
            and max(russian) - min(russian) <= 0.05 * max(russian))


def fullwidth_font(font: sfnt.Font) -> bool:
    """Кириллица шрифта ``font`` полноширинная (см. :func:`fullwidth_letters`)."""
    latin = [font.advance(c) for c in LATIN_UPPER + LATIN_LOWER if font.has(c)]
    russian = [font.advance(c) for c in RUSSIAN if font.has(c)]
    return fullwidth_letters([v for v in latin if v], [v for v in russian if v], font.upem)


def missing(font: sfnt.Font, need: Iterable[str]) -> List[str]:
    """Символы ``need``, которых в шрифте нет, — и буквы, которые в таблице шрифта есть, но нарисованы
    пустыми (Dimbo в The Game of Life 2: «к» и «Й» с шириной, но без контура — в игре на их месте
    дырка «Классичес ий») или полноширинными (китайский шрифт, см. :func:`fullwidth_letters`). Такие
    буквы рисуются заново, таблица шрифта указывает на новый глиф."""
    out = set()
    need = list(need)
    full = any(c in RUSSIAN for c in need) and fullwidth_font(font)
    for c in need:
        if c.isspace():
            continue
        g = font.cmap.get(ord(c))
        if g is None or (c.isalpha() and 0 <= g < font.num_glyphs and _blank(font, g)) or (full and c in RUSSIAN):
            out.add(c)
    return sorted(out)


def _blank(font: sfnt.Font, gid: int) -> bool:
    try:
        box = font.bbox(gid)
    except Exception:  # noqa: BLE001 — нечитаемый глиф: пусть остаётся как в игре
        return False
    return box is None or box[3] - box[1] <= 0 or box[2] - box[0] <= 0


def twin_of(ch: str, style: Style) -> Optional[str]:
    """Латинская буква, которой в этом шрифте рисуется русская ``ch`` (или None)."""
    if ch.islower() and not style.has_lower:
        return None
    return HOMO_UPPER.get(ch) or (HOMO_SMALLCAPS if style.smallcaps else HOMO_LOWER).get(ch)


def make_glyphs(style: Style, todo: Sequence[str], font: Optional[sfnt.Font] = None, kit=None) -> List[sfnt.NewGlyph]:
    """Глифы для символов ``todo`` по меркам ``style`` (в единицах ``style.upem``).

    ``font`` — сам шрифт игры: из него берутся буквы-двойники латиницы; без него (мерки сняты с
    атласа) все буквы строятся заново. ``kit`` — латиница пиксельного шрифта в клетках
    (:mod:`pixel_cyrillic`), если шрифта нет, а есть картинки букв атласа."""
    donors = {b: (_donor_font(b), donor_style(b)) for b in (False, True)}

    def pick(upper: bool) -> Tuple[sfnt.Font, _Fit]:
        best = None
        for b, (dfont, dstyle) in donors.items():
            fit = _fit(style, dstyle, upper)
            # чем меньше приходится утолщать/утончать, тем ближе донор по весу
            cost = abs(fit.xstr) / max(1.0, style.stem) + abs(fit.ystr) / max(1.0, style.hbar)
            if best is None or cost < best[0]:
                best = (cost, dfont, fit)
        return best[1], best[2]

    upper_fit = pick(True)
    lower_fit = pick(False)
    track = _pixel_tracking(style) if style.pixel else 0
    # пиксельный шрифт: русские буквы собираются из его же латиницы, клетка в клетку
    if kit is None and style.pixel and font is not None:
        from . import pixel_cyrillic
        kit = pixel_cyrillic.kit_from_font(font, style)
    new: List[sfnt.NewGlyph] = []
    by_char: Dict[str, sfnt.NewGlyph] = {}
    for ch in todo:
        upper = ch.isupper() or not ch.isalpha()
        # 1) такая же латинская буква в самом шрифте
        twin = twin_of(ch, style) if font is not None else None
        if twin and _twin_ok(font, style, twin):
            g = font.gid(twin)
            ng = sfnt.NewGlyph(codepoint=ord(ch), advance=font.advances[g], raw=font.raw_glyph(g))
        elif ch.islower() and not style.has_lower and ch.upper() in by_char:
            # без строчных в шрифте строчная русская = заглавная того же шрифта
            up = by_char[ch.upper()]
            ng = sfnt.NewGlyph(codepoint=ord(ch), advance=up.advance, contours=up.contours, raw=up.raw)
        else:
            # 2) строчные «капителью» — та же форма, что у заглавной
            src = ch.upper() if (ch.islower() and style.smallcaps) else ch
            if style.pixel:
                bm = None
                if kit is not None:
                    bm = kit.upper(ch) if ch.isupper() else kit.lower(ch)
                ng = (_kit_glyph(ch, bm, style, kit.spacing(upper)) if bm is not None
                      else _pixel_glyph(ch, src, style, upper, track))
            else:
                dfont, fit = upper_fit if upper else lower_fit
                ng = _vector_glyph(ch, src, dfont, fit, style.mono)
        if ng is not None:
            new.append(ng)
            by_char[ch] = ng
    return new


def plan(data: bytes, need: Iterable[str]) -> Tuple[Optional[sfnt.Font], List[sfnt.NewGlyph]]:
    """Какие глифы дописать в шрифт ``data``, чтобы в нём были все символы ``need``."""
    font = sfnt.Font(data)
    todo = missing(font, need)
    if not todo:
        return font, []
    style = measure(data, font)
    if style is None:
        return None, []
    return font, make_glyphs(style, todo, font)


def synth_font(style: Style, chars: Iterable[str], boxes: Optional[Dict[str, GlyphBox]] = None) -> bytes:
    """Шрифт (TrueType, PT Sans), в котором символы ``chars`` нарисованы по меркам ``style``.

    Для атласов TextMeshPro и растровых шрифтов, у которых нет исходного TTF: мерки снимаются с
    самого атласа, а этот шрифт рисует недостающие буквы кеглем, равным кеглю атласа. У пиксельного
    атласа (``boxes`` с картинками букв) русские буквы собираются из его латиницы, клетка в клетку."""
    st = style.scaled(1000.0 / style.upem)
    base = _donor_font(False)
    kit = None
    if style.pixel and boxes:
        from . import pixel_cyrillic
        kit = pixel_cyrillic.kit_from_boxes(boxes, style)
    new = make_glyphs(st, sorted(set(c for c in chars if not c.isspace())), kit=kit)
    return sfnt.build(base, new)


def add_cyrillic(data: bytes, need: Iterable[str]) -> Optional[bytes]:
    """Шрифт ``data`` с дописанными символами ``need`` (кириллица по меркам шрифта).

    None — дописывать нечего, шрифт не текстовый или результат не прошёл проверку
    (тогда шрифт игры остаётся как был)."""
    need = list(need)
    try:
        font, new = plan(data, need)
        if font is None or not new:
            return None
        out = sfnt.build(font, new)
        if not verify(data, out, [chr(g.codepoint) for g in new]):
            return None
        return out
    except sfnt.FontError as exc:
        log.info("шрифт не дополнен: %s", exc)
    except Exception as exc:  # noqa: BLE001 — необычный шрифт не должен ломать русификацию
        log.warning("шрифт не дополнен (%s): %s", type(exc).__name__, exc)
    return None


def verify(old: bytes, new: bytes, added: Sequence[str]) -> bool:
    """Собранный шрифт открывается FreeType, новые буквы рисуются, латиница не изменилась."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    try:
        f_new = ImageFont.truetype(io.BytesIO(new), 48)
        f_old = ImageFont.truetype(io.BytesIO(old), 48)
    except OSError as exc:
        log.warning("собранный шрифт не открывается: %s", exc)
        return False
    letters = [c for c in added if c.isalpha()]
    for ch in letters:
        bb = f_new.getbbox(ch, anchor="ls")
        if bb[2] <= bb[0] or bb[3] <= bb[1]:
            log.warning("собранный шрифт: буква %s пустая", ch)
            return False
    # латиница та же: TrueType — пиксель в пиксель; CFF, переведённый в TrueType, хинтуется иначе
    # (сдвиги на полпикселя), поэтому у него сравнивается форма каждой буквы отдельно
    sample = "Hamburgefonts0123"
    if old[:4] != b"OTTO" and b"CFF " not in _table_tags(old):
        def draw(font):
            im = Image.new("L", (900, 80), 0)
            ImageDraw.Draw(im).text((4, 8), sample, font=font, fill=255)
            return np.asarray(im) > 127
        a, b = draw(f_old), draw(f_new)
        iou = (a & b).sum() / max(1, (a | b).sum())
    else:
        scores = []
        for ch in sample:
            a, b = _shape(old, ch, 96), _shape(new, ch, 96)
            if a is None or b is None:
                if (a is None) != (b is None):
                    scores.append(0.0)
                continue
            scores.append((a & b).sum() / max(1, (a | b).sum()))
        iou = sum(scores) / len(scores) if scores else 0.0
        iou = 1.0 if iou >= 0.8 else iou
    if iou < 0.98:
        log.warning("собранный шрифт рисует латиницу иначе (IoU %.2f)", iou)
        return False
    return True


def _table_tags(data: bytes) -> List[bytes]:
    import struct
    try:
        off = struct.unpack_from(">I", data, 12)[0] if data[:4] == b"ttcf" else 0
        n = struct.unpack_from(">H", data, off + 4)[0]
        return [data[off + 12 + 16 * i:off + 16 + 16 * i] for i in range(n)]
    except struct.error:
        return []


def _typographic(ch: str) -> bool:
    """Символ, который может понадобиться русскому тексту: кириллица, латиница с диакритикой, греческие,
    знаки препинания, стрелки, математика, рамки, значки. Иероглифы, арабское письмо, тайский и т.п.
    встречаются только в непереведённых строках других языков игры — шрифт им не дописывается."""
    c = ord(ch)
    return 127 < c < 0x590 or 0x1E00 <= c < 0x2C00


def needed_chars(translations: Iterable[str]) -> Set[str]:
    """Весь русский алфавит + знаки русской типографики + все такие же символы из перевода."""
    need = set(RUSSIAN) | set(EXTRA)
    for t in translations:
        need.update(ch for ch in t if _typographic(ch))
    return need
