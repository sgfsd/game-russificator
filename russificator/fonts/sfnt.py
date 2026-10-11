"""Чтение и сборка шрифтов TrueType/OpenType без сторонних библиотек.

Нужно, чтобы дописать русские буквы прямо в шрифт игры: тогда они рисуются тем же
движком, того же размера и с теми же межстрочными интервалами, что и родной текст, —
а не системным Arial другого роста, как получается, когда Unity не находит буквы в шрифте.

Что умеет:
  * читать таблицы, метрики, cmap, контуры глифов TrueType (``glyf``, включая составные)
    и CFF (``CFF ``, Type 2 charstrings, включая CID-шрифты) — кубические кривые CFF
    переводятся в квадратичные;
  * собирать шрифт TrueType заново: исходные глифы остаются как были (с теми же номерами,
    поэтому кернинг GPOS/kern продолжает работать), новые дописываются в конец, cmap,
    hmtx, maxp, hhea, OS/2, post и head пересчитываются. CFF-шрифт при этом переводится
    в TrueType целиком (FreeType в Unity читает оба вида одинаково).
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float, bool]          # x, y, точка на контуре (False — управляющая)
Contour = List[Point]


class FontError(ValueError):
    """Шрифт не читается или его нельзя дополнить."""


# ---------- общие таблицы ----------

def parse_cmap(table: bytes) -> Dict[int, int]:
    """Код символа -> номер глифа из лучшей юникодной подтаблицы (форматы 0, 4, 6, 12)."""
    if len(table) < 4:
        return {}
    n = struct.unpack_from(">H", table, 2)[0]
    best = None
    for i in range(n):
        pid, eid, off = struct.unpack_from(">HHI", table, 4 + i * 8)
        if off + 2 > len(table):
            continue
        fmt = struct.unpack_from(">H", table, off)[0]
        unicode_ = pid == 0 or (pid == 3 and eid in (1, 10))
        symbol = pid == 3 and eid == 0
        if fmt not in (0, 4, 6, 12) or not (unicode_ or symbol or pid == 1):
            continue
        score = (3 if unicode_ else 1 if symbol else 0) * 10 + {12: 3, 4: 2, 6: 1, 0: 0}[fmt]
        if best is None or score > best[0]:
            best = (score, off, fmt)
    if best is None:
        return {}
    _, off, fmt = best
    out: Dict[int, int] = {}
    try:
        if fmt == 0:
            for c in range(256):
                gid = table[off + 6 + c]
                if gid:
                    out[c] = gid
        elif fmt == 6:
            first, count = struct.unpack_from(">HH", table, off + 6)
            for i in range(count):
                gid = struct.unpack_from(">H", table, off + 10 + 2 * i)[0]
                if gid:
                    out[first + i] = gid
        elif fmt == 4:
            seg_x2 = struct.unpack_from(">H", table, off + 6)[0]
            segs = seg_x2 // 2
            ends = struct.unpack_from(f">{segs}H", table, off + 14)
            starts = struct.unpack_from(f">{segs}H", table, off + 16 + seg_x2)
            deltas = struct.unpack_from(f">{segs}h", table, off + 16 + 2 * seg_x2)
            ro_pos = off + 16 + 3 * seg_x2
            ranges = struct.unpack_from(f">{segs}H", table, ro_pos)
            for i in range(segs):
                for c in range(starts[i], ends[i] + 1):
                    if c == 0xFFFF:
                        continue
                    if ranges[i] == 0:
                        gid = (c + deltas[i]) & 0xFFFF
                    else:
                        addr = ro_pos + 2 * i + ranges[i] + 2 * (c - starts[i])
                        if addr + 2 > len(table):
                            continue
                        gid = struct.unpack_from(">H", table, addr)[0]
                        if gid:
                            gid = (gid + deltas[i]) & 0xFFFF
                    if gid:
                        out[c] = gid
        else:  # 12
            n_groups = struct.unpack_from(">I", table, off + 12)[0]
            for i in range(n_groups):
                start, end, gid0 = struct.unpack_from(">III", table, off + 16 + 12 * i)
                if end - start > 0x10FFFF:
                    break
                for c in range(start, end + 1):
                    out[c] = gid0 + c - start
    except struct.error:
        pass
    if best[0] < 30 and out and min(out) >= 0xF000:   # символьный шрифт: коды сдвинуты в F0xx
        out = {c - 0xF000: g for c, g in out.items()}
    return out


def _checksum(data: bytes) -> int:
    padded = data + b"\0" * (-len(data) % 4)
    return sum(struct.unpack(f">{len(padded) // 4}I", padded)) & 0xFFFFFFFF


# ---------- CFF ----------

def _read_index(data: bytes, pos: int) -> Tuple[List[bytes], int]:
    count = struct.unpack_from(">H", data, pos)[0]
    if count == 0:
        return [], pos + 2
    off_size = data[pos + 2]
    offs = []
    p = pos + 3
    for _ in range(count + 1):
        offs.append(int.from_bytes(data[p:p + off_size], "big"))
        p += off_size
    base = p - 1
    items = [data[base + offs[i]:base + offs[i + 1]] for i in range(count)]
    return items, base + offs[-1]


def _parse_dict(data: bytes) -> Dict[int, List[float]]:
    out: Dict[int, List[float]] = {}
    ops: List[float] = []
    i = 0
    while i < len(data):
        b0 = data[i]
        if b0 <= 21:
            if b0 == 12:
                key = 1200 + data[i + 1]
                i += 2
            else:
                key = b0
                i += 1
            out[key] = ops
            ops = []
        elif b0 == 28:
            ops.append(struct.unpack_from(">h", data, i + 1)[0])
            i += 3
        elif b0 == 29:
            ops.append(struct.unpack_from(">i", data, i + 1)[0])
            i += 5
        elif b0 == 30:
            s = ""
            i += 1
            done = False
            while not done:
                byte = data[i]
                i += 1
                for nib in (byte >> 4, byte & 15):
                    if nib == 0xF:
                        done = True
                        break
                    s += "0123456789.EE?-"[nib] + ("-" if nib == 0xC else "")
            try:
                ops.append(float(s.replace("?", "")))
            except ValueError:
                ops.append(0.0)
        elif 32 <= b0 <= 246:
            ops.append(b0 - 139)
            i += 1
        elif 247 <= b0 <= 250:
            ops.append((b0 - 247) * 256 + data[i + 1] + 108)
            i += 2
        elif 251 <= b0 <= 254:
            ops.append(-(b0 - 251) * 256 - data[i + 1] - 108)
            i += 2
        else:
            i += 1
    return out


def _bias(subrs: Sequence[bytes]) -> int:
    n = len(subrs)
    return 107 if n < 1240 else 1131 if n < 33900 else 32768


class _Cff:
    """Контуры глифов из таблицы CFF (Type 2 charstrings)."""

    def __init__(self, data: bytes):
        hdr = data[2]
        _, pos = _read_index(data, hdr)
        tops, pos = _read_index(data, pos)
        _, pos = _read_index(data, pos)
        self.gsubrs, _ = _read_index(data, pos)
        if not tops:
            raise FontError("в CFF нет верхнего словаря")
        top = _parse_dict(tops[0])
        if 17 not in top:
            raise FontError("в CFF нет глифов")
        self.charstrings, _ = _read_index(data, int(top[17][0]))
        self.data = data
        self.fd_select: Optional[List[int]] = None
        self.privates: List[List[bytes]] = []
        if 1236 in top:      # FDArray — CID-шрифт
            fds, _ = _read_index(data, int(top[1236][0]))
            for fd in fds:
                self.privates.append(self._subrs(_parse_dict(fd)))
            self.fd_select = self._read_fd_select(int(top[1237][0]), len(self.charstrings))
        else:
            self.privates.append(self._subrs(top))

    def _subrs(self, d: Dict[int, List[float]]) -> List[bytes]:
        if 18 not in d or len(d[18]) < 2:
            return []
        size, off = int(d[18][0]), int(d[18][1])
        priv = _parse_dict(self.data[off:off + size])
        if 19 not in priv:
            return []
        subrs, _ = _read_index(self.data, off + int(priv[19][0]))
        return subrs

    def _read_fd_select(self, pos: int, n: int) -> List[int]:
        fmt = self.data[pos]
        if fmt == 0:
            return list(self.data[pos + 1:pos + 1 + n])
        out = [0] * n
        if fmt == 3:
            n_ranges = struct.unpack_from(">H", self.data, pos + 1)[0]
            p = pos + 3
            for i in range(n_ranges):
                first, fd = struct.unpack_from(">HB", self.data, p)
                nxt = struct.unpack_from(">H", self.data, p + 3)[0]
                for g in range(first, min(nxt, n)):
                    out[g] = fd
                p += 3
        return out

    def contours(self, gid: int) -> List[List[tuple]]:
        """Контуры глифа: список команд ("L", x, y) / ("C", x1, y1, x2, y2, x, y), первая — ("M", x, y)."""
        if gid >= len(self.charstrings):
            return []
        fd = self.fd_select[gid] if self.fd_select and gid < len(self.fd_select) else 0
        subrs = self.privates[fd] if fd < len(self.privates) else []
        st = _T2State()
        self._run(self.charstrings[gid], st, subrs, 0)
        st.close()
        return st.paths

    def _run(self, code: bytes, st: "_T2State", subrs: List[bytes], depth: int) -> bool:
        """Выполнить charstring. True — встретился endchar."""
        if depth > 10:
            return True
        stack = st.stack
        i = 0
        n = len(code)
        while i < n:
            b0 = code[i]
            if b0 >= 32 or b0 == 28 or b0 == 255:
                if b0 == 28:
                    stack.append(struct.unpack_from(">h", code, i + 1)[0])
                    i += 3
                elif b0 == 255:
                    stack.append(struct.unpack_from(">i", code, i + 1)[0] / 65536.0)
                    i += 5
                elif b0 <= 246:
                    stack.append(b0 - 139)
                    i += 1
                elif b0 <= 250:
                    stack.append((b0 - 247) * 256 + code[i + 1] + 108)
                    i += 2
                else:
                    stack.append(-(b0 - 251) * 256 - code[i + 1] - 108)
                    i += 2
                continue
            i += 1
            if b0 in (1, 3, 18, 23):                     # hstem, vstem, hstemhm, vstemhm
                st.take_width(len(stack) % 2 == 1)
                st.nstems += len(stack) // 2
                stack.clear()
            elif b0 in (19, 20):                         # hintmask, cntrmask
                st.take_width(len(stack) % 2 == 1)
                st.nstems += len(stack) // 2
                stack.clear()
                i += (st.nstems + 7) // 8
            elif b0 == 21:                               # rmoveto
                st.take_width(len(stack) > 2)
                st.move(stack[-2], stack[-1])
                stack.clear()
            elif b0 == 22:                               # hmoveto
                st.take_width(len(stack) > 1)
                st.move(stack[-1], 0)
                stack.clear()
            elif b0 == 4:                                # vmoveto
                st.take_width(len(stack) > 1)
                st.move(0, stack[-1])
                stack.clear()
            elif b0 == 5:                                # rlineto
                for k in range(0, len(stack) - 1, 2):
                    st.line(stack[k], stack[k + 1])
                stack.clear()
            elif b0 in (6, 7):                           # hlineto, vlineto
                horiz = b0 == 6
                for v in stack:
                    st.line(v, 0) if horiz else st.line(0, v)
                    horiz = not horiz
                stack.clear()
            elif b0 == 8:                                # rrcurveto
                for k in range(0, len(stack) - 5, 6):
                    st.curve(*stack[k:k + 6])
                stack.clear()
            elif b0 == 24:                               # rcurveline
                k = 0
                while len(stack) - k >= 8:
                    st.curve(*stack[k:k + 6])
                    k += 6
                if len(stack) - k >= 2:
                    st.line(stack[k], stack[k + 1])
                stack.clear()
            elif b0 == 25:                               # rlinecurve
                k = 0
                while len(stack) - k >= 8:
                    st.line(stack[k], stack[k + 1])
                    k += 2
                if len(stack) - k >= 6:
                    st.curve(*stack[k:k + 6])
                stack.clear()
            elif b0 == 26:                               # vvcurveto
                k = 0
                dx1 = 0.0
                if len(stack) % 4 == 1:
                    dx1 = stack[0]
                    k = 1
                while len(stack) - k >= 4:
                    st.curve(dx1, stack[k], stack[k + 1], stack[k + 2], 0, stack[k + 3])
                    dx1 = 0.0
                    k += 4
                stack.clear()
            elif b0 == 27:                               # hhcurveto
                k = 0
                dy1 = 0.0
                if len(stack) % 4 == 1:
                    dy1 = stack[0]
                    k = 1
                while len(stack) - k >= 4:
                    st.curve(stack[k], dy1, stack[k + 1], stack[k + 2], stack[k + 3], 0)
                    dy1 = 0.0
                    k += 4
                stack.clear()
            elif b0 in (30, 31):                         # vhcurveto, hvcurveto
                horiz = b0 == 31
                k = 0
                while len(stack) - k >= 4:
                    last = len(stack) - k == 5
                    a, b, c, d = stack[k:k + 4]
                    e = stack[k + 4] if last else 0
                    if horiz:
                        st.curve(a, 0, b, c, e, d)
                    else:
                        st.curve(0, a, b, c, d, e)
                    horiz = not horiz
                    k += 5 if last else 4
                stack.clear()
            elif b0 in (10, 29):                         # callsubr, callgsubr
                if not stack:
                    continue
                table = subrs if b0 == 10 else self.gsubrs
                idx = int(stack.pop()) + _bias(table)
                if 0 <= idx < len(table) and self._run(table[idx], st, subrs, depth + 1):
                    return True
            elif b0 == 11:                               # return
                return False
            elif b0 == 14:                               # endchar
                st.take_width(len(stack) in (1, 5))
                stack.clear()
                return True
            elif b0 == 12:
                b1 = code[i]
                i += 1
                s = stack
                if b1 == 35 and len(s) >= 13:            # flex
                    st.curve(*s[0:6])
                    st.curve(*s[6:12])
                elif b1 == 34 and len(s) >= 7:           # hflex
                    st.curve(s[0], 0, s[1], s[2], s[3], 0)
                    st.curve(s[4], 0, s[5], -s[2], s[6], 0)
                elif b1 == 36 and len(s) >= 9:           # hflex1
                    st.curve(s[0], s[1], s[2], s[3], s[4], 0)
                    st.curve(s[5], 0, s[6], s[7], s[8], -(s[1] + s[3] + s[7]))
                elif b1 == 37 and len(s) >= 11:          # flex1
                    dx = s[0] + s[2] + s[4] + s[6] + s[8]
                    dy = s[1] + s[3] + s[5] + s[7] + s[9]
                    st.curve(*s[0:6])
                    if abs(dx) > abs(dy):
                        st.curve(s[6], s[7], s[8], s[9], s[10], -dy)
                    else:
                        st.curve(s[6], s[7], s[8], s[9], -dx, s[10])
                stack.clear()
            else:
                stack.clear()
        return False


class _T2State:
    def __init__(self) -> None:
        self.stack: List[float] = []
        self.x = self.y = 0.0
        self.nstems = 0
        self.width_done = False
        self.paths: List[List[tuple]] = []
        self.cur: Optional[List[tuple]] = None

    def take_width(self, has: bool) -> None:
        if not self.width_done:
            self.width_done = True
            if has and self.stack:
                self.stack.pop(0)

    def close(self) -> None:
        if self.cur is not None and len(self.cur) > 1:
            self.paths.append(self.cur)
        self.cur = None

    def move(self, dx: float, dy: float) -> None:
        self.close()
        self.x += dx
        self.y += dy
        self.cur = [("M", self.x, self.y)]

    def _ensure(self) -> None:
        if self.cur is None:
            self.cur = [("M", self.x, self.y)]

    def line(self, dx: float, dy: float) -> None:
        self._ensure()
        self.x += dx
        self.y += dy
        self.cur.append(("L", self.x, self.y))

    def curve(self, dx1, dy1, dx2, dy2, dx3, dy3) -> None:
        self._ensure()
        x1, y1 = self.x + dx1, self.y + dy1
        x2, y2 = x1 + dx2, y1 + dy2
        self.x, self.y = x2 + dx3, y2 + dy3
        self.cur.append(("C", x1, y1, x2, y2, self.x, self.y))


def _cubic_to_quads(p0, p1, p2, p3, tol: float) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """Кубическая кривая -> квадратичные куски [(управляющая, конец)] с погрешностью до ``tol``."""
    def at(t, a, b, c, d):
        mt = 1 - t
        return (mt ** 3 * a[0] + 3 * mt * mt * t * b[0] + 3 * mt * t * t * c[0] + t ** 3 * d[0],
                mt ** 3 * a[1] + 3 * mt * mt * t * b[1] + 3 * mt * t * t * c[1] + t ** 3 * d[1])

    def split(t0, t1):
        a = at(t0, p0, p1, p2, p3)
        d = at(t1, p0, p1, p2, p3)
        # производные на концах -> управляющие точки куска
        def deriv(t):
            mt = 1 - t
            return (3 * (mt * mt * (p1[0] - p0[0]) + 2 * mt * t * (p2[0] - p1[0]) + t * t * (p3[0] - p2[0])),
                    3 * (mt * mt * (p1[1] - p0[1]) + 2 * mt * t * (p2[1] - p1[1]) + t * t * (p3[1] - p2[1])))
        h = (t1 - t0) / 3
        da, dd = deriv(t0), deriv(t1)
        b = (a[0] + da[0] * h, a[1] + da[1] * h)
        c = (d[0] - dd[0] * h, d[1] - dd[1] * h)
        return a, b, c, d

    for n in range(1, 17):
        out = []
        ok = True
        for k in range(n):
            a, b, c, d = split(k / n, (k + 1) / n)
            q = ((3 * (b[0] + c[0]) - a[0] - d[0]) / 4, (3 * (b[1] + c[1]) - a[1] - d[1]) / 4)
            for t in (0.25, 0.5, 0.75):
                cx, cy = at(t, a, b, c, d)
                mt = 1 - t
                qx = mt * mt * a[0] + 2 * mt * t * q[0] + t * t * d[0]
                qy = mt * mt * a[1] + 2 * mt * t * q[1] + t * t * d[1]
                if math.hypot(cx - qx, cy - qy) > tol:
                    ok = False
                    break
            out.append((q, d))
            if not ok:
                break
        if ok:
            return out
    return out


def cff_paths_to_contours(paths: List[List[tuple]], tol: float) -> List[Contour]:
    """Пути CFF (кубические, против часовой стрелки) -> контуры TrueType (квадратичные, по часовой)."""
    out: List[Contour] = []
    for path in paths:
        pts: Contour = []
        x0, y0 = path[0][1], path[0][2]
        cur = (x0, y0)
        pts.append((x0, y0, True))
        for cmd in path[1:]:
            if cmd[0] == "L":
                cur = (cmd[1], cmd[2])
                pts.append((cur[0], cur[1], True))
            else:
                p1, p2, p3 = (cmd[1], cmd[2]), (cmd[3], cmd[4]), (cmd[5], cmd[6])
                for q, end in _cubic_to_quads(cur, p1, p2, p3, tol):
                    pts.append((q[0], q[1], False))
                    pts.append((end[0], end[1], True))
                cur = p3
        if len(pts) > 1 and pts[-1][2] and abs(pts[-1][0] - x0) < 1e-6 and abs(pts[-1][1] - y0) < 1e-6:
            pts.pop()                                   # контур замкнут на старт — точка лишняя
        if len(pts) >= 2:
            out.append(list(reversed(pts)))
    return out


# ---------- глифы TrueType ----------

def decode_glyf(raw: bytes) -> Tuple[List[Contour], List[Tuple[int, tuple]]]:
    """Простой глиф -> контуры; составной -> ([], [(номер глифа, (a, b, c, d, dx, dy)), ...])."""
    if len(raw) < 10:
        return [], []
    n = struct.unpack_from(">h", raw, 0)[0]
    if n >= 0:
        ends = struct.unpack_from(f">{n}H", raw, 10)
        n_pts = (ends[-1] + 1) if n else 0
        p = 10 + 2 * n
        ins_len = struct.unpack_from(">H", raw, p)[0]
        p += 2 + ins_len
        flags = []
        while len(flags) < n_pts:
            f = raw[p]
            p += 1
            flags.append(f)
            if f & 8:
                rep = raw[p]
                p += 1
                flags.extend([f] * rep)
        flags = flags[:n_pts]
        xs, ys = [], []
        v = 0
        for f in flags:
            if f & 2:
                d = raw[p]
                p += 1
                v += d if f & 16 else -d
            elif not f & 16:
                v += struct.unpack_from(">h", raw, p)[0]
                p += 2
            xs.append(v)
        v = 0
        for f in flags:
            if f & 4:
                d = raw[p]
                p += 1
                v += d if f & 32 else -d
            elif not f & 32:
                v += struct.unpack_from(">h", raw, p)[0]
                p += 2
            ys.append(v)
        contours, start = [], 0
        for e in ends:
            contours.append([(float(xs[k]), float(ys[k]), bool(flags[k] & 1)) for k in range(start, e + 1)])
            start = e + 1
        return contours, []
    comps = []
    p = 10
    while True:
        flags, gid = struct.unpack_from(">HH", raw, p)
        p += 4
        if flags & 1:
            a1, a2 = struct.unpack_from(">hh", raw, p) if flags & 2 else struct.unpack_from(">HH", raw, p)
            p += 4
        else:
            a1, a2 = struct.unpack_from(">bb", raw, p) if flags & 2 else struct.unpack_from(">BB", raw, p)
            p += 2
        if not flags & 2:
            a1 = a2 = 0                                   # привязка по точкам — редкость, считаем без сдвига
        a, b, c, d = 1.0, 0.0, 0.0, 1.0
        if flags & 8:
            a = d = struct.unpack_from(">h", raw, p)[0] / 16384
            p += 2
        elif flags & 0x40:
            a, d = (v / 16384 for v in struct.unpack_from(">hh", raw, p))
            p += 4
        elif flags & 0x80:
            a, b, c, d = (v / 16384 for v in struct.unpack_from(">hhhh", raw, p))
            p += 8
        comps.append((gid, (a, b, c, d, float(a1), float(a2))))
        if not flags & 0x20:
            break
    return [], comps


def encode_glyf(contours: Sequence[Sequence[Tuple[int, int, bool]]]) -> bytes:
    """Контуры (целые координаты) -> простой глиф TrueType без инструкций."""
    pts = [p for c in contours for p in c]
    if not pts:
        return b""
    ends, k = [], -1
    for c in contours:
        k += len(c)
        ends.append(k)
    xs = [int(p[0]) for p in pts]
    ys = [int(p[1]) for p in pts]
    flags = bytearray()
    xb, yb = bytearray(), bytearray()
    px = py = 0
    for x, y, (_, _, on) in zip(xs, ys, pts):
        f = 1 if on else 0
        dx, dy = x - px, y - py
        px, py = x, y
        if dx == 0:
            f |= 16
        elif -255 <= dx <= 255:
            f |= 2 | (16 if dx > 0 else 0)
            xb.append(abs(dx))
        else:
            xb += struct.pack(">h", dx)
        if dy == 0:
            f |= 32
        elif -255 <= dy <= 255:
            f |= 4 | (32 if dy > 0 else 0)
            yb.append(abs(dy))
        else:
            yb += struct.pack(">h", dy)
        flags.append(f)
    head = struct.pack(">hhhhh", len(contours), min(xs), min(ys), max(xs), max(ys))
    return head + struct.pack(f">{len(ends)}H", *ends) + b"\0\0" + bytes(flags) + bytes(xb) + bytes(yb)


def _bbox_of_raw(raw: bytes) -> Optional[Tuple[int, int, int, int]]:
    if len(raw) < 10:
        return None
    return struct.unpack_from(">hhhh", raw, 2)


# ---------- шрифт ----------

class Font:
    """Шрифт TrueType/OpenType (одиночный; из коллекции TTC — первый)."""

    def __init__(self, data: bytes):
        data = bytes(data)
        try:
            self._load(data)
        except FontError:
            raise
        except (struct.error, IndexError, KeyError, ValueError, ZeroDivisionError) as exc:
            raise FontError(f"шрифт не читается: {exc}") from exc

    def _load(self, data: bytes) -> None:
        offset = struct.unpack_from(">I", data, 12)[0] if data[:4] == b"ttcf" else 0
        n = struct.unpack_from(">H", data, offset + 4)[0]
        self.tables: Dict[bytes, bytes] = {}
        for i in range(n):
            tag, _, off, length = struct.unpack_from(">4sIII", data, offset + 12 + 16 * i)
            self.tables[tag] = data[off:off + length]
        for need in (b"head", b"hhea", b"hmtx", b"maxp", b"cmap"):
            if need not in self.tables:
                raise FontError(f"нет таблицы {need.decode()}")
        head = self.tables[b"head"]
        self.upem = struct.unpack_from(">H", head, 18)[0] or 1000
        self.loc_format = struct.unpack_from(">h", head, 50)[0]
        self.num_glyphs = struct.unpack_from(">H", self.tables[b"maxp"], 4)[0]
        hhea = self.tables[b"hhea"]
        self.ascender, self.descender, self.line_gap = struct.unpack_from(">hhh", hhea, 4)
        n_h = max(1, struct.unpack_from(">H", hhea, 34)[0])
        hmtx = self.tables[b"hmtx"]
        self.advances: List[int] = []
        self.lsbs: List[int] = []
        for g in range(self.num_glyphs):
            if g < n_h and 4 * g + 4 <= len(hmtx):
                adv, lsb = struct.unpack_from(">Hh", hmtx, 4 * g)
            else:
                adv = self.advances[-1] if self.advances else 0
                p = 4 * n_h + 2 * (g - n_h)
                lsb = struct.unpack_from(">h", hmtx, p)[0] if g >= n_h and p + 2 <= len(hmtx) else 0
            self.advances.append(adv)
            self.lsbs.append(lsb)
        self.cmap = parse_cmap(self.tables[b"cmap"])
        self.cff: Optional[_Cff] = None
        self.loca: List[int] = []
        if b"glyf" in self.tables and b"loca" in self.tables:
            loca = self.tables[b"loca"]
            if self.loc_format == 0:
                cnt = min(self.num_glyphs + 1, len(loca) // 2)
                self.loca = [2 * v for v in struct.unpack_from(f">{cnt}H", loca)]
            else:
                cnt = min(self.num_glyphs + 1, len(loca) // 4)
                self.loca = list(struct.unpack_from(f">{cnt}I", loca))
            self.loca += [self.loca[-1] if self.loca else 0] * (self.num_glyphs + 1 - len(self.loca))
        elif b"CFF " in self.tables:
            self.cff = _Cff(self.tables[b"CFF "])
        else:
            raise FontError("нет контуров глифов (glyf/CFF)")
        self._contours: Dict[int, List[Contour]] = {}

    # --- сведения ---

    @property
    def is_cff(self) -> bool:
        return self.cff is not None

    def gid(self, ch: str) -> Optional[int]:
        g = self.cmap.get(ord(ch))
        return g if g is not None and 0 < g < self.num_glyphs else None

    def has(self, ch: str) -> bool:
        return self.gid(ch) is not None

    def advance(self, ch: str) -> Optional[int]:
        g = self.gid(ch)
        return self.advances[g] if g is not None else None

    def raw_glyph(self, gid: int) -> bytes:
        if self.cff is not None:
            return encode_glyf(_round(self.contours(gid)))
        a, b = self.loca[gid], self.loca[gid + 1]
        return self.tables[b"glyf"][a:b] if b > a else b""

    def contours(self, gid: int, _depth: int = 0) -> List[Contour]:
        """Контуры глифа (TrueType: по часовой стрелке, квадратичные), составные — раскрыты."""
        if gid in self._contours:
            return self._contours[gid]
        if gid < 0 or gid >= self.num_glyphs:
            return []
        if self.cff is not None:
            out = cff_paths_to_contours(self.cff.contours(gid), tol=max(0.5, self.upem / 2000))
        else:
            a, b = self.loca[gid], self.loca[gid + 1]
            raw = self.tables[b"glyf"][a:b] if b > a else b""
            simple, comps = decode_glyf(raw)
            out = simple
            if comps and _depth < 8:
                out = []
                for cg, (ta, tb, tc, td, dx, dy) in comps:
                    for c in self.contours(cg, _depth + 1):
                        out.append([(ta * x + tc * y + dx, tb * x + td * y + dy, on) for x, y, on in c])
        self._contours[gid] = out
        return out

    def bbox(self, gid: int) -> Optional[Tuple[float, float, float, float]]:
        pts = [p for c in self.contours(gid) for p in c]
        if not pts:
            return None
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))

    def os2(self) -> Dict[str, int]:
        t = self.tables.get(b"OS/2", b"")
        out: Dict[str, int] = {}
        if len(t) >= 8:
            out["weight"] = struct.unpack_from(">H", t, 4)[0]
        if len(t) >= 90:
            out["x_height"], out["cap_height"] = struct.unpack_from(">hh", t, 86)
        return out

    def italic_angle(self) -> float:
        t = self.tables.get(b"post", b"")
        return struct.unpack_from(">i", t, 4)[0] / 65536.0 if len(t) >= 8 else 0.0

    def is_fixed_pitch(self) -> bool:
        t = self.tables.get(b"post", b"")
        return len(t) >= 16 and struct.unpack_from(">I", t, 12)[0] != 0


def _round(contours: Iterable[Contour]) -> List[List[Tuple[int, int, bool]]]:
    return [[(int(round(x)), int(round(y)), on) for x, y, on in c] for c in contours]


# ---------- сборка ----------

@dataclass
class NewGlyph:
    """Новый глиф: либо готовые байты glyf (копия существующего), либо контуры."""
    codepoint: int
    advance: int
    contours: List[List[Tuple[int, int, bool]]] = field(default_factory=list)
    raw: Optional[bytes] = None


#: таблицы, которые зависят от числа глифов и не пересчитываются — выбрасываются
_DROP = {b"DSIG", b"hdmx", b"LTSH", b"VDMX", b"vhea", b"vmtx", b"VORG", b"CFF ", b"CFF2", b"gvar", b"fvar",
         b"avar", b"HVAR", b"VVAR", b"MVAR", b"STAT", b"cvar", b"glyf", b"loca", b"hmtx", b"cmap", b"post",
         b"maxp", b"head", b"hhea"}


def build(font: Font, new: Sequence[NewGlyph]) -> bytes:
    """Собрать шрифт TrueType: все глифы ``font`` под прежними номерами + ``new`` в конце."""
    total = font.num_glyphs + len(new)
    if total > 65535:
        raise FontError("в шрифте нет места для новых глифов")
    glyphs: List[bytes] = [font.raw_glyph(g) for g in range(font.num_glyphs)]
    advances = list(font.advances)
    cmap = {c: g for c, g in font.cmap.items() if 0 <= g < font.num_glyphs}
    for k, ng in enumerate(new):
        raw = ng.raw if ng.raw is not None else encode_glyf(ng.contours)
        glyphs.append(raw)
        advances.append(max(0, int(round(ng.advance))))
        cmap[ng.codepoint] = font.num_glyphs + k

    glyf = bytearray()
    loca: List[int] = []
    lsbs: List[int] = []
    max_pts = max_cnt = 0
    bx0 = by0 = 32767
    bx1 = by1 = -32768
    min_lsb = min_rsb = 32767
    max_extent = -32768
    for g, raw in enumerate(glyphs):
        loca.append(len(glyf))
        glyf += raw
        glyf += b"\0" * (-len(glyf) % 4)
        bb = _bbox_of_raw(raw)
        if bb is None:
            lsbs.append(0)
            continue
        x0, y0, x1, y1 = bb
        lsbs.append(x0)
        bx0, by0, bx1, by1 = min(bx0, x0), min(by0, y0), max(bx1, x1), max(by1, y1)
        min_lsb = min(min_lsb, x0)
        min_rsb = min(min_rsb, advances[g] - x1)
        max_extent = max(max_extent, x1)
        n_cont = struct.unpack_from(">h", raw, 0)[0]
        if n_cont > 0:
            ends = struct.unpack_from(f">{n_cont}H", raw, 10)
            max_pts = max(max_pts, ends[-1] + 1)
            max_cnt = max(max_cnt, n_cont)
    loca.append(len(glyf))
    if bx0 > bx1:
        bx0 = by0 = bx1 = by1 = 0
        min_lsb = min_rsb = max_extent = 0

    tables: Dict[bytes, bytes] = {t: d for t, d in font.tables.items() if t not in _DROP}
    tables[b"glyf"] = bytes(glyf)
    tables[b"loca"] = struct.pack(f">{len(loca)}I", *loca)
    tables[b"hmtx"] = b"".join(struct.pack(">Hh", a, l) for a, l in zip(advances, lsbs))

    head = bytearray(font.tables[b"head"][:54].ljust(54, b"\0"))
    struct.pack_into(">I", head, 8, 0)                       # checkSumAdjustment — ниже
    struct.pack_into(">hhhh", head, 36, bx0, by0, bx1, by1)
    struct.pack_into(">hh", head, 50, 1, 0)                  # длинная loca, glyphDataFormat 0
    tables[b"head"] = bytes(head)

    hhea = bytearray(font.tables[b"hhea"][:36].ljust(36, b"\0"))
    struct.pack_into(">Hhhh", hhea, 10, max(advances) if advances else 0, min_lsb, min_rsb, max_extent)
    struct.pack_into(">H", hhea, 34, total)
    tables[b"hhea"] = bytes(hhea)

    old_maxp = font.tables[b"maxp"]
    maxp = bytearray(32)
    struct.pack_into(">IH", maxp, 0, 0x00010000, total)
    if len(old_maxp) >= 32 and font.cff is None:
        maxp[6:32] = old_maxp[6:32]
        o_pts, o_cnt = struct.unpack_from(">HH", old_maxp, 6)
        struct.pack_into(">HH", maxp, 6, max(o_pts, max_pts), max(o_cnt, max_cnt))
    else:
        struct.pack_into(">HHHHHHHHHHHHH", maxp, 6, max_pts, max_cnt, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0)
    tables[b"maxp"] = bytes(maxp)

    tables[b"cmap"] = _build_cmap(cmap)
    old_post = font.tables.get(b"post", b"")
    post = bytearray(32)
    struct.pack_into(">I", post, 0, 0x00030000)
    if len(old_post) >= 16:
        post[4:16] = old_post[4:16]
    tables[b"post"] = bytes(post)

    if b"OS/2" in tables:
        os2 = bytearray(tables[b"OS/2"])
        if len(os2) >= 68:
            ur1 = struct.unpack_from(">I", os2, 42)[0]
            struct.pack_into(">I", os2, 42, ur1 | (1 << 9))  # Cyrillic
            bmp = [c for c in cmap if c <= 0xFFFF]
            if bmp:
                struct.pack_into(">HH", os2, 64, min(bmp), max(bmp))
        if len(os2) >= 86:
            cp1 = struct.unpack_from(">I", os2, 78)[0]
            struct.pack_into(">I", os2, 78, cp1 | (1 << 2))  # 1251
        tables[b"OS/2"] = bytes(os2)
    return _write_sfnt(tables)


def _build_cmap(cmap: Dict[int, int]) -> bytes:
    bmp = sorted((c, g) for c, g in cmap.items() if 0 <= c <= 0xFFFF and c != 0xFFFF)
    # сегменты подряд идущих кодов с постоянной разностью «глиф − код»
    segs: List[Tuple[int, int, int]] = []
    for c, g in bmp:
        if segs and segs[-1][1] == c - 1 and (segs[-1][2] + c) & 0xFFFF == g:
            segs[-1] = (segs[-1][0], c, segs[-1][2])
        else:
            segs.append((c, c, (g - c) & 0xFFFF))
    segs.append((0xFFFF, 0xFFFF, 1))
    n = len(segs)
    search = 2 ** int(math.floor(math.log2(n))) * 2
    sub4 = struct.pack(">HHHHH", n * 2, search, int(math.log2(search // 2)), n * 2 - search, 0)
    ends = struct.pack(f">{n}H", *(s[1] for s in segs))
    starts = struct.pack(f">{n}H", *(s[0] for s in segs))
    deltas = struct.pack(f">{n}H", *(s[2] for s in segs))
    ranges = struct.pack(f">{n}H", *([0] * n))
    body4 = sub4[:8] + ends + b"\0\0" + starts + deltas + ranges
    fmt4 = struct.pack(">HHH", 4, 6 + len(body4), 0) + body4
    if len(fmt4) > 0xFFFF:
        raise FontError("таблица cmap слишком велика")
    big = sorted((c, g) for c, g in cmap.items() if c > 0xFFFF)
    subs = [(0, 3, fmt4), (3, 1, fmt4)]
    if big:
        groups: List[Tuple[int, int, int]] = []
        for c, g in sorted(cmap.items()):
            if groups and groups[-1][1] == c - 1 and groups[-1][2] + (c - groups[-1][0]) == g:
                groups[-1] = (groups[-1][0], c, groups[-1][2])
            else:
                groups.append((c, c, g))
        fmt12 = struct.pack(">HHIII", 12, 0, 16 + 12 * len(groups), 0, len(groups)) + \
            b"".join(struct.pack(">III", *gr) for gr in groups)
        subs = [(0, 3, fmt4), (0, 4, fmt12), (3, 1, fmt4), (3, 10, fmt12)]
    out = bytearray(struct.pack(">HH", 0, len(subs)))
    data = bytearray()
    placed: Dict[int, int] = {}
    base = 4 + 8 * len(subs)
    for pid, eid, sub in subs:
        if id(sub) not in placed:
            placed[id(sub)] = base + len(data)
            data += sub
            data += b"\0" * (-len(data) % 4)
        out += struct.pack(">HHI", pid, eid, placed[id(sub)])
    return bytes(out + data)


def _write_sfnt(tables: Dict[bytes, bytes]) -> bytes:
    tags = sorted(tables)
    n = len(tags)
    search = 2 ** int(math.floor(math.log2(n))) * 16
    header = struct.pack(">IHHHH", 0x00010000, n, search, int(math.log2(search // 16)), n * 16 - search)
    offset = 12 + 16 * n
    directory = bytearray()
    body = bytearray()
    for tag in tags:
        data = tables[tag]
        directory += struct.pack(">4sIII", tag, _checksum(data), offset + len(body), len(data))
        body += data
        body += b"\0" * (-len(body) % 4)
    font = bytearray(header + directory + body)
    head_off = struct.unpack_from(">I", directory, 16 * tags.index(b"head") + 8)[0]
    struct.pack_into(">I", font, head_off + 8, (0xB1B0AFBA - _checksum(bytes(font))) & 0xFFFFFFFF)
    return bytes(font)
