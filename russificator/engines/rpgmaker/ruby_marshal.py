"""Ruby Marshal 4.8 — чтение и запись данных RPG Maker XP/VX/VX Ace.

Реализовано по семантике ``marshal.c`` Ruby:
  * целые: 0; ``n+5`` одним байтом для 1..122; ``n-5`` для -123..-1;
    иначе 1–4 байта (положительные) или «отрицательная длина»;
  * таблица объектов для ссылок ``@``: регистрируются строки, массивы,
    хэши, объекты, float, bignum, регулярные выражения, структуры,
    ``u``/``U``-объекты, классы/модули — ровно в том порядке, в каком их
    читает Ruby (иначе ссылки указывают не туда и файл портится);
  * символы — отдельная таблица (``:`` / ``;``);
  * ``I`` — переменные экземпляра строки (``E: true`` = UTF-8 в Ruby 1.9+,
    RPG Maker VX Ace). Для новых строк перевода кодировка ставится так же,
    как в исходном файле — иначе VX Ace падает на кириллице.

Символы и исходные строки — отдельные подклассы ``str``, чтобы при записи
символ не превратился в строку, а совпавшие по тексту разные строки не
склеились в одну ссылку.
"""

from __future__ import annotations

import io
import re
from typing import Any, Dict, List, Optional, Tuple


class MarshalError(ValueError):
    pass


class RubySymbol(str):
    pass


class RubyString(str):
    """Строка из файла: помнит свои ivars (кодировку) и точные байты."""

    def __new__(cls, value: str, ivars: Optional[List[Tuple[str, Any]]] = None):
        obj = str.__new__(cls, value)
        obj.ivars = ivars or []
        return obj


class RubyObject:
    def __init__(self, classname: str, ivars: Optional[Dict[str, Any]] = None):
        self.classname = classname
        self.ivars: Dict[str, Any] = ivars if ivars is not None else {}

    def get(self, name: str, default=None):
        return self.ivars.get("@" + name, default)

    def __repr__(self) -> str:  # pragma: no cover
        return f"RubyObject({self.classname}, {list(self.ivars)})"


class RubyHash(dict):
    """Hash (с необязательным значением по умолчанию)."""

    NO_DEFAULT = object()

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.default = RubyHash.NO_DEFAULT


class RubyUserDef:
    """Объект с _dump (u): Table, Color, Tone в RPG Maker — байты храним как есть."""

    def __init__(self, classname: str, payload: bytes, ivars: Optional[list] = None):
        self.classname, self.payload, self.ivars = classname, payload, ivars or []


class RubyUserMarshal:
    """Объект с marshal_dump (U)."""

    def __init__(self, classname: str, data: Any):
        self.classname, self.data = classname, data


class RubyStruct:
    def __init__(self, classname: str, members: Dict[str, Any]):
        self.classname, self.members = classname, members


class RubyUserClass:
    """Подкласс String/Array/Hash (C)."""

    def __init__(self, classname: str, value: Any):
        self.classname, self.value = classname, value


class RubyExtended:
    def __init__(self, module: str, value: Any):
        self.module, self.value = module, value


class RubyClassRef:
    def __init__(self, name: str, kind: str):
        self.name, self.kind = name, kind   # kind: "c" | "m" | "M"


class RubyRegexp:
    def __init__(self, source: bytes, options: int, ivars: Optional[list] = None):
        self.source, self.options, self.ivars = source, options, ivars or []


def _s(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _b(s: str) -> bytes:
    return s.encode("utf-8", "surrogateescape")


# ---------- чтение ----------

class MarshalReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.symbols: List[str] = []
        self.objects: List[Any] = []
        self.has_encoding = False   # встречались строки с E (Ruby 1.9+)

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise MarshalError("неожиданный конец данных")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def read(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise MarshalError("неожиданный конец данных")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def long(self) -> int:
        c = self.byte()
        if c == 0:
            return 0
        if c > 127:
            c -= 256
        if 4 < c < 128:
            return c - 5
        if -129 < c < -4:
            return c + 5
        if c > 0:
            return int.from_bytes(self.read(c), "little")
        n = -c
        return int.from_bytes(self.read(n), "little") - (1 << (8 * n))

    def bytes_(self) -> bytes:
        return self.read(self.long())

    def symbol(self) -> str:
        t = chr(self.byte())
        if t == ":":
            sym = RubySymbol(_s(self.bytes_()))
            self.symbols.append(sym)
            return sym
        if t == ";":
            return self.symbols[self.long()]
        if t == "I":  # символ с кодировкой
            sym = self.symbol()
            for _ in range(self.long()):
                self.symbol()
                self.value()
            return sym
        raise MarshalError(f"ожидался символ, получено {t!r}")

    def _register(self, obj: Any) -> int:
        self.objects.append(obj)
        return len(self.objects) - 1

    def load(self) -> Any:
        if self.read(2) != b"\x04\x08":
            raise MarshalError("не Ruby Marshal 4.8")
        return self.value()

    def value(self) -> Any:
        t = chr(self.byte())
        if t == "0":
            return None
        if t == "T":
            return True
        if t == "F":
            return False
        if t == "i":
            return self.long()
        if t == ":":
            self.pos -= 1
            return self.symbol()
        if t == ";":
            return self.symbols[self.long()]
        if t == "@":
            return self.objects[self.long()]
        if t == "I":
            start = self.pos
            obj = self.value()
            n = self.long()
            ivars = []
            for _ in range(n):
                k = self.symbol()
                v = self.value()
                ivars.append((k, v))
                if k in ("E", "encoding"):
                    self.has_encoding = True
            if isinstance(obj, RubyString):
                obj.ivars = ivars
            elif isinstance(obj, (RubyUserDef, RubyRegexp)):
                obj.ivars = ivars
            return obj
        if t == '"':
            s = RubyString(_s(self.bytes_()))
            self._register(s)
            return s
        if t == "f":
            raw = self.bytes_().split(b"\0")[0].decode("ascii")
            val = {"nan": float("nan"), "inf": float("inf"), "-inf": float("-inf")}.get(raw)
            f = val if val is not None else float(raw)
            self._register(f)
            return f
        if t == "l":
            sign = chr(self.byte())
            n = self.long() * 2
            v = int.from_bytes(self.read(n), "little")
            v = -v if sign == "-" else v
            self._register(v)
            return v
        if t == "[":
            n = self.long()
            arr: List[Any] = []
            self._register(arr)
            for _ in range(n):
                arr.append(self.value())
            return arr
        if t in "{}":
            n = self.long()
            h = RubyHash()
            self._register(h)
            for _ in range(n):
                k = self.value()
                h[_hashable(k)] = self.value()
            if t == "}":
                h.default = self.value()
            return h
        if t == "o":
            cls = self.symbol()
            obj = RubyObject(cls)
            self._register(obj)
            for _ in range(self.long()):
                k = self.symbol()
                obj.ivars[k] = self.value()
            return obj
        if t == "u":
            cls = self.symbol()
            payload = self.bytes_()
            obj = RubyUserDef(cls, payload)
            self._register(obj)
            return obj
        if t == "U":
            cls = self.symbol()
            obj = RubyUserMarshal(cls, None)
            self._register(obj)
            obj.data = self.value()
            return obj
        if t == "S":
            cls = self.symbol()
            st = RubyStruct(cls, {})
            self._register(st)
            for _ in range(self.long()):
                k = self.symbol()
                st.members[k] = self.value()
            return st
        if t == "C":
            cls = self.symbol()
            return RubyUserClass(cls, self.value())
        if t == "e":
            mod = self.symbol()
            return RubyExtended(mod, self.value())
        if t in "cmM":
            ref = RubyClassRef(_s(self.bytes_()), t)
            self._register(ref)
            return ref
        if t == "/":
            src = self.bytes_()
            opts = self.byte()
            rx = RubyRegexp(src, opts)
            self._register(rx)
            return rx
        raise MarshalError(f"неподдерживаемый тип {t!r} в позиции {self.pos - 1}")


def _hashable(k: Any) -> Any:
    if isinstance(k, list):
        return tuple(k)
    return k


# ---------- запись ----------

class MarshalWriter:
    def __init__(self, utf8_strings: bool):
        self.out = io.BytesIO()
        self.symbols: Dict[str, int] = {}
        self.objects: Dict[int, int] = {}
        self.count = 0
        self.utf8 = utf8_strings
        self._keep: List[Any] = []   # держим записанные объекты живыми (id не переиспользуется)

    def w(self, b: bytes) -> None:
        self.out.write(b)

    def long(self, v: int) -> None:
        if v == 0:
            self.w(b"\0")
        elif 0 < v < 123:
            self.w(bytes([v + 5]))
        elif -124 < v < 0:
            self.w(bytes([(v - 5) & 0xFF]))
        elif v > 0:
            n = (v.bit_length() + 7) // 8
            self.w(bytes([n]) + v.to_bytes(n, "little"))
        else:
            n = 1
            while v < -(1 << (8 * n)):
                n += 1
            self.w(bytes([256 - n]) + (v + (1 << (8 * n))).to_bytes(n, "little"))

    def bytes_(self, b: bytes) -> None:
        self.long(len(b))
        self.w(b)

    def symbol(self, name: str) -> None:
        if name in self.symbols:
            self.w(b";")
            self.long(self.symbols[name])
            return
        self.symbols[name] = len(self.symbols)
        self.w(b":")
        self.bytes_(_b(name))

    def _link(self, obj: Any) -> bool:
        idx = self.objects.get(id(obj))
        if idx is not None:
            self.w(b"@")
            self.long(idx)
            return True
        return False

    def _register(self, obj: Any, linkable: bool = True) -> None:
        if linkable:
            self.objects[id(obj)] = self.count
            self._keep.append(obj)
        self.count += 1

    def dump(self, value: Any) -> bytes:
        self.w(b"\x04\x08")
        self.value(value)
        return self.out.getvalue()

    def value(self, v: Any) -> None:
        if v is None:
            self.w(b"0")
        elif v is True:
            self.w(b"T")
        elif v is False:
            self.w(b"F")
        elif isinstance(v, RubySymbol):
            self.symbol(v)
        elif isinstance(v, int):
            if -(1 << 31) <= v < (1 << 31):  # Ruby пишет Fixnum как «i», если влезает в 32 бита
                self.w(b"i")
                self.long(v)
            else:
                self.w(b"l")
                self.w(b"+" if v >= 0 else b"-")
                mag = abs(v)
                raw = mag.to_bytes(max(1, (mag.bit_length() + 7) // 8), "little")
                if len(raw) % 2:
                    raw += b"\0"
                self.long(len(raw) // 2)
                self.w(raw)
                self._register(v, linkable=False)
        elif isinstance(v, float):
            self.w(b"f")
            if v != v:
                txt = "nan"
            elif v in (float("inf"), float("-inf")):
                txt = "inf" if v > 0 else "-inf"
            else:
                txt = repr(v)
                if txt.endswith(".0"):
                    txt = txt[:-2]
            self.bytes_(txt.encode("ascii"))
            self._register(v, linkable=False)
        elif isinstance(v, RubyString):
            if self._link(v):
                return
            if v.ivars:
                self.w(b"I")
            self.w(b'"')
            self.bytes_(_b(v))
            self._register(v)
            if v.ivars:
                self.long(len(v.ivars))
                for k, val in v.ivars:
                    self.symbol(k)
                    self.value(val)
        elif isinstance(v, str):  # новая строка (перевод)
            if self.utf8:
                self.w(b'I"')
            else:
                self.w(b'"')
            self.bytes_(v.encode("utf-8"))
            self._register(v, linkable=False)
            if self.utf8:
                self.long(1)
                self.symbol("E")
                self.w(b"T")
        elif isinstance(v, list):
            if self._link(v):
                return
            self.w(b"[")
            self.long(len(v))
            self._register(v)
            for item in v:
                self.value(item)
        elif isinstance(v, dict):
            if self._link(v):
                return
            default = getattr(v, "default", RubyHash.NO_DEFAULT)
            self.w(b"}" if default is not RubyHash.NO_DEFAULT else b"{")
            self.long(len(v))
            self._register(v)
            for k, val in v.items():
                self.value(list(k) if isinstance(k, tuple) else k)
                self.value(val)
            if default is not RubyHash.NO_DEFAULT:
                self.value(default)
        elif isinstance(v, RubyObject):
            if self._link(v):
                return
            self.w(b"o")
            self.symbol(v.classname)
            self._register(v)
            self.long(len(v.ivars))
            for k, val in v.ivars.items():
                self.symbol(k)
                self.value(val)
        elif isinstance(v, RubyUserDef):
            if self._link(v):
                return
            if v.ivars:
                self.w(b"I")
            self.w(b"u")
            self.symbol(v.classname)
            self.bytes_(v.payload)
            self._register(v)
            if v.ivars:
                self.long(len(v.ivars))
                for k, val in v.ivars:
                    self.symbol(k)
                    self.value(val)
        elif isinstance(v, RubyUserMarshal):
            if self._link(v):
                return
            self.w(b"U")
            self.symbol(v.classname)
            self._register(v)
            self.value(v.data)
        elif isinstance(v, RubyStruct):
            if self._link(v):
                return
            self.w(b"S")
            self.symbol(v.classname)
            self._register(v)
            self.long(len(v.members))
            for k, val in v.members.items():
                self.symbol(k)
                self.value(val)
        elif isinstance(v, RubyUserClass):
            self.w(b"C")
            self.symbol(v.classname)
            self.value(v.value)
        elif isinstance(v, RubyExtended):
            self.w(b"e")
            self.symbol(v.module)
            self.value(v.value)
        elif isinstance(v, RubyClassRef):
            if self._link(v):
                return
            self.w(v.kind.encode("ascii"))
            self.bytes_(_b(v.name))
            self._register(v)
        elif isinstance(v, RubyRegexp):
            if self._link(v):
                return
            if v.ivars:
                self.w(b"I")
            self.w(b"/")
            self.bytes_(v.source)
            self.w(bytes([v.options]))
            self._register(v)
            if v.ivars:
                self.long(len(v.ivars))
                for k, val in v.ivars:
                    self.symbol(k)
                    self.value(val)
        else:
            raise MarshalError(f"не умею записать {type(v).__name__}")


def load(data: bytes) -> Any:
    return MarshalReader(data).load()


def load_with_info(data: bytes) -> Tuple[Any, bool]:
    """Вернуть (объект, строки_в_UTF-8_с_кодировкой) — второе нужно для записи."""
    r = MarshalReader(data)
    return r.load(), r.has_encoding


def dump(value: Any, utf8_strings: bool = False) -> bytes:
    return MarshalWriter(utf8_strings).dump(value)


def is_text(v: Any) -> bool:
    return isinstance(v, str) and not isinstance(v, RubySymbol)
