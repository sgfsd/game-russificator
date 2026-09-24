"""Какие символы есть в шрифте TTF/OTF — чтение таблицы cmap без сторонних библиотек.

Нужно, чтобы заранее знать, покажет ли шрифт игры русские буквы, а не
узнавать это по квадратикам уже в игре.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Set, Union


def codepoints(font: Union[bytes, Path, str]) -> Set[int]:
    """Множество кодов символов, для которых в шрифте есть глиф (пусто — не шрифт/ошибка)."""
    data = font if isinstance(font, (bytes, bytearray)) else Path(font).read_bytes()
    try:
        return _parse(bytes(data))
    except (struct.error, ValueError, IndexError):
        return set()


def _parse(data: bytes) -> Set[int]:
    if data[:4] == b"wOFF":  # WOFF 1.0: таблицы сжаты zlib по отдельности
        import zlib
        num_tables = struct.unpack_from(">H", data, 12)[0]
        for i in range(num_tables):
            tag, off, comp, orig, _ = struct.unpack_from(">4sIIII", data, 44 + i * 20)
            if tag == b"cmap":
                table = data[off:off + comp]
                return _parse_cmap(zlib.decompress(table) if comp < orig else table, 0)
        return set()
    if data[:4] == b"ttcf":  # коллекция — берём первый шрифт
        offset = struct.unpack_from(">I", data, 12)[0]
    else:
        offset = 0
    num_tables = struct.unpack_from(">H", data, offset + 4)[0]
    cmap_off = None
    for i in range(num_tables):
        tag, _, off, _ = struct.unpack_from(">4sIII", data, offset + 12 + i * 16)
        if tag == b"cmap":
            cmap_off = off
            break
    if cmap_off is None:
        return set()
    return _parse_cmap(data, cmap_off)


def _parse_cmap(data: bytes, cmap_off: int) -> Set[int]:
    n = struct.unpack_from(">H", data, cmap_off + 2)[0]
    best = None
    for i in range(n):
        pid, eid, sub = struct.unpack_from(">HHI", data, cmap_off + 4 + i * 8)
        fmt = struct.unpack_from(">H", data, cmap_off + sub)[0]
        score = {12: 3, 4: 2}.get(fmt, 0) + (1 if pid in (0, 3) else 0)
        if fmt in (4, 12) and (best is None or score > best[0]):
            best = (score, cmap_off + sub, fmt)
    if best is None:
        return set()
    _, sub, fmt = best
    out: Set[int] = set()
    if fmt == 4:
        seg_x2 = struct.unpack_from(">H", data, sub + 6)[0]
        segs = seg_x2 // 2
        ends = struct.unpack_from(f">{segs}H", data, sub + 14)
        starts = struct.unpack_from(f">{segs}H", data, sub + 16 + seg_x2)
        deltas = struct.unpack_from(f">{segs}h", data, sub + 16 + 2 * seg_x2)
        ro_pos = sub + 16 + 3 * seg_x2
        ranges = struct.unpack_from(f">{segs}H", data, ro_pos)
        for i in range(segs):
            for c in range(starts[i], ends[i] + 1):
                if c == 0xFFFF:
                    continue
                if ranges[i] == 0:
                    gid = (c + deltas[i]) & 0xFFFF
                else:
                    addr = ro_pos + 2 * i + ranges[i] + 2 * (c - starts[i])
                    gid = struct.unpack_from(">H", data, addr)[0]
                    if gid:
                        gid = (gid + deltas[i]) & 0xFFFF
                if gid:
                    out.add(c)
    else:
        ngroups = struct.unpack_from(">I", data, sub + 12)[0]
        for g in range(ngroups):
            start, end, _ = struct.unpack_from(">III", data, sub + 16 + g * 12)
            out.update(range(start, min(end, start + 0x10000) + 1))
    return out


def has_cyrillic(font: Union[bytes, Path, str]) -> bool:
    cps = codepoints(font)
    return all(ord(c) in cps for c in "АБВЖЯабвжя")
