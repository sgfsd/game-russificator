"""Кириллица для шрифтов TextMeshPro: дорисовка глифов прямо в атлас шрифта.

Главная причина «квадратиков» в Unity-играх — статичный атлас TextMeshPro:
шрифт запечён в текстуру один раз при сборке игры и содержит только латиницу.
Готовые TMP-шрифты XUnity есть не для всех версий Unity, поэтому здесь
атлас чинится на месте для любой версии TMP (1.x, 2.x, 3.x):

  1. берём буквы, которых в шрифте не хватает (весь русский алфавит + все
     символы, реально встречающиеся в переводе: «», —, №, …);
  2. рендерим их из встроенного PT Sans (жирный — для жирных шрифтов) в
     SDF — точно в том же кодировании, что у TMP (0.5 на контуре,
     наклон 1/(2·GradientScale), обрезка на расстоянии Padding), и
     подгоняем размер под высоту заглавных букв оригинального шрифта;
  3. дописываем их в свободную часть атласа (если места нет — атлас
     увеличивается вдвое, старые глифы остаются на своих координатах);
  4. добавляем записи в таблицы шрифта и обновляем размеры атласа в
     материалах, затем проверяем результат, перечитав файл заново.

Изменённые файлы ассетов попадают в бэкап — откат возвращает оригиналы.
"""

from __future__ import annotations

import copy
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from . import unitypy_io

log = logging.getLogger("russificator.unity.tmp")

RUSSIAN = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя"
EXTRA = "«»—–…№„“”’"


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
    def __init__(self, font_path: Path):
        self.font_path = Path(font_path)
        self._cache: Dict[Tuple, RenderedGlyph] = {}
        self._cap_ratio: Optional[float] = None

    def cap_ratio(self) -> float:
        """Высота заглавной «Н» в долях кегля (для подгонки под оригинальный шрифт)."""
        if self._cap_ratio is None:
            from PIL import ImageFont
            f = ImageFont.truetype(str(self.font_path), size=200)
            l, t, r, b = f.getbbox("Н", anchor="ls")
            self._cap_ratio = (b - t) / 200.0
        return self._cap_ratio

    def mean_advance(self, chars: str, size: float) -> float:
        from PIL import ImageFont
        f = ImageFont.truetype(str(self.font_path), size=max(1, round(size)))
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
        font = ImageFont.truetype(str(self.font_path), size=max(1, round(size * ss)))
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
        self.v1 = "m_glyphInfoList" in tree

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

def patch_font(font: TmpFont, texture, materials: List, renderer: GlyphRenderer, bold_renderer: GlyphRenderer,
               need: Iterable[str], only_new_area: bool = False) -> int:
    """Дорисовать недостающие символы. Возвращает, сколько добавлено (0 — не нужно).

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
    rend = bold_renderer if any(k in font.name.lower() for k in ("bold", "black", "heavy")) else renderer
    size = font.cap_height() / rend.cap_ratio()
    mono = font.monospace_advance()
    # ширина: подгоняем среднюю ширину латиницы PT Sans под латиницу шрифта игры
    latin = "".join(ch for ch in "ABCDEFGHKLMNOPRSTUXYZabcdeghknopstuxyz" if ord(ch) in have)
    orig_adv = font.mean_advance(latin)
    xscale = 1.0
    if orig_adv and len(latin) >= 10:
        xscale = max(0.55, min(1.3, orig_adv / rend.mean_advance(latin, size)))
    glyphs = [rend.render(c, size, pad, gradient, sdf, xscale) for c in missing]

    img = texture.image
    if img.size != (atlas_w, atlas_h):
        atlas_w, atlas_h = img.size
    alpha = np.asarray(img.getchannel("A"), dtype=np.float32) / 255.0

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
        advance = mono if mono else g.advance
        bearing_x = (mono - g.width) / 2 if mono else g.bearing_x
        if g.char not in placements:  # пробельный символ без картинки
            font.add_glyph(g, 0, 0, atlas_h, advance, bearing_x)
            continue
        px, py = placements[g.char]
        bh, bw = g.bitmap.shape
        region = alpha[py:py + bh, px:px + bw]
        np.maximum(region, g.bitmap, out=region)
        font.add_glyph(g, px + pad, py + pad, atlas_h, advance, bearing_x)

    font.set_atlas_size(atlas_w, atlas_h)
    a8 = Image.fromarray(np.round(alpha * 255).astype(np.uint8), "L")
    rgba = Image.merge("RGBA", (a8, a8, a8, a8))
    texture.set_image(rgba, target_format=texture.m_TextureFormat)
    for mat_obj, mtree in materials:
        _set_float(mtree, "_TextureHeight", float(atlas_h))
        _set_float(mtree, "_TextureWidth", float(atlas_w))
    return len(missing)


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


def _source_has_cyrillic(font: TmpFont, objects_by_id: Dict[int, object]) -> bool:
    """Есть ли кириллица в исходном TTF динамического TMP-шрифта (если его удаётся прочитать)."""
    from ...fonts.cmap import has_cyrillic
    pptr = font.tree.get("m_SourceFontFile") or {}
    obj = objects_by_id.get(int(pptr.get("m_PathID", 0))) if int(pptr.get("m_FileID", 0)) == 0 else None
    if obj is None:
        return False
    try:
        data = bytes(getattr(obj.read(), "m_FontData", b"") or b"")
    except Exception:  # noqa: BLE001
        return False
    return bool(data) and has_cyrillic(data)


def patch_file(path: Path, gen, need: Set[str], renderer: GlyphRenderer, bold: GlyphRenderer,
               write: Callable[[Path, bytes], None]) -> List[FontReport]:
    """Починить все TMP-шрифты в одном файле ассетов. write(path, data) — запись с бэкапом."""
    with unitypy_io.load_assets(str(path)) as env:
        return _patch_env(env, path, gen, need, renderer, bold, write)


def _patch_env(env, path: Path, gen, need: Set[str], renderer: GlyphRenderer, bold: GlyphRenderer,
               write: Callable[[Path, bytes], None]) -> List[FontReport]:
    if gen is not None:
        env.typetree_generator = gen
    reports: List[FontReport] = []
    changed = False
    fonts = []
    material_trees: Dict[int, Tuple] = {}
    legacy = []
    for obj in env.objects:
        try:
            if obj.type.name == "MonoBehaviour":
                tree = obj.read_typetree()
                if TmpFont.match(tree):
                    fonts.append((obj, TmpFont(tree)))
            elif obj.type.name == "Font":
                tree = obj.read_typetree()
                if is_bitmap_font(tree):
                    legacy.append((obj, tree))
            elif obj.type.name == "Material":
                material_trees[obj.path_id] = (obj, obj.read_typetree())
        except Exception:  # noqa: BLE001
            continue
    objects_by_id = {o.path_id: o for o in env.objects}
    for obj, font in fonts:
        rep = FontReport(path.name, font.name)
        reports.append(rep)
        dynamic = font.is_dynamic()
        if dynamic and _source_has_cyrillic(font, objects_by_id):
            continue  # динамический шрифт сам дорисует кириллицу из исходного TTF
        try:
            pptr = font.atlas_pptr() or {}
            if int(pptr.get("m_FileID", 0)) != 0 or int(pptr.get("m_PathID", 0)) not in objects_by_id:
                rep.error = "атлас шрифта лежит в другом файле"
                continue
            tex_obj = objects_by_id[int(pptr["m_PathID"])]
            texture = tex_obj.read()
            mats = [(o, t) for pid, (o, t) in material_trees.items() if _material_texture_id(t) == tex_obj.path_id]
            added = patch_font(font, texture, mats, renderer, bold, need, only_new_area=dynamic)
            if not added:
                continue
            obj.save_typetree(font.tree)
            texture.save()
            for mobj, mtree in mats:
                mobj.save_typetree(mtree)
            rep.added = added
            changed = True
        except Exception as exc:  # noqa: BLE001
            log.warning("TMP-шрифт %s не починен: %s", font.name, exc)
            rep.error = str(exc)
    for obj, tree in legacy:
        rep = FontReport(path.name, str(tree.get("m_Name", "font")))
        try:
            pptr = tree.get("m_Texture") or {}
            if int(pptr.get("m_FileID", 0)) != 0 or int(pptr.get("m_PathID", 0)) not in objects_by_id:
                continue
            texture = objects_by_id[int(pptr["m_PathID"])].read()
            added = patch_legacy_font(tree, texture, renderer, need)
            if added:
                obj.save_typetree(tree)
                texture.save()
                rep.added = added
                changed = True
                reports.append(rep)
        except Exception as exc:  # noqa: BLE001
            log.warning("Растровый шрифт %s не починен: %s", rep.name, exc)
            rep.error = str(exc)
            reports.append(rep)
    if not changed:
        return reports
    data = env.file.save()
    _verify(data, path, gen, [(obj.path_id, font) for obj, font in fonts], reports, need)
    write(path, data)
    return reports


def _verify(data: bytes, path: Path, gen, fonts: List[Tuple[int, TmpFont]], reports: List[FontReport],
            need: Set[str]) -> None:
    """Перечитать сохранённый файл и убедиться, что каждый исправленный шрифт покрывает нужные символы.

    Проверка обязана реально прочитать каждый изменённый шрифт и его атлас —
    если что-то не читается, файл не записывается (лучше не тронуть игру,
    чем записать непроверенное).
    """
    with unitypy_io.load_assets(data) as env:
        env.path = str(path.parent)  # внешние ссылки (скрипты шрифтов) — из папки игры
        _verify_env(env, gen, fonts, reports, need)


def _verify_env(env, gen, fonts: List[Tuple[int, TmpFont]], reports: List[FontReport], need: Set[str]) -> None:
    if gen is not None:
        env.typetree_generator = gen
    objects = {o.path_id: o for o in env.objects}
    for obj in env.objects:
        if obj.type.name != "Font":
            continue
        tree = obj.read_typetree()
        rep = next((r for r in reports if r.name == tree.get("m_Name") and r.added), None)
        if rep is None:
            continue
        have = {int(r["index"]) for r in tree.get("m_CharacterRects", [])}
        rep.missing_after = [c for c in need if ord(c) not in have and not c.isspace()]
        if rep.missing_after:
            raise RuntimeError(f"проверка: в шрифте {rep.name} по-прежнему нет {''.join(rep.missing_after[:10])}")
    for (pid, font), rep in zip(fonts, reports):
        if not rep.added:
            continue
        obj = objects.get(pid)
        if obj is None:
            raise RuntimeError(f"проверка: шрифт {rep.name} пропал из файла")
        fresh = TmpFont(obj.read_typetree())
        rep.missing_after = [c for c in need if ord(c) not in fresh.chars() and not c.isspace()]
        pptr = fresh.atlas_pptr() or {}
        tex = objects[int(pptr["m_PathID"])].read()
        w, h = fresh.atlas_size()
        if (tex.m_Width, tex.m_Height) != (w, h) or tex.image.size != (w, h):
            raise RuntimeError(f"проверка: размер атласа {rep.name} не совпадает с описанием шрифта")
        if rep.missing_after:
            raise RuntimeError(f"проверка: в шрифте {rep.name} по-прежнему нет {''.join(rep.missing_after[:10])}")


def needed_chars(translations: Iterable[str]) -> Set[str]:
    """Весь русский алфавит + все не-ASCII символы, реально встречающиеся в переводе."""
    need = set(RUSSIAN) | set(EXTRA)
    for t in translations:
        need.update(ch for ch in t if ord(ch) > 127)
    return need


# ---------- растровые шрифты старого формата (UnityEngine.Font без TTF) ----------

def is_bitmap_font(tree: dict) -> bool:
    return isinstance(tree, dict) and not tree.get("m_FontData") and bool(tree.get("m_CharacterRects"))


def patch_legacy_font(tree: dict, texture, renderer: GlyphRenderer, need: Iterable[str]) -> int:
    """Дорисовать недостающие символы в растровый шрифт Unity (CharacterInfo + текстура)."""
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
    by_code = {int(r["index"]): r for r in rects}
    ref = by_code.get(ord("H")) or rects[0]
    cap = abs(float(ref["vert"]["height"])) - 2  # вместе с отступом 1 px с каждой стороны
    size = max(6.0, cap / renderer.cap_ratio())
    advs = [float(r["advance"]) for r in rects if 65 <= int(r["index"]) <= 122]
    mono = advs[0] if advs and max(advs) - min(advs) < 0.5 else None
    latin = "ABCDEFGHKLMNOPRSTUXYZabcdeghknopstuxyz"
    orig = [float(by_code[ord(c)]["advance"]) for c in latin if ord(c) in by_code]
    xscale = 1.0
    if len(orig) >= 10:
        xscale = max(0.55, min(1.3, (sum(orig) / len(orig)) / renderer.mean_advance(latin, size)))
    glyphs = [renderer.render(c, size, 1, 1.0, sdf=False, xscale=xscale) for c in missing]

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

    template = copy.deepcopy(ref)
    for g in glyphs:
        advance = mono if mono else round(g.advance)
        entry = copy.deepcopy(template)
        entry["index"] = ord(g.char)
        entry["advance"] = float(advance)
        entry["flipped"] = False
        if g.char not in placements:
            entry["uv"] = {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}
            entry["vert"] = {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}
        else:
            px, py = placements[g.char]
            bh, bw = g.bitmap.shape
            region = alpha[py:py + bh, px:px + bw]
            np.maximum(region, g.bitmap, out=region)
            bearing = (mono - g.width) / 2 if mono else g.bearing_x
            entry["uv"] = {"x": px / W, "y": 1 - py / H, "width": bw / W, "height": -bh / H}
            entry["vert"] = {"x": float(round(bearing) - 1), "y": float(round(g.bearing_y) + 1),
                             "width": float(bw), "height": float(-bh)}
        rects.append(entry)
    a8 = Image.fromarray(np.round(alpha * 255).astype(np.uint8), "L")
    texture.set_image(Image.merge("RGBA", (a8, a8, a8, a8)), target_format=texture.m_TextureFormat)
    return len(missing)


# ---------- вся игра ----------

def fix_game(files: Iterable[Path], gen, translations: Iterable[str], backup,
             status: Callable[[str, Optional[float]], None] = lambda m, f=None: None) -> Tuple[List[FontReport], List[str]]:
    """Проверить все шрифты игры и дорисовать кириллицу в TMP-шрифты.

    Возвращает (отчёты по TMP-шрифтам, проблемы, которые починить не удалось).
    """
    from ...fonts.cmap import codepoints
    from ...fonts.library import ensure_font

    regular, bold = ensure_font(), ensure_font(bold=True)
    if regular is None:
        return [], ["Встроенный шрифт PT Sans не найден — кириллица в TMP-шрифты не дорисована."]
    cover = codepoints(regular)
    need = needed_chars(translations)
    unsupported = sorted(c for c in need if ord(c) not in cover)
    need = {c for c in need if ord(c) in cover}
    rend, rend_bold = GlyphRenderer(regular), GlyphRenderer(bold or regular)
    problems: List[str] = []
    if unsupported:
        problems.append("Символы, которых нет во встроенном шрифте, могут не отображаться: " + "".join(unsupported[:20]))

    def write(path: Path, data: bytes) -> None:
        backup.save_original(path)
        path.write_bytes(data)

    reports: List[FontReport] = []
    files = list(files)
    for i, f in enumerate(files):
        status(f"Проверка шрифтов: {f.name}", i / max(1, len(files)))
        try:
            reports += patch_file(f, gen, need, rend, rend_bold, write)
        except Exception as exc:  # noqa: BLE001
            log.warning("Шрифты в %s не исправлены: %s", f.name, exc)
            problems.append(f"{f.name}: шрифты не исправлены ({exc})")
    for r in reports:
        if r.error:
            problems.append(f"Шрифт {r.name} ({r.file}): {r.error}")
    return reports, problems
