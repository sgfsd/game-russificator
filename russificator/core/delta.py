"""Бинарные патчи файлов игры для архива-русификатора.

Когда русификатор меняет файл игры целиком (Unity: шрифты в ассетах,
страницы встроенного браузера), в архив для друзей кладётся не сам файл
(это был бы кусок чужой игры весом в сотни мегабайт), а патч: «скопируй
из оригинала такие-то куски, а вот эти байты — новые».

Формат рассчитан на то, как меняются такие файлы: большая часть байтов
та же, но сдвинута (объект шрифта вырос — всё, что после него, уехало).
Поэтому патч строится жадно: общий участок растягивается сравнением
больших блоков, а после несовпадения следующая точка синхронизации ищется
поиском короткого образца в оригинале рядом с ожидаемым местом — всё это
делают ``mmap.find`` и сравнение срезов на C, без побайтового цикла на
Python. Файл, в котором совпадений почти нет (например, пережатый бандл),
патчем не станет — :func:`make_patch` вернёт ``None``.

Применение (:func:`apply_patch`) потоковое, память не зависит от размера
файла: оригинал проверяется по SHA-1 до начала и результат — после.
"""

from __future__ import annotations

import hashlib
import json
import mmap
import os
import struct
from pathlib import Path
from typing import BinaryIO, Callable, List, Optional, Tuple

MAGIC = b"RUDELTA1"

#: минимальная длина совпадения, ради которой стоит делать «копирование»
MIN_COPY = 48
#: длина образца, по которому ищется точка синхронизации
NEEDLE = 32
#: насколько далеко от ожидаемого места искать продолжение в оригинале
WINDOW = 96 << 20
#: блок сравнения при растягивании совпадения
CHUNK = 1 << 20

ProgressFn = Callable[[float], None]


class PatchError(Exception):
    """Патч нельзя применить: другой оригинал, повреждённый файл и т.п."""


def sha1_file(path: Path, progress: Optional[ProgressFn] = None) -> str:
    h = hashlib.sha1()
    size = max(1, os.path.getsize(path))
    done = 0
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(4 << 20), b""):
            h.update(block)
            done += len(block)
            if progress is not None:
                progress(done / size)
    return h.hexdigest()


class _Buf:
    """Файл как буфер только для чтения (mmap; пустой файл — пустые байты)."""

    def __init__(self, path: Path):
        self._fh = open(path, "rb")
        size = os.fstat(self._fh.fileno()).st_size
        self.data = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ) if size else b""
        self.size = size

    def close(self) -> None:
        if isinstance(self.data, mmap.mmap):
            self.data.close()
        self._fh.close()


def _common_run(a, ai: int, b, bi: int, limit: int) -> int:
    """Сколько байтов подряд совпадает: a[ai:] и b[bi:] (не больше limit)."""
    run = 0
    while run < limit:
        n = min(CHUNK, limit - run)
        if a[ai + run:ai + run + n] == b[bi + run:bi + run + n]:
            run += n
            continue
        lo, hi = 0, n            # первое несовпадение внутри блока — двоичным поиском
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if a[ai + run:ai + run + mid] == b[bi + run:bi + run + mid]:
                lo = mid
            else:
                hi = mid
        if a[ai + run:ai + run + hi] == b[bi + run:bi + run + hi]:
            lo = hi
        return run + lo
    return run


def _common_back(a, ai: int, b, bi: int, limit: int) -> int:
    """Сколько байтов подряд совпадает перед a[ai] и b[bi] (двоичным поиском, не больше limit)."""
    if limit <= 0 or a[ai - 1:ai] != b[bi - 1:bi]:
        return 0
    lo, hi = 1, limit
    if a[ai - hi:ai] == b[bi - hi:bi]:
        return hi
    while hi - lo > 1:          # a[ai-lo:ai] совпадает, a[ai-hi:ai] — нет
        mid = (lo + hi) // 2
        if a[ai - mid:ai] == b[bi - mid:bi]:
            lo = mid
        else:
            hi = mid
    return lo


def _good_needle(s: bytes) -> bool:
    """Образец с разнообразием байтов: нули и заливки совпадают где угодно."""
    return len(set(s)) >= 6


#: сколько байтов нового файла просматривать за один поиск синхронизации
MAX_SCAN = 8 << 20


def _find_sync(a, asize: int, b, bsize: int, start: int, shift: int,
               budget: List[int]) -> Tuple[Optional[Tuple[int, int]], int]:
    """Найти следующую точку, где b снова совпадает с a.

    Возвращает ((позиция в b, позиция в a) или None, докуда просмотрен b):
    за один раз просматривается не больше MAX_SCAN байтов.
    """
    step = 16
    j = start
    stop = min(bsize, start + MAX_SCAN)
    while j + NEEDLE <= bsize and j < stop:
        if budget[0] <= 0:
            return None, bsize
        needle = b[j:j + NEEDLE]
        if _good_needle(needle):
            budget[0] -= 1
            expect = j + shift
            lo, hi = max(0, expect - WINDOW), min(asize, expect + WINDOW + NEEDLE)
            p = a.find(needle, lo, hi)
            tries = 0
            while p >= 0 and tries < 8:
                if _common_run(a, p, b, j, 256) >= min(256, bsize - j, asize - p):
                    # шаг поиска мог перескочить начало совпадения — дотягиваем назад
                    k = _common_back(a, p, b, j, min(j - start, p))
                    return (j - k, p - k), j - k
                tries += 1
                p = a.find(needle, p + 1, hi)
        j += step
        step = min(step * 2, 1 << 20) if step < 4096 else step + 4096
    if j + NEEDLE > bsize:
        return None, bsize
    return None, min(j, bsize)


def make_patch(orig: Path, new: Path, out: Path, max_literal_ratio: float = 0.6) -> Optional[dict]:
    """Построить патч new относительно orig в файл out.

    Возвращает сведения о патче (размеры, SHA-1) или None, если файлы почти
    не похожи и патч не имеет смысла (доля новых байтов больше max_literal_ratio).
    """
    A, B = _Buf(Path(orig)), _Buf(Path(new))
    try:
        ops: List[Tuple[int, int, int]] = []     # (0, off_a, len) — копия; (1, off_lit, len) — новые байты
        tmp = Path(str(out) + ".lit")
        literal = copied = 0
        budget = [max(4096, B.size // 256)]      # число поисков синхронизации (защита от худшего случая)
        hopeless = False
        with open(tmp, "wb") as lit:
            i, shift = 0, 0
            while i < B.size:
                ai = i + shift
                run = 0
                if 0 <= ai < A.size:
                    run = _common_run(A.data, ai, B.data, i, min(A.size - ai, B.size - i))
                if run >= MIN_COPY or (run and i + run == B.size):
                    ops.append((0, ai, run))
                    i += run
                    copied += run
                    continue
                if i > (16 << 20) and copied < i // 10:
                    hopeless = True         # файлы почти не похожи (например, пережатый бандл)
                    break
                sync, end = _find_sync(A.data, A.size, B.data, B.size, i + 1, shift, budget)
                chunk = B.data[i:end]
                ops.append((1, lit.tell(), len(chunk)))
                lit.write(chunk)
                literal += len(chunk)
                if literal > max(1 << 20, B.size * max_literal_ratio):
                    break
                i = end
                if sync is not None:
                    shift = sync[1] - sync[0]
        if hopeless or literal > max(1 << 20, B.size * max_literal_ratio):
            tmp.unlink(missing_ok=True)
            return None
        info = {
            "orig_size": A.size, "orig_sha1": sha1_file(Path(orig)),
            "new_size": B.size, "new_sha1": sha1_file(Path(new)),
            "literal": literal, "ops": len(ops),
        }
        header = dict(info, ops=_pack_ops(_merge(ops)))
        raw = json.dumps(header, separators=(",", ":")).encode("utf-8")
        with open(out, "wb") as fh, open(tmp, "rb") as lit:
            fh.write(MAGIC)
            fh.write(struct.pack("<I", len(raw)))
            fh.write(raw)
            for block in iter(lambda: lit.read(4 << 20), b""):
                fh.write(block)
        tmp.unlink(missing_ok=True)
        info["patch_size"] = out.stat().st_size
        return info
    finally:
        A.close()
        B.close()


def _merge(ops: List[Tuple[int, int, int]]) -> List[Tuple[int, int, int]]:
    """Склеить соседние операции одного вида, идущие подряд."""
    out: List[Tuple[int, int, int]] = []
    for op in ops:
        if op[2] <= 0:
            continue
        if out and out[-1][0] == op[0] and out[-1][1] + out[-1][2] == op[1]:
            out[-1] = (op[0], out[-1][1], out[-1][2] + op[2])
        else:
            out.append(op)
    return out


def _pack_ops(ops: List[Tuple[int, int, int]]) -> List[List[int]]:
    return [list(op) for op in ops]


def read_header(patch: Path) -> dict:
    with open(patch, "rb") as fh:
        return _read_header(fh)[0]


def _read_header(fh: BinaryIO) -> Tuple[dict, int]:
    if fh.read(len(MAGIC)) != MAGIC:
        raise PatchError("файл патча повреждён")
    (n,) = struct.unpack("<I", fh.read(4))
    header = json.loads(fh.read(n).decode("utf-8"))
    return header, len(MAGIC) + 4 + n


def apply_patch(orig: Path, patch: Path, out: Path, progress: Optional[ProgressFn] = None,
                check_orig: bool = True) -> None:
    """Собрать новый файл из оригинала и патча. Бросает PatchError при несовпадении."""
    orig, patch, out = Path(orig), Path(patch), Path(out)
    with open(patch, "rb") as pf:
        header, base = _read_header(pf)
        if os.path.getsize(orig) != header["orig_size"]:
            raise PatchError("файл игры другой версии (размер не совпадает)")
        if check_orig and sha1_file(orig, (lambda f: progress(f * 0.4)) if progress else None) != header["orig_sha1"]:
            raise PatchError("файл игры другой версии (содержимое не совпадает)")
        total = max(1, header["new_size"])
        written = 0
        h = hashlib.sha1()
        tmp = Path(str(out) + ".rutmp")
        with open(orig, "rb") as src, open(tmp, "wb") as dst:
            for kind, off, length in header["ops"]:
                fh = src if kind == 0 else pf
                fh.seek(off if kind == 0 else base + off)
                left = length
                while left > 0:
                    block = fh.read(min(left, 4 << 20))
                    if not block:
                        raise PatchError("файл патча повреждён (обрыв данных)")
                    dst.write(block)
                    h.update(block)
                    left -= len(block)
                    written += len(block)
                    if progress is not None:
                        progress(0.4 + 0.6 * written / total)
        if written != header["new_size"] or h.hexdigest() != header["new_sha1"]:
            tmp.unlink(missing_ok=True)
            raise PatchError("после применения патча файл не совпал с ожидаемым")
        os.replace(tmp, out)
