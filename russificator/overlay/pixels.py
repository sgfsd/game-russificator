"""Приметы разметки экрана по пикселям кадра: где кончается одна надпись и начинается другая,
и видна ли надпись целиком.

Распознавание видит только текст и может склеить соседние надписи разных элементов интерфейса
(вкладки «Graphics | Input», два выпадающих списка друг под другом) или прочитать обрывок
надписи, срезанной краем кадра. Здесь — общие правила по самой картинке, без привязки к игре:

  * черта рамки (вкладки, кнопки, ячейки, поля) — ровная линия на всю высоту/ширину строк,
    отличная по цвету от фона вокруг: буква так не тянется, над и под ней — поля рамки строки;
  * надпись обрезана, если буквы упираются прямо в край кадра или в однотонную полосу поверх
    (заголовок окна, панель), хотя у целой надписи до края всегда есть поле фона.

Кадр — массив (высота, ширина, 3) в любом порядке каналов: сравниваются только расстояния
между цветами.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .text import Line

Box = Tuple[int, int, int, int]


def frame_array(frame: bytes, w: int, h: int):
    """Кадр BGRA → массив (h, w, 3) без копирования."""
    import numpy as np
    return np.frombuffer(frame, dtype=np.uint8, count=w * h * 4).reshape(h, w, 4)[..., :3]


def thumb(img, rect: Box):
    """Уменьшенная серая копия области кадра (до 8 × 32): по ней видно, что картинка под
    надписью сменилась (реплика исчезла, текст сменился), а мелкое мерцание сглаживается."""
    import numpy as np
    x, y, w, h = rect
    H, W = img.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    g = img[y0:y1, x0:x1].astype(np.float32).mean(axis=2)
    ry, rx = max(1, g.shape[0] // 8), max(1, g.shape[1] // 32)
    g = g[:g.shape[0] // ry * ry, :g.shape[1] // rx * rx]
    return g.reshape(g.shape[0] // ry, ry, g.shape[1] // rx, rx).mean(axis=(1, 3))


def changed(a, b, limit: float = 22.0) -> bool:
    """Картинка под надписью заметно другая, чем была, когда рисовался перевод."""
    import numpy as np
    if a is None or b is None or a.shape != b.shape:
        return a is not b
    return float(np.abs(a - b).mean()) > limit


def cells(img, cell: int = 8):
    """Средняя яркость (по зелёному каналу) в клетках cell × cell — по ней сравниваются кадры:
    где картинка поменялась (допечаталась буква, сменилась реплика, прошла анимация)."""
    import numpy as np
    g = img[:, :, 1]
    h, w = g.shape
    hh, ww = h // cell * cell, w // cell * cell
    if hh == 0 or ww == 0:
        return np.zeros((0, 0), np.float32)
    return g[:hh, :ww].reshape(hh // cell, cell, ww // cell, cell).mean(axis=(1, 3), dtype=np.float32)


def _dominant(px):
    """Самый частый цвет среди пикселей (n, 3) — с точностью до 16 уровней на канал."""
    import numpy as np
    q = (px // 16).astype(np.int32)
    code = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
    values, counts = np.unique(code, return_counts=True)
    top = int(np.argmax(counts))
    return px[code == values[top]].mean(axis=0), counts[top] / max(1, len(px))


def line_colors(img, box: Box) -> Optional[Tuple[Tuple[int, int, int], Tuple[int, int, int]]]:
    """Цвет букв и цвет фона строки (None — строку не разобрать). Фон — самый частый цвет рамки
    строки, буквы — пиксели, заметно отличные от него (берётся середина по удалённости: не края
    сглаживания и не обводка)."""
    import numpy as np
    x, y, w, h = box
    H, W = img.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    px = img[y0:y1, x0:x1, :3].reshape(-1, 3).astype(np.int32)
    if len(px) > 6000:
        px = px[:: len(px) // 6000 + 1]
    bg, _ = _dominant(px)
    dist = np.sqrt(((px - bg) ** 2).sum(axis=1))
    ink = px[dist > 60]
    if len(ink) < 8:
        return None
    d = dist[dist > 60]
    core = ink[d >= np.percentile(d, 50)]
    fg = np.median(core, axis=0)
    return tuple(int(v) for v in fg), tuple(int(v) for v in bg)


def color_distance(a, b) -> float:
    return float(sum((int(p) - int(q)) ** 2 for p, q in zip(a, b)) ** 0.5)


class Probe:
    """Отпечаток надписи на кадре: точки её букв (и фона между ними) с цветами, а ещё цвет самих
    букв и какая доля рамки строк им закрашена.

    Точки сменились (:meth:`changed`) — надпись пропала, сменилась или «шевелится» (дрожь, глитч,
    волна). Отличить помогает цвет букв (:meth:`present`): у анимированной надписи его в рамке
    столько же, а у пропавшей — нет. Всё это — доли миллисекунды на кадр, без распознавания.
    Мигающий значок «дальше» и анимация фона под полупрозрачной рамкой меняют малую долю точек."""

    __slots__ = ("ys", "xs", "colors", "size", "rects", "fg", "ref")

    def __init__(self, ys, xs, colors, size, rects=(), fg=None, ref=0.0):
        self.ys, self.xs, self.colors, self.size = ys, xs, colors, size
        self.rects, self.fg, self.ref = list(rects), fg, ref

    def changed(self, img) -> float:
        """Доля точек, заметно сменивших цвет (1.0 — кадр другого размера)."""
        import numpy as np
        if img.shape[:2] != self.size:
            return 1.0
        cur = img[self.ys, self.xs, :3].astype(np.int16)
        return float((np.abs(cur - self.colors).max(axis=1) > 48).mean())

    def present(self, img) -> float:
        """Сколько цвета букв осталось в рамке надписи по сравнению с тем, когда её прочитали
        (1.0 — столько же; судить не по чему — тоже 1.0)."""
        if self.fg is None or self.ref < 0.02 or img.shape[:2] != self.size:
            return 1.0 if img.shape[:2] == self.size else 0.0
        return _ink_share(img, self.rects, self.fg) / self.ref


def _ink_share(img, rects, fg) -> float:
    """Доля пикселей цвета ``fg`` в рамках (каждый второй пиксель — хватает и быстрее)."""
    import numpy as np
    total = hit = 0
    for x0, y0, x1, y1 in rects:
        px = img[y0:y1:2, x0:x1:2, :3].astype(np.int16)
        if px.size == 0:
            continue
        hit += int((np.abs(px - fg).max(axis=2) <= 50).sum())
        total += px.shape[0] * px.shape[1]
    return hit / total if total else 0.0


def make_probe(img, boxes, limit: int = 400) -> Optional[Probe]:
    """Отпечаток надписи из рамок её строк: до ``limit`` точек букв (ровно по всем строкам) и
    четверть от этого — точек фона внутри рамок (буквы сменились другими — фон тоже меняется)."""
    import numpy as np
    H, W = img.shape[:2]
    ink_y, ink_x, bg_y, bg_x, rects = [], [], [], [], []
    for x, y, w, h in boxes:
        x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
        if x1 - x0 < 3 or y1 - y0 < 3:
            continue
        rects.append((x0, y0, x1, y1))
        crop = img[y0:y1, x0:x1, :3].astype(np.int32)
        bg, _ = _dominant(crop.reshape(-1, 3))
        dist = np.sqrt(((crop - bg) ** 2).sum(axis=2))
        iy, ix = np.nonzero(dist > 60)
        by, bx = np.nonzero(dist < 20)
        ink_y.append(iy + y0)
        ink_x.append(ix + x0)
        bg_y.append(by + y0)
        bg_x.append(bx + x0)
    if not ink_y:
        return None
    iy, ix = np.concatenate(ink_y), np.concatenate(ink_x)
    by, bx = np.concatenate(bg_y), np.concatenate(bg_x)
    if len(iy) < 12:
        return None
    pick_i = np.linspace(0, len(iy) - 1, min(limit, len(iy))).astype(np.intp)
    pick_b = np.linspace(0, len(by) - 1, min(limit // 4, len(by))).astype(np.intp) if len(by) else \
        np.zeros(0, np.intp)
    ys = np.concatenate([iy[pick_i], by[pick_b]])
    xs = np.concatenate([ix[pick_i], bx[pick_b]])
    fg, _ = _dominant(img[iy[pick_i], ix[pick_i], :3].astype(np.int32))    # цвет букв — самый частый у точек букв
    fg = fg.astype(np.int16)
    return Probe(ys, xs, img[ys, xs, :3].astype(np.int16), img.shape[:2], rects, fg, _ink_share(img, rects, fg))


def _rules(lines) -> List[int]:
    """Номера черт среди ``lines`` (столбцов или строк пикселей, массив (n, длина, 3)): линия ровного
    цвета между ровным же фоном, заметно отличная от него с обеих сторон (соседи — через 3 пикселя:
    черта бывает толщиной в пару пикселей). Плавный градиент фона так не выглядит (соседи почти того
    же цвета), просвет между буквами — тоже (соседи проходят через буквы и не ровные)."""
    import numpy as np
    n = lines.shape[0]
    if n < 7:
        return []
    std = lines.std(axis=1).mean(axis=1)
    mean = lines.mean(axis=1)
    out = []
    for i in range(3, n - 3):
        if std[i] >= 12 or std[i - 3] >= 12 or std[i + 3] >= 12:
            continue                        # у черты по обе стороны — ровный фон, а не буквы
        left = float(np.sqrt(((mean[i] - mean[i - 3]) ** 2).sum()))
        right = float(np.sqrt(((mean[i] - mean[i + 3]) ** 2).sum()))
        if min(left, right) > 22:
            out.append(i)
    return out


def split_columns(img, box: Box) -> List[Box]:
    """Рамка строки, разрезанная по вертикальным чертам внутри неё (соседние вкладки или кнопки,
    которые детектор принял за одну строку). Черта у самых краёв рамки (поля) не режет."""
    import numpy as np
    x, y, w, h = box
    H, W = img.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 - x0 < 3 * (y1 - y0) or y1 - y0 < 6:
        return [box]
    region = img[y0:y1, x0:x1].astype(np.int32)
    margin = int(0.5 * (y1 - y0))
    cuts = [c for c in _rules(region.transpose(1, 0, 2)) if margin <= c < (x1 - x0) - margin]
    if not cuts:
        return [box]
    parts, start = [], x0
    for c in cuts:
        if x0 + c - start >= 2:
            parts.append((start, y0, x0 + c - start, y1 - y0))
        start = x0 + c + 1
    if x1 - start >= 2:
        parts.append((start, y0, x1 - start, y1 - y0))
    return [p for p in parts if p[2] >= 0.5 * (y1 - y0)] or [box]


def rule_beside(img, a: Line, b: Line) -> bool:
    """Между соседними по строке кусками ``a`` и ``b`` — вертикальная черта рамки (соседние
    кнопки, вкладки, ячейки таблицы): это разные надписи, а не одна фраза."""
    if img is None:
        return False
    left, right = (a, b) if a.x <= b.x else (b, a)
    hmin = min(a.h, b.h)
    x0 = min(left.x + left.w, right.x) - int(0.3 * hmin)
    y0, y1 = min(a.y, b.y), max(a.y + a.h, b.y + b.h)
    width = max(right.x, left.x + left.w) - x0 + int(0.3 * hmin)
    if y1 <= y0:
        return False
    return len(split_columns(img, (x0, y0, max(width, 3 * (y1 - y0)), y1 - y0))) > 1


def rule_between(img, a: Line, b: Line) -> bool:
    """Между строкой ``a`` и строкой ``b`` под ней — горизонтальная черта (граница полей, кнопок,
    ячеек): это разные надписи, а не строки одного абзаца."""
    import numpy as np
    if img is None:
        return False
    top, bottom = (a, b) if a.y <= b.y else (b, a)
    hh = (a.h + b.h) / 2
    x0, x1 = max(top.x, bottom.x), min(top.x + top.w, bottom.x + bottom.w)
    y0, y1 = int(top.y + top.h - 0.25 * hh), int(bottom.y + 0.25 * hh)
    H, W = img.shape[:2]
    x0, x1, y0, y1 = max(0, x0), min(W, x1), max(0, y0), min(H, y1)
    if x1 - x0 < 6 or y1 - y0 < 1:
        return False
    # в промежутке между строками одного абзаца ровные строки пикселей — одного цвета фона (или
    # плавно меняются по градиенту); между полями, кнопками, ячейками — рамки и щели другого цвета:
    # рядом (до 3 пикселей) лежат ровные строки заметно разного цвета
    region = img[y0:y1, x0:x1].astype(np.int32)
    flat = [(i, row.mean(axis=0)) for i, row in enumerate(region) if float(row.std(axis=0).mean()) < 12]
    for k, (i, a) in enumerate(flat):
        for j, b in flat[k + 1:]:
            if j - i > 3:
                break
            if float(np.sqrt(((a - b) ** 2).sum())) > 22:
                return True
    return False


def cut_side(ln: Line, img) -> Optional[str]:
    """Сторона, с которой надпись обрезана (None — видна целиком).

    Граница — край кадра или ровная однотонная полоса у самого края рамки строки, не того цвета,
    что фон надписи (фон — самый частый цвет в рамке), и за ней между буквами этот цвет не
    продолжается (иначе это подложка под надписью — наклейка, плашка, — а не то, что её закрывает).
    Вдоль стороны смотрится только середина рамки: там сами буквы, а не захваченные рамкой соседи."""
    import numpy as np
    H, W = img.shape[:2]
    x0, y0, x1, y1 = max(0, ln.x), max(0, ln.y), min(W, ln.x + ln.w), min(H, ln.y + ln.h)
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None
    box = img[y0:y1, x0:x1].astype(np.int32)
    q = (box // 16).reshape(-1, 3)
    code = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
    values, counts = np.unique(code, return_counts=True)
    bg = box.reshape(-1, 3)[code == values[int(np.argmax(counts))]].mean(axis=0)

    def inked(row) -> bool:
        return float((np.sqrt(((row - bg) ** 2).sum(axis=1)) > 40).mean()) >= 0.08

    hh, ww = box.shape[:2]
    mx = slice(int(ww * 0.1), max(int(ww * 0.1) + 1, int(ww * 0.9)))
    my = slice(int(hh * 0.25), max(int(hh * 0.25) + 1, int(hh * 0.75)))
    cols = box[my].transpose(1, 0, 2)
    sides = (("top", box[:, mx], y0 <= 1), ("bottom", box[::-1, mx], y1 >= H - 1),
             ("left", cols, x0 <= 1), ("right", cols[::-1], x1 >= W - 1))
    for name, rows, at_edge in sides:
        n = rows.shape[0]
        if n < 4:
            continue
        if at_edge and inked(rows[0]) and inked(rows[1]):
            return name                     # буквы уходят за край кадра
        if ln.rot is not None:
            continue                        # у наклонной строки в описанной рамке много чужого
        # с края рамки: пара строк шума, затем однотонная полоса (первая же), сразу за ней — буквы
        run, color = 0, None
        for r in range(n // 2):
            row = rows[r]
            if float(row.std(axis=0).mean()) < 10:
                mean = row.mean(axis=0)
                if color is not None and float(np.abs(mean - color).max()) >= 12:
                    break                   # за полосой — ещё одна ровная (рамка, поле): не обрыв
                run, color = run + 1, mean
                continue
            if run == 0 and r < 3:
                continue
            if run >= 2 and float(np.sqrt(((color - bg) ** 2).sum())) >= 40 and inked(row) and \
                    r + 1 < n and inked(rows[r + 1]) and \
                    float((np.sqrt(((row - color) ** 2).sum(axis=1)) < 30).mean()) < 0.2:
                return name
            break
    return None
