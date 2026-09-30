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


@contextlib.contextmanager
def load_assets(src) -> Iterator[object]:
    """``UnityPy.load(src)`` на время блока ``with``; файлы закрываются даже при ошибке."""
    import UnityPy
    env = UnityPy.load(src)
    try:
        yield env
    finally:
        release(env)
