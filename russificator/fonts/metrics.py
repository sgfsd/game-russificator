"""Ширина текста в пикселях по метрикам шрифта (TTF: таблицы cmap, head, hhea, hmtx).

Перенос строк «по числу символов» врёт: «ш», «щ», «ж», «ю» заметно шире «i»
или «l», и строка русского текста той же длины выходит за край окна. Здесь
ширина считается по настоящим ширинам глифов шрифта, которым игра будет
рисовать русский текст (PT Sans из библиотеки программы).
"""

from __future__ import annotations

import struct
from functools import lru_cache
from pathlib import Path
from typing import Dict, Tuple, Union


class FontMetrics:
    def __init__(self, data: bytes):
        tables = _tables(data)
        head = tables[b"head"]
        self.upem = struct.unpack_from(">H", data, head + 18)[0]
        n_hmetrics = struct.unpack_from(">H", data, tables[b"hhea"] + 34)[0]
        hmtx = tables[b"hmtx"]
        advances = [struct.unpack_from(">H", data, hmtx + 4 * i)[0] for i in range(n_hmetrics)]
        self._adv: Dict[int, int] = {}
        for cp, gid in _cmap(data, tables[b"cmap"]).items():
            self._adv[cp] = advances[gid] if gid < len(advances) else advances[-1]
        letters = [self._adv[c] for c in range(0x430, 0x450) if c in self._adv]
        self.average = sum(letters) / len(letters) if letters else self.upem * 0.55

    def advance(self, ch: str) -> float:
        return self._adv.get(ord(ch), self.average)

    def width(self, text: str, size: float) -> float:
        """Ширина строки в пикселях при размере шрифта ``size``."""
        return sum(self.advance(c) for c in text) * size / self.upem


def _tables(data: bytes) -> Dict[bytes, int]:
    offset = struct.unpack_from(">I", data, 12)[0] if data[:4] == b"ttcf" else 0
    n = struct.unpack_from(">H", data, offset + 4)[0]
    out = {}
    for i in range(n):
        tag, _, off, _ = struct.unpack_from(">4sIII", data, offset + 12 + i * 16)
        out[tag] = off
    return out


def _cmap(data: bytes, cmap_off: int) -> Dict[int, int]:
    n = struct.unpack_from(">H", data, cmap_off + 2)[0]
    best = None
    for i in range(n):
        pid, eid, sub = struct.unpack_from(">HHI", data, cmap_off + 4 + i * 8)
        fmt = struct.unpack_from(">H", data, cmap_off + sub)[0]
        if fmt == 4 and (best is None or pid in (0, 3)):
            best = cmap_off + sub
    out: Dict[int, int] = {}
    if best is None:
        return out
    sub = best
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
                out[c] = gid
    return out


@lru_cache(maxsize=4)
def load(path: Union[str, Path]) -> FontMetrics:
    return FontMetrics(Path(path).read_bytes())


def default_metrics() -> FontMetrics:
    """Метрики PT Sans из библиотеки программы (им игра рисует русские буквы)."""
    from .library import ensure_font
    return load(str(ensure_font()))


def measure(text: str, size: float) -> float:
    return default_metrics().width(text, size)


def split_width(text: str, size: float, limit: float) -> Tuple[str, str]:  # pragma: no cover - служебное
    """Разрезать строку по ширине (для отладки)."""
    m = default_metrics()
    width = 0.0
    for i, c in enumerate(text):
        width += m.advance(c) * size / m.upem
        if width > limit:
            return text[:i], text[i:]
    return text, ""
