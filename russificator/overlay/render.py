"""Картинка оверлея: полупрозрачные плашки с переводом поверх оригинального текста.

Размер шрифта подбирается под место: начинаем с высоты строки оригинала и
уменьшаем, пока русский текст (он длиннее английского) не поместится в рамку
абзаца; если не помещается и на минимальном размере — плашка растёт вниз.
Картинка отдаётся как BGRA с премультиплицированной альфой — так её принимает
UpdateLayeredWindow.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

Rect = Tuple[int, int, int, int]


@dataclass
class Style:
    font_scale: float = 1.0      # множитель размера шрифта
    opacity: float = 0.86        # непрозрачность плашки 0..1
    min_px: int = 11


@dataclass
class Item:
    rect: Rect                   # рамка оригинального текста (в координатах картинки)
    text: str                    # перевод
    line_h: float                # высота строки оригинала


def _font_files(bold: bool) -> List[Path]:
    out = []
    if sys.platform == "win32":
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        out += [fonts / ("seguisb.ttf" if bold else "segoeui.ttf"), fonts / "arial.ttf"]
    res = Path(__file__).resolve().parents[1] / "resources" / "fonts"
    out.append(res / ("PT_Sans-Web-Bold.ttf" if bold else "PT_Sans-Web-Regular.ttf"))
    return [p for p in out if p.is_file()]


@lru_cache(maxsize=64)
def font(px: int, bold: bool = True):
    from PIL import ImageFont
    for f in _font_files(bold):
        try:
            return ImageFont.truetype(str(f), px)
        except OSError:
            continue
    return ImageFont.load_default()


def wrap(text: str, fnt, width: float) -> List[str]:
    """Перенос по словам по ширине в пикселях (слово длиннее строки — режется)."""
    lines: List[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            cand = (cur + " " + word) if cur else word
            if fnt.getlength(cand) <= width or not cur:
                if not cur and fnt.getlength(word) > width:
                    # очень длинное слово — режем по буквам
                    piece = ""
                    for ch in word:
                        if fnt.getlength(piece + ch) > width and piece:
                            lines.append(piece)
                            piece = ch
                        else:
                            piece += ch
                    cur = piece
                else:
                    cur = cand
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return [ln for ln in lines if ln] or [""]


def layout(item: Item, canvas: Tuple[int, int], style: Style):
    """Где и каким шрифтом рисовать перевод: (рамка плашки, шрифт, строки, размер)."""
    cw, ch = canvas
    x, y, w, h = item.rect
    lh = max(8.0, item.line_h)
    pad = int(max(4, lh * 0.28))
    box_w = min(cw, max(int(w + pad * 2), int(lh * 4)))
    box_x = max(0, min(int(x - pad), cw - box_w))
    size = int(max(style.min_px, min(72, lh * 0.8 * style.font_scale)))
    limit_h = max(h, lh) * 1.35 + pad * 2
    while True:
        fnt = font(size)
        lines = wrap(item.text, fnt, box_w - pad * 2)
        text_h = len(lines) * size * 1.22
        if text_h + pad * 2 <= limit_h or size <= style.min_px:
            break
        size -= 1
    # плашка закрывает весь абзац оригинала (иначе короткий перевод оставил бы видными его нижние строки)
    box_h = int(min(ch, max(text_h, h) + pad * 2))
    box_y = max(0, min(int(y - pad), ch - box_h))
    return (box_x, box_y, box_w, box_h), fnt, lines, size, pad


def plate_rects(canvas: Tuple[int, int], items: Sequence[Item], style: Optional[Style] = None) -> List[Rect]:
    """Где будут плашки (x, y, w, h) — чтобы закрасить их в снимке экрана перед распознаванием."""
    style = style or Style()
    out: List[Rect] = []
    for it in items:
        if it.text:
            (bx, by, bw, bh), *_ = layout(it, canvas, style)
            out.append((bx - 2, by - 2, bw + 5, bh + 5))
    return out


def render(canvas: Tuple[int, int], items: Sequence[Item], style: Optional[Style] = None):
    """Картинка RGBA размера canvas с плашками перевода."""
    from PIL import Image, ImageDraw
    style = style or Style()
    img = Image.new("RGBA", canvas, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    alpha = int(max(0.2, min(1.0, style.opacity)) * 255)
    for it in items:
        if not it.text:
            continue
        (bx, by, bw, bh), fnt, lines, size, pad = layout(it, canvas, style)
        radius = max(4, int(size * 0.35))
        draw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=radius, fill=(14, 16, 24, alpha),
                               outline=(139, 123, 255, min(255, alpha // 2 + 40)), width=1)
        text_h = len(lines) * int(size * 1.22)
        ty = by + max(pad, (bh - text_h) // 2)          # по центру плашки, если она выше перевода
        for ln in lines:
            draw.text((bx + pad + 1, ty + 1), ln, font=fnt, fill=(0, 0, 0, 200))
            draw.text((bx + pad, ty), ln, font=fnt, fill=(245, 246, 250, 255))
            ty += int(size * 1.22)
    return img


#: прозрачный цвет окна оверлея (как win32.COLOR_KEY): синий, зелёный, красный
KEY_BGR = (255, 0, 254)


def to_colorkey(img, under=None) -> bytes:
    """RGBA → непрозрачная BGRA для окна с цветовым ключом: прозрачное — ключевой цвет,
    полупрозрачное (плашка, сглаженный край, тень) смешивается с кадром игры под ним ``under``
    (массив высота × ширина × BGRA того же размера) — окно с ключом само смешивать не умеет."""
    import numpy as np
    a = np.asarray(img.convert("RGBA"), dtype=np.uint16)
    alpha = a[..., 3:4]
    rgb = a[..., :3]
    if under is not None and under.shape[:2] == a.shape[:2]:
        base = under[..., 2::-1].astype(np.uint16)
        rgb = (rgb * alpha + base * (255 - alpha) + 127) // 255
    out = np.empty(a.shape[:2] + (4,), np.uint8)
    out[..., 0] = rgb[..., 2]
    out[..., 1] = rgb[..., 1]
    out[..., 2] = rgb[..., 0]
    out[..., 3] = 255
    kb, kg, kr = KEY_BGR
    same = (out[..., 0] == kb) & (out[..., 1] == kg) & (out[..., 2] == kr)
    out[..., 0][same] = kb - 1                          # настоящий пиксель картинки не должен пропасть
    clear = a[..., 3] < (8 if under is not None else 128)
    out[clear] = (kb, kg, kr, 0)
    return out.tobytes()


def to_bgra_premultiplied(img) -> bytes:
    """RGBA → BGRA с премультиплицированной альфой (формат UpdateLayeredWindow)."""
    try:
        import numpy as np
        a = np.asarray(img, dtype=np.uint16)
        out = np.empty(a.shape, dtype=np.uint8)
        alpha = a[..., 3:4]
        out[..., 0:3] = (a[..., 2::-1] * alpha // 255).astype(np.uint8)
        out[..., 3] = a[..., 3].astype(np.uint8)
        return out.tobytes()
    except ImportError:
        r, g, b, al = img.split()
        from PIL import Image, ImageChops
        r = ImageChops.multiply(r, al)
        g = ImageChops.multiply(g, al)
        b = ImageChops.multiply(b, al)
        return Image.merge("RGBA", (b, g, r, al)).tobytes()
