"""Кириллица в шрифтах Unity-игры — того же размера и вида, что родной текст.

Когда в шрифте игры нет русских букв, Unity подставляет их из системного Arial того же кегля, а
TextMeshPro — из запасного шрифта; у таких букв другой рост и вес, поэтому русский текст выходил
в полтора-два раза крупнее английского и наезжал строками. Здесь буквы дописываются в сами шрифты
игры по меркам каждого шрифта (``fonts/cyrillic.py``):

  * TTF/OTF внутри ассета ``Font`` (UI Text, TextMesh, IMGUI, исходники динамических шрифтов
    TextMeshPro) — буквы встраиваются прямо в шрифт;
  * атлас TextMeshPro (любой версии TMP: 1.x, 2.x, 3.x) — мерки снимаются с глифов атласа
    (высота заглавных и строчных, ширина, толщина штриха, капитель), русские буквы, совпадающие
    с латинскими, ссылаются на готовые глифы, остальные рисуются в SDF — точно в том же
    кодировании, что у TMP (0.5 на контуре, наклон 1/(2·GradientScale), обрезка на расстоянии
    Padding) — кеглем атласа; если исходный TTF шрифта есть в игре, буквы рисуются им самим;
  * растровый шрифт (``Font`` с готовой текстурой букв) — так же, у пиксельных — без полутонов.

Новые глифы ложатся в свободную часть атласа (если места нет — атлас увеличивается вдвое, старые
глифы остаются на своих координатах); результат проверяется, перечитав файл заново. Изменённые
файлы ассетов попадают в бэкап — откат возвращает оригиналы.
"""

from __future__ import annotations

import copy
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import unitypy_io

log = logging.getLogger("russificator.unity.tmp")

from ...fonts.cyrillic import EXTRA, RUSSIAN, needed_chars  # noqa: F401,E402 — общие для всех движков


@dataclass
class RenderedGlyph:
    char: str
    width: float        # размер «чернил» в пикселях атласа
    height: float
    bearing_x: float    # от точки пера до левого края
    bearing_y: float    # от базовой линии до верхнего края
    advance: float
    bitmap: "object"    # np.ndarray float32 (h + 2p, w + 2p), значения 0..1


@dataclass
class FontReport:
    file: str
    name: str
    added: int = 0
    error: str = ""
    missing_after: List[str] = field(default_factory=list)


# ---------- рендер SDF ----------

def _edt_sq(f, radius: int):
    """Квадрат евклидова расстояния до ближайшего нуля f (точно в пределах radius)."""
    import numpy as np
    out = f.copy()
    for s in range(1, radius + 1):
        s2 = float(s * s)
        np.minimum(out[s:], f[:-s] + s2, out=out[s:])
        np.minimum(out[:-s], f[s:] + s2, out=out[:-s])
    g = out
    out = g.copy()
    for s in range(1, radius + 1):
        s2 = float(s * s)
        np.minimum(out[:, s:], g[:, :-s] + s2, out=out[:, s:])
        np.minimum(out[:, :-s], g[:, s:] + s2, out=out[:, :-s])
    return out


class GlyphRenderer:
    """Отрисовка символов шрифта (файл или байты TTF) в растр/SDF для атласа."""

    def __init__(self, font):
        self.font_path = Path(font) if not isinstance(font, (bytes, bytearray)) else None
        self.font_data = bytes(font) if isinstance(font, (bytes, bytearray)) else None
        self._cache: Dict[Tuple, RenderedGlyph] = {}
        self._cap_ratio: Optional[float] = None

    def _pil(self, size: int):
        import io
        from PIL import ImageFont
        src = io.BytesIO(self.font_data) if self.font_data is not None else str(self.font_path)
        return ImageFont.truetype(src, size=max(1, size))

    def cap_ratio(self) -> float:
        """Высота заглавной «Н» в долях кегля (для подгонки под оригинальный шрифт)."""
        if self._cap_ratio is None:
            f = self._pil(200)
            l, t, r, b = f.getbbox("Н", anchor="ls")
            self._cap_ratio = (b - t) / 200.0
        return self._cap_ratio

    def mean_advance(self, chars: str, size: float) -> float:
        f = self._pil(round(size))
        return sum(f.getlength(c) for c in chars) / max(1, len(chars))

    def render(self, char: str, size: float, padding: int, gradient: float, sdf: bool = True,
               xscale: float = 1.0) -> RenderedGlyph:
        """Отрисовать символ. ``xscale`` — сжатие по ширине под узкие/широкие шрифты игры."""
        key = (char, round(size, 2), padding, round(gradient, 3), sdf, round(xscale, 3))
        if key in self._cache:
            return self._cache[key]
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
        ss = 4 if size < 64 else 2 if size < 160 else 1
        font = self._pil(round(size * ss))
        l, t, r, b = font.getbbox(char, anchor="ls")
        advance = font.getlength(char) / ss * xscale
        if r <= l or b <= t:  # пробельный символ
            g = RenderedGlyph(char, 0.0, 0.0, 0.0, 0.0, advance, np.zeros((0, 0), np.float32))
            self._cache[key] = g
            return g
        ink = Image.new("L", (r - l, b - t), 0)
        ImageDraw.Draw(ink).text((-l, -t), char, font=font, fill=255, anchor="ls")
        if abs(xscale - 1.0) > 0.01:
            ink = ink.resize((max(1, round((r - l) * xscale)), b - t), Image.Resampling.LANCZOS)
        w_out = math.ceil(ink.width / ss)
        h_out = math.ceil(ink.height / ss)
        margin = (padding + 1) * ss
        W = w_out * ss + 2 * margin
        H = h_out * ss + 2 * margin
        img = Image.new("L", (W, H), 0)
        img.paste(ink, (margin, margin))
        cov = np.asarray(img, dtype=np.float32) / 255.0
        # выход: (h_out + 2p) x (w_out + 2p) пикселей; центр пикселя -> точка в hi-res
        oy = margin - padding * ss
        ys = oy + np.arange(h_out + 2 * padding) * ss + (ss - 1) / 2.0
        xs = oy + np.arange(w_out + 2 * padding) * ss + (ss - 1) / 2.0
        if sdf:
            inside = cov >= 0.5
            big = np.float32(1e6)
            radius = (padding + 2) * ss
            d_out = np.sqrt(_edt_sq(np.where(inside, 0, big).astype(np.float32), radius))
            d_in = np.sqrt(_edt_sq(np.where(inside, big, 0).astype(np.float32), radius))
            signed = np.where(inside, d_in - 0.5, -(d_out - 0.5)) / ss
            yi = np.clip(np.round(ys).astype(int), 0, H - 1)
            xi = np.clip(np.round(xs).astype(int), 0, W - 1)
            d = signed[np.ix_(yi, xi)]
            val = 0.5 + d / (2.0 * gradient)
            val[d < -padding] = 0.0  # как у TMP: дальше Padding от контура — ноль
            bitmap = np.clip(val, 0.0, 1.0).astype(np.float32)
        else:
            small = img.resize((W // ss, H // ss), Image.Resampling.BOX)
            arr = np.asarray(small, dtype=np.float32) / 255.0
            off = (margin - padding * ss) // ss
            bitmap = arr[off:off + h_out + 2 * padding, off:off + w_out + 2 * padding]
        g = RenderedGlyph(char, ink.width / ss, (b - t) / ss, l / ss * xscale, -t / ss, advance, bitmap)
        self._cache[key] = g
        return g


# ---------- модель TMP-шрифта (две версии формата) ----------

class TmpFont:
    """Обёртка над typetree TMP_FontAsset: TMP 1.x (m_glyphInfoList) и 2.x+ (m_GlyphTable)."""

    def __init__(self, tree: dict):
        self.tree = tree
        # шрифт, обновлённый из TMP 1.x в 1.4+/2.x, хранит и старый (пустой) m_glyphInfoList, и новые
        # таблицы — TMP читает новые, туда и надо дописывать
        self.v1 = "m_glyphInfoList" in tree and (bool(tree["m_glyphInfoList"]) or "m_GlyphTable" not in tree)

    @staticmethod
    def match(tree: dict) -> bool:
        return isinstance(tree, dict) and ("m_glyphInfoList" in tree or
                                           ("m_GlyphTable" in tree and "m_CharacterTable" in tree))

    @property
    def name(self) -> str:
        return str(self.tree.get("m_Name", "TMP font"))

    def chars(self) -> Set[int]:
        if self.v1:
            return {int(g["id"]) for g in self.tree["m_glyphInfoList"]}
        return {int(c["m_Unicode"]) for c in self.tree["m_CharacterTable"]}

    def is_dynamic(self) -> bool:
        return not self.v1 and int(self.tree.get("m_AtlasPopulationMode", 0)) == 1

    def is_sdf(self) -> bool:
        if self.v1:
            return int(self.tree.get("fontAssetType", 1)) != 2  # FontAssetTypes: None=0, SDF=1, Bitmap=2
        mode = int(self.tree.get("m_AtlasRenderMode", 4165))
        return mode >= 4096  # GlyphRenderMode: SDF* = 0x1000+

    def padding(self) -> int:
        if self.v1:
            return int(round(self.tree["m_fontInfo"].get("Padding", 5)))
        return int(self.tree.get("m_AtlasPadding", 5))

    def atlas_size(self) -> Tuple[int, int]:
        if self.v1:
            fi = self.tree["m_fontInfo"]
            return int(fi["AtlasWidth"]), int(fi["AtlasHeight"])
        return int(self.tree["m_AtlasWidth"]), int(self.tree["m_AtlasHeight"])

    def set_atlas_size(self, w: int, h: int) -> None:
        if self.v1:
            self.tree["m_fontInfo"]["AtlasWidth"] = float(w)
            self.tree["m_fontInfo"]["AtlasHeight"] = float(h)
        else:
            self.tree["m_AtlasWidth"] = w
            self.tree["m_AtlasHeight"] = h

    def atlas_pptr(self) -> Optional[dict]:
        if self.v1:
            return self.tree.get("atlas")
        textures = self.tree.get("m_AtlasTextures") or []
        return textures[0] if textures else self.tree.get("atlas")

    def material_pptr(self) -> Optional[dict]:
        return self.tree.get("material") or self.tree.get("m_Material")

    def cap_height(self) -> float:
        """Высота заглавной H в пикселях атласа."""
        if self.v1:
            for g in self.tree["m_glyphInfoList"]:
                if int(g["id"]) == ord("H"):
                    return float(g["height"])
            return float(self.tree["m_fontInfo"].get("CapHeight") or self.tree["m_fontInfo"]["PointSize"] * 0.7)
        glyph_by_index = {g["m_Index"]: g for g in self.tree["m_GlyphTable"]}
        for c in self.tree["m_CharacterTable"]:
            if int(c["m_Unicode"]) == ord("H"):
                g = glyph_by_index.get(c["m_GlyphIndex"])
                if g:
                    return float(g["m_Metrics"]["m_Height"])
        fi = self.tree.get("m_FaceInfo", {})
        return float(fi.get("m_CapLine") or fi.get("m_PointSize", 40) * 0.7)

    def advances(self) -> Dict[int, float]:
        if self.v1:
            return {int(g["id"]): float(g["xAdvance"]) for g in self.tree["m_glyphInfoList"]}
        by_idx = {g["m_Index"]: g for g in self.tree["m_GlyphTable"]}
        return {int(c["m_Unicode"]): float(by_idx[c["m_GlyphIndex"]]["m_Metrics"]["m_HorizontalAdvance"])
                for c in self.tree["m_CharacterTable"] if c["m_GlyphIndex"] in by_idx}

    def mean_advance(self, chars: str) -> float:
        adv = self.advances()
        vals = [adv[ord(c)] for c in chars if ord(c) in adv]
        return sum(vals) / len(vals) if vals else 0.0

    def monospace_advance(self) -> Optional[float]:
        if self.v1:
            adv = [float(g["xAdvance"]) for g in self.tree["m_glyphInfoList"] if 65 <= int(g["id"]) <= 122]
        else:
            by_idx = {g["m_Index"]: g for g in self.tree["m_GlyphTable"]}
            adv = [float(by_idx[c["m_GlyphIndex"]]["m_Metrics"]["m_HorizontalAdvance"])
                   for c in self.tree["m_CharacterTable"]
                   if 65 <= int(c["m_Unicode"]) <= 122 and c["m_GlyphIndex"] in by_idx]
        if len(adv) >= 10 and max(adv) - min(adv) <= max(adv) * 0.02:
            return sum(adv) / len(adv)
        return None

    def occupied_rects(self) -> List[Tuple[int, int, int, int]]:
        """Занятые прямоугольники (x, y, w, h) в координатах «сверху вниз», с отступом."""
        pad = self.padding()
        _, atlas_h = self.atlas_size()
        out = []
        if self.v1:
            for g in self.tree["m_glyphInfoList"]:
                out.append((int(g["x"]) - pad, int(g["y"]) - pad,
                            int(math.ceil(g["width"])) + 2 * pad, int(math.ceil(g["height"])) + 2 * pad))
        else:
            for g in self.tree["m_GlyphTable"]:
                r = g["m_GlyphRect"]
                top = atlas_h - (int(r["m_Y"]) + int(r["m_Height"]))
                out.append((int(r["m_X"]) - pad, top - pad, int(r["m_Width"]) + 2 * pad, int(r["m_Height"]) + 2 * pad))
        return out

    def point_size(self) -> float:
        """Кегль, которым нарисован атлас (метрики глифов — в пикселях этого кегля)."""
        if self.v1:
            return float(self.tree["m_fontInfo"].get("PointSize") or 0)
        fi = self.tree.get("m_FaceInfo") or {}
        return float(fi.get("m_PointSize") or 0)

    def boxes(self, alpha=None) -> Dict[str, "cyrillic.GlyphBox"]:
        """Глифы атласа с метриками и (если дан альфа-канал атласа) картинкой «чернил»."""
        from ...fonts import cyrillic
        out: Dict[str, cyrillic.GlyphBox] = {}
        H = alpha.shape[0] if alpha is not None else 0

        def crop(x, y_top, w, h):
            if alpha is None or w <= 0 or h <= 0:
                return None
            m = alpha[int(y_top):int(y_top) + int(math.ceil(h)), int(x):int(x) + int(math.ceil(w))] >= 0.5
            return m if m.size and m.any() else None
        if self.v1:
            for g in self.tree["m_glyphInfoList"]:
                code = int(g["id"])
                if 0 < code < 0x110000:
                    out[chr(code)] = cyrillic.GlyphBox(
                        float(g["width"]), float(g["height"]), float(g["xOffset"]), float(g["yOffset"]),
                        float(g["xAdvance"]), crop(g["x"], g["y"], g["width"], g["height"]))
            return out
        by_idx = {g["m_Index"]: g for g in self.tree["m_GlyphTable"]}
        for c in self.tree["m_CharacterTable"]:
            g = by_idx.get(c["m_GlyphIndex"])
            code = int(c["m_Unicode"])
            if g is None or not 0 < code < 0x110000:
                continue
            m, r = g["m_Metrics"], g["m_GlyphRect"]
            mask = None
            if int(g.get("m_AtlasIndex", 0)) == 0:
                mask = crop(r["m_X"], H - (int(r["m_Y"]) + int(r["m_Height"])), r["m_Width"], r["m_Height"])
            out[chr(code)] = cyrillic.GlyphBox(
                float(m["m_Width"]), float(m["m_Height"]), float(m["m_HorizontalBearingX"]),
                float(m["m_HorizontalBearingY"]), float(m["m_HorizontalAdvance"]), mask)
        return out

    def add_alias(self, code: int, src: int) -> bool:
        """Символ ``code`` рисуется уже готовым глифом символа ``src`` (русская «А» — латинской «A»)."""
        if self.v1:
            tmpl = next((g for g in self.tree["m_glyphInfoList"] if int(g["id"]) == src), None)
            if tmpl is None:
                return False
            new = copy.deepcopy(tmpl)
            new["id"] = code
            self.tree["m_glyphInfoList"].append(new)
            fi = self.tree["m_fontInfo"]
            if "CharacterCount" in fi:
                fi["CharacterCount"] = len(self.tree["m_glyphInfoList"])
            return True
        chars = self.tree["m_CharacterTable"]
        tmpl = next((c for c in chars if int(c["m_Unicode"]) == src), None)
        if tmpl is None:
            return False
        new = copy.deepcopy(tmpl)
        new["m_Unicode"] = code
        chars.append(new)
        return True

    def add_glyph(self, g: RenderedGlyph, x: int, y_top: int, atlas_h: int, advance: float, bearing_x: float) -> None:
        """Добавить глиф: (x, y_top) — левый верхний угол «чернил» в атласе (сверху вниз)."""
        code = ord(g.char)
        if self.v1:
            tmpl = self.tree["m_glyphInfoList"][0]
            new = copy.deepcopy(tmpl)
            new.update({"id": code, "x": float(x), "y": float(y_top), "width": float(g.width),
                        "height": float(g.height), "xOffset": float(bearing_x), "yOffset": float(g.bearing_y),
                        "xAdvance": float(advance), "scale": 1.0})
            self.tree["m_glyphInfoList"].append(new)
            fi = self.tree["m_fontInfo"]
            if "CharacterCount" in fi:
                fi["CharacterCount"] = len(self.tree["m_glyphInfoList"])
            return
        glyphs = self.tree["m_GlyphTable"]
        index = max([int(x_["m_Index"]) for x_ in glyphs] + [0]) + 1
        tmpl = copy.deepcopy(glyphs[0]) if glyphs else {}
        tmpl["m_Index"] = index
        tmpl["m_Metrics"] = {"m_Width": float(g.width), "m_Height": float(g.height),
                             "m_HorizontalBearingX": float(bearing_x), "m_HorizontalBearingY": float(g.bearing_y),
                             "m_HorizontalAdvance": float(advance)}
        h = int(math.ceil(g.height))
        w = int(math.ceil(g.width))
        tmpl["m_GlyphRect"] = {"m_X": int(x), "m_Y": int(atlas_h - (y_top + h)), "m_Width": w, "m_Height": h}
        tmpl["m_Scale"] = 1.0
        tmpl["m_AtlasIndex"] = 0
        glyphs.append(tmpl)
        chars = self.tree["m_CharacterTable"]
        ctmpl = copy.deepcopy(chars[0]) if chars else {}
        ctmpl.update({"m_ElementType": 1, "m_Unicode": code, "m_GlyphIndex": index, "m_Scale": 1.0})
        chars.append(ctmpl)
        used = self.tree.get("m_UsedGlyphRects")
        if isinstance(used, list):
            pad = self.padding()
            used.append({"m_X": int(x) - pad, "m_Y": int(atlas_h - (y_top + h)) - pad,
                         "m_Width": w + 2 * pad, "m_Height": h + 2 * pad})


# ---------- упаковка ----------

class _Shelf:
    """Простой полочный упаковщик в полосе [y0, y1) атласа (координаты сверху вниз)."""

    def __init__(self, width: int, y0: int, y1: int, gap: int = 1):
        self.width, self.y, self.y1, self.gap = width, y0, y1, gap
        self.x = 0
        self.row_h = 0

    def place(self, w: int, h: int) -> Optional[Tuple[int, int]]:
        if w > self.width:
            return None
        if self.x + w > self.width:
            self.y += self.row_h + self.gap
            self.x, self.row_h = 0, 0
        if self.y + h > self.y1:
            return None
        pos = (self.x, self.y)
        self.x += w + self.gap
        self.row_h = max(self.row_h, h)
        return pos


# ---------- патч одного шрифта ----------

def plan_atlas(boxes: Dict[str, "cyrillic.GlyphBox"], size: float, missing: Sequence[str],
               source: Optional[bytes] = None, pixel: Optional[float] = None
               ) -> Tuple[Dict[str, str], List[str], Optional[GlyphRenderer], float]:
    """Как дописать в атлас символы ``missing``: (двойники {русская: латинская}, что рисовать,
    чем рисовать, каким кеглем).

    Мерки снимаются с самого атласа (высота заглавных и строчных, ширина, толщина штриха,
    капитель), русские буквы, совпадающие с латинскими, ссылаются на готовые глифы, остальные
    рисуются шрифтом, построенным под эти мерки, — кеглем атласа, поэтому выходят того же размера.
    ``source`` — исходный TTF шрифта (уже с кириллицей): тогда буквы рисуются им самим."""
    from ...fonts import cyrillic, sfnt
    style = cyrillic.style_from_glyphs(size, boxes, pixel) if size > 0 else None
    aliases: Dict[str, str] = {}
    if style is not None:
        for c in missing:
            twin = cyrillic.twin_of(c, style)
            if twin and cyrillic.box_twin_ok(boxes, twin, style):
                aliases[c] = twin
    rest = [c for c in missing if c not in aliases]
    if not rest:
        return aliases, rest, None, size
    if source is not None and size > 0:
        try:
            cmap = sfnt.Font(source).cmap
            if all(ord(c) in cmap for c in rest):
                return aliases, rest, GlyphRenderer(source), size
        except sfnt.FontError:
            pass
    if style is None:
        return aliases, [], None, size                  # не текстовый шрифт (значки, другая письменность)
    return aliases, rest, GlyphRenderer(cyrillic.synth_font(style, rest, boxes)), size


def patch_font(font: TmpFont, texture, materials: List, need: Iterable[str], source: Optional[bytes] = None,
               only_new_area: bool = False) -> int:
    """Дорисовать недостающие символы. Возвращает, сколько добавлено (0 — не нужно).

    ``source`` — исходный TTF шрифта с кириллицей (если есть в игре).
    ``only_new_area`` — для динамических шрифтов: глифы кладутся только в
    добавленную часть атласа, куда динамический упаковщик TMP не пишет.
    """
    import numpy as np
    from PIL import Image

    have = font.chars()
    missing = sorted({c for c in need if ord(c) not in have and not c.isspace()})
    if not missing:
        return 0
    pad = font.padding()
    gradient = _gradient_scale(materials) or (pad + 1)
    sdf = font.is_sdf()
    atlas_w, atlas_h = font.atlas_size()

    img = texture.image
    if img.size != (atlas_w, atlas_h):
        atlas_w, atlas_h = img.size
    alpha = np.asarray(img.getchannel("A"), dtype=np.float32) / 255.0
    aliases, rest, rend, size = plan_atlas(font.boxes(alpha), font.point_size(), missing, source)
    glyphs = [rend.render(c, size, pad, gradient, sdf, 1.0) for c in rest] if rend is not None else []

    # свободная полоса после занятых глифов (TMP1 — внизу картинки, TMP2 — вверху)
    rects = font.occupied_rects()
    placements: Dict[str, Tuple[int, int]] = {}
    grown_rows = 0
    for attempt in range(4):
        if font.v1:
            used_bottom = max((y + h for x, y, w, h in rects), default=0)
            band = (used_bottom + 1, atlas_h)
        elif only_new_area:
            band = (0, grown_rows - 1)
        else:
            used_top = min((y for x, y, w, h in rects), default=atlas_h)
            band = (0, used_top - 1)
        shelf = _Shelf(atlas_w, band[0], band[1])
        placements = {}
        ok = True
        for g in glyphs:
            if g.bitmap.size == 0:
                continue
            bh, bw = g.bitmap.shape
            pos = shelf.place(bw, bh)
            if pos is None:
                ok = False
                break
            placements[g.char] = pos
        if ok:
            break
        # места нет — растим атлас вдвое по высоте (старые глифы не двигаются в своих координатах)
        new = np.zeros((atlas_h * 2, atlas_w), np.float32)
        if font.v1:
            new[:atlas_h] = alpha
        else:
            new[atlas_h:] = alpha
            rects = [(x, y + atlas_h, w, h) for x, y, w, h in rects]
            grown_rows += atlas_h
        alpha = new
        atlas_h *= 2
        if atlas_h > 8192:
            raise RuntimeError("атлас шрифта слишком велик для дорисовки")
    else:
        raise RuntimeError("не удалось разместить глифы в атласе")

    for g in glyphs:
        if g.char not in placements:  # пробельный символ без картинки
            font.add_glyph(g, 0, 0, atlas_h, g.advance, g.bearing_x)
            continue
        px, py = placements[g.char]
        bh, bw = g.bitmap.shape
        region = alpha[py:py + bh, px:px + bw]
        np.maximum(region, g.bitmap, out=region)
        font.add_glyph(g, px + pad, py + pad, atlas_h, g.advance, g.bearing_x)
    added = len(glyphs) + sum(1 for c, t in aliases.items() if font.add_alias(ord(c), ord(t)))
    if not added:
        return 0
    if glyphs:
        font.set_atlas_size(atlas_w, atlas_h)
        a8 = Image.fromarray(np.round(alpha * 255).astype(np.uint8), "L")
        rgba = Image.merge("RGBA", (a8, a8, a8, a8))
        texture.set_image(rgba, target_format=texture.m_TextureFormat)
        for mat_obj, mtree in materials:
            _set_float(mtree, "_TextureHeight", float(atlas_h))
            _set_float(mtree, "_TextureWidth", float(atlas_w))
    return added


def _floats(mtree: dict) -> list:
    return mtree.get("m_SavedProperties", {}).get("m_Floats", [])


def _gradient_scale(materials: List) -> Optional[float]:
    for _, mtree in materials:
        for item in _floats(mtree):
            k, v = (item[0], item[1]) if isinstance(item, (list, tuple)) else (item.get("first"), item.get("second"))
            if k == "_GradientScale":
                return float(v)
    return None


def _set_float(mtree: dict, name: str, value: float) -> None:
    fl = _floats(mtree)
    for i, item in enumerate(fl):
        if isinstance(item, (list, tuple)) and item[0] == name:
            fl[i] = (name, value) if isinstance(item, tuple) else [name, value]
        elif isinstance(item, dict) and item.get("first") == name:
            item["second"] = value


# ---------- по всей игре ----------

def _material_texture_id(mtree: dict) -> Optional[int]:
    for item in mtree.get("m_SavedProperties", {}).get("m_TexEnvs", []):
        k, v = (item[0], item[1]) if isinstance(item, (list, tuple)) else (item.get("first"), item.get("second"))
        if k == "_MainTex" and isinstance(v, dict):
            tex = v.get("m_Texture") or {}
            if int(tex.get("m_FileID", 0)) == 0:
                return int(tex.get("m_PathID", 0))
    return None


def _source_font(font: TmpFont, objects_by_id: Dict[int, object], patched: Dict[int, bytes]) -> Optional[bytes]:
    """Исходный TTF TMP-шрифта, если он лежит в том же файле (уже дополненный кириллицей, если дополнялся)."""
    pptr = font.tree.get("m_SourceFontFile") or {}
    if int(pptr.get("m_FileID", 0)) != 0:
        return None
    pid = int(pptr.get("m_PathID", 0))
    if pid in patched:
        return patched[pid]
    obj = objects_by_id.get(pid)
    if obj is None:
        return None
    try:
        data = font_bytes(obj.read_typetree().get("m_FontData"))
    except Exception:  # noqa: BLE001
        return None
    return data or None


def font_bytes(value) -> bytes:
    """m_FontData из typetree (список байтов или bytes) -> bytes."""
    if not value:
        return b""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    return bytes(v & 0xFF for v in value)


def font_list(data: bytes, like) -> object:
    """bytes -> значение m_FontData того же вида, что было в typetree (список знаковых/беззнаковых байтов)."""
    if isinstance(like, (bytes, bytearray, memoryview)):
        return bytes(data)
    signed = any(v < 0 for v in like[:4096]) if like else False
    return [v - 256 if v > 127 else v for v in data] if signed else list(data)


def patch_file(path: Path, gen, need: Set[str], write: Callable[[Path, bytes], None]) -> List[FontReport]:
    """Дописать кириллицу во все шрифты одного файла ассетов. write(path, data) — запись с бэкапом."""
    with unitypy_io.load_assets(str(path)) as env:
        return _patch_env(env, path, gen, need, write)


def _patch_env(env, path: Path, gen, need: Set[str], write: Callable[[Path, bytes], None]) -> List[FontReport]:
    from ...fonts import cyrillic
    if gen is not None:
        env.typetree_generator = gen
    reports: List[FontReport] = []
    changed = False
    fonts = []
    material_trees: Dict[int, Tuple] = {}
    legacy = []
    ex2d = []
    ttf = []
    for obj in env.objects:
        try:
            if obj.type.name == "MonoBehaviour":
                tree = obj.read_typetree()
                if TmpFont.match(tree):
                    fonts.append((obj, TmpFont(tree)))
                elif is_ex2d_font(tree):
                    ex2d.append((obj, tree))
            elif obj.type.name == "Font":
                tree = obj.read_typetree()
                if is_bitmap_font(tree):
                    legacy.append((obj, tree))
                elif tree.get("m_FontData"):
                    ttf.append((obj, tree))
            elif obj.type.name == "Material":
                material_trees[obj.path_id] = (obj, obj.read_typetree())
        except Exception:  # noqa: BLE001
            continue
    objects_by_id = {o.path_id: o for o in env.objects}

    # 1) обычные шрифты (TTF/OTF внутри ассета Font): кириллица дописывается в сам шрифт по его меркам —
    #    ими рисуют UI Text, TextMesh, IMGUI и динамические шрифты TextMeshPro
    patched: Dict[int, bytes] = {}
    checks: List[Tuple[int, object, FontReport]] = []        # что перепроверить после сохранения
    for obj, tree in ttf:
        rep = FontReport(path.name, str(tree.get("m_Name", "font")))
        try:
            data = font_bytes(tree["m_FontData"])
            new = cyrillic.add_cyrillic(data, need)
            if new is None:
                continue
            from ...fonts import sfnt
            rep.added = len(cyrillic.missing(sfnt.Font(data), need))
            tree["m_FontData"] = font_list(new, tree["m_FontData"])
            obj.save_typetree(tree)
            patched[obj.path_id] = new
            changed = True
            reports.append(rep)
            checks.append((obj.path_id, False, rep))
        except Exception as exc:  # noqa: BLE001
            log.warning("Шрифт %s не дополнен: %s", rep.name, exc)
            rep.error = str(exc)
            reports.append(rep)

    # 2) атласы TextMeshPro
    for obj, font in fonts:
        rep = FontReport(path.name, font.name)
        reports.append(rep)
        dynamic = font.is_dynamic()
        source = _source_font(font, objects_by_id, patched)
        if dynamic and source is not None and all(ord(c) in _cmap_of(source) for c in need):
            continue  # динамический шрифт сам дорисует кириллицу из исходного TTF
        try:
            pptr = font.atlas_pptr() or {}
            if int(pptr.get("m_FileID", 0)) != 0 or int(pptr.get("m_PathID", 0)) not in objects_by_id:
                rep.error = "атлас шрифта лежит в другом файле"
                continue
            tex_obj = objects_by_id[int(pptr["m_PathID"])]
            texture = tex_obj.read()
            mats = [(o, t) for pid, (o, t) in material_trees.items() if _material_texture_id(t) == tex_obj.path_id]
            added = patch_font(font, texture, mats, need, source=source, only_new_area=dynamic)
            if not added:
                continue
            obj.save_typetree(font.tree)
            texture.save()
            for mobj, mtree in mats:
                mobj.save_typetree(mtree)
            rep.added = added
            changed = True
            checks.append((obj.path_id, True, rep))
        except Exception as exc:  # noqa: BLE001
            log.warning("TMP-шрифт %s не починен: %s", font.name, exc)
            rep.error = str(exc)

    # 3) растровые шрифты (Font с готовой текстурой букв)
    for obj, tree in legacy:
        rep = FontReport(path.name, str(tree.get("m_Name", "font")))
        try:
            pptr = tree.get("m_Texture") or {}
            if int(pptr.get("m_FileID", 0)) != 0 or int(pptr.get("m_PathID", 0)) not in objects_by_id:
                continue
            texture = objects_by_id[int(pptr["m_PathID"])].read()
            added = patch_legacy_font(tree, texture, need)
            if added:
                obj.save_typetree(tree)
                texture.save()
                rep.added = added
                changed = True
                reports.append(rep)
                checks.append((obj.path_id, False, rep))
        except Exception as exc:  # noqa: BLE001
            log.warning("Растровый шрифт %s не починен: %s", rep.name, exc)
            rep.error = str(exc)
            reports.append(rep)
    # 4) растровые шрифты ex2D (exBitmapFont): текст exSpriteFont XUnity не видит — его переводит
    #    плагин, а буквы для него — здесь
    ex2d_textures: Dict[int, object] = {}             # одна текстура бывает у нескольких шрифтов
    for obj, tree in ex2d:
        rep = FontReport(path.name, str(tree.get("m_Name", "ex2D font")))
        try:
            pptr = ex2d_texture_pptr(tree) or {}
            if int(pptr.get("m_FileID", 0)) != 0 or int(pptr.get("m_PathID", 0)) not in objects_by_id:
                continue
            tex_id = int(pptr["m_PathID"])
            if tex_id not in ex2d_textures:
                ex2d_textures[tex_id] = objects_by_id[tex_id].read()
            texture = ex2d_textures[tex_id]
            added = patch_ex2d_font(tree, texture, need)
            if added:
                obj.save_typetree(tree)
                texture.save()
                rep.added = added
                changed = True
                reports.append(rep)
                checks.append((obj.path_id, "ex2d", rep))
        except Exception as exc:  # noqa: BLE001
            log.warning("Шрифт ex2D %s не починен: %s", rep.name, exc)
            rep.error = str(exc)
            reports.append(rep)
    if not changed:
        return reports
    table = unitypy_io.object_table(env)
    data = env.file.save()
    _verify(data, path, gen, checks, need, table)
    write(path, data)
    return reports


def _cmap_of(data: bytes) -> Dict[int, int]:
    from ...fonts import sfnt
    try:
        return sfnt.Font(data).cmap
    except sfnt.FontError:
        return {}


def _verify(data: bytes, path: Path, gen, checks: List[Tuple[int, object, FontReport]], need: Set[str],
            table: Optional[dict] = None) -> None:
    """Перечитать сохранённый файл и убедиться, что каждый исправленный шрифт покрывает нужные символы.

    ``checks`` — (path_id объекта, это TMP-шрифт, отчёт) для каждого изменённого шрифта. Проверка
    обязана реально прочитать каждый изменённый шрифт и его атлас — если что-то не читается, файл
    не записывается (лучше не тронуть игру, чем записать непроверенное).
    """
    with unitypy_io.load_assets(data) as env:
        env.path = str(path.parent)  # внешние ссылки (скрипты шрифтов) — из папки игры
        if table is not None:
            # остальные объекты файла — те же, с теми же скриптами (UnityPy умеет их перепутать в Unity 5)
            problem = unitypy_io.same_object_table(table, unitypy_io.object_table(env))
            if problem:
                raise RuntimeError("проверка: " + problem)
        _verify_env(env, gen, checks, need)


def _verify_env(env, gen, checks: List[Tuple[int, object, FontReport]], need: Set[str]) -> None:
    if gen is not None:
        env.typetree_generator = gen
    objects = {o.path_id: o for o in env.objects}
    for pid, is_tmp, rep in checks:
        obj = objects.get(pid)
        if obj is None:
            raise RuntimeError(f"проверка: шрифт {rep.name} пропал из файла")
        tree = obj.read_typetree()
        if is_tmp == "ex2d":
            have = {int(c["id"]) for c in tree.get("charInfos", [])}
            pptr = ex2d_texture_pptr(tree) or {}
            tex = objects[int(pptr["m_PathID"])].read()
            if tex.image.size != (tex.m_Width, tex.m_Height):
                raise RuntimeError(f"проверка: текстура шрифта {rep.name} не читается")
        elif is_tmp:
            fresh = TmpFont(tree)
            have = fresh.chars()
            pptr = fresh.atlas_pptr() or {}
            tex = objects[int(pptr["m_PathID"])].read()
            w, h = fresh.atlas_size()
            if (tex.m_Width, tex.m_Height) != (w, h) or tex.image.size != (w, h):
                raise RuntimeError(f"проверка: размер атласа {rep.name} не совпадает с описанием шрифта")
        else:
            data = font_bytes(tree.get("m_FontData"))
            have = set(_cmap_of(data)) if data else {int(r["index"]) for r in tree.get("m_CharacterRects", [])}
        rep.missing_after = [c for c in need if ord(c) not in have and not c.isspace()]
        if rep.missing_after:
            raise RuntimeError(f"проверка: в шрифте {rep.name} по-прежнему нет {''.join(rep.missing_after[:10])}")


# ---------- растровые шрифты старого формата (UnityEngine.Font без TTF) ----------

def is_bitmap_font(tree: dict) -> bool:
    return isinstance(tree, dict) and not tree.get("m_FontData") and bool(tree.get("m_CharacterRects"))


def legacy_boxes(tree: dict, alpha) -> Dict[str, "cyrillic.GlyphBox"]:
    """Глифы растрового шрифта Unity: метрики «чернил» (vert — вместе с отступом m_CharacterPadding)."""
    from ...fonts import cyrillic
    H, W = alpha.shape
    pad = float(tree.get("m_CharacterPadding") or 0)
    out: Dict[str, cyrillic.GlyphBox] = {}
    for r in tree["m_CharacterRects"]:
        code = int(r["index"])
        if not 0 < code < 0x110000:
            continue
        v, uv = r["vert"], r["uv"]
        h = abs(float(v["height"]))
        top = float(v["y"]) if float(v["height"]) < 0 else float(v["y"]) + float(v["height"])
        mask = None
        if not r.get("flipped"):
            ya, yb = float(uv["y"]), float(uv["y"]) + float(uv["height"])
            r0, r1 = int(round((1 - max(ya, yb)) * H)), int(round((1 - min(ya, yb)) * H))
            x0 = int(round(float(uv["x"]) * W))
            x1 = int(round((float(uv["x"]) + float(uv["width"])) * W))
            crop = alpha[max(0, r0):r1, max(0, x0):x1] >= 0.5
            mask = crop if crop.size and crop.any() else None
        out[chr(code)] = cyrillic.GlyphBox(max(0.0, float(v["width"]) - 2 * pad), max(0.0, h - 2 * pad),
                                           float(v["x"]) + pad, top - pad, float(r["advance"]), mask)
    return out


def _binary(alpha) -> bool:
    """Буквы нарисованы без сглаживания (пиксельный шрифт): полутонов почти нет."""
    ink = (alpha > 0.05).sum()
    mid = ((alpha > 0.05) & (alpha < 0.95)).sum()
    return ink > 0 and mid / ink < 0.03


def patch_legacy_font(tree: dict, texture, need: Iterable[str]) -> int:
    """Дорисовать недостающие символы в растровый шрифт Unity (CharacterInfo + текстура) — по меркам
    его же букв; русские буквы, совпадающие с латинскими, берутся готовыми из текстуры."""
    import numpy as np
    from PIL import Image

    rects = tree["m_CharacterRects"]
    have = {int(r["index"]) for r in rects}
    missing = sorted({c for c in need if ord(c) not in have and not c.isspace()})
    if not missing:
        return 0
    img = texture.image
    W, H = img.size
    alpha = np.asarray(img.getchannel("A"), dtype=np.float32) / 255.0
    boxes = legacy_boxes(tree, alpha)
    caps = [b.top for c, b in boxes.items() if c in "HIEFTLNZ" and b.height > 0]
    size = float(tree.get("m_FontSize") or 0) or (max(caps) / 0.7 if caps else 16.0)
    pixel = 1.0 if _binary(alpha) else None
    aliases, rest, rend, size = plan_atlas(boxes, size, missing, None, pixel)
    glyphs = [rend.render(c, size, 1, 1.0, sdf=False, xscale=1.0) for c in rest] if rend is not None else []
    if pixel:                                       # пиксельный шрифт — без полутонов, как буквы игры
        for g in glyphs:
            if g.bitmap.size:
                g.bitmap = (g.bitmap >= 0.5).astype(np.float32)
    by_code = {int(r["index"]): r for r in rects}

    def used_bottom_row() -> int:
        """Нижняя занятая строка текстуры (сверху вниз) по UV существующих символов."""
        rows = [int(math.ceil((1 - min(r["uv"]["y"], r["uv"]["y"] + r["uv"]["height"])) * H)) for r in rects]
        return max(rows, default=0)

    placements: Dict[str, Tuple[int, int]] = {}
    for attempt in range(4):
        shelf = _Shelf(W, used_bottom_row() + 1, H)
        placements, ok = {}, True
        for g in glyphs:
            if g.bitmap.size == 0:
                continue
            bh, bw = g.bitmap.shape
            pos = shelf.place(bw, bh)
            if pos is None:
                ok = False
                break
            placements[g.char] = pos
        if ok:
            break
        # растим текстуру вниз; UV нормированы — пересчитываем их для старых символов
        new = np.zeros((H * 2, W), np.float32)
        new[:H] = alpha
        for r in rects:
            uv = r["uv"]
            uv["y"] = 1 - (1 - uv["y"]) / 2
            uv["height"] = uv["height"] / 2
        alpha = new
        H *= 2
        if H > 8192:
            raise RuntimeError("текстура шрифта слишком велика")
    else:
        raise RuntimeError("не удалось разместить символы в текстуре шрифта")

    template = copy.deepcopy(by_code.get(ord("H")) or rects[0])
    for g in glyphs:
        entry = copy.deepcopy(template)
        entry["index"] = ord(g.char)
        entry["advance"] = float(round(g.advance))
        entry["flipped"] = False
        if g.char not in placements:
            entry["uv"] = {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}
            entry["vert"] = {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}
        else:
            px, py = placements[g.char]
            bh, bw = g.bitmap.shape
            region = alpha[py:py + bh, px:px + bw]
            np.maximum(region, g.bitmap, out=region)
            entry["uv"] = {"x": px / W, "y": 1 - py / H, "width": bw / W, "height": -bh / H}
            entry["vert"] = {"x": float(round(g.bearing_x) - 1), "y": float(round(g.bearing_y) + 1),
                             "width": float(bw), "height": float(-bh)}
        rects.append(entry)
    for c, t in aliases.items():
        src = by_code.get(ord(t))
        if src is not None:
            entry = copy.deepcopy(src)
            entry["index"] = ord(c)
            rects.append(entry)
    if glyphs:
        a8 = Image.fromarray(np.round(alpha * 255).astype(np.uint8), "L")
        texture.set_image(Image.merge("RGBA", (a8, a8, a8, a8)), target_format=texture.m_TextureFormat)
    return len(glyphs) + len(aliases)


# ---------- растровые шрифты ex2D (exBitmapFont: разметка BMFont + текстура) ----------

#: буквы без выносных — по их низу находится базовая линия шрифта BMFont (yoffset — от верха строки)
_EX2D_BASE_CHARS = "ABCDEFHIKLMNOPRSTUVWXZacemnorsuvwxz"


def is_ex2d_font(tree: dict) -> bool:
    return (isinstance(tree, dict) and isinstance(tree.get("charInfos"), list) and bool(tree["charInfos"])
            and isinstance(tree.get("pageInfos"), list) and bool(tree["pageInfos"]))


def ex2d_texture_pptr(tree: dict) -> Optional[dict]:
    page = tree["pageInfos"][0] if tree.get("pageInfos") else None
    return page.get("texture") if isinstance(page, dict) else None


def _ex2d_base(chars: List[dict]) -> int:
    bottoms = [int(c["yoffset"]) + int(c["height"]) for c in chars
               if 0 < int(c["id"]) < 0x110000 and chr(int(c["id"])) in _EX2D_BASE_CHARS and int(c["height"]) > 0]
    if not bottoms:
        return int(max((int(c["yoffset"]) + int(c["height"]) for c in chars), default=0))
    bottoms.sort()
    return bottoms[len(bottoms) // 2]


def ex2d_boxes(tree: dict, alpha) -> Dict[str, "cyrillic.GlyphBox"]:
    """Глифы exBitmapFont: x, y — левый верхний угол в текстуре, yoffset — от верха строки до верха глифа."""
    from ...fonts import cyrillic
    base = _ex2d_base(tree["charInfos"])
    out: Dict[str, cyrillic.GlyphBox] = {}
    for c in tree["charInfos"]:
        code = int(c["id"])
        if not 0 < code < 0x110000:
            continue
        x, y, w, h = int(c["x"]), int(c["y"]), int(c["width"]), int(c["height"])
        crop = alpha[y:y + h, x:x + w] >= 0.5 if w > 0 and h > 0 else None
        mask = crop if crop is not None and crop.size and crop.any() else None
        out[chr(code)] = cyrillic.GlyphBox(float(w), float(h), float(c["xoffset"]), float(base - int(c["yoffset"])),
                                           float(c["xadvance"]), mask)
    return out


def patch_ex2d_font(tree: dict, texture, need: Iterable[str]) -> int:
    """Дорисовать недостающие символы в шрифт ex2D (exBitmapFont): мерки — по его же буквам (пиксельный
    шрифт — из его латиницы, клетка в клетку), двойники латиницы — готовыми глифами; текстура при
    нехватке места растёт вниз, нормированные UV всех символов пересчитываются."""
    import numpy as np
    from PIL import Image

    chars = tree["charInfos"]
    have = {int(c["id"]) for c in chars}
    missing = sorted({c for c in need if ord(c) not in have and not c.isspace()})
    if not missing:
        return 0
    img = texture.image.convert("RGBA")
    W, H = img.size
    alpha = np.asarray(img.getchannel("A"), dtype=np.float32) / 255.0
    boxes = ex2d_boxes(tree, alpha)
    base = _ex2d_base(chars)
    size = float(tree.get("size") or tree.get("lineHeight") or 16)
    pixel = 1.0 if _binary(alpha) else None
    aliases, rest, rend, size = plan_atlas(boxes, size, missing, None, pixel)
    pad = 1
    glyphs = [rend.render(c, size, pad, 1.0, sdf=False, xscale=1.0) for c in rest] if rend is not None else []
    if pixel:
        for g in glyphs:
            if g.bitmap.size:
                g.bitmap = (g.bitmap >= 0.5).astype(np.float32)

    used = max((int(c["y"]) + int(c["height"]) for c in chars), default=0)
    ink_rows = np.nonzero(alpha.max(axis=1) > 0)[0]
    if len(ink_rows):                               # буквы другого шрифта с той же текстурой
        used = max(used, int(ink_rows[-1]) + 1)
    placements: Dict[str, Tuple[int, int]] = {}
    for attempt in range(6):
        shelf = _Shelf(W, used + 1, H)
        placements, ok = {}, True
        for g in glyphs:
            if g.bitmap.size == 0:
                continue
            bh, bw = g.bitmap.shape
            pos = shelf.place(bw, bh)
            if pos is None:
                ok = False
                break
            placements[g.char] = pos
        if ok:
            break
        # места нет — текстура растёт вниз: старые буквы остаются там же (x, y — от верха)
        grow = max(16, int(math.ceil(H * 0.5)))
        alpha = np.vstack([alpha, np.zeros((grow, W), np.float32)])
        H += grow
        if H > 4096:
            raise RuntimeError("текстура шрифта ex2D слишком велика")
    else:
        raise RuntimeError("не удалось разместить символы в текстуре шрифта ex2D")

    by_code = {int(c["id"]): c for c in chars}
    template = by_code.get(ord("H")) or chars[0]
    for g in glyphs:
        entry = copy.deepcopy(template)
        entry["id"] = ord(g.char)
        entry["xadvance"] = int(round(g.advance))
        if g.char not in placements:
            entry.update({"x": 0, "y": 0, "width": 0, "height": 0, "xoffset": 0, "yoffset": 0})
        else:
            px, py = placements[g.char]
            bh, bw = g.bitmap.shape
            region = alpha[py:py + bh, px:px + bw]
            np.maximum(region, g.bitmap, out=region)
            entry.update({"x": px, "y": py, "width": bw, "height": bh,
                          "xoffset": int(round(g.bearing_x)) - pad,
                          "yoffset": base - (int(round(g.bearing_y)) + pad)})
        chars.append(entry)
    for c, t in aliases.items():
        src = by_code.get(ord(t))
        if src is not None:
            entry = copy.deepcopy(src)
            entry["id"] = ord(c)
            chars.append(entry)
    for c in chars:                                 # UV нормированы по размеру текстуры — пересчёт для всех
        if isinstance(c.get("uv0"), dict):
            c["uv0"] = {"x": int(c["x"]) / W, "y": (H - int(c["y"]) - int(c["height"])) / H}
    if glyphs:
        rgba = np.asarray(img, dtype=np.uint8)
        if rgba.shape[0] < H:
            rgba = np.vstack([rgba, np.zeros((H - rgba.shape[0], W, 4), np.uint8)])
        rgba = rgba.copy()
        new_ink = alpha > (rgba[..., 3].astype(np.float32) / 255.0 + 1e-6)
        rgba[new_ink, 0:3] = 255                    # буквы белые: цвет даёт материал и вершины ex2D
        rgba[..., 3] = np.maximum(rgba[..., 3], np.round(alpha * 255).astype(np.uint8))
        texture.set_image(Image.fromarray(rgba, "RGBA"), target_format=texture.m_TextureFormat)
    return len(glyphs) + len(aliases)

# ---------- вся игра ----------

def fix_game(files: Iterable[Path], gen, translations: Iterable[str], backup,
             status: Callable[[str, Optional[float]], None] = lambda m, f=None: None) -> Tuple[List[FontReport], List[str]]:
    """Дописать кириллицу во все шрифты игры (TTF, TextMeshPro, растровые) по меркам каждого шрифта.

    Возвращает (отчёты по шрифтам, проблемы, которые починить не удалось).
    """
    from ...fonts.cmap import codepoints
    from ...fonts.library import ensure_font

    regular = ensure_font()
    if regular is None:
        return [], ["Встроенный шрифт PT Sans не найден — кириллица в шрифты игры не дорисована."]
    cover = codepoints(regular)
    need = needed_chars(translations)
    unsupported = sorted(c for c in need if ord(c) not in cover)
    need = {c for c in need if ord(c) in cover}
    problems: List[str] = []
    if unsupported:
        problems.append("Символы, которых нет во встроенном шрифте, могут не отображаться: " + "".join(unsupported[:20]))

    written: List[Path] = []

    def write(path: Path, data: bytes) -> None:
        backup.save_original(path)
        path.write_bytes(data)
        written.append(path)

    reports: List[FontReport] = []
    files = list(files)
    for i, f in enumerate(files):
        status(f"Проверка шрифтов: {f.name}", i / max(1, len(files)))
        try:
            reports += patch_file(f, gen, need, write)
        except Exception as exc:  # noqa: BLE001
            log.warning("Шрифты в %s не исправлены: %s", f.name, exc)
            problems.append(f"{f.name}: шрифты не исправлены ({exc})")
    if written:
        from . import addressables
        addressables.disable_crc(written, backup)   # изменённый бандл Addressables не прошёл бы проверку CRC
    for r in reports:
        if r.error:
            problems.append(f"Шрифт {r.name} ({r.file}): {r.error}")
    return reports, problems
