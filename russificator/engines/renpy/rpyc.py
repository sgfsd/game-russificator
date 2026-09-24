"""Чтение скомпилированных скриптов Ren'Py (.rpyc) — без Ren'Py и без выполнения кода.

.rpyc — это zlib-сжатый pickle дерева операторов. Любой класс Ren'Py при
распаковке подменяется «заглушкой», которая просто хранит состояние объекта
(так работает и unrpyc), поэтому ни одна строка кода игры не выполняется.

Из дерева берётся ровно то, что игрок видит на экране:
  * реплики (``Say.what``) и пункты меню (``Menu.items``) — строки в том
    виде, в каком их получает ``config.say_menu_text_filter``;
  * имена персонажей из ``define x = Character("Имя")``;
  * текст экранов (``text "..."``, ``textbutton _("...")``) и ``_("...")``
    в Python-блоках.
"""

from __future__ import annotations

import ast
import io
import pickle
import re
import struct
import zlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

RPYC2_HEADER = b"RENPY RPC2"


class _Stub:
    """Экземпляр любого класса Ren'Py: хранит состояние, ничего не исполняет."""

    _cls = ""

    def __init__(self, *args, **kwargs):
        self._args = args

    def __setstate__(self, state):
        if isinstance(state, tuple) and len(state) == 2 and isinstance(state[1], dict) \
                and (state[0] is None or isinstance(state[0], dict)):
            if state[0]:
                self.__dict__.update(state[0])
            self.__dict__.update(state[1])
        elif isinstance(state, dict):
            self.__dict__.update(state)
        else:
            self._state = state
            if isinstance(state, tuple) and len(state) >= 2 and isinstance(state[1], str):
                self.source = state[1]  # renpy.ast.PyCode: (1, source, location, mode[, py])

    # классы-наследники list/dict (RevertableList и т.п.) распаковываются через append/setitem
    def append(self, item):
        self.__dict__.setdefault("_items", []).append(item)

    def extend(self, items):
        self.__dict__.setdefault("_items", []).extend(items)

    def __setitem__(self, key, value):
        self.__dict__.setdefault("_map", {})[key] = value

    def add(self, item):
        self.append(item)


class _StrStub(str):
    """renpy.ast.PyExpr — строка с кодом Python (плюс файл/строка)."""

    def __new__(cls, s="", *args, **kwargs):
        return str.__new__(cls, s)

    def __setstate__(self, state):
        pass


def _reconstructor(cls, base, state=None):
    obj = cls.__new__(cls) if isinstance(cls, type) else _Stub()
    return obj


_SAFE_BUILTINS = {"set": set, "frozenset": frozenset, "list": list, "dict": dict, "tuple": tuple,
                  "bytearray": bytearray, "bytes": bytes, "str": str, "unicode": str, "int": int,
                  "long": int, "float": float, "complex": complex, "bool": bool, "object": object,
                  "slice": slice, "range": range}


class _SafeUnpickler(pickle.Unpickler):
    _classes: Dict[Tuple[str, str], type] = {}

    def find_class(self, module, name):
        if module in ("builtins", "__builtin__") and name in _SAFE_BUILTINS:
            return _SAFE_BUILTINS[name]
        if module in ("collections",) and name in ("OrderedDict", "defaultdict", "deque"):
            import collections
            return getattr(collections, name)
        if module in ("copy_reg", "copyreg") and name == "_reconstructor":
            return _reconstructor
        key = (module, name)
        cls = self._classes.get(key)
        if cls is None:
            base = _StrStub if name in ("PyExpr",) else _Stub
            cls = type(name, (base,), {"_cls": f"{module}.{name}", "__module__": "russificator.renpy_stub"})
            self._classes[key] = cls
        return cls


def load_rpyc(data: bytes):
    """Вернуть список операторов верхнего уровня (или None, если файл не разобран)."""
    raw = None
    if data.startswith(RPYC2_HEADER):
        pos = len(RPYC2_HEADER)
        slots = {}
        while pos + 12 <= len(data):
            slot, start, length = struct.unpack("<III", data[pos:pos + 12])
            if slot == 0:
                break
            slots[slot] = (start, length)
            pos += 12
        for slot in (1, 2):
            if slot in slots:
                start, length = slots[slot]
                raw = zlib.decompress(data[start:start + length])
                break
    else:
        raw = zlib.decompress(data)
    if raw is None:
        return None
    obj = _SafeUnpickler(io.BytesIO(raw), encoding="utf-8", errors="replace").load()
    if isinstance(obj, tuple) and len(obj) == 2:
        return obj[1]
    return obj


# ---------- обход дерева ----------

def iter_nodes(root) -> List[_Stub]:
    """Все объекты-заглушки дерева (без рекурсии — деревья бывают очень глубокими)."""
    out: List[_Stub] = []
    seen = set()
    stack = [root]
    while stack:
        o = stack.pop()
        if isinstance(o, (str, bytes, int, float, bool)) or o is None:
            continue
        oid = id(o)
        if oid in seen:
            continue
        seen.add(oid)
        if isinstance(o, _Stub):
            out.append(o)
            for k, v in vars(o).items():
                if k != "next":
                    stack.append(v)
        elif isinstance(o, dict):
            stack.extend(o.values())
        elif isinstance(o, (list, tuple, set, frozenset)):
            stack.extend(o)
    return out


def _cls(node) -> str:
    return getattr(type(node), "_cls", "")


@dataclass
class FoundText:
    text: str
    kind: str            # "dialogue" | "narration" | "choice" | "name" | "ui"
    who: Optional[str]   # переменная персонажа (для реплик)
    file: str
    line: int


_CHAR_RE = re.compile(r"""^\s*(?:Character|DynamicCharacter|ADVCharacter|NVLCharacter)\s*\(\s*(_\(\s*)?([ru]?(['"])(?:\\.|(?!\3).)*\3)""")
_UNDERSCORE_RE = re.compile(r"""(?<![\w.])__?\(\s*([ru]?(['"])(?:\\.|(?!\2).)*\2)\s*\)""")
_LITERAL_RE = re.compile(r"""^\s*(?:__?\(\s*)?([ru]?(['"])(?:\\.|(?!\2).)*\2)\s*\)?\s*$""")
#: строки, которые код показывает игроку: renpy.notify("…"), Notify("…"), renpy.input("…"),
#: renpy.say(who, "…"), Text("…"), Confirm("…"), Tooltip("…")
_DISPLAY_CALL_RE = re.compile(r"""(?<![\w.])(?:renpy\.)?(?:notify|Notify|input|say|Text|Confirm|confirm|Tooltip|"""
                              r"""display_notify|display_menu_item)\s*\(\s*(?:[\w.]+\s*,\s*)?"""
                              r"""([ru]?(['"])(?:\\.|(?!\2).)*\2)""")


def display_strings(src: str) -> List[str]:
    """Строки-аргументы вызовов, которые выводят текст (без _() — их ищет _UNDERSCORE_RE)."""
    out = []
    for m in _DISPLAY_CALL_RE.finditer(src):
        val = _literal(m.group(1))
        if val and val.strip():
            out.append(val)
    return out


def _literal(src: str) -> Optional[str]:
    try:
        val = ast.literal_eval(src)
    except (ValueError, SyntaxError):
        return None
    return val if isinstance(val, str) else None


def extract_texts(stmts, filename: str) -> Tuple[List[FoundText], Dict[str, str]]:
    """Тексты скрипта и словарь «переменная персонажа -> имя»."""
    found: List[FoundText] = []
    characters: Dict[str, str] = {}
    for node in iter_nodes(stmts):
        cls = _cls(node)
        line = int(getattr(node, "linenumber", 0) or 0)
        if cls.endswith("ast.Say"):
            what = getattr(node, "what", None)
            if isinstance(what, str) and what.strip():
                who = getattr(node, "who", None)
                who = str(who) if who else None
                found.append(FoundText(str(what), "dialogue" if who else "narration", who, filename, line))
        elif cls.endswith("ast.Menu"):
            for item in getattr(node, "items", None) or []:
                if isinstance(item, (list, tuple)) and item and isinstance(item[0], str) and item[0].strip():
                    kind = "choice" if len(item) > 2 and item[2] is not None else "narration"
                    found.append(FoundText(str(item[0]), kind, None, filename, line))
        elif cls.endswith("ast.Define") or cls.endswith("ast.Default"):
            code = getattr(node, "code", None)
            src = getattr(code, "source", "") or ""
            m = _CHAR_RE.match(src)
            if m:
                name = _literal(m.group(2))
                if name and name.strip():
                    characters[str(getattr(node, "varname", ""))] = name
                    found.append(FoundText(name, "name", None, filename, line))
        elif cls.endswith("ast.PyCode"):
            src = getattr(node, "source", "") or ""
            if isinstance(src, str):
                for m in _UNDERSCORE_RE.finditer(src):
                    val = _literal(m.group(1))
                    if val and val.strip():
                        found.append(FoundText(val, "ui", None, filename, line))
                for val in display_strings(src):
                    found.append(FoundText(val, "ui", None, filename, line))
        elif ".sl2." in cls or "screenlang" in cls:
            for arg in getattr(node, "positional", None) or []:
                if isinstance(arg, str):
                    m = _LITERAL_RE.match(arg)
                    if m:
                        val = _literal(m.group(1))
                        if val and val.strip():
                            found.append(FoundText(val, "ui", None, filename, line))
            # свойства экранов: tooltip "…", hovered Notify("…"), action Confirm("…", …)
            for kw in getattr(node, "keyword", None) or []:
                if not (isinstance(kw, (list, tuple)) and len(kw) == 2 and isinstance(kw[1], str)):
                    continue
                if kw[0] in ("tooltip", "alt", "caption", "prompt"):
                    m = _LITERAL_RE.match(kw[1])
                    val = _literal(m.group(1)) if m else None
                    if val and val.strip():
                        found.append(FoundText(val, "ui", None, filename, line))
                for val in display_strings(kw[1]):
                    found.append(FoundText(val, "ui", None, filename, line))
    found.sort(key=lambda f: f.line)
    return found, characters


def font_references(stmts) -> List[str]:
    """Имена файлов шрифтов, упомянутые в коде игры (gui.text_font, стили и т.п.)."""
    refs = set()
    for node in iter_nodes(stmts):
        for v in vars(node).values():
            srcs = [v] if isinstance(v, str) else []
            src = getattr(v, "source", None)
            if isinstance(src, str):
                srcs.append(src)
            for s in srcs:
                for m in re.finditer(r"""['"]([^'"\n]+\.(?:ttf|otf|ttc))['"]""", s, re.I):
                    refs.add(m.group(1))
    return sorted(refs)
