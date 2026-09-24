"""Чтение архивов Ren'Py (.rpa) — только чтение, без распаковки на диск.

Поддерживаются RPA-3.0 (с ключом-обфускацией), RPA-3.2 и RPA-2.0. Индекс
архива — pickle со словарём {имя: [(смещение, длина[, префикс])]}; он
разбирается «безопасным» распаковщиком, который не создаёт никаких
объектов, кроме базовых типов.
"""

from __future__ import annotations

import io
import pickle
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class _SafeUnpickler(pickle.Unpickler):
    """Только базовые типы: так пишут индекс и Ren'Py 7 (Python 2), и Ren'Py 8."""

    _ALLOWED = {("builtins", "bytes"): bytes, ("__builtin__", "bytes"): bytes,
                ("builtins", "str"): str, ("__builtin__", "str"): str, ("__builtin__", "unicode"): str,
                ("builtins", "set"): set, ("__builtin__", "set"): set,
                ("builtins", "list"): list, ("__builtin__", "list"): list,
                ("builtins", "tuple"): tuple, ("__builtin__", "tuple"): tuple}

    def find_class(self, module, name):  # noqa: D401
        if (module, name) in self._ALLOWED:
            return self._ALLOWED[(module, name)]
        if module == "_codecs" and name == "encode":
            import codecs
            return codecs.encode
        raise pickle.UnpicklingError(f"в индексе архива недопустимый объект {module}.{name}")


class RpaArchive:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.index: Dict[str, Tuple[int, int, bytes]] = {}
        self.error = ""
        try:
            self._read_index()
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.index = {}

    def _read_index(self) -> None:
        with self.path.open("rb") as fh:
            head = fh.read(64).split(b"\n", 1)[0]
            parts = head.split()
            if not parts:
                raise ValueError("пустой архив")
            kind = parts[0]
            if kind in (b"RPA-3.0", b"RPA-3.2"):
                offset = int(parts[1], 16)
                key = 0
                for p in parts[2:]:
                    key ^= int(p, 16)
            elif kind == b"RPA-2.0":
                offset, key = int(parts[1], 16), 0
            else:
                raise ValueError(f"неизвестный формат архива {kind[:16]!r} (игра, возможно, защищена)")
            fh.seek(offset)
            raw = zlib.decompress(fh.read())
        index = _SafeUnpickler(io.BytesIO(raw), encoding="latin1").load()
        for name, entries in index.items():
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            else:
                try:
                    name = name.encode("latin1").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            e = entries[0]
            off, length = e[0] ^ key, e[1] ^ key
            prefix = e[2] if len(e) > 2 else b""
            if isinstance(prefix, str):
                prefix = prefix.encode("latin1")
            self.index[name.replace("\\", "/")] = (off, length, prefix or b"")

    def names(self) -> List[str]:
        return sorted(self.index)

    def read(self, name: str) -> Optional[bytes]:
        e = self.index.get(name)
        if e is None:
            return None
        off, length, prefix = e
        with self.path.open("rb") as fh:
            fh.seek(off)
            return prefix + fh.read(length - len(prefix))


def open_archives(game_root: Path) -> List[RpaArchive]:
    return [RpaArchive(p) for p in sorted(Path(game_root).rglob("*.rpa"))]
