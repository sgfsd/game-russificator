"""Кириллица в шрифтах игр: чтение/сборка TrueType и буквы по меркам шрифта."""

import io
import string

import pytest

from russificator.fonts import cyrillic, sfnt

LATIN = string.ascii_letters + string.digits + " .,:!?-"


def _font_from(contours_of, advance_of, chars=LATIN) -> bytes:
    """Шрифт только с символами ``chars`` (основа — PT Sans, его собственные символы из cmap убраны)."""
    base = sfnt.Font(cyrillic.donor_bytes(False))
    base.cmap = {}
    new = [sfnt.NewGlyph(ord(c), advance_of(c), [[(int(round(x)), int(round(y)), on) for x, y, on in cn]
                                                  for cn in contours_of(c)]) for c in chars]
    return sfnt.build(base, new)


def _transformed(sx: float, sy: float, bold: bool = True) -> bytes:
    """Латинский шрифт другого вида: PT Sans, сжатый/растянутый по осям."""
    donor = sfnt.Font(cyrillic.donor_bytes(bold))

    def contours(c):
        g = donor.gid(c)
        return [[(x * sx, y * sy, on) for x, y, on in cn] for cn in donor.contours(g)] if g else []

    return _font_from(contours, lambda c: int(donor.advance(c) * sx) if donor.has(c) else 500)


def _pixel_font(ppem: int = 12, grid: int = 100) -> bytes:
    """Пиксельный латинский шрифт: буквы PT Sans без сглаживания, каждый пиксель — квадрат grid."""
    def contours(c):
        arr, left, top, _ = cyrillic._mono_bitmap(ppem, c, False)
        out = []
        if arr is None:
            return out
        for r, row in enumerate(arr):
            for x, v in enumerate(row):
                if v:
                    X, Y = (left + x) * grid, (top - r - 1) * grid
                    out.append([(X, Y, True), (X, Y + grid, True), (X + grid, Y + grid, True), (X + grid, Y, True)])
        return out

    return _font_from(contours, lambda c: int(round(cyrillic._mono_bitmap(ppem, c, False)[3])) * grid)


def _height(font: sfnt.Font, ch: str) -> float:
    bb = font.bbox(font.gid(ch))
    return bb[3] - bb[1]


def test_rebuild_keeps_every_glyph():
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    data = cyrillic.donor_bytes(False)
    out = sfnt.build(sfnt.Font(data), [])

    def draw(d):
        im = Image.new("L", (900, 80), 0)
        ImageDraw.Draw(im).text((4, 8), "Hamburgefonts Съешь 0123", font=ImageFont.truetype(io.BytesIO(d), 40), fill=255)
        return np.asarray(im)
    assert (draw(data) == draw(out)).all()


def test_cyrillic_matches_latin_height_and_width():
    data = _transformed(0.8, 0.9)
    assert not any(0x400 <= c < 0x500 for c in sfnt.Font(data).cmap)
    out = cyrillic.add_cyrillic(data, cyrillic.needed_chars([]))
    assert out is not None
    font = sfnt.Font(out)
    assert all(font.has(c) for c in cyrillic.RUSSIAN + cyrillic.EXTRA)
    # рост русских букв — ровно как у латинских этого шрифта
    assert _height(font, "Н") == pytest.approx(_height(font, "H"), rel=0.02)
    assert _height(font, "Ш") == pytest.approx(_height(font, "H"), rel=0.03)
    assert font.bbox(font.gid("н"))[3] == pytest.approx(font.bbox(font.gid("x"))[3], rel=0.04)
    # ширина: средняя ширина русских заглавных — того же порядка, что у латинских
    ru = sum(font.advance(c) for c in "БГДЖЗИЛПУФЦЧШЭЮЯ") / 16
    en = sum(font.advance(c) for c in "BCDGHKNPRUZ") / 11
    assert 0.8 < ru / en < 1.35
    # русская «А» — та же буква, что латинская «A»
    assert font.raw_glyph(font.gid("А")) == font.raw_glyph(font.gid("A"))


def test_pixel_font_gets_pixel_letters_of_same_height():
    data = _pixel_font()
    style = cyrillic.measure(data)
    assert style is not None and style.pixel == pytest.approx(100)
    out = cyrillic.add_cyrillic(data, cyrillic.needed_chars([]))
    assert out is not None
    font = sfnt.Font(out)
    for ch in "ЖЩЮбдф":
        pts = [p for c in font.contours(font.gid(ch)) for p in c]
        assert pts and all(on for _, _, on in pts)                       # только прямые углы
        assert all(x % 100 == 0 and y % 100 == 0 for x, y, _ in pts)      # на сетке шрифта
    assert _height(font, "Н") == _height(font, "H")
    assert font.bbox(font.gid("н"))[3] == font.bbox(font.gid("x"))[3]


def test_fake_diaeresis_twin_is_not_used():
    # шрифт, где «Ë» и «ë» ведут не туда (как в DOS-шрифтах CP437): берётся не двойник, а новая буква
    donor = sfnt.Font(cyrillic.donor_bytes(False))
    chars = LATIN + "Ëë"

    def contours(c):
        src = {"Ë": "B", "ë": "g"}.get(c, c)
        g = donor.gid(src)
        return donor.contours(g) if g else []
    data = _font_from(contours, lambda c: donor.advance({"Ë": "B", "ë": "g"}.get(c, c)) or 500, chars)
    font = sfnt.Font(data)
    style = cyrillic.measure(data, font)
    assert not cyrillic._twin_ok(font, style, "Ë") and not cyrillic._twin_ok(font, style, "ë")
    assert cyrillic._twin_ok(font, style, "E")


def test_icon_font_is_left_alone():
    donor = sfnt.Font(cyrillic.donor_bytes(False))
    data = _font_from(lambda c: donor.contours(donor.gid("O")), lambda c: 600, "0123456789")
    assert cyrillic.add_cyrillic(data, cyrillic.needed_chars([])) is None


def test_cubic_to_quadratic_is_close():
    import math
    k = 0.5523 * 100        # четверть окружности радиуса 100
    quads = sfnt._cubic_to_quads((100, 0), (100, k), (k, 100), (0, 100), tol=0.5)
    prev = (100, 0)
    for q, end in quads:
        for t in (0.25, 0.5, 0.75):
            mt = 1 - t
            x = mt * mt * prev[0] + 2 * mt * t * q[0] + t * t * end[0]
            y = mt * mt * prev[1] + 2 * mt * t * q[1] + t * t * end[1]
            assert abs(math.hypot(x, y) - 100) < 0.6
        prev = end


def test_style_from_atlas_boxes():
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    # «атлас»: буквы PT Sans кеглем 40 — мерки должны совпасть с мерками самого шрифта
    f = ImageFont.truetype(io.BytesIO(cyrillic.donor_bytes(False)), 40)
    boxes = {}
    for c in string.ascii_letters:
        l, t, r, b = f.getbbox(c, anchor="ls")
        im = Image.new("L", (r - l, b - t), 0)
        ImageDraw.Draw(im).text((-l, -t), c, font=f, fill=255, anchor="ls")
        boxes[c] = cyrillic.GlyphBox(r - l, b - t, l, -t, f.getlength(c), np.asarray(im) > 127)
    st = cyrillic.style_from_glyphs(40, boxes)
    ref = cyrillic.donor_style(False).scaled(40 / 1000)
    assert st.cap == pytest.approx(ref.cap, abs=1.0)
    assert st.xh == pytest.approx(ref.xh, abs=1.0)
    assert not st.smallcaps
    # шрифт под эти мерки рисует «Н» того же роста, что «H» атласа
    synth = cyrillic.synth_font(st, "НЖн")
    fs = ImageFont.truetype(io.BytesIO(synth), 40)
    l, t, r, b = fs.getbbox("Н", anchor="ls")
    assert b - t == pytest.approx(boxes["H"].height, abs=1)


def test_needed_chars_skip_other_scripts():
    need = cyrillic.needed_chars(["«Ёлки» — №5 ✓", "设置 настройки", "مرحبا"])
    assert {"«", "»", "—", "№", "✓", "Ё"} <= need
    assert not any(ord(c) >= 0x3000 or 0x600 <= ord(c) < 0x700 for c in need)
