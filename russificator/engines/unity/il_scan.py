"""Как код игры обращается с текстом компонентов — по IL-коду .NET-сборок (Mono).

Зачем. «Печатная машинка» в играх бывает двух видов:

* ``label.text += nextChar`` — код читает текст обратно из компонента и
  дописывает букву. После перевода первой буквы игра дописывает английское
  продолжение к русскому началу, и строка становится русской только целиком
  (так было с монологом в Welcome to the Game II). Лечится режимом XUnity
  ``TextGetterCompatibilityMode``: коду игры возвращается оригинал;
* ``label.maxVisibleCharacters = i`` до ``label.text.Length`` — наоборот,
  с этим режимом игра видела бы длину английской строки и обрезала бы
  более длинную русскую.

Ещё сканер считает вызовы IMGUI (``GUI.Label``, ``GUILayout.Button``…): если
игра рисует интерфейс так, XUnity нужно перехватывать и его (по умолчанию это
выключено — лишняя нагрузка для игр, которые IMGUI не используют).

Сканер разбирает таблицы метаданных (TypeRef, MethodDef, MemberRef) и тела
методов, собирает последовательность вызовов в каждом методе и ищет шаблоны.
Только чтение; ничего не выполняется.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class TextUsage:
    appends: int = 0          # text = text + … (чтение текста обратно и дописывание)
    length_reveals: int = 0   # maxVisibleCharacters по длине text
    imgui_calls: int = 0      # вызовы GUI/GUILayout, рисующие текст


#: методы IMGUI, которые выводят текст
IMGUI_TYPES = {"GUI", "GUILayout"}
IMGUI_METHODS = {"Label", "Button", "Box", "Toggle", "TextField", "TextArea", "Window", "RepeatButton",
                 "SelectionGrid", "Toolbar", "PasswordField", "ModalWindow"}


# операнды однобайтовых опкодов: размер в байтах (None — неизвестный опкод)
_OP1: Dict[int, int] = {}
for _op in range(0x00, 0x0E):
    _OP1[_op] = 0
for _op in range(0x0E, 0x14):
    _OP1[_op] = 1
for _op in range(0x14, 0x1F):
    _OP1[_op] = 0
_OP1.update({0x1F: 1, 0x20: 4, 0x21: 8, 0x22: 4, 0x23: 8, 0x25: 0, 0x26: 0, 0x27: 4, 0x28: 4, 0x29: 4, 0x2A: 0})
for _op in range(0x2B, 0x38):
    _OP1[_op] = 1
for _op in range(0x38, 0x45):
    _OP1[_op] = 4
for _op in range(0x46, 0x6F):
    _OP1[_op] = 0
_OP1.update({0x6F: 4, 0x70: 4, 0x71: 4, 0x72: 4, 0x73: 4, 0x74: 4, 0x75: 4, 0x76: 0, 0x79: 4, 0x7A: 0,
             0x7B: 4, 0x7C: 4, 0x7D: 4, 0x7E: 4, 0x7F: 4, 0x80: 4, 0x81: 4, 0x8C: 4, 0x8D: 4, 0x8E: 0,
             0x8F: 4, 0xA3: 4, 0xA4: 4, 0xA5: 4, 0xC2: 4, 0xC3: 0, 0xC6: 4, 0xD0: 4, 0xDD: 4, 0xDE: 1,
             0xDF: 0, 0xE0: 0})
for _op in list(range(0x82, 0x8C)) + list(range(0x90, 0xA3)) + list(range(0xB3, 0xBB)) + list(range(0xD1, 0xDD)):
    _OP1[_op] = 0
_OP2: Dict[int, int] = {0x00: 0, 0x01: 0, 0x02: 0, 0x03: 0, 0x04: 0, 0x05: 0, 0x06: 4, 0x07: 4, 0x09: 2, 0x0A: 2,
                        0x0B: 2, 0x0C: 2, 0x0D: 2, 0x0E: 2, 0x0F: 0, 0x11: 0, 0x12: 1, 0x13: 0, 0x14: 0, 0x15: 4,
                        0x16: 4, 0x17: 0, 0x18: 0, 0x19: 1, 0x1A: 0, 0x1C: 4, 0x1D: 0, 0x1E: 0}

_CALLS = (0x28, 0x6F)  # call, callvirt


class _Meta:
    """Минимальный читатель метаданных .NET: имена методов и тела MethodDef."""

    def __init__(self, data: bytes):
        self.data = data
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            raise ValueError("не PE")
        nsec = struct.unpack_from("<H", data, pe + 6)[0]
        opt_size = struct.unpack_from("<H", data, pe + 20)[0]
        opt = pe + 24
        magic = struct.unpack_from("<H", data, opt)[0]
        dd = opt + (96 if magic == 0x10B else 112)
        cli_rva = struct.unpack_from("<I", data, dd + 14 * 8)[0]
        self.sections = []
        sec = opt + opt_size
        for i in range(nsec):
            vsize, va, rsize, raw = struct.unpack_from("<IIII", data, sec + i * 40 + 8)
            self.sections.append((va, max(vsize, rsize), raw))
        cli = self.off(cli_rva)
        md = self.off(struct.unpack_from("<I", data, cli + 8)[0])
        if data[md:md + 4] != b"BSJB":
            raise ValueError("нет метаданных")
        vlen = struct.unpack_from("<I", data, md + 12)[0]
        p = md + 16 + vlen + 2
        n = struct.unpack_from("<H", data, p)[0]
        p += 2
        self.streams: Dict[str, Tuple[int, int]] = {}
        for _ in range(n):
            s_off, s_size = struct.unpack_from("<II", data, p)
            p += 8
            end = data.index(b"\0", p)
            name = data[p:end].decode("ascii", "replace")
            p = (end + 4) & ~3
            self.streams[name] = (md + s_off, s_size)
        if "#~" not in self.streams:
            raise ValueError("нестандартные таблицы")
        self._tables()

    def off(self, rva: int) -> int:
        for va, size, raw in self.sections:
            if va <= rva < va + size:
                return rva - va + raw
        raise ValueError("rva вне секций")

    def _string(self, idx: int) -> str:
        base = self.streams["#Strings"][0] + idx
        end = self.data.index(b"\0", base)
        return self.data[base:end].decode("utf-8", "replace")

    def _tables(self) -> None:
        d = self.data
        t = self.streams["#~"][0]
        heap = d[t + 6]
        valid = struct.unpack_from("<Q", d, t + 8)[0]
        rows = [0] * 64
        p = t + 24
        for i in range(64):
            if valid >> i & 1:
                rows[i] = struct.unpack_from("<I", d, p)[0]
                p += 4
        S = 4 if heap & 1 else 2
        G = 4 if heap & 2 else 2
        B = 4 if heap & 4 else 2

        def idx(table: int) -> int:
            return 4 if rows[table] >= 0x10000 else 2

        def coded(tables, bits) -> int:
            return 4 if max(rows[x] for x in tables) >= (1 << (16 - bits)) else 2

        rs = coded((0x00, 0x1A, 0x23, 0x01), 2)
        tdor = coded((0x02, 0x01, 0x1B), 2)
        mrp = coded((0x02, 0x01, 0x1A, 0x06, 0x1B), 3)
        sizes = {
            0x00: 2 + S + 3 * G, 0x01: rs + 2 * S, 0x02: 4 + 2 * S + tdor + idx(0x04) + idx(0x06),
            0x03: idx(0x04), 0x04: 2 + S + B, 0x05: idx(0x06), 0x06: 4 + 2 + 2 + S + B + idx(0x08),
            0x07: idx(0x08), 0x08: 2 + 2 + S, 0x09: idx(0x02) + tdor, 0x0A: mrp + S + B,
        }
        pos = p
        starts = {}
        for tb in range(0x0B):
            starts[tb] = pos
            pos += rows[tb] * sizes[tb]

        def rd(at: int, size: int) -> int:
            return struct.unpack_from("<I" if size == 4 else "<H", d, at)[0]

        self.methods: List[Tuple[int, str]] = []   # (rva, имя) по порядку строк MethodDef
        for r in range(rows[0x06]):
            at = starts[0x06] + r * sizes[0x06]
            rva = struct.unpack_from("<I", d, at)[0]
            self.methods.append((rva, self._string(rd(at + 8, S))))
        self.type_refs: List[str] = []
        for r in range(rows[0x01]):
            at = starts[0x01] + r * sizes[0x01]
            self.type_refs.append(self._string(rd(at + rs, S)))
        self.member_refs: List[str] = []
        self.member_parents: List[Optional[str]] = []   # имя типа-владельца (для ссылок на TypeRef)
        for r in range(rows[0x0A]):
            at = starts[0x0A] + r * sizes[0x0A]
            self.member_refs.append(self._string(rd(at + mrp, S)))
            parent = rd(at, mrp)
            tag, row = parent & 7, parent >> 3        # MemberRefParent: 1 — TypeRef
            self.member_parents.append(self.type_refs[row - 1] if tag == 1 and 0 < row <= len(self.type_refs)
                                       else None)

    def owner_of(self, token: int) -> Optional[str]:
        """Тип-владелец вызываемого метода из другой сборки (``GUI`` для ``GUI.Label``)."""
        table, row = token >> 24, token & 0xFFFFFF
        if table == 0x0A and 0 < row <= len(self.member_parents):
            return self.member_parents[row - 1]
        return None

    def name_of(self, token: int) -> Optional[str]:
        table, row = token >> 24, token & 0xFFFFFF
        if table == 0x0A and 0 < row <= len(self.member_refs):
            return self.member_refs[row - 1]
        if table == 0x06 and 0 < row <= len(self.methods):
            return self.methods[row - 1][1]
        return None

    def calls(self, rva: int, owners: Optional[List[Optional[str]]] = None) -> List[str]:
        """Имена вызываемых методов по порядку (call/callvirt) в теле метода.

        ``owners`` — если передан список, в него добавляются типы-владельцы тех же вызовов.
        """
        d = self.data
        try:
            p = self.off(rva)
        except ValueError:
            return []
        head = d[p]
        if head & 3 == 2:
            size, code = head >> 2, p + 1
        elif head & 3 == 3:
            flags = struct.unpack_from("<H", d, p)[0]
            size, code = struct.unpack_from("<I", d, p + 4)[0], p + 4 * (flags >> 12)
        else:
            return []
        out: List[str] = []
        i, end = code, min(code + size, len(d))
        while i < end:
            op = d[i]
            i += 1
            if op == 0xFE:
                if i >= end:
                    break
                n = _OP2.get(d[i])
                i += 1
            elif op == 0x45:  # switch
                count = struct.unpack_from("<I", d, i)[0]
                n = 4 + 4 * count
            else:
                n = _OP1.get(op)
                if op in _CALLS and i + 4 <= end:
                    token = struct.unpack_from("<I", d, i)[0]
                    name = self.name_of(token)
                    if name:
                        out.append(name)
                        if owners is not None:
                            owners.append(self.owner_of(token))
            if n is None:
                break  # неизвестный опкод — дальше не разбираем
            i += n
        return out


def text_usage(dll: Path) -> TextUsage:
    """Шаблоны работы с текстом компонентов в сборке (пустой результат, если сборку не разобрать)."""
    res = TextUsage()
    try:
        meta = _Meta(Path(dll).read_bytes())
    except (OSError, ValueError, struct.error, IndexError):
        return res
    for rva, _name in meta.methods:
        if not rva:
            continue
        owners: List[Optional[str]] = []
        try:
            calls = meta.calls(rva, owners)
        except (struct.error, IndexError):
            continue
        res.imgui_calls += sum(1 for name, owner in zip(calls, owners)
                               if owner in IMGUI_TYPES and name in IMGUI_METHODS)
        if "get_text" not in calls:
            continue
        for k, name in enumerate(calls):
            if name != "get_text":
                continue
            window = calls[k + 1:k + 5]
            if "Concat" in window:
                j = window.index("Concat")
                if "set_text" in calls[k + 2 + j:k + 4 + j]:
                    res.appends += 1
            if calls[k + 1:k + 2] == ["get_Length"] and "set_maxVisibleCharacters" in calls:
                res.length_reveals += 1
    return res
