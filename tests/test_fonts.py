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


# --- мелкий пиксельный шрифт: русские буквы из его же латиницы ---

_PX5 = {   # латиница 4x5 в духе 04B_03 (строки сверху вниз)
    "A": ".##./#..#/####/#..#/#..#", "B": "###./#..#/###./#..#/###.", "C": ".###/#.../#.../#.../.###",
    "D": "###./#..#/#..#/#..#/###.", "E": "####/#.../###./#.../####", "F": "####/#.../###./#.../#...",
    "G": ".###/#.../#.##/#..#/.###", "H": "#..#/#..#/####/#..#/#..#", "I": "###/.#./.#./.#./###",
    "J": "...#/...#/...#/#..#/.##.", "K": "#..#/#.#./##../#.#./#..#", "L": "#.../#.../#.../#.../####",
    "M": "#...#/##.##/#.#.#/#...#/#...#", "N": "#..#/##.#/#.##/#..#/#..#", "O": ".##./#..#/#..#/#..#/.##.",
    "P": "###./#..#/###./#.../#...", "Q": ".##./#..#/#..#/#.##/.###", "R": "###./#..#/###./#..#/#..#",
    "S": ".###/#.../.##./...#/###.", "T": "###/.#./.#./.#./.#.", "U": "#..#/#..#/#..#/#..#/.##.",
    "V": "#..#/#..#/#..#/.#.#/..#.", "W": "#...#/#...#/#.#.#/##.##/#...#", "X": "#..#/#..#/.##./#..#/#..#",
    "Y": "#.#/#.#/.#./.#./.#.", "Z": "####/...#/.##./#.../####",
    "3": "###./...#/.##./...#/###.", "6": ".##./#.../###./#..#/.##.", "0": ".##./#..#/#..#/#..#/.##.",
}


def _bitmap_font(glyphs: dict, grid: int = 100) -> bytes:
    """Пиксельный шрифт из картинок «#.»: каждая клетка — квадрат grid, низ — на базовой линии.
    Строчные — те же буквы без второй строки (маленькие заглавные)."""
    rows_of = {}
    for ch, pic in glyphs.items():
        rows_of[ch] = pic.split("/")
        if ch.isalpha():
            r = pic.split("/")
            rows_of[ch.lower()] = [r[0]] + r[2:]

    def contours(c):
        rows = rows_of.get(c)
        if not rows:
            return []
        out, h = [], len(rows)
        for r, row in enumerate(rows):
            for x, v in enumerate(row):
                if v == "#":
                    X, Y = x * grid, (h - r - 1) * grid
                    out.append([(X, Y, True), (X, Y + grid, True), (X + grid, Y + grid, True), (X + grid, Y, True)])
        return out

    return _font_from(contours, lambda c: (len(rows_of[c][0]) + 1) * grid if c in rows_of else 4 * grid,
                      "".join(rows_of) + " ")


def _cells(font: sfnt.Font, ch: str):
    from russificator.fonts import pixel_cyrillic
    return pixel_cyrillic.rasterize(font.contours(font.gid(ch)), 100, (0.0, 0.0))


def _pic(b) -> str:
    return "/".join("".join("#" if v else "." for v in row) for row in b.a)


def test_small_pixel_font_letters_built_from_its_latin():
    from russificator.fonts import pixel_cyrillic
    data = _bitmap_font(_PX5)
    style = cyrillic.measure(data)
    assert style is not None and style.pixel == pytest.approx(100) and style.cap_px == 5
    font = sfnt.Font(cyrillic.add_cyrillic(data, cyrillic.needed_chars([])))
    cells = {c: _cells(font, c) for c in cyrillic.RUSSIAN}
    # отражения и перестановки латиницы шрифта — клетка в клетку
    assert _pic(cells["И"]) == "#..#/#.##/##.#/#..#/#..#"                 # зеркальная N
    assert _pic(cells["Я"]) == ".###/#..#/.###/#..#/#..#"                 # зеркальная R
    assert _pic(cells["Г"]) == "####/#.../#.../#.../#..."                 # перевёрнутая L
    assert _pic(cells["Ь"]) == "#.../#.../###./#..#/###."                 # чаша P внизу
    assert _pic(cells["З"]) == _pic(_cells(font, "3"))
    # высота — как у латиницы; у Д, Ц, Щ — выносной вниз, у Й, Ё — значок сверху
    for ch in "БГЖЗИЛПУФЧШЪЫЬЭЮЯ":
        assert cells[ch].h == 5 and cells[ch].top == 5, ch
    for ch in "ДЦЩ":
        assert cells[ch].top == 5 and cells[ch].bottom == -1, ch
    # у шрифта нет заглавных с диакритикой (строки меню могут стоять вплотную): знак — в высоте
    # заглавной; Й — кратка вплотную над сжатой И, Ё без места для точек — как Е
    for ch in "ЙЁ":
        assert cells[ch].bottom == 0 and cells[ch].top == 5, ch
    assert _pic(cells["Й"]).startswith(".##./#..#/")
    assert _pic(cells["Ё"]) == _PX5["E"]
    # а если в шрифте есть Ë со знаком над буквой — Й и Ё так же поднимаются над заглавной
    with_acc = dict(_PX5, **{"Ë": "#..#/..../####/#.../###./#.../####"})
    f2 = sfnt.Font(cyrillic.add_cyrillic(_bitmap_font(with_acc), "ЙЁ"))
    assert _cells(f2, "Й").top == 7 and _pic(_cells(f2, "Й")).startswith(".##./..../#..#/")
    # ни одна буква не слиплась в кляксу и не распалась
    for ch in "БЖФШЩЮЭЯ":
        comp, holes = pixel_cyrillic.topology(cells[ch].a)
        assert comp == 1, ch
        assert cells[ch].a.mean() < 0.7, ch
    assert pixel_cyrillic.topology(cells["Ф"].a)[1] == 2
    assert pixel_cyrillic.topology(cells["Ы"].a)[0] == 2
    assert _pic(cells["Ж"]) == _pic(type(cells["Ж"])(cells["Ж"].a[:, ::-1], 5))     # симметрична
    assert list(cells["Ш"].a.sum(axis=0)).count(5) == 3                   # три стойки
    # строчные (шрифт «капителью») — те же формы ростом строчных, без разрывов
    for ch in "бгжзилпфчшъыьэюя":
        b = cells[ch]
        assert b.top == 4 and b.bottom == 0 and pixel_cyrillic.topology(b.a)[0] == (2 if ch == "ы" else 1), ch


def test_pixel_n_without_diagonal_gives_real_i():
    # в узком шрифте N нарисована как «П» со скосом: зеркальная N читалась бы как «П»
    glyphs = dict(_PX5, H="#.#/#.#/###/#.#/#.#", N="##./#.#/#.#/#.#/#.#")
    font = sfnt.Font(cyrillic.add_cyrillic(_bitmap_font(glyphs), "И"))
    i = _cells(font, "И")
    assert _pic(i) != _pic(type(i)(_cells(font, "N").a[:, ::-1], 5))
    assert i.h == 5 and i.a[1:4, 1:-1].any(axis=1).sum() >= 2           # диагональ через несколько строк


def test_fit_rows_keeps_letters_whole():
    import numpy as np
    from russificator.fonts import pixel_cyrillic as pc

    def arr(pic):
        return np.array([[c == "#" for c in row] for row in pic.split("/")])
    k = pc.fit_rows(arr("#..#/#.#./##../#.#./#..#"), 4)
    assert pc.topology(k)[0] == 1                                         # стык диагоналей со стойкой цел
    b = pc.fit_rows(arr("###./#..#/###./#..#/###."), 4)
    assert "/".join("".join("#" if v else "." for v in r) for r in b) == "###./###./#..#/###."
    h = pc.fit_rows(arr("#..#/#..#/####/#..#/#..#"), 4)
    assert h.sum(axis=1).max() == 4 and pc.topology(h) == (1, 0)          # перекладина не съедена


def test_pixel_atlas_letters_built_from_atlas_latin():
    """Пиксельный шрифт без TTF (атлас TextMeshPro): русские буквы — из латиницы атласа."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    k = 4                                              # клетка шрифта = 4x4 пикселя атласа
    boxes = {}
    for ch, pic in _PX5.items():
        if not ch.isalpha():
            continue
        cells = np.array([[c == "#" for c in row] for row in pic.split("/")])
        mask = np.kron(cells, np.ones((k, k), dtype=bool))
        h, w = mask.shape
        boxes[ch] = cyrillic.GlyphBox(w, h, 0, h, w + k, mask)
    style = cyrillic.style_from_glyphs(8 * k, boxes)
    assert style is not None and style.pixel == pytest.approx(k, abs=0.11)
    font = ImageFont.truetype(io.BytesIO(cyrillic.synth_font(style, "ИЖ", boxes)), 8 * k)

    def cells_of(ch):
        l, t, r, b = font.getbbox(ch, anchor="ls")
        im = Image.new("L", (r - l, b - t), 0)
        d = ImageDraw.Draw(im)
        d.fontmode = "1"
        d.text((-l, -t), ch, font=font, fill=255, anchor="ls")
        a = (np.asarray(im) > 127)[k // 2::k, k // 2::k]
        ys, xs = np.nonzero(a)
        return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    assert "/".join("".join("#" if v else "." for v in row) for row in cells_of("И")) == "#..#/#.##/##.#/#..#/#..#"
    assert cells_of("Ж").shape == (5, 7)


def test_pixel_letters_are_single_outlines():
    """Буква из клеток обводится по границе — контур на каждый кусок и на каждый просвет (как у родных
    букв пиксельного шрифта), а не россыпь прямоугольников: иначе хинтинг Unity двигает их края
    по-разному и буква «плывёт»."""
    import numpy as np
    from russificator.fonts import pixel_cyrillic

    def pieces(a):                      # куски по 4 соседям (касание углом — разные контуры)
        seen, n = np.zeros_like(a), 0
        for y, x in zip(*np.nonzero(a)):
            if seen[y, x]:
                continue
            n += 1
            stack = [(y, x)]
            seen[y, x] = True
            while stack:
                cy, cx = stack.pop()
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < a.shape[0] and 0 <= nx < a.shape[1] and a[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        return n
    font = sfnt.Font(cyrillic.add_cyrillic(_bitmap_font(_PX5), cyrillic.needed_chars([])))
    for ch in "БДЖИЙЛФШЩЫЮЯ":
        b = _cells(font, ch)
        holes = pixel_cyrillic.topology(b.a)[1]
        assert len(font.contours(font.gid(ch))) <= pieces(b.a) + holes, ch
    for ch in "ГПШЩЦЧ":
        assert len(font.contours(font.gid(ch))) == 1, ch
    # две клетки, касающиеся углом, — два отдельных контура
    two = cyrillic._cell_contours(np.array([[True, False], [False, True]]), 0, 2, 100, 0, 0)
    assert len(two) == 2 and all(len(c) == 4 for c in two)


def test_fullwidth_cyrillic_of_cjk_font_is_redrawn_proportional():
    """Китайский шрифт кладёт кириллицу в клетку иероглифа (FangZhengLiBian, CardCultivation): «п» шириной
    в кегль при латинской «n» вдвое уже — текст выходит вразрядку. Такие буквы рисуются заново по меркам
    латиницы, и шрифт перестаёт быть «полноширинным»."""
    donor = sfnt.Font(cyrillic.donor_bytes(False))
    em = donor.upem

    def contours(c):
        g = donor.gid(c)
        return [[(x, y, on) for x, y, on in cn] for cn in donor.contours(g)] if g else []

    def advance(c):
        return em if c in cyrillic.RUSSIAN else donor.advance(c) or em // 2   # кириллица — в клетке «кегль»

    data = _font_from(contours, advance, LATIN + cyrillic.RUSSIAN)
    font = sfnt.Font(data)
    assert cyrillic.fullwidth_font(font)
    assert set(cyrillic.missing(font, "Привет")) == set("Привет")
    new = sfnt.Font(cyrillic.add_cyrillic(data, cyrillic.RUSSIAN))
    assert not cyrillic.fullwidth_font(new)
    assert new.advance("п") < 0.75 * em and new.advance("A") == font.advance("A")
    # моноширинный шрифт (и латиница, и кириллица в одной клетке) — не китайский случай
    assert not cyrillic.fullwidth_letters([600.0] * 30, [600.0] * 30, 1000)
    assert cyrillic.fullwidth_letters([500.0 + i for i in range(30)], [1000.0] * 30, 1000)
