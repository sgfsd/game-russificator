"""Чтение ассетов Unity через UnityPy без «зависших» файлов игры.

UnityPy открывает файл ассетов (и то, что тот подтягивает: .resS, соседние файлы) и держит
его открытым, пока объекты не соберёт сборщик мусора, — а они ссылаются друг на друга, так
что в долго работающей программе это может не случиться никогда. Windows не даёт изменить
или удалить открытый файл: Steam при проверке/переустановке игры пишет «Файл с контентом
заблокирован», пока не закрыть программу. Поэтому каждое чтение идёт через
:func:`load_assets`: по выходе из блока все потоки, открытые UnityPy, закрываются явно.
"""

from __future__ import annotations

import contextlib
import gc
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

#: версия Unity игры (из :func:`use_game_version`): по ней находятся и длинные ключи защиты бандлов
_GAME_VERSION = ""


def release(env) -> None:
    """Закрыть все файлы, открытые окружением UnityPy (и вложенными файлами бандлов)."""
    seen = set()
    stack = [env] + list((getattr(env, "cabs", None) or {}).values())
    while stack:
        obj = stack.pop()
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        for reader in (obj, getattr(obj, "reader", None)):
            stream = getattr(reader, "stream", None)
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        files = getattr(obj, "files", None)
        if isinstance(files, dict):
            stack.extend(files.values())
    for name in ("files", "cabs"):
        d = getattr(env, name, None)
        if isinstance(d, dict):
            d.clear()
    gc.collect()


def use_game_version(version: str) -> None:
    """Версия Unity игры — для файлов, из которых сборка вырезала номер версии (бандлы IL2CPP-игр:
    «No valid Unity version found»). Без неё UnityPy такие файлы не открывает, и весь текст, шрифты и
    прямой перевод в них пропадают (The Game of Life 2: все бандлы меню и интерфейса)."""
    import re
    global _GAME_VERSION
    if not version or not re.match(r"^\d{4}\.\d+\.\d+[a-z]\d+$", version):
        return
    _GAME_VERSION = version
    try:
        import warnings
        import UnityPy.config as config
        config.FALLBACK_UNITY_VERSION = version
        warnings.filterwarnings("ignore", message="No valid Unity version found")   # на каждый файл
    except Exception:  # noqa: BLE001 — старый UnityPy без этой настройки
        pass


_patched = False


def fix_script_type_index() -> None:
    """Сохранять номер скрипта каждого объекта в файлах Unity 5.x (формат 11–16) как был.

    В этих форматах номер скрипта (ScriptTypeIndex) хранится у каждого объекта в таблице объектов.
    UnityPy при чтении кладёт его в общий на все объекты одного вида SerializedType — каждый следующий
    объект затирает значение предыдущего, и при сохранении все MonoBehaviour файла получают номер
    последнего прочитанного. Unity 5 привязывает их к чужим скриптам: данные пропадают (Crawl после
    дорисовки кириллицы в шрифт падал в главном меню на создании достижений). Здесь номер
    запоминается у каждого объекта при чтении и подставляется при записи. Новые форматы (17+, Unity
    2017+) хранят номер в таблице типов — их это не касается."""
    global _patched
    if _patched:
        return
    import importlib
    cls = importlib.import_module("UnityPy.files.ObjectReader").ObjectReader
    orig_from_reader = cls.from_reader.__func__
    orig_write = cls.write

    def from_reader(klass, assets_file, reader):
        obj = orig_from_reader(klass, assets_file, reader)
        version = assets_file.header.version
        if 11 <= version < 17:
            end = reader.Position
            # int16 номера скрипта — в конце записи объекта, перед байтом «stripped» (форматы 15–16)
            reader.Position = end - 2 - (1 if version in (15, 16) else 0)
            own = assets_file.__dict__.setdefault("_russificator_script_index", {})
            own[obj.path_id] = reader.read_short()      # у ObjectReader __slots__ — храним в файле
            reader.Position = end
        return obj

    def write(self, header, writer, data_writer):
        own = (getattr(self.assets_file, "_russificator_script_index", None) or {}).get(self.path_id)
        st = self.serialized_type
        if own is None or st is None or not (11 <= header.version < 17):
            return orig_write(self, header, writer, data_writer)
        shared = st.script_type_index
        st.script_type_index = own
        try:
            return orig_write(self, header, writer, data_writer)
        finally:
            st.script_type_index = shared

    cls.from_reader = classmethod(from_reader)
    cls.write = write
    _patched = True


def object_table(env) -> dict:
    """Таблица объектов сохраняемого файла: (номер файла в бандле, path_id) -> (class_id, type_id,
    номер скрипта) — для сверки после сохранения.

    Только сам файл (``env.file`` и файлы внутри него, если это бандл): пока читаются скрипты
    объектов, UnityPy подгружает в окружение соседние файлы игры, а их path_id пересекаются с path_id
    этого файла."""
    out = {}
    stack = [getattr(env, "file", None)]
    n = 0
    while stack:
        f = stack.pop(0)
        objects = getattr(f, "objects", None)
        if isinstance(objects, dict):
            own = getattr(f, "_russificator_script_index", None) or {}
            for pid, obj in objects.items():
                st = obj.serialized_type
                out[(n, pid)] = (obj.class_id, obj.type_id,
                                 own.get(pid, st.script_type_index if st is not None else None))
            n += 1
        files = getattr(f, "files", None)
        if isinstance(files, dict):
            stack.extend(files.values())
    return out


def same_object_table(before: dict, after: dict) -> str:
    """Пусто, если таблицы объектов совпадают; иначе — что разошлось (для отказа от записи файла)."""
    if before.keys() != after.keys():
        return f"состав объектов изменился ({len(before)} -> {len(after)})"
    bad = [pid for pid in before if before[pid] != after[pid]]
    if bad:
        return f"у {len(bad)} объектов изменились тип или номер скрипта (например, {bad[0]})"
    return ""


@contextlib.contextmanager
def load_assets(src) -> Iterator[object]:
    """``UnityPy.load(src)`` на время блока ``with``; файлы закрываются даже при ошибке.

    Путь к бандлу под простой защитой (см. :func:`layout`) открывается расшифрованным в памяти."""
    env = open_env(src)
    try:
        yield env
    finally:
        release(env)


def open_env(src):
    """``UnityPy.load(src)``; защищённый бандл по пути открывается расшифрованным. Закрыть — :func:`release`."""
    import UnityPy
    fix_script_type_index()
    if isinstance(src, (str, Path)) and os.path.isfile(src):
        lay = layout(Path(src))
        if lay is not None and not lay.plain:
            src = decode(Path(src).read_bytes(), lay)
    return UnityPy.load(str(src) if isinstance(src, Path) else src)


# ---------- бандлы под простой защитой ----------
#
# Игры (чаще китайские) прячут бандлы от распаковщиков: весь файл ксорится коротким ключом
# (CardCultivation: «StreamingAssets/*.dat», у каждого файла свой 4-байтный ключ) или перед бандлом
# дописываются лишние байты. Игра снимает защиту сама при загрузке (LoadFromMemory/LoadFromStream), а
# для UnityPy такой файл — не бандл: текст, таблицы локализации и шрифты в нём пропадали, и всё
# переводилось только на лету. Ключ находится по известному началу любого бандла («UnityFS», версия
# формата, «5.x.x», версия Unity, размер файла); изменённый бандл шифруется обратно тем же ключом.

_SIGNATURE = b"UnityFS\0"
_PLAYER = b"5.x.x\0"
#: длиннее ключи не встречались; для ключа длины L нужно L+6 известных байт начала файла
_MAX_KEY = 24
#: сколько байт мусора перед бандлом ещё ищется
_MAX_PREFIX = 64 * 1024


@dataclass(frozen=True)
class Layout:
    """Как бандл лежит в файле: ``key`` — ключ XOR, повторяющийся от начала файла (пусто — без шифра);
    ``offset`` — сколько байт перед бандлом (их игра пропускает при загрузке)."""
    key: bytes = b""
    offset: int = 0

    @property
    def plain(self) -> bool:
        return not self.key and not self.offset


def _known_heads(size: int) -> List[bytes]:
    """Возможные начала бандла UnityFS: версия формата 6–8, с версией Unity и размером файла (если версия
    игры известна) и без них."""
    out = []
    for fmt in (8, 7, 6):
        base = _SIGNATURE + struct.pack(">I", fmt) + _PLAYER
        if _GAME_VERSION:
            out.append(base + _GAME_VERSION.encode("ascii") + b"\0" + struct.pack(">Q", size))
        out.append(base)
    return out


def _solve_xor(head: bytes, size: int) -> Optional[bytes]:
    """Самый короткий ключ XOR, с которым начало файла становится началом бандла (или None)."""
    for known in _known_heads(size):
        if len(head) < len(known):
            continue
        for n in range(1, min(_MAX_KEY, len(known) - 6) + 1):
            key = bytes(head[i] ^ known[i] for i in range(n))
            if all(head[i] ^ key[i % n] == known[i] for i in range(len(known))):
                return key if any(key) else None
    return None


def _bundle_size(head: bytes) -> Optional[int]:
    """Полный размер бандла из его заголовка (подпись, формат, две строки версий, int64 размер)."""
    try:
        pos = head.index(b"\0") + 1 + 4
        pos = head.index(b"\0", pos) + 1
        pos = head.index(b"\0", pos) + 1
        return struct.unpack_from(">Q", head, pos)[0]
    except (ValueError, struct.error):
        return None


def layout(path: Path) -> Optional[Layout]:
    """Как лежит бандл в файле ``path``: ``Layout()`` — обычный файл Unity, иначе ключ XOR или лишние
    байты впереди; None — это не бандл (или защита посложнее). Защищённый файл признаётся бандлом, только
    если размер в его заголовке сходится с размером файла: склейку нескольких бандлов в один файл
    («пакет» игры) переписывать нельзя — остальные бандлы потерялись бы."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(256)
            if head.startswith((b"UnityFS", b"UnityWeb", b"UnityRaw")):
                return Layout()
            key = _solve_xor(head, size)
            if key is not None:
                plain = bytes(c ^ key[i % len(key)] for i, c in enumerate(head))
                return Layout(key=key) if _bundle_size(plain) == size else None
            fh.seek(0)
            chunk = fh.read(_MAX_PREFIX)
    except OSError:
        return None
    at = chunk.find(_SIGNATURE, 1)
    if at > 0 and _bundle_size(chunk[at:at + 256]) == size - at:
        return Layout(offset=at)
    return None


def _xor(buf: bytearray, key: bytes) -> None:
    """XOR буфера с повторяющимся ключом на месте (numpy: сотни мегабайт за доли секунды)."""
    import numpy as np
    a = np.frombuffer(buf, dtype=np.uint8)
    k = np.frombuffer(key, dtype=np.uint8)
    n = len(a) - len(a) % len(k)
    if n:
        a[:n].reshape(-1, len(k))[...] ^= k
    if len(a) > n:
        a[n:] ^= k[:len(a) - n]


def decode(data: bytes, lay: Layout) -> bytes:
    """Бандл из байтов файла (без защиты)."""
    if lay.key:
        buf = bytearray(data)
        _xor(buf, lay.key)
        return bytes(buf)
    return data[lay.offset:]


def as_stored(path: Path, data: bytes) -> bytes:
    """Байты нового бандла в том виде, в каком игра ждёт их в файле ``path``: с тем же ключом или с теми
    же байтами впереди. Обычный файл — без изменений. Вызывать до перезаписи ``path``."""
    lay = layout(Path(path))
    if lay is None or lay.plain:
        return data
    if lay.key:
        buf = bytearray(data)
        _xor(buf, lay.key)
        return bytes(buf)
    with Path(path).open("rb") as fh:
        prefix = fh.read(lay.offset)
    return prefix + data
