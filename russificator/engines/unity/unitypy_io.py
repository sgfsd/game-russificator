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
from typing import Iterator


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
    """``UnityPy.load(src)`` на время блока ``with``; файлы закрываются даже при ошибке."""
    import UnityPy
    fix_script_type_index()
    env = UnityPy.load(src)
    try:
        yield env
    finally:
        release(env)
