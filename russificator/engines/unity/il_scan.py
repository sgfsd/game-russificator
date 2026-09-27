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
        self.method_sigs: List[int] = []           # индекс сигнатуры в #Blob
        for r in range(rows[0x06]):
            at = starts[0x06] + r * sizes[0x06]
            rva = struct.unpack_from("<I", d, at)[0]
            self.methods.append((rva, self._string(rd(at + 8, S))))
            self.method_sigs.append(rd(at + 8 + S, B))
        self.type_refs: List[str] = []
        for r in range(rows[0x01]):
            at = starts[0x01] + r * sizes[0x01]
            self.type_refs.append(self._string(rd(at + rs, S)))
        self.member_refs: List[str] = []
        self.member_sigs: List[int] = []
        self.member_parents: List[Optional[str]] = []   # имя типа-владельца (для ссылок на TypeRef)
        for r in range(rows[0x0A]):
            at = starts[0x0A] + r * sizes[0x0A]
            self.member_refs.append(self._string(rd(at + mrp, S)))
            self.member_sigs.append(rd(at + mrp + S, B))
            parent = rd(at, mrp)
            tag, row = parent & 7, parent >> 3        # MemberRefParent: 1 — TypeRef
            self.member_parents.append(self.type_refs[row - 1] if tag == 1 and 0 < row <= len(self.type_refs)
                                       else None)

    def _blob(self, idx: int) -> bytes:
        base, size = self.streams.get("#Blob", (0, 0))
        p = base + idx
        b = self.data[p]
        if b & 0x80 == 0:
            n, p = b, p + 1
        elif b & 0xC0 == 0x80:
            n, p = ((b & 0x3F) << 8) | self.data[p + 1], p + 2
        else:
            n, p = ((b & 0x1F) << 24) | (self.data[p + 1] << 16) | (self.data[p + 2] << 8) | self.data[p + 3], p + 4
        return self.data[p:p + n]

    def stack_effect(self, token: int, newobj: bool = False) -> Optional[Tuple[int, int]]:
        """(сколько снимает со стека, сколько кладёт) для call/callvirt/newobj по сигнатуре метода."""
        table, row = token >> 24, token & 0xFFFFFF
        if table == 0x0A and 0 < row <= len(self.member_sigs):
            sig = self._blob(self.member_sigs[row - 1])
        elif table == 0x06 and 0 < row <= len(self.method_sigs):
            sig = self._blob(self.method_sigs[row - 1])
        else:
            return None          # MethodSpec (обобщённые методы) и прочее — не разбираем
        if len(sig) < 3:
            return None
        conv, i = sig[0], 1
        if conv & 0x10:          # GENERIC: число параметров-типов
            i += 1
        count = sig[i]
        if count & 0x80:
            return None
        ret_void = sig[i + 1] == 0x01
        has_this = bool(conv & 0x20) and not (conv & 0x40)
        if newobj:
            return count, 1
        return count + (1 if has_this else 0), 0 if ret_void else 1

    def user_string(self, token: int) -> Optional[str]:
        """Строка из кучи #US по токену ldstr."""
        if token >> 24 != 0x70 or "#US" not in self.streams:
            return None
        base, size = self.streams["#US"]
        off = token & 0xFFFFFF
        if off >= size:
            return None
        p = base + off
        b = self.data[p]
        if b & 0x80 == 0:
            n, p = b, p + 1
        elif b & 0xC0 == 0x80:
            n, p = ((b & 0x3F) << 8) | self.data[p + 1], p + 2
        else:
            n, p = ((b & 0x1F) << 24) | (self.data[p + 1] << 16) | (self.data[p + 2] << 8) | self.data[p + 3], p + 4
        return self.data[p:p + max(0, n - 1)].decode("utf-16-le", "replace")

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


# ---------------------------------------------------------------- склейки строк

class _Lit(str):
    """Строковый литерал на стеке вычислений."""


_VAL = object()        # неизвестное значение (переменная, результат вызова)


class _Arr:
    """Массив из newarr: элементы по индексам (для String.Concat(string[]))."""

    def __init__(self, size: Optional[int]):
        self.size = size
        self.items: Dict[int, object] = {}


# опкоды, которые кладут одно значение, ничего не снимая
_PUSH1 = set(range(0x02, 0x0A)) | {0x0E, 0x0F, 0x11, 0x12, 0x14, 0x21, 0x22, 0x23, 0x7E, 0x7F, 0xD0}
# снимают одно и кладут одно (конверсии, box, поля экземпляра, длина массива…)
_POP1_PUSH1 = {0x7B, 0x7C, 0x8C, 0xA5, 0x74, 0x75, 0x8E, 0x79, 0x71, 0x65, 0x66, 0x76} | set(range(0x67, 0x6F)) | \
    set(range(0x82, 0x8C)) | set(range(0xB3, 0xBB)) | {0xD1, 0xD2, 0xD3, 0xD4, 0xD5}
# снимают два, кладут одно: арифметика, ldelema/ldelem*, *.ovf
_POP2_PUSH1 = set(range(0x58, 0x65)) | {0x8F, 0xA3} | set(range(0x90, 0x9B)) | set(range(0xD6, 0xDC))
_STELEM = set(range(0x9B, 0xA3)) | {0xA4}                       # stelem.* — снимают три
_POP1 = set(range(0x0A, 0x0E)) | {0x10, 0x13, 0x26, 0x80}      # stloc*, starg.s, pop, stsfld
_POP2 = {0x7D, 0x81}                                            # stfld, stobj
_NOP = {0x00, 0x01}
_BRANCH = set(range(0x2B, 0x45)) | {0x45, 0xDD, 0xDE, 0x2A, 0x7A, 0xDC}
# двухбайтовые (0xFE xx): префиксы ничего не меняют, остальное — эффект на стек
_FE_NOP = {0x12, 0x13, 0x14, 0x16, 0x19, 0x1E}
_FE_EFFECT = {0x01: (2, 1), 0x02: (2, 1), 0x03: (2, 1), 0x04: (2, 1), 0x05: (2, 1), 0x06: (0, 1), 0x07: (1, 1),
              0x09: (0, 1), 0x0A: (0, 1), 0x0B: (1, 0), 0x0C: (0, 1), 0x0D: (0, 1), 0x0E: (1, 0), 0x0F: (1, 1),
              0x15: (1, 0), 0x1C: (0, 1), 0x1D: (1, 1)}


def concat_templates(meta: "_Meta", limit: int = 5000) -> List[str]:
    """Шаблоны склеек «литерал + значение + литерал…» из тел методов: ``"Day " + n + " of " + m``
    даёт ``"Day {0} of {1}"``. Стек вычисляется упрощённо: на ветвлениях и незнакомых
    инструкциях сбрасывается — лучше пропустить шаблон, чем придумать неверный."""
    out: List[str] = []
    seen = set()
    for rva, _name in meta.methods:
        if not rva or len(out) >= limit:
            continue
        try:
            for t in _method_templates(meta, rva):
                if t not in seen:
                    seen.add(t)
                    out.append(t)
        except (struct.error, IndexError, ValueError):
            continue
    return out


def _method_templates(meta: "_Meta", rva: int) -> List[str]:
    d = meta.data
    p = meta.off(rva)
    head = d[p]
    if head & 3 == 2:
        size, code = head >> 2, p + 1
    elif head & 3 == 3:
        flags = struct.unpack_from("<H", d, p)[0]
        size, code = struct.unpack_from("<I", d, p + 4)[0], p + 4 * (flags >> 12)
    else:
        return []
    found: List[str] = []
    stack: List[object] = []
    i, end = code, min(code + size, len(d))

    def pop(n: int) -> Optional[List[object]]:
        if n > len(stack):
            return None
        if n == 0:
            return []
        items = stack[-n:]
        del stack[-n:]
        return items

    while i < end:
        op = d[i]
        i += 1
        if op == 0xFE:
            op2 = d[i]
            i += 1 + (_OP2.get(op2) or 0)
            if op2 in _FE_NOP:
                continue
            eff2 = _FE_EFFECT.get(op2)
            if eff2 is None or pop(eff2[0]) is None:
                stack.clear()
                continue
            if eff2[1]:
                stack.append(_VAL)
            continue
        if op == 0x45:
            count = struct.unpack_from("<I", d, i)[0]
            i += 4 + 4 * count
            stack.clear()
            continue
        n = _OP1.get(op)
        if n is None:
            break
        arg = d[i:i + n]
        i += n
        if op == 0x72:                                   # ldstr
            s = meta.user_string(struct.unpack_from("<I", arg)[0])
            stack.append(_Lit(s) if s is not None else _VAL)
        elif 0x15 <= op <= 0x1E:                         # ldc.i4.m1 … ldc.i4.8
            stack.append(op - 0x16)
        elif op == 0x1F:                                 # ldc.i4.s
            stack.append(struct.unpack_from("<b", arg)[0])
        elif op == 0x20:                                 # ldc.i4
            stack.append(struct.unpack_from("<i", arg)[0])
        elif op in _PUSH1:
            stack.append(_VAL)
        elif op == 0x25:                                 # dup
            if not stack:
                stack.clear()
                continue
            stack.append(stack[-1])
        elif op == 0x8D:                                 # newarr
            got = pop(1)
            if got is None:
                stack.clear()
                continue
            stack.append(_Arr(got[0] if isinstance(got[0], int) else None))
        elif op in _NOP:
            continue
        elif op in _STELEM:                              # stelem.*
            got = pop(3)
            if got is None:
                stack.clear()
                continue
            arr, idx, val = got
            if isinstance(arr, _Arr) and isinstance(idx, int):
                arr.items[idx] = val
        elif op in _POP1_PUSH1:
            if pop(1) is None:
                stack.clear()
                continue
            stack.append(_VAL)
        elif op in _POP2_PUSH1:
            if pop(2) is None:
                stack.clear()
                continue
            stack.append(_VAL)
        elif op in _POP1:
            if pop(1) is None:
                stack.clear()
        elif op in _POP2:
            if pop(2) is None:
                stack.clear()
        elif op in (0x28, 0x6F, 0x73):                   # call, callvirt, newobj
            token = struct.unpack_from("<I", arg)[0]
            eff = meta.stack_effect(token, newobj=op == 0x73)
            name = meta.name_of(token)
            owner = meta.owner_of(token)
            if eff is None:
                stack.clear()
                continue
            args = pop(eff[0])
            if args is None:
                stack.clear()
                continue
            if name == "Concat" and owner == "String" and op == 0x28:
                parts = args
                if len(args) == 1 and isinstance(args[0], _Arr):
                    arr = args[0]
                    size = arr.size if arr.size is not None else (max(arr.items) + 1 if arr.items else 0)
                    parts = [arr.items.get(k, _VAL) for k in range(size)]
                t = _template(parts)
                if t:
                    found.append(t)
            if eff[1]:
                stack.append(_VAL)
        elif op in _BRANCH:
            stack.clear()
        else:
            stack.clear()
    return found


def _template(parts: List[object]) -> Optional[str]:
    """Склейка -> шаблон с {0}, {1}…: нужны и литералы со словами, и хотя бы одно значение."""
    out, n, letters = [], 0, 0
    for x in parts:
        if isinstance(x, _Lit):
            out.append(str(x).replace("{", "{{").replace("}", "}}"))
            letters += sum(1 for c in x if c.isalpha())
        elif isinstance(x, (_Arr, int)) or x is _VAL:
            if out and out[-1].startswith("{") and out[-1].endswith("}") and not out[-1].startswith("{{"):
                return None                  # два значения подряд — шаблон неоднозначен
            out.append("{%d}" % n)
            n += 1
        else:
            return None
    if not n or n > 4 or letters < 3:
        return None
    return "".join(out)


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
